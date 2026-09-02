import asyncio
import base64
import contextlib
import logging
import struct
import time

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from pumpbot.config import FeesConfig, Settings
from pumpbot.curve import (
    LAMPORTS_PER_SOL,
    TOKEN_DECIMALS,
    BondingCurveState,
    sol_to_tokens,
)
from pumpbot.filters.tier1 import Candidate
from pumpbot.heartbeat import Heartbeat
from pumpbot.ledger import Ledger, read_events
from pumpbot.main import (
    TradingState,
    _build_buy,
    _build_sell,
    count_real_closed_trades,
    run_position_monitor,
    run_trader,
)
from pumpbot.positions import Position, PositionManager
from pumpbot.program import (
    TOKEN_2022_PROGRAM_ID,
    derive_associated_token_address,
    derive_bonding_curve_pda,
)
from pumpbot.shadow import ShadowTracker

TEST_KEYPAIR = Keypair()
MINT = Keypair().pubkey()
CREATOR = Keypair().pubkey()


def make_candidate(mint: str = str(MINT)) -> Candidate:
    return Candidate(
        mint=mint,
        creator=str(CREATOR),
        name="Test Coin",
        symbol="TST",
        creator_supply_fraction=0.01,
        curve=BondingCurveState(
            virtual_token_reserves=1_073_000_000_000_000,
            virtual_sol_reserves=30_000_000_000,
            real_token_reserves=793_100_000_000_000,
            real_sol_reserves=0,
            token_total_supply=1_000_000_000_000_000,
            complete=False,
        ),
    )


def make_fees_config(**overrides) -> FeesConfig:
    defaults = {
        "max_fee_fraction": 0.25,
        "max_fee_absolute_sol": 0.0015,
        "priority_fee_ceiling_sol": 0.0008,
        "close_fee_reserve_sol": 0.0003,
        "compute_unit_limit": 40000,
        "priority_fee_sol": 0.0,
    }
    defaults.update(overrides)
    return FeesConfig(**defaults)


def load_test_settings(**fee_overrides) -> Settings:
    settings = Settings.load()
    fees = settings.config.fees.model_copy(update=fee_overrides)
    filters = settings.config.filters.model_copy(
        update={"tier2": settings.config.filters.tier2.model_copy(update={"enabled": False})}
    )
    settings.config = settings.config.model_copy(update={"fees": fees, "filters": filters})
    return settings


async def _run_one_candidate(
    settings: Settings, ledger: Ledger, state: TradingState, candidate, rpc=None
) -> None:
    """Feeds exactly one candidate through run_trader and stops the loop
    once it has been handled (run_trader is an infinite consumer loop with
    no task_done signal to await, so this just gives it a beat to process
    the single queued item before cancelling)."""
    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(candidate)
    position_manager = PositionManager(max_concurrent_positions=1)
    heartbeat = Heartbeat(settings.config.heartbeat)
    shadow_tracker = ShadowTracker(settings.config.shadow, rpc=None, ledger=ledger)

    task = asyncio.create_task(
        run_trader(
            settings,
            rpc=rpc,
            keypair=TEST_KEYPAIR,
            wallet_pubkey=TEST_KEYPAIR.pubkey(),
            queue=queue,
            position_manager=position_manager,
            heartbeat=heartbeat,
            state=state,
            tier2_filter=None,
            ledger=ledger,
            shadow_tracker=shadow_tracker,
        )
    )
    try:
        for _ in range(50):
            await asyncio.sleep(0.01)
            if queue.empty():
                break
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_fee_gate_rejects_and_does_not_touch_failsafe_counter(tmp_path):
    # Zero ceilings guarantee the fixed 5000-lamport base fee always
    # exceeds them, so this candidate is rejected by the fee gate before
    # ever reaching _simulate_buy (which would need a real rpc).
    settings = load_test_settings(max_fee_fraction=0.0, max_fee_absolute_sol=0.0)
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="test-run", dry_run=True)
    state = TradingState()

    await _run_one_candidate(settings, ledger, state, make_candidate())
    ledger.close()

    rows = list(read_events(tmp_path))
    fee_gate_rows = [r for r in rows if r.get("reason") == "fee_gate"]
    assert len(fee_gate_rows) == 1
    assert fee_gate_rows[0]["event"] == "CandidateSkipped"

    # The most dangerous mistake available in this milestone (per
    # MILESTONE-3-HANDOFF.md): a fee-gate rejection must NEVER increment
    # the consecutive-failure failsafe counter, or a deterministic config
    # mismatch would halt entries after 3 hits in a row.
    assert state._consecutive_failures == 0
    assert state.entries_halted is False


@pytest.mark.asyncio
async def test_fee_gate_excludes_ata_rent_from_its_estimate(tmp_path):
    # ATA rent (~0.002 SOL) dwarfs the 5000-lamport base fee. A ceiling set
    # comfortably above the base fee but far below rent must NOT reject --
    # if the gate were (wrongly) counting rent, this candidate would be
    # rejected instead.
    settings = load_test_settings(max_fee_fraction=1.0, max_fee_absolute_sol=0.00001)
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="test-run", dry_run=True)
    state = TradingState()

    # rpc=None means _simulate_buy would crash if reached (no real RPC) --
    # that's fine here: we only need to observe whether the fee gate itself
    # let the candidate through (no fee_gate skip recorded), not simulate a
    # full buy.
    await _run_one_candidate(settings, ledger, state, make_candidate())
    ledger.close()

    rows = list(read_events(tmp_path))
    fee_gate_rows = [r for r in rows if r.get("reason") == "fee_gate"]
    assert fee_gate_rows == []


def test_count_real_closed_trades_ignores_dry_run_trades():
    events = [
        {"event": "TradeClosed", "dry_run": True},
        {"event": "TradeClosed", "dry_run": True},
    ]
    assert count_real_closed_trades(events) == 0


def test_count_real_closed_trades_counts_only_real_trade_closed_events():
    events = [
        {"event": "TradeClosed", "dry_run": False},
        {"event": "TradeClosed", "dry_run": True},
        {"event": "CandidateSkipped", "dry_run": False},
        {"event": "TradeClosed", "dry_run": False},
    ]
    assert count_real_closed_trades(events) == 2


def test_count_real_closed_trades_empty_ledger_is_zero():
    assert count_real_closed_trades([]) == 0


def test_build_buy_prepends_compute_budget_before_create_ata_and_buy():
    instructions = _build_buy(
        mint=MINT,
        wallet_pubkey=TEST_KEYPAIR.pubkey(),
        creator=CREATOR,
        token_program_id=TOKEN_2022_PROGRAM_ID,
        tokens_out_raw=1000,
        max_sol_cost_lamports=1_000_000,
        fees_config=make_fees_config(priority_fee_sol=0.0),
    )
    # [SetComputeUnitLimit, create-ATA, buy] at zero priority fee.
    assert len(instructions) == 3

    instructions_with_priority = _build_buy(
        mint=MINT,
        wallet_pubkey=TEST_KEYPAIR.pubkey(),
        creator=CREATOR,
        token_program_id=TOKEN_2022_PROGRAM_ID,
        tokens_out_raw=1000,
        max_sol_cost_lamports=1_000_000,
        fees_config=make_fees_config(priority_fee_sol=0.0005, priority_fee_ceiling_sol=0.0008),
    )
    # [SetComputeUnitLimit, SetComputeUnitPrice, create-ATA, buy] once a
    # nonzero priority fee is configured.
    assert len(instructions_with_priority) == 4


# --- Milestone 4: behavior-aware exits (run_position_monitor) ---


def load_monitor_settings(**exits_overrides) -> Settings:
    settings = Settings.load()
    exits = settings.config.exits.model_copy(update=exits_overrides)
    settings.config = settings.config.model_copy(update={"exits": exits})
    return settings


def make_curve(
    virtual_sol_reserves: int = 30_000_000_000,
    virtual_token_reserves: int = 1_073_000_000_000_000,
    real_token_reserves: int = 793_100_000_000_000,
    real_sol_reserves: int = 0,
) -> BondingCurveState:
    return BondingCurveState(
        virtual_token_reserves=virtual_token_reserves,
        virtual_sol_reserves=virtual_sol_reserves,
        real_token_reserves=real_token_reserves,
        real_sol_reserves=real_sol_reserves,
        token_total_supply=1_000_000_000_000_000,
        complete=False,
    )


def encode_curve_base64(curve: BondingCurveState) -> str:
    raw = struct.pack(
        "<8s5QB",
        b"\x00" * 8,
        curve.virtual_token_reserves,
        curve.virtual_sol_reserves,
        curve.real_token_reserves,
        curve.real_sol_reserves,
        curve.token_total_supply,
        int(curve.complete),
    )
    return base64.b64encode(raw).decode()


def open_test_position(
    position_manager: PositionManager,
    state: TradingState,
    mint: str,
    creator: str,
    curve: BondingCurveState,
    position_sol_lamports: int = 1_000_000,
) -> Position:
    """Mirrors main.py's real entry-price computation exactly (all-in
    execution price, buy fee and impact baked in) so a test curve held flat
    from entry produces a realizable multiple near 1.0, not exactly 1.0 --
    close enough to sit well clear of every exit threshold by default."""
    tokens_out_raw = sol_to_tokens(curve, position_sol_lamports)
    tokens_out_whole = tokens_out_raw / 10**TOKEN_DECIMALS
    entry_price_sol = (position_sol_lamports / LAMPORTS_PER_SOL) / tokens_out_whole
    position = Position(
        mint=mint, entry_price_sol=entry_price_sol, entry_tokens=tokens_out_whole, opened_at=time.monotonic()
    )
    position_manager.open(position)
    state.remember_creator(mint, creator)
    state.remember_token_program(mint, str(TOKEN_2022_PROGRAM_ID))
    return position


class _ScriptedMonitorRpc:
    """Dispatches getAccountInfo by address (bonding curve vs creator ATA
    need different answers) rather than by method name alone -- test_ata_close.py's
    _FakeRpc can't express that. `on_creator_fetch`, if set, is called
    instead of returning `creator_balance_raw`/`creator_ata_exists`, to
    simulate a monitoring RPC failure."""

    def __init__(
        self,
        bonding_curve_pda: Pubkey,
        curve: BondingCurveState,
        creator_ata: Pubkey | None = None,
        creator_ata_exists: bool = True,
        creator_balance_raw: int = 1000,
        on_creator_fetch=None,
        sell_error: dict | None = None,
    ) -> None:
        self._bonding_curve_pda = str(bonding_curve_pda)
        self._curve_b64 = encode_curve_base64(curve)
        self._creator_ata = str(creator_ata) if creator_ata is not None else None
        self._creator_ata_exists = creator_ata_exists
        self._creator_balance_raw = creator_balance_raw
        self._on_creator_fetch = on_creator_fetch
        self._sell_error = sell_error
        self.calls: list[tuple[str, list]] = []

    async def call(self, method, params=None, pool="trading"):
        self.calls.append((method, params or []))
        address = params[0] if params else None

        if method == "getAccountInfo" and address == self._bonding_curve_pda:
            return {"value": {"data": [self._curve_b64, "base64"]}}

        if method == "getAccountInfo" and address == self._creator_ata:
            if self._on_creator_fetch is not None:
                self._on_creator_fetch()
            if not self._creator_ata_exists:
                return {"value": None}
            return {"value": {"data": ["", "base64"]}}

        if method == "getTokenAccountBalance" and address == self._creator_ata:
            return {"value": {"amount": str(self._creator_balance_raw)}}

        if method == "simulateTransaction":
            return {"value": {"err": self._sell_error}}

        raise AssertionError(f"unexpected rpc call: {method} {params}")

    def creator_ata_call_count(self) -> int:
        return sum(
            1
            for method, params in self.calls
            if method == "getAccountInfo" and params and params[0] == self._creator_ata
        )


async def _run_monitor_for(
    settings: Settings, rpc, position_manager: PositionManager, state: TradingState, ledger: Ledger,
    real_seconds: float = 0.05,
) -> None:
    """Runs run_position_monitor with its sleep interval collapsed to ~0
    (patched via settings is not possible -- POSITION_MONITOR_INTERVAL_SECONDS
    is a module constant -- so this monkeypatches it directly) for
    `real_seconds` of wall-clock time, giving it several loop iterations,
    then cancels. There is no task_done signal to await, mirroring
    _run_one_candidate's approach for run_trader."""
    import pumpbot.main as main_module

    original_interval = main_module.POSITION_MONITOR_INTERVAL_SECONDS
    main_module.POSITION_MONITOR_INTERVAL_SECONDS = 0.0
    heartbeat = Heartbeat(settings.config.heartbeat)
    task = asyncio.create_task(
        run_position_monitor(
            settings, rpc, TEST_KEYPAIR, TEST_KEYPAIR.pubkey(), position_manager, heartbeat, state, ledger
        )
    )
    try:
        await asyncio.sleep(real_seconds)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        main_module.POSITION_MONITOR_INTERVAL_SECONDS = original_interval


@pytest.mark.asyncio
async def test_creator_ata_polling_uses_default_pool_not_shadow(tmp_path):
    settings = load_monitor_settings(creator_sell_enabled=True, trailing_enabled=False)
    curve = make_curve()
    bonding_curve_pda = derive_bonding_curve_pda(MINT)
    creator_ata = derive_associated_token_address(CREATOR, MINT, TOKEN_2022_PROGRAM_ID)
    rpc = _ScriptedMonitorRpc(bonding_curve_pda, curve, creator_ata=creator_ata, creator_ata_exists=True)

    manager = PositionManager(max_concurrent_positions=1)
    state = TradingState()
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="test-run", dry_run=True)
    open_test_position(manager, state, str(MINT), str(CREATOR), curve)

    await _run_monitor_for(settings, rpc, manager, state, ledger)
    ledger.close()

    # Every getAccountInfo/getTokenAccountBalance call above went through
    # rpc.call() with the default pool= ("trading"), never pool="shadow" --
    # asserted by construction, since _ScriptedMonitorRpc.call would reject
    # an unexpected signature. Positive check: at least one creator-ATA
    # call happened, proving the polling path actually ran.
    assert rpc.creator_ata_call_count() >= 1


@pytest.mark.asyncio
async def test_creator_ata_fetch_failure_leaves_position_open_and_does_not_touch_failsafe(tmp_path):
    settings = load_monitor_settings(creator_sell_enabled=True, trailing_enabled=False)
    curve = make_curve()
    bonding_curve_pda = derive_bonding_curve_pda(MINT)
    creator_ata = derive_associated_token_address(CREATOR, MINT, TOKEN_2022_PROGRAM_ID)

    def blow_up():
        raise RuntimeError("simulated RPC hiccup")

    rpc = _ScriptedMonitorRpc(
        bonding_curve_pda, curve, creator_ata=creator_ata, on_creator_fetch=blow_up
    )

    manager = PositionManager(max_concurrent_positions=1)
    state = TradingState()
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="test-run", dry_run=True)
    open_test_position(manager, state, str(MINT), str(CREATOR), curve)

    await _run_monitor_for(settings, rpc, manager, state, ledger)
    ledger.close()

    # A monitoring-call failure is not an execution failure (Section 3.5) --
    # this mirrors the Milestone 3 test proving the fee gate doesn't trip
    # the failsafe counter either.
    assert state._consecutive_failures == 0
    assert state.entries_halted is False
    assert manager.get(str(MINT)) is not None  # position still open
    rows = list(read_events(tmp_path))
    assert not any(r.get("exit_reason") == "creator_sold" for r in rows)


@pytest.mark.asyncio
async def test_missing_creator_ata_establishes_zero_baseline_and_never_fires(tmp_path):
    settings = load_monitor_settings(creator_sell_enabled=True, trailing_enabled=False)
    curve = make_curve()
    bonding_curve_pda = derive_bonding_curve_pda(MINT)
    creator_ata = derive_associated_token_address(CREATOR, MINT, TOKEN_2022_PROGRAM_ID)
    rpc = _ScriptedMonitorRpc(bonding_curve_pda, curve, creator_ata=creator_ata, creator_ata_exists=False)

    manager = PositionManager(max_concurrent_positions=1)
    state = TradingState()
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="test-run", dry_run=True)
    open_test_position(manager, state, str(MINT), str(CREATOR), curve)

    await _run_monitor_for(settings, rpc, manager, state, ledger)
    ledger.close()

    assert state.creator_ata_baseline_for(str(MINT)) == 0
    # A zero baseline can never decrease -- polling stops once established.
    assert state.creator_ata_poll_done(str(MINT)) is True
    assert rpc.creator_ata_call_count() == 1
    rows = list(read_events(tmp_path))
    assert not any(r.get("exit_reason") == "creator_sold" for r in rows)


@pytest.mark.asyncio
async def test_creator_balance_decrease_fires_creator_sold_exit(tmp_path):
    settings = load_monitor_settings(creator_sell_enabled=True, trailing_enabled=False)
    curve = make_curve()
    bonding_curve_pda = derive_bonding_curve_pda(MINT)
    creator_ata = derive_associated_token_address(CREATOR, MINT, TOKEN_2022_PROGRAM_ID)

    manager = PositionManager(max_concurrent_positions=1)
    state = TradingState()
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="test-run", dry_run=True)
    open_test_position(manager, state, str(MINT), str(CREATOR), curve)

    # First tick establishes a baseline of 1000; a decrease has nothing to
    # compare against a baseline set in the SAME rpc instance across two
    # runs, so drive this with a mutable balance via two scripted rpcs
    # sharing the same TradingState (baseline persists on state, not rpc).
    rpc1 = _ScriptedMonitorRpc(
        bonding_curve_pda, curve, creator_ata=creator_ata, creator_ata_exists=True, creator_balance_raw=1000
    )
    await _run_monitor_for(settings, rpc1, manager, state, ledger)
    assert state.creator_ata_baseline_for(str(MINT)) == 1000

    rpc2 = _ScriptedMonitorRpc(
        bonding_curve_pda, curve, creator_ata=creator_ata, creator_ata_exists=True, creator_balance_raw=400
    )
    await _run_monitor_for(settings, rpc2, manager, state, ledger)
    ledger.close()

    rows = list(read_events(tmp_path))
    exit_rows = [r for r in rows if r.get("event") == "ExitFilled"]
    assert len(exit_rows) == 1
    assert exit_rows[0]["exit_reason"] == "creator_sold"
    assert manager.get(str(MINT)) is None  # fully exited, position closed


@pytest.mark.asyncio
async def test_creator_balance_increase_does_not_fire(tmp_path):
    settings = load_monitor_settings(creator_sell_enabled=True, trailing_enabled=False)
    curve = make_curve()
    bonding_curve_pda = derive_bonding_curve_pda(MINT)
    creator_ata = derive_associated_token_address(CREATOR, MINT, TOKEN_2022_PROGRAM_ID)

    manager = PositionManager(max_concurrent_positions=1)
    state = TradingState()
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="test-run", dry_run=True)
    open_test_position(manager, state, str(MINT), str(CREATOR), curve)

    rpc1 = _ScriptedMonitorRpc(
        bonding_curve_pda, curve, creator_ata=creator_ata, creator_ata_exists=True, creator_balance_raw=1000
    )
    await _run_monitor_for(settings, rpc1, manager, state, ledger)
    assert state.creator_ata_baseline_for(str(MINT)) == 1000

    rpc2 = _ScriptedMonitorRpc(
        bonding_curve_pda, curve, creator_ata=creator_ata, creator_ata_exists=True, creator_balance_raw=1500
    )
    await _run_monitor_for(settings, rpc2, manager, state, ledger)
    ledger.close()

    assert manager.get(str(MINT)) is not None  # still open, never exited


@pytest.mark.asyncio
async def test_creator_is_none_is_handled_by_continuing_not_exiting(tmp_path):
    settings = load_monitor_settings(creator_sell_enabled=True, trailing_enabled=False)
    curve = make_curve()
    bonding_curve_pda = derive_bonding_curve_pda(MINT)

    manager = PositionManager(max_concurrent_positions=1)
    state = TradingState()  # no remember_creator call -- creator stays None
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="test-run", dry_run=True)
    tokens_out_raw = sol_to_tokens(curve, 1_000_000)
    tokens_out_whole = tokens_out_raw / 10**TOKEN_DECIMALS
    entry_price_sol = (1_000_000 / LAMPORTS_PER_SOL) / tokens_out_whole
    position = Position(
        mint=str(MINT), entry_price_sol=entry_price_sol, entry_tokens=tokens_out_whole, opened_at=time.monotonic()
    )
    manager.open(position)
    # No creator_ata registered at all -- _ScriptedMonitorRpc would reject
    # any getAccountInfo call for one, proving none was attempted.
    rpc = _ScriptedMonitorRpc(bonding_curve_pda, curve)

    await _run_monitor_for(settings, rpc, manager, state, ledger)
    ledger.close()

    assert state._consecutive_failures == 0
    assert state.entries_halted is False
    rows = list(read_events(tmp_path))
    assert not any(r.get("event") == "ExitFilled" for r in rows)


@pytest.mark.asyncio
async def test_creator_sold_outranks_trailing_stop_in_the_ladder(tmp_path):
    settings = load_monitor_settings(creator_sell_enabled=True, trailing_enabled=True)
    curve = make_curve()
    bonding_curve_pda = derive_bonding_curve_pda(MINT)
    creator_ata = derive_associated_token_address(CREATOR, MINT, TOKEN_2022_PROGRAM_ID)

    manager = PositionManager(max_concurrent_positions=1)
    state = TradingState()
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="test-run", dry_run=True)
    open_test_position(manager, state, str(MINT), str(CREATOR), curve)

    rpc1 = _ScriptedMonitorRpc(
        bonding_curve_pda, curve, creator_ata=creator_ata, creator_ata_exists=True, creator_balance_raw=1000
    )
    await _run_monitor_for(settings, rpc1, manager, state, ledger)  # baseline

    rpc2 = _ScriptedMonitorRpc(
        bonding_curve_pda, curve, creator_ata=creator_ata, creator_ata_exists=True, creator_balance_raw=1
    )
    await _run_monitor_for(settings, rpc2, manager, state, ledger)
    ledger.close()

    rows = list(read_events(tmp_path))
    exit_rows = [r for r in rows if r.get("event") == "ExitFilled"]
    assert len(exit_rows) == 1
    assert exit_rows[0]["exit_reason"] == "creator_sold"


@pytest.mark.asyncio
async def test_creator_sell_disabled_never_polls_or_fires(tmp_path):
    settings = load_monitor_settings(creator_sell_enabled=False, trailing_enabled=False)
    curve = make_curve()
    bonding_curve_pda = derive_bonding_curve_pda(MINT)

    manager = PositionManager(max_concurrent_positions=1)
    state = TradingState()
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="test-run", dry_run=True)
    open_test_position(manager, state, str(MINT), str(CREATOR), curve)
    # No creator_ata registered at all -- the scripted rpc would reject any
    # getAccountInfo call for one, proving the switch actually disables
    # polling rather than just ignoring what it finds.
    rpc = _ScriptedMonitorRpc(bonding_curve_pda, curve)

    await _run_monitor_for(settings, rpc, manager, state, ledger)
    ledger.close()

    assert manager.get(str(MINT)) is not None
    rows = list(read_events(tmp_path))
    assert not any(r.get("event") == "ExitFilled" for r in rows)


def test_creator_sell_enabled_defaults_true_in_config_yaml():
    settings = Settings.load()
    assert settings.config.exits.creator_sell_enabled is True


def test_trailing_enabled_defaults_true_in_config_yaml():
    settings = Settings.load()
    assert settings.config.exits.trailing_enabled is True


def test_build_sell_prepends_compute_budget_before_sell():
    instructions = _build_sell(
        mint=MINT,
        wallet_pubkey=TEST_KEYPAIR.pubkey(),
        creator=CREATOR,
        token_program_id=TOKEN_2022_PROGRAM_ID,
        tokens_raw=1000,
        min_sol_output_lamports=1,
        fees_config=make_fees_config(priority_fee_sol=0.0),
    )
    # [SetComputeUnitLimit, sell] at zero priority fee.
    assert len(instructions) == 2

    instructions_with_priority = _build_sell(
        mint=MINT,
        wallet_pubkey=TEST_KEYPAIR.pubkey(),
        creator=CREATOR,
        token_program_id=TOKEN_2022_PROGRAM_ID,
        tokens_raw=1000,
        min_sol_output_lamports=1,
        fees_config=make_fees_config(priority_fee_sol=0.0005, priority_fee_ceiling_sol=0.0008),
    )
    # [SetComputeUnitLimit, SetComputeUnitPrice, sell] once a nonzero
    # priority fee is configured.
    assert len(instructions_with_priority) == 3


# --- MEASUREMENT-RUN-HANDOFF.md Task 0: dry-run sell-simulation classification ---

# Anchor's AccountNotInitialized, confirmed live during Phase 1 -- see
# main.py's _DRY_RUN_OWNERSHIP_ERROR_MARKERS. Every dry-run sell hits this
# because a dry-run buy never really sends, so the wallet's ATA never
# really exists on-chain.
_OWNERSHIP_ERROR = {"InstructionError": [1, {"Custom": 3012}]}
# An arbitrary different Custom code -- stands in for a real shape defect
# (wrong account ordering, bad encoding, a rejected slippage floor).
_SHAPE_ERROR = {"InstructionError": [1, {"Custom": 6074}]}


@pytest.mark.asyncio
async def test_dry_run_ownership_error_closes_position_normally(tmp_path):
    settings = load_monitor_settings(timeout_seconds=0, trailing_enabled=False, creator_sell_enabled=False)
    curve = make_curve()
    bonding_curve_pda = derive_bonding_curve_pda(MINT)
    manager = PositionManager(max_concurrent_positions=1)
    state = TradingState()
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="test-run", dry_run=True)
    open_test_position(manager, state, str(MINT), str(CREATOR), curve)

    rpc = _ScriptedMonitorRpc(bonding_curve_pda, curve, sell_error=_OWNERSHIP_ERROR)
    await _run_monitor_for(settings, rpc, manager, state, ledger)
    ledger.close()

    rows = list(read_events(tmp_path))
    exit_rows = [r for r in rows if r.get("event") == "ExitFilled"]
    assert len(exit_rows) == 1
    assert exit_rows[0]["exit_reason"] == "timeout"
    assert any(r.get("event") == "TradeClosed" for r in rows)
    assert manager.get(str(MINT)) is None
    assert state._consecutive_failures == 0
    assert state.entries_halted is False


@pytest.mark.asyncio
async def test_dry_run_ownership_error_never_touches_failsafe_across_repeats(tmp_path):
    settings = load_monitor_settings(timeout_seconds=0, trailing_enabled=False, creator_sell_enabled=False)
    curve = make_curve()
    manager = PositionManager(max_concurrent_positions=1)
    state = TradingState()
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="test-run", dry_run=True)

    for _ in range(3):
        mint = str(Keypair().pubkey())
        open_test_position(manager, state, mint, str(CREATOR), curve)
        bonding_curve_pda = derive_bonding_curve_pda(Pubkey.from_string(mint))
        rpc = _ScriptedMonitorRpc(bonding_curve_pda, curve, sell_error=_OWNERSHIP_ERROR)
        await _run_monitor_for(settings, rpc, manager, state, ledger)
        assert manager.get(mint) is None

    ledger.close()
    assert state._consecutive_failures == 0
    assert state.entries_halted is False


@pytest.mark.asyncio
async def test_dry_run_shape_class_error_leaves_position_open_and_logs_critical(tmp_path, caplog):
    settings = load_monitor_settings(timeout_seconds=0, trailing_enabled=False, creator_sell_enabled=False)
    curve = make_curve()
    bonding_curve_pda = derive_bonding_curve_pda(MINT)
    manager = PositionManager(max_concurrent_positions=1)
    state = TradingState()
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="test-run", dry_run=True)
    open_test_position(manager, state, str(MINT), str(CREATOR), curve)

    rpc = _ScriptedMonitorRpc(bonding_curve_pda, curve, sell_error=_SHAPE_ERROR)
    with caplog.at_level(logging.CRITICAL, logger="pumpbot.main"):
        await _run_monitor_for(settings, rpc, manager, state, ledger)
    ledger.close()

    assert manager.get(str(MINT)) is not None
    assert state._consecutive_failures == 0
    assert state.entries_halted is False
    assert any(record.levelno == logging.CRITICAL for record in caplog.records)
    rows = list(read_events(tmp_path))
    assert not any(r.get("event") == "ExitFilled" for r in rows)


@pytest.mark.asyncio
async def test_dry_run_unrecognized_error_takes_shape_branch_not_ownership(tmp_path):
    settings = load_monitor_settings(timeout_seconds=0, trailing_enabled=False, creator_sell_enabled=False)
    curve = make_curve()
    bonding_curve_pda = derive_bonding_curve_pda(MINT)
    manager = PositionManager(max_concurrent_positions=1)
    state = TradingState()
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="test-run", dry_run=True)
    open_test_position(manager, state, str(MINT), str(CREATOR), curve)

    # An allowlist (not denylist, per MEASUREMENT-RUN-HANDOFF.md 3.6) must
    # treat anything it doesn't recognize as a real defect, not ownership
    # noise -- so this must NOT be silently proceeded past.
    rpc = _ScriptedMonitorRpc(bonding_curve_pda, curve, sell_error={"SomeTotallyUnrelatedError": True})
    await _run_monitor_for(settings, rpc, manager, state, ledger)
    ledger.close()

    assert manager.get(str(MINT)) is not None
    assert state._consecutive_failures == 0
    assert state.entries_halted is False
    rows = list(read_events(tmp_path))
    assert not any(r.get("event") == "ExitFilled" for r in rows)


@pytest.mark.asyncio
async def test_live_mode_sell_simulation_failure_still_records_failure(tmp_path):
    """Regression guard on MEASUREMENT-RUN-HANDOFF.md 3.6's 'live path
    completely unchanged': the exact same error code that dry run proceeds
    past is a real, unresolved execution failure once DRY_RUN=false --
    there is no send path in dry run, but there is one here, and simulation
    failing means a real send was never attempted."""
    settings = load_monitor_settings(timeout_seconds=0, trailing_enabled=False, creator_sell_enabled=False)
    settings.secrets = settings.secrets.model_copy(update={"dry_run": False})
    curve = make_curve()
    bonding_curve_pda = derive_bonding_curve_pda(MINT)
    manager = PositionManager(max_concurrent_positions=1)
    state = TradingState()
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="test-run", dry_run=False)
    open_test_position(manager, state, str(MINT), str(CREATOR), curve)

    rpc = _ScriptedMonitorRpc(bonding_curve_pda, curve, sell_error=_OWNERSHIP_ERROR)
    await _run_monitor_for(settings, rpc, manager, state, ledger)
    ledger.close()

    assert manager.get(str(MINT)) is not None
    # >=1 rather than ==1: the position stays open every tick (unlike the
    # dry-run tests above, which close on the first tick), so several
    # monitor ticks fire within the test window -- the point is that the
    # live path still calls record_failure at all, unlike dry run.
    assert state._consecutive_failures >= 1


# --- PHASE-1-RERUN-HANDOFF.md Task 1: buy-simulation failure classification ---

_BUY_RACE_ERROR = {"InstructionError": [2, {"Custom": 6002}]}
_BUY_STRUCTURAL_ERROR = {"InstructionError": [2, {"Custom": 6063}]}
_BUY_UNRECOGNIZED_ERROR = {"SomeTotallyUnrelatedError": True}


class _ScriptedBuyRpc:
    """Fakes just enough of _simulate_buy's optimistic-then-resolved flow:
    getAccountInfo (for resolve_token_program_id's fallback, once the
    optimistic Token-2022 guess's simulation fails) and simulateTransaction
    (returning the same scripted error both times, since the test only
    cares about the final classified error)."""

    def __init__(self, mint: Pubkey, token_program_id: Pubkey, sim_error: dict) -> None:
        self._mint = str(mint)
        self._token_program_id = str(token_program_id)
        self._sim_error = sim_error
        self.simulate_calls = 0

    async def call(self, method, params=None, pool="trading"):
        params = params or []
        if method == "getAccountInfo" and params and params[0] == self._mint:
            return {"value": {"owner": self._token_program_id, "data": ["", "base64"]}}
        if method == "simulateTransaction":
            self.simulate_calls += 1
            return {"value": {"err": self._sim_error}}
        raise AssertionError(f"unexpected rpc call: {method} {params}")


@pytest.mark.asyncio
async def test_buy_race_error_skips_and_does_not_touch_failsafe(tmp_path):
    settings = load_test_settings()
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="test-run", dry_run=True)
    state = TradingState()
    mint = str(Keypair().pubkey())
    rpc = _ScriptedBuyRpc(Pubkey.from_string(mint), TOKEN_2022_PROGRAM_ID, _BUY_RACE_ERROR)

    await _run_one_candidate(settings, ledger, state, make_candidate(mint), rpc=rpc)
    ledger.close()

    rows = list(read_events(tmp_path))
    skip_rows = [r for r in rows if r.get("event") == "CandidateSkipped" and r.get("mint") == mint]
    assert len(skip_rows) == 1
    assert skip_rows[0]["reason"] == "sim_would_fail_race"
    assert state._consecutive_failures == 0
    assert state.entries_halted is False


@pytest.mark.asyncio
async def test_buy_structural_error_skips_and_increments_failsafe(tmp_path):
    settings = load_test_settings()
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="test-run", dry_run=True)
    state = TradingState()
    mint = str(Keypair().pubkey())
    rpc = _ScriptedBuyRpc(Pubkey.from_string(mint), TOKEN_2022_PROGRAM_ID, _BUY_STRUCTURAL_ERROR)

    await _run_one_candidate(settings, ledger, state, make_candidate(mint), rpc=rpc)
    ledger.close()

    rows = list(read_events(tmp_path))
    skip_rows = [r for r in rows if r.get("event") == "CandidateSkipped" and r.get("mint") == mint]
    assert len(skip_rows) == 1
    assert skip_rows[0]["reason"] == "sim_would_fail_structural"
    assert state._consecutive_failures == 1


@pytest.mark.asyncio
async def test_buy_unrecognized_error_is_treated_as_structural_allowlist_behavior(tmp_path):
    settings = load_test_settings()
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="test-run", dry_run=True)
    state = TradingState()
    mint = str(Keypair().pubkey())
    rpc = _ScriptedBuyRpc(Pubkey.from_string(mint), TOKEN_2022_PROGRAM_ID, _BUY_UNRECOGNIZED_ERROR)

    await _run_one_candidate(settings, ledger, state, make_candidate(mint), rpc=rpc)
    ledger.close()

    rows = list(read_events(tmp_path))
    skip_rows = [r for r in rows if r.get("event") == "CandidateSkipped" and r.get("mint") == mint]
    assert skip_rows[0]["reason"] == "sim_would_fail_structural"
    assert state._consecutive_failures == 1


@pytest.mark.asyncio
async def test_buy_failure_classes_still_track_shadow_and_emit_skip(tmp_path, monkeypatch):
    calls = []
    original_track = ShadowTracker.track

    def spy_track(self, mint, **kwargs):
        calls.append((mint, kwargs.get("reject_reason")))
        return original_track(self, mint, **kwargs)

    monkeypatch.setattr(ShadowTracker, "track", spy_track)

    settings = load_test_settings()
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="test-run", dry_run=True)
    state = TradingState()

    mint_race = str(Keypair().pubkey())
    rpc_race = _ScriptedBuyRpc(Pubkey.from_string(mint_race), TOKEN_2022_PROGRAM_ID, _BUY_RACE_ERROR)
    await _run_one_candidate(settings, ledger, state, make_candidate(mint_race), rpc=rpc_race)

    mint_structural = str(Keypair().pubkey())
    rpc_structural = _ScriptedBuyRpc(
        Pubkey.from_string(mint_structural), TOKEN_2022_PROGRAM_ID, _BUY_STRUCTURAL_ERROR
    )
    await _run_one_candidate(settings, ledger, state, make_candidate(mint_structural), rpc=rpc_structural)
    ledger.close()

    assert (mint_race, "sim_would_fail_race") in calls
    assert (mint_structural, "sim_would_fail_structural") in calls
    rows = list(read_events(tmp_path))
    skip_reasons = {r["mint"]: r["reason"] for r in rows if r.get("event") == "CandidateSkipped"}
    assert skip_reasons[mint_race] == "sim_would_fail_race"
    assert skip_reasons[mint_structural] == "sim_would_fail_structural"


@pytest.mark.asyncio
async def test_three_consecutive_buy_race_errors_do_not_halt_entries(tmp_path):
    settings = load_test_settings()
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="test-run", dry_run=True)
    state = TradingState()

    for _ in range(3):
        mint = str(Keypair().pubkey())
        rpc = _ScriptedBuyRpc(Pubkey.from_string(mint), TOKEN_2022_PROGRAM_ID, _BUY_RACE_ERROR)
        await _run_one_candidate(settings, ledger, state, make_candidate(mint), rpc=rpc)

    ledger.close()
    assert state._consecutive_failures == 0
    assert state.entries_halted is False
