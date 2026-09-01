import asyncio
import contextlib

import pytest
from solders.keypair import Keypair

from pumpbot.config import FeesConfig, Settings
from pumpbot.curve import BondingCurveState
from pumpbot.filters.tier1 import Candidate
from pumpbot.heartbeat import Heartbeat
from pumpbot.ledger import Ledger, read_events
from pumpbot.main import (
    TradingState,
    _build_buy,
    _build_sell,
    count_real_closed_trades,
    run_trader,
)
from pumpbot.positions import PositionManager
from pumpbot.program import TOKEN_2022_PROGRAM_ID
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


async def _run_one_candidate(settings: Settings, ledger: Ledger, state: TradingState, candidate) -> None:
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
            rpc=None,
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
