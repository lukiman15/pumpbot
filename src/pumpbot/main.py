"""Wires config, rpc, listener, filters, curve, positions, heartbeat,
executor, and now real submission into the live decision loop.

Entry point: `python -m pumpbot.main`.

Every buy/sell decision is always built into a real `Instruction` (via
executor.py's verified account orderings) and run through
`simulateTransaction` first, regardless of DRY_RUN -- that's a read-only
RPC call against current chain state, zero risk, nothing signed or
broadcast, and it's the gate that decides whether a real send is even
attempted.

DRY_RUN=true (the default) stops there: a passing simulation is logged as
"[DRY RUN] simulated ... would succeed" and the position/state bookkeeping
proceeds AS IF it landed, but nothing is ever signed or sent.

DRY_RUN=false additionally signs and submits for real (submit.py's
send_and_confirm) once simulation passes, and only updates position/state
bookkeeping after real on-chain confirmation -- never optimistically. A
sell that fully exits a position additionally attempts to close that
mint's ATA for rent recovery (ata_close.py), best-effort. See
`send_and_confirm`'s docstring for how SubmissionError, OnChainFailureError,
and ConfirmationTimeoutError are each handled distinctly: the first two are
definite outcomes (nothing moved beyond a spent fee, respectively);
ConfirmationTimeoutError means the real outcome is UNKNOWN, and this module
responds by halting all new entries immediately and logging CRITICAL for
manual reconciliation -- it does not guess.

Setting DRY_RUN=false is a real decision to let this bot move real SOL.
Nothing in this file flips that setting itself; it only respects whatever
the operator has configured in `.env`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from spl.token.instructions import create_idempotent_associated_token_account

from pumpbot.ata_close import close_ata_after_exit, get_token_account_balance_raw
from pumpbot.config import PROJECT_ROOT, FeesConfig, Settings, load_settings
from pumpbot.curve import (
    LAMPORTS_PER_SOL,
    TOKEN_DECIMALS,
    CurveCompleteError,
    decode_bonding_curve,
    sol_to_tokens,
    spot_price_sol_per_token,
    tokens_to_sol,
)
from pumpbot.executor import (
    MintNotYetVisibleError,
    build_buy_instruction,
    build_sell_instruction,
    resolve_token_program_id,
)
from pumpbot.filters.tier1 import (
    Candidate,
    Tier1Filter,
    load_creator_blocklist,
    load_name_symbol_blocklist,
)
from pumpbot.filters.tier2 import Tier2Filter
from pumpbot.heartbeat import Heartbeat, HeartbeatReport
from pumpbot.ledger import (
    AtaClosed,
    CandidateSkipped,
    EntryFilled,
    ExitFilled,
    Ledger,
    Tier2Evaluated,
    TradeClosed,
    find_orphans,
    new_run_id,
    new_trade_id,
    read_events,
)
from pumpbot.listener import MintListener
from pumpbot.persistence import load_state, save_state
from pumpbot.positions import Position, PositionLimitReachedError, PositionManager
from pumpbot.program import (
    TOKEN_2022_PROGRAM_ID,
    derive_associated_token_address,
    derive_bonding_curve_pda,
    derive_bonding_curve_v2_pda,
    pick_buyback_fee_recipient,
    pick_fee_recipient,
)
from pumpbot.rpc import RpcClient
from pumpbot.settlement import (
    Settlement,
    SettlementMismatchError,
    SettlementUnavailableError,
    settle,
)
from pumpbot.shadow import ShadowTracker
from pumpbot.submit import (
    BlockhashExpiredError,
    ConfirmationTimeoutError,
    OnChainFailureError,
    SubmissionError,
    build_compute_budget_instructions,
    estimate_entry_fee_lamports,
    send_and_confirm,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("pumpbot.main")

CANDIDATE_QUEUE_MAXSIZE = 100
POSITION_MONITOR_INTERVAL_SECONDS = 2.0
STATE_PATH = PROJECT_ROOT / "state" / "positions.json"


class PreflightError(RuntimeError):
    """Raised when startup conditions aren't safe to trade under."""


@dataclass
class _LegRecord:
    leg_kind: str  # "BUY" | "SELL" | "ATA_CLOSE"
    settlement: Settlement | None


class TradingState:
    """Mutable state shared between the trader, monitor, and reconciliation
    tasks that doesn't belong inside PositionManager itself."""

    def __init__(self) -> None:
        self.entries_halted = False
        self._consecutive_failures = 0
        self._creators: dict[str, str] = {}
        # token_program_id doesn't change over a mint's lifetime -- once a
        # buy confirms it, a sell of the same mint can reuse it directly
        # instead of paying another resolve_token_program_id round-trip
        # (and its retries) against the same laggy RPC node.
        self._token_programs: dict[str, str] = {}
        # trade_id bookkeeping for the ledger (ledger.py) -- purely
        # in-memory, unlike persistence.py's positions.json, so a position
        # that survives a restart has no trade_id here until
        # ensure_trade_id_for mints one defensively (see that method).
        self._trade_ids: dict[str, str] = {}  # mint -> currently open trade_id
        self._trade_legs: dict[str, list[_LegRecord]] = {}
        self._trade_entry_wall: dict[str, float] = {}
        self._trade_exit_reasons: dict[str, list[str]] = {}
        # Creator-sell detection (MILESTONE-4-HANDOFF.md Section 4.3): the
        # creator's ATA balance observed once, at the first successful poll
        # for that mint. A mint whose baseline turned out to be zero (no ATA,
        # or an already-empty one -- there is no signal to be had) is added
        # to _creator_ata_poll_done so the monitor loop stops spending an RPC
        # call on it every tick.
        self._creator_ata_baseline: dict[str, int] = {}
        self._creator_ata_poll_done: set[str] = set()

    def record_failure(self, limit: int) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= limit and not self.entries_halted:
            self.entries_halted = True
            logger.critical(
                "failsafe: %d consecutive failures, halting new entries "
                "(existing positions and reconciliation are unaffected)",
                self._consecutive_failures,
            )

    def record_success(self) -> None:
        self._consecutive_failures = 0

    def halt_entries_immediately(self, reason: str) -> None:
        """For a real send whose outcome is UNKNOWN (ConfirmationTimeoutError
        or an exhausted BlockhashExpiredError, see submit.py) -- distinct
        from record_failure's threshold-based halt, this halts on the very
        first occurrence. An unknown real-money outcome is categorically
        worse than a simulation that correctly predicted a failure; it
        needs a human to reconcile the signature before any more entries,
        not a few more chances first."""
        if not self.entries_halted:
            self.entries_halted = True
        logger.critical("failsafe: halting new entries immediately -- %s", reason)

    def remember_creator(self, mint: str, creator: str) -> None:
        self._creators[mint] = creator

    def creator_for(self, mint: str) -> str | None:
        return self._creators.get(mint)

    def remember_token_program(self, mint: str, token_program_id: str) -> None:
        self._token_programs[mint] = token_program_id

    def token_program_for(self, mint: str) -> str | None:
        return self._token_programs.get(mint)

    def all_creators(self) -> dict[str, str]:
        return dict(self._creators)

    def all_token_programs(self) -> dict[str, str]:
        return dict(self._token_programs)

    def creator_ata_baseline_for(self, mint: str) -> int | None:
        return self._creator_ata_baseline.get(mint)

    def set_creator_ata_baseline(self, mint: str, balance: int) -> None:
        self._creator_ata_baseline[mint] = balance
        if balance == 0:
            self._creator_ata_poll_done.add(mint)

    def creator_ata_poll_done(self, mint: str) -> bool:
        return mint in self._creator_ata_poll_done

    def forget_creator_ata(self, mint: str) -> None:
        self._creator_ata_baseline.pop(mint, None)
        self._creator_ata_poll_done.discard(mint)

    def open_trade(self, mint: str) -> str:
        """Mints a fresh trade_id for a buy that just confirmed. Call
        exactly once per EntryFilled."""
        trade_id = new_trade_id()
        self._trade_ids[mint] = trade_id
        self._trade_legs[trade_id] = []
        self._trade_entry_wall[trade_id] = time.time()
        self._trade_exit_reasons[trade_id] = []
        return trade_id

    def trade_id_for(self, mint: str) -> str | None:
        return self._trade_ids.get(mint)

    def ensure_trade_id_for(self, mint: str) -> str:
        """Defensive fallback for a position that survived a restart via
        persistence.py's positions.json without a matching trade_id --
        this ledger's trade_id tracking is purely in-memory and doesn't
        span restarts the way position bookkeeping does. Rare in practice
        (see pumpbot-sniper-status.md's note on the one historical
        mid-position restart) but must not crash the exit path."""
        trade_id = self._trade_ids.get(mint)
        if trade_id is None:
            trade_id = self.open_trade(mint)
            logger.warning(
                "mint=%s had no trade_id on record (position likely restored "
                "across a restart) -- minted a fresh trade_id=%s for it",
                mint, trade_id,
            )
        return trade_id

    def record_leg(self, trade_id: str, leg_kind: str, settlement: Settlement | None) -> None:
        self._trade_legs.setdefault(trade_id, []).append(_LegRecord(leg_kind, settlement))

    def record_exit_reason(self, trade_id: str, reason: str) -> None:
        self._trade_exit_reasons.setdefault(trade_id, []).append(reason)

    def pop_trade(self, mint: str) -> tuple[str, list[_LegRecord], list[str], float] | None:
        """Removes and returns (trade_id, legs, exit_reasons, entry_ts_wall)
        for a trade that just fully closed, or None if this mint has no
        trade_id on record."""
        trade_id = self._trade_ids.pop(mint, None)
        if trade_id is None:
            return None
        legs = self._trade_legs.pop(trade_id, [])
        exit_reasons = self._trade_exit_reasons.pop(trade_id, [])
        entry_wall = self._trade_entry_wall.pop(trade_id, time.time())
        return trade_id, legs, exit_reasons, entry_wall


def _build_trade_closed(
    mint: str,
    trade_id: str,
    legs: list[_LegRecord],
    exit_reasons: list[str],
    entry_ts_wall: float,
    dry_run: bool,
) -> TradeClosed:
    """Aggregates a closed trade's legs per the plan's Accounting Rules.

    dry_run forces all four PnL fields to None (never 0 -- a real trade's
    turnover is never legitimately zero, and a silent 0 would read as
    "closed flat" rather than "not applicable"). For a real trade, a field
    is also None specifically where its one supporting leg's settlement
    never arrived (e.g. gross_turnover with no readable BUY leg) --
    settlement_complete flags this for downstream consumers rather than
    hiding it behind a number that looks more complete than it is.
    """
    hold_seconds = time.time() - entry_ts_wall
    settlement_complete = False if dry_run else all(leg.settlement is not None for leg in legs)

    if dry_run:
        realized_pnl_lamports = None
        rent_recovered_lamports = None
        total_fee_lamports = None
        gross_turnover_lamports = None
    else:
        available = [leg.settlement for leg in legs if leg.settlement is not None]
        realized_pnl_lamports = (
            sum(s.sol_delta_lamports for s in available) if available else None
        )
        total_fee_lamports = sum(s.fee_lamports for s in available) if available else None
        ata_close = next(
            (leg.settlement for leg in legs if leg.leg_kind == "ATA_CLOSE" and leg.settlement),
            None,
        )
        rent_recovered_lamports = ata_close.sol_delta_lamports if ata_close else 0
        buy_leg = next(
            (leg.settlement for leg in legs if leg.leg_kind == "BUY" and leg.settlement),
            None,
        )
        gross_turnover_lamports = abs(buy_leg.sol_delta_lamports) if buy_leg else None

    return TradeClosed(
        mint=mint,
        trade_id=trade_id,
        realized_pnl_lamports=realized_pnl_lamports,
        rent_recovered_lamports=rent_recovered_lamports,
        total_fee_lamports=total_fee_lamports,
        gross_turnover_lamports=gross_turnover_lamports,
        hold_seconds=hold_seconds,
        leg_count=len(legs),
        exit_reasons=exit_reasons,
        settlement_complete=settlement_complete,
    )


async def _settle_leg(
    rpc: RpcClient, signature: str, wallet_pubkey: Pubkey, leg_kind: str, mint: str
) -> Settlement | None:
    """A settlement failure must never affect trading -- it's a read-only
    afterthought on an already-confirmed trade. Logged at warning, never
    raised."""
    try:
        return await settle(rpc, signature, str(wallet_pubkey), leg_kind)
    except (SettlementUnavailableError, SettlementMismatchError) as exc:
        logger.warning(
            "settlement failed for leg=%s mint=%s signature=%s: %s",
            leg_kind, mint, signature, exc,
        )
        return None


def _persist(position_manager: PositionManager, state: TradingState) -> None:
    """Best-effort: a failed save shouldn't crash the trading loop, but it
    does mean the next restart could lose track of open positions, so it's
    logged loudly rather than silently swallowed."""
    try:
        save_state(STATE_PATH, position_manager, state.all_creators(), state.all_token_programs())
    except Exception:
        logger.exception("failed to persist position state to %s", STATE_PATH)


def _load_keypair(path: str) -> Keypair:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return Keypair.from_bytes(bytes(raw))


async def preflight(settings: Settings, rpc: RpcClient, keypair: Keypair) -> Pubkey:
    pubkey = keypair.pubkey()
    balance = await rpc.get_balance_sol(str(pubkey))
    wallet_cfg = settings.config.wallet
    if not (wallet_cfg.min_balance_sol <= balance <= wallet_cfg.max_balance_sol):
        raise PreflightError(
            f"wallet {pubkey} balance {balance:.5f} SOL outside configured range "
            f"[{wallet_cfg.min_balance_sol}, {wallet_cfg.max_balance_sol}]"
        )
    logger.info("preflight ok: wallet=%s balance=%.5f SOL", pubkey, balance)
    return pubkey


async def _simulate(rpc: RpcClient, instructions: list | object, fee_payer: Pubkey) -> str | None:
    """Builds an unsigned transaction around instructions and simulates it
    against live chain state. Returns None on success, or a string
    describing the on-chain error. Never signs or sends anything.

    Accepts either a single Instruction (for backward compatibility) or a
    list of them, executed in order in one transaction -- real pump.fun
    clients bundle a create-ATA instruction ahead of buy, and a live test
    confirmed this project's simulations need to as well (see the
    AccountNotInitialized finding on associated_user in this module's
    docstring / commit history)."""
    ix_list = instructions if isinstance(instructions, list) else [instructions]
    msg = Message.new_with_blockhash(ix_list, fee_payer, Hash.default())
    tx = Transaction.new_unsigned(msg)
    raw_b64 = base64.b64encode(bytes(tx)).decode()
    result = await rpc.call(
        "simulateTransaction",
        [
            raw_b64,
            {
                "encoding": "base64",
                "sigVerify": False,
                "replaceRecentBlockhash": True,
                # Default commitment is `finalized`, which simulates against
                # chain state as of ~10s ago on a brand-new mint -- see
                # executor.py's resolve_token_program_id docstring for how
                # that was measured. `confirmed` reflects state from ~0.1s
                # after PumpPortal's own notification instead.
                "commitment": "confirmed",
            },
        ],
    )
    err = result["value"]["err"]
    return None if err is None else str(err)


def _build_buy(
    mint: Pubkey, wallet_pubkey: Pubkey, creator: Pubkey, token_program_id: Pubkey,
    tokens_out_raw: int, max_sol_cost_lamports: int, fees_config: FeesConfig,
) -> list:
    """Returns [compute-budget..., create-ATA, buy]. The compute-budget
    instructions are prepended here (not in send_and_confirm) specifically
    so the exact same instruction list is both simulated (via _simulate,
    below) and later sent for real -- see submit.py's
    build_compute_budget_instructions docstring for the fee formula.

    A live test found that simulating the bare buy instruction alone fails
    with AccountNotInitialized on associated_user for every brand-new
    mint, since the wallet has never held this token before and nothing
    creates its ATA first. Real pump.fun clients bundle an idempotent
    create-ATA instruction ahead of buy in the same transaction; simulating
    buy alone was testing something no real client actually sends."""
    create_ata_ix = create_idempotent_associated_token_account(
        payer=wallet_pubkey, owner=wallet_pubkey, mint=mint, token_program_id=token_program_id
    )
    buy_ix = build_buy_instruction(
        mint=mint,
        user=wallet_pubkey,
        creator=creator,
        token_program_id=token_program_id,
        fee_recipient=pick_fee_recipient(),
        buyback_fee_recipient=pick_buyback_fee_recipient(),
        # Resolved (see program.py's module docstring on account [16]) --
        # an arbitrary value here was only safe for already-established
        # mints; brand-new mints require the real bonding_curve_v2 PDA or
        # pump.fun's own program rejects the buy with InvalidBondingCurveV2.
        unresolved_account=derive_bonding_curve_v2_pda(mint),
        amount_tokens_raw=tokens_out_raw,
        max_sol_cost_lamports=max_sol_cost_lamports,
    )
    return [*build_compute_budget_instructions(fees_config), create_ata_ix, buy_ix]


async def _simulate_buy(
    rpc: RpcClient,
    wallet_pubkey: Pubkey,
    candidate: Candidate,
    position_sol_lamports: int,
    slippage_tolerance_fraction: float,
    fees_config: FeesConfig,
) -> tuple[bool, int, str | None, bool, str | None]:
    """Returns (would_succeed, tokens_out_raw, error_summary, is_transient,
    token_program_id_used).

    is_transient marks MintNotYetVisibleError specifically -- an expected,
    recoverable RPC-propagation-lag condition for a brand-new mint, not
    evidence of a real bug (see executor.py's docstring on it). Callers
    should not count it toward the failsafe's consecutive-failure limit.

    Optimistically tries Token-2022 first, with NO getAccountInfo call --
    every sampled pump.fun mint has used Token-2022 (0 counterexamples, see
    program.py's module docstring), and a wrong guess here is caught safely
    by simulateTransaction before any real money could move, so there's no
    money-risk in guessing. Saves one RPC round-trip in the common case;
    falls back to resolve_token_program_id's retries if the guess fails.

    tokens_out_raw is still sized off the exact position_sol_lamports
    target (how much we intend to spend), but max_sol_cost_lamports -- the
    on-chain ceiling the instruction actually enforces -- is padded up by
    slippage_tolerance_fraction. A live test found zero buffer here trips
    pump.fun's own slippage-protection error on essentially every buy,
    since even the ~0.1s gap from a `confirmed`-commitment simulate can see
    a competing buy move the curve first. See config.yaml's comment on
    trading.slippage_tolerance_fraction.
    """
    mint = Pubkey.from_string(candidate.mint)
    try:
        tokens_out_raw = sol_to_tokens(candidate.curve, position_sol_lamports)
    except CurveCompleteError:
        return False, 0, "curve already complete", False, None
    if tokens_out_raw <= 0:
        return False, 0, "zero tokens out at this size", False, None

    max_sol_cost_lamports = round(position_sol_lamports * (1 + slippage_tolerance_fraction))
    creator = Pubkey.from_string(candidate.creator)
    optimistic_ix = _build_buy(
        mint, wallet_pubkey, creator, TOKEN_2022_PROGRAM_ID, tokens_out_raw,
        max_sol_cost_lamports, fees_config,
    )
    optimistic_error = await _simulate(rpc, optimistic_ix, wallet_pubkey)
    if optimistic_error is None:
        return True, tokens_out_raw, None, False, str(TOKEN_2022_PROGRAM_ID)

    try:
        token_program_id = await resolve_token_program_id(rpc, mint)
    except MintNotYetVisibleError as exc:
        return False, tokens_out_raw, str(exc), True, None
    except Exception as exc:  # noqa: BLE001 - summarized into the return value, not swallowed
        return False, tokens_out_raw, f"resolve_token_program_id failed: {exc}", False, None

    ix = _build_buy(
        mint, wallet_pubkey, creator, token_program_id, tokens_out_raw,
        max_sol_cost_lamports, fees_config,
    )
    error = await _simulate(rpc, ix, wallet_pubkey)
    return error is None, tokens_out_raw, error, False, str(token_program_id)


def _build_sell(
    mint: Pubkey, wallet_pubkey: Pubkey, creator: Pubkey, token_program_id: Pubkey,
    tokens_raw: int, min_sol_output_lamports: int, fees_config: FeesConfig,
) -> list:
    """Returns [compute-budget..., sell] -- see _build_buy's docstring for
    why the compute-budget instructions are prepended here rather than in
    send_and_confirm."""
    sell_ix = build_sell_instruction(
        mint=mint,
        user=wallet_pubkey,
        creator=creator,
        token_program_id=token_program_id,
        fee_recipient=pick_fee_recipient(),
        buyback_fee_recipient=pick_buyback_fee_recipient(),
        # CORRECTED (see program.py's module docstring on account [14]):
        # the earlier "confirmed unvalidated" conclusion was tested only
        # against brand-new mints at t=0 and did NOT hold -- a live position
        # (mint progressed past creation, real trading activity) hit
        # InvalidBondingCurveV2 (Custom 6074) with an arbitrary value here,
        # exactly the same check buy's [16] has. Using the same derivation
        # as buy resolved it (confirmed via live simulateTransaction:
        # err: null with this value, Custom 6074 with any other).
        unresolved_account=derive_bonding_curve_v2_pda(mint),
        amount_tokens_raw=tokens_raw,
        min_sol_output_lamports=min_sol_output_lamports,
    )
    return [*build_compute_budget_instructions(fees_config), sell_ix]


async def _simulate_sell(
    rpc: RpcClient,
    wallet_pubkey: Pubkey,
    mint: Pubkey,
    creator: Pubkey,
    tokens_raw: int,
    min_sol_output_lamports: int,
    fees_config: FeesConfig,
    known_token_program_id: Pubkey | None = None,
) -> tuple[str | None, Pubkey | None]:
    """Returns (error, token_program_id_used) -- the resolved token program
    is returned so a caller doing a REAL sell after this simulation passes
    can reuse it to build the real instruction without re-resolving.

    known_token_program_id skips the resolve_token_program_id round-trip
    entirely -- the token program doesn't change over a mint's lifetime, so
    if the earlier buy already confirmed it, a sell of the same mint can
    reuse it directly instead of paying the same laggy RPC lookup again.

    min_sol_output_lamports is the on-chain floor the instruction actually
    enforces -- callers should derive it from curve.tokens_to_sol() padded
    down by config.yaml's trading.slippage_tolerance_fraction, the same way
    _simulate_buy pads its max_sol_cost up (see that function's docstring).

    In DRY_RUN, this can still only validate that a sell of this shape
    would be accepted by pump.fun's program logic (account ordering, data
    encoding, slippage) -- it can't prove the wallet actually holds enough
    tokens to sell, since a simulated buy never really acquires any. Once
    DRY_RUN=false and buys land for real, this limitation goes away."""
    if known_token_program_id is not None:
        token_program_id = known_token_program_id
    else:
        try:
            token_program_id = await resolve_token_program_id(rpc, mint)
        except Exception as exc:  # noqa: BLE001 - summarized into the return value, not swallowed
            return f"resolve_token_program_id failed: {exc}", None

    ix = _build_sell(
        mint, wallet_pubkey, creator, token_program_id, tokens_raw,
        min_sol_output_lamports, fees_config,
    )
    error = await _simulate(rpc, ix, wallet_pubkey)
    return error, token_program_id


async def run_trader(
    settings: Settings,
    rpc: RpcClient,
    keypair: Keypair,
    wallet_pubkey: Pubkey,
    queue: asyncio.Queue[Candidate],
    position_manager: PositionManager,
    heartbeat: Heartbeat,
    state: TradingState,
    tier2_filter: Tier2Filter,
    ledger: Ledger,
    shadow_tracker: ShadowTracker,
) -> None:
    position_sol = settings.config.trading.position_sol
    position_sol_lamports = int(position_sol * LAMPORTS_PER_SOL)
    slippage_tolerance_fraction = settings.config.trading.slippage_tolerance_fraction
    failure_limit = settings.config.failsafe.consecutive_failure_limit
    dry_run = settings.secrets.dry_run
    fees_config = settings.config.fees

    while True:
        candidate = await queue.get()
        heartbeat.record_candidate()

        if state.entries_halted:
            logger.warning("skip mint=%s: entries halted by failsafe", candidate.mint)
            ledger.append(
                CandidateSkipped(mint=candidate.mint, reason="entries_halted", detail=None)
            )
            shadow_tracker.track(candidate.mint, arm="skipped", reject_reason="entries_halted")
            continue
        if not position_manager.can_open_new():
            logger.info("skip mint=%s: max_concurrent_positions reached", candidate.mint)
            ledger.append(
                CandidateSkipped(
                    mint=candidate.mint, reason="max_concurrent_positions", detail=None
                )
            )
            shadow_tracker.track(
                candidate.mint, arm="skipped", reject_reason="max_concurrent_positions"
            )
            continue

        if settings.config.filters.tier2.enabled:
            t0 = time.monotonic()
            tier2_result = await tier2_filter.evaluate(candidate)
            fetch_s = time.monotonic() - t0
            socials_count = sum(
                [tier2_result.has_twitter, tier2_result.has_telegram, tier2_result.has_website]
            )
            # This is the durable record of a tier2 evaluation -- emitted
            # for every candidate the gate evaluates, pass or fail, so the
            # gate's effect is measurable retroactively against the
            # ledger, not just visible in a greppable log line.
            ledger.append(
                Tier2Evaluated(
                    mint=candidate.mint,
                    outcome=tier2_result.outcome.value,
                    has_twitter=tier2_result.has_twitter,
                    has_telegram=tier2_result.has_telegram,
                    has_website=tier2_result.has_website,
                    passed=tier2_result.passed,
                    mode=settings.config.filters.tier2.mode,
                    fetch_seconds=fetch_s,
                )
            )
            logger.info(
                "tier2 mint=%s outcome=%s passed=%s socials=%d fetch_s=%.3f",
                candidate.mint, tier2_result.outcome.value, tier2_result.passed,
                socials_count, fetch_s,
            )
            if not tier2_result.passed:
                # A tier2 reject is not an execution failure -- a token
                # simply lacking a Telegram link is a normal outcome, not
                # evidence the bot is broken. Do NOT call record_failure()
                # here: that would halt entries after three link-less
                # tokens in a row.
                logger.info(
                    "reject mint=%s reason=tier2_%s", candidate.mint, tier2_result.outcome.value
                )
                ledger.append(
                    CandidateSkipped(
                        mint=candidate.mint, reason="tier2_rejected",
                        detail=tier2_result.outcome.value,
                    )
                )
                shadow_tracker.track(
                    candidate.mint, arm="skipped", reject_reason="tier2_rejected"
                )
                continue

        estimated_fee_lamports = estimate_entry_fee_lamports(fees_config, signature_count=1)
        max_allowed_fraction_lamports = fees_config.max_fee_fraction * position_sol_lamports
        max_allowed_absolute_lamports = fees_config.max_fee_absolute_sol * LAMPORTS_PER_SOL
        if (
            estimated_fee_lamports > max_allowed_fraction_lamports
            or estimated_fee_lamports > max_allowed_absolute_lamports
        ):
            # A fee-gate rejection is a normal skip, exactly like a tier-2
            # rejection -- NOT an execution failure. Do NOT call
            # record_failure() here (same bug class flagged in Milestone 1
            # for tier-2 rejects): this would halt entries after 3
            # consecutive hits over a deterministic config mismatch, not a
            # sign anything is broken.
            detail = (
                f"estimated_fee_lamports={estimated_fee_lamports} exceeds "
                f"max_fee_fraction*position={max_allowed_fraction_lamports:.0f} "
                f"or max_fee_absolute_sol={max_allowed_absolute_lamports:.0f}"
            )
            logger.info("skip mint=%s: fee gate rejected (%s)", candidate.mint, detail)
            ledger.append(CandidateSkipped(mint=candidate.mint, reason="fee_gate", detail=detail))
            shadow_tracker.track(candidate.mint, arm="skipped", reject_reason="fee_gate")
            continue

        try:
            would_succeed, tokens_out_raw, error, is_transient, token_program_id = await _simulate_buy(
                rpc, wallet_pubkey, candidate, position_sol_lamports, slippage_tolerance_fraction,
                fees_config,
            )
        except Exception as exc:
            logger.exception("buy simulation crashed for mint=%s", candidate.mint)
            ledger.append(
                CandidateSkipped(mint=candidate.mint, reason="sim_crashed", detail=str(exc))
            )
            shadow_tracker.track(candidate.mint, arm="skipped", reject_reason="sim_crashed")
            state.record_failure(failure_limit)
            continue

        if not would_succeed:
            if is_transient:
                # Mint not yet visible to this RPC node -- an expected,
                # recoverable condition on a brand-new mint, not a sign
                # anything is broken. Doesn't count toward the failsafe.
                logger.info("skip mint=%s: %s", candidate.mint, error)
                ledger.append(
                    CandidateSkipped(mint=candidate.mint, reason="mint_not_visible", detail=error)
                )
                shadow_tracker.track(
                    candidate.mint, arm="skipped", reject_reason="mint_not_visible"
                )
            else:
                logger.info("simulated buy would fail mint=%s reason=%s", candidate.mint, error)
                ledger.append(
                    CandidateSkipped(mint=candidate.mint, reason="sim_would_fail", detail=error)
                )
                shadow_tracker.track(
                    candidate.mint, arm="skipped", reject_reason="sim_would_fail"
                )
                state.record_failure(failure_limit)
            continue

        signature: str | None = None
        if not dry_run:
            mint = Pubkey.from_string(candidate.mint)
            creator = Pubkey.from_string(candidate.creator)
            max_sol_cost_lamports = round(position_sol_lamports * (1 + slippage_tolerance_fraction))
            # Rebuilt fresh rather than reusing the simulation's instructions --
            # cheap (no RPC call, just re-derives the same accounts and rerolls
            # fee_recipient/buyback_fee_recipient, which is inconsequential:
            # simulation already showed every NORMAL_FEE_RECIPIENTS address
            # behaves identically for a given mint, see program.py).
            real_ix = _build_buy(
                mint, wallet_pubkey, creator, Pubkey.from_string(token_program_id),
                tokens_out_raw, max_sol_cost_lamports, fees_config,
            )
            try:
                signature = await send_and_confirm(rpc, keypair, real_ix, settings.config.execution)
            except (SubmissionError, OnChainFailureError) as exc:
                # Definite outcomes: rejected before landing (fee not spent),
                # or landed and failed on-chain (fee spent, nothing else). No
                # position was opened either way -- safe to just count this
                # as a failure and move on.
                logger.warning("real buy failed mint=%s reason=%s", candidate.mint, exc)
                state.record_failure(failure_limit)
                continue
            except (ConfirmationTimeoutError, BlockhashExpiredError) as exc:
                # UNKNOWN outcome -- the transaction might have landed for
                # real with no confirmation observed. Opening a position
                # here could track tokens we don't have; not opening one
                # could strand real tokens we do have. Neither guess is
                # safe, so: halt new entries immediately and require manual
                # reconciliation of this signature before resuming.
                logger.critical(
                    "real buy outcome UNKNOWN mint=%s: %s -- NOT opening a "
                    "position, reconcile manually before resuming",
                    candidate.mint, exc,
                )
                state.halt_entries_immediately(str(exc))
                continue

        state.record_success()
        tokens_out_whole = tokens_out_raw / 10**TOKEN_DECIMALS
        entry_price_sol = position_sol / tokens_out_whole
        confirmed_at_wall = time.time()
        if dry_run:
            logger.info(
                "[DRY RUN] simulated buy would succeed mint=%s position_sol=%.4f "
                "tokens=%.2f entry_price=%.10f",
                candidate.mint, position_sol, tokens_out_whole, entry_price_sol,
            )
            buy_settlement = None
        else:
            logger.info(
                "buy confirmed mint=%s position_sol=%.4f tokens=%.2f "
                "entry_price=%.10f signature=%s",
                candidate.mint, position_sol, tokens_out_whole, entry_price_sol, signature,
            )
            buy_settlement = await _settle_leg(rpc, signature, wallet_pubkey, "BUY", candidate.mint)

        position = Position(
            mint=candidate.mint,
            entry_price_sol=entry_price_sol,
            entry_tokens=tokens_out_whole,
            opened_at=time.monotonic(),
        )
        position_manager.open(position)
        state.remember_creator(candidate.mint, candidate.creator)
        if token_program_id is not None:
            state.remember_token_program(candidate.mint, token_program_id)

        trade_id = state.open_trade(candidate.mint)
        state.record_leg(trade_id, "BUY", buy_settlement)
        ledger.append(
            EntryFilled(
                mint=candidate.mint,
                trade_id=trade_id,
                signature=signature,
                position_sol=position_sol,
                tokens_bought=tokens_out_whole,
                entry_price_sol=entry_price_sol,
                latency_seconds=confirmed_at_wall - candidate.notified_at_wall,
                settlement=buy_settlement.to_dict() if buy_settlement is not None else None,
            )
        )
        shadow_tracker.track(candidate.mint, arm="bought", trade_id=trade_id)

        _persist(position_manager, state)
        heartbeat.record_trade()


async def run_position_monitor(
    settings: Settings,
    rpc: RpcClient,
    keypair: Keypair,
    wallet_pubkey: Pubkey,
    position_manager: PositionManager,
    heartbeat: Heartbeat,
    state: TradingState,
    ledger: Ledger,
) -> None:
    exits_cfg = settings.config.exits
    slippage_tolerance_fraction = settings.config.trading.slippage_tolerance_fraction
    failure_limit = settings.config.failsafe.consecutive_failure_limit
    dry_run = settings.secrets.dry_run
    # Exits and ATA closes are NEVER fee-gated (5.5) -- fees_config is only
    # used here to size the compute-budget instructions on the sell/close
    # transactions themselves, never to reject one.
    fees_config = settings.config.fees

    while True:
        await asyncio.sleep(POSITION_MONITOR_INTERVAL_SECONDS)
        for position in position_manager.all_open():
            mint = Pubkey.from_string(position.mint)
            try:
                bonding_curve_pda = derive_bonding_curve_pda(mint)
                info = await rpc.call(
                    "getAccountInfo",
                    [str(bonding_curve_pda), {"encoding": "base64", "commitment": "confirmed"}],
                )
                value = info.get("value")
                if value is None:
                    logger.warning("bonding curve missing for open position mint=%s", position.mint)
                    continue
                raw = base64.b64decode(value["data"][0])
                curve = decode_bonding_curve(raw)
            except Exception:
                logger.exception("failed to refresh curve state for mint=%s", position.mint)
                continue

            # curve_price_sol below stays the marginal spot price -- same
            # meaning it had before Milestone 4, kept for continuity in the
            # ledger. The exit DECISION itself uses realizable proceeds
            # (what tokens_remaining would actually net right now, fee and
            # price impact deducted) instead -- see MILESTONE-4-HANDOFF.md
            # Section 3.1: spot price quietly biases every rung, firing
            # take-profits early and the stop-loss late.
            curve_price_sol = (
                spot_price_sol_per_token(curve) * 10**TOKEN_DECIMALS / LAMPORTS_PER_SOL
            )
            tokens_remaining_raw = round(position.tokens_remaining * 10**TOKEN_DECIMALS)
            try:
                realizable_sol_lamports = (
                    tokens_to_sol(curve, tokens_remaining_raw)
                    if tokens_remaining_raw > 0
                    else 0
                )
            except CurveCompleteError:
                logger.warning(
                    "bonding curve completed/migrated for open position mint=%s -- "
                    "cannot price exits this tick",
                    position.mint,
                )
                continue
            realizable_sol_value = realizable_sol_lamports / LAMPORTS_PER_SOL

            creator = state.creator_for(position.mint)
            known_token_program = state.token_program_for(position.mint)

            # Creator-sell detection (Section 4.3): baseline the creator's
            # ATA balance on first successful observation, then watch for a
            # decrease. Absent ATA is a valid zero baseline, not an error --
            # record it and stop polling, there's no signal to be had. A
            # failed fetch leaves creator_sold False and must NEVER touch
            # the failsafe counter (Section 3.5/4.3): this is a monitoring
            # call, not an execution failure, and treating "unknown" as
            # "sold" would exit every position on the first RPC blip.
            creator_sold = False
            if (
                exits_cfg.creator_sell_enabled
                and creator is not None
                and known_token_program is not None
                and not state.creator_ata_poll_done(position.mint)
            ):
                try:
                    creator_ata = derive_associated_token_address(
                        Pubkey.from_string(creator), mint, Pubkey.from_string(known_token_program)
                    )
                    creator_balance = await get_token_account_balance_raw(rpc, creator_ata)
                except Exception:
                    logger.warning(
                        "creator ATA balance fetch failed mint=%s -- treating as "
                        "unknown, not a sell signal",
                        position.mint,
                        exc_info=True,
                    )
                else:
                    if creator_balance is None:
                        creator_balance = 0
                    baseline = state.creator_ata_baseline_for(position.mint)
                    if baseline is None:
                        state.set_creator_ata_baseline(position.mint, creator_balance)
                    elif creator_balance < baseline:
                        creator_sold = True

            decision = position.evaluate_exit(
                realizable_sol_value, time.monotonic(), exits_cfg, creator_sold=creator_sold
            )
            if decision is None:
                continue

            # Captured before apply_exit() reduces tokens_remaining below --
            # this is the multiple the decision was actually made on.
            decision_realizable_multiple = (
                realizable_sol_value / position.cost_basis_of_remaining_sol
                if position.cost_basis_of_remaining_sol > 0
                else 0.0
            )
            decision_peak_multiple = position.trailing_peak_multiple

            tokens_to_sell_raw = round(
                position.tokens_remaining * decision.fraction * 10**TOKEN_DECIMALS
            )
            error: str | None = None
            resolved_token_program: Pubkey | None = None
            min_sol_output_lamports = 0
            if creator is None:
                error = "no creator on record for this mint (position opened before a restart?)"
            elif tokens_to_sell_raw > 0:
                try:
                    # Floor padded down from the curve's own current-state
                    # estimate -- same slippage buffer _simulate_buy pads
                    # its max cost up by, see that function's docstring.
                    expected_sol_out_lamports = tokens_to_sol(curve, tokens_to_sell_raw)
                    min_sol_output_lamports = round(
                        expected_sol_out_lamports * (1 - slippage_tolerance_fraction)
                    )
                    error, resolved_token_program = await _simulate_sell(
                        rpc,
                        wallet_pubkey,
                        mint,
                        Pubkey.from_string(creator),
                        tokens_to_sell_raw,
                        min_sol_output_lamports,
                        fees_config,
                        known_token_program_id=(
                            Pubkey.from_string(known_token_program)
                            if known_token_program is not None
                            else None
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 - summarized into the return value, not swallowed
                    error = f"sell simulation crashed: {exc}"

            if error is not None:
                logger.warning(
                    "simulated sell would fail mint=%s reason=%s decision=%s -- "
                    "leaving position open",
                    position.mint, error, decision.reason.value,
                )
                state.record_failure(failure_limit)
                continue

            signature: str | None = None
            if not dry_run:
                real_ix = _build_sell(
                    mint, wallet_pubkey, Pubkey.from_string(creator), resolved_token_program,
                    tokens_to_sell_raw, min_sol_output_lamports, fees_config,
                )
                try:
                    signature = await send_and_confirm(rpc, keypair, real_ix, settings.config.execution)
                except (SubmissionError, OnChainFailureError) as exc:
                    logger.warning(
                        "real sell failed mint=%s reason=%s decision=%s -- leaving "
                        "position open",
                        position.mint, exc, decision.reason.value,
                    )
                    state.record_failure(failure_limit)
                    continue
                except (ConfirmationTimeoutError, BlockhashExpiredError) as exc:
                    # UNKNOWN outcome -- do NOT touch position state either
                    # way (apply_exit would understate real holdings if the
                    # sell never landed, or overstate them -- wrongly
                    # implying tokens are still there -- if it did). Halt
                    # new entries and require manual reconciliation; the
                    # position stays open in local state exactly as it was,
                    # for a human to check against the real chain.
                    logger.critical(
                        "real sell outcome UNKNOWN mint=%s: %s -- position left "
                        "open in local state, reconcile manually before resuming",
                        position.mint, exc,
                    )
                    state.halt_entries_immediately(str(exc))
                    continue

            state.record_success()
            tokens_sold = position.apply_exit(decision)
            trade_id = state.ensure_trade_id_for(position.mint)
            if dry_run:
                logger.info(
                    "[DRY RUN] simulated sell would succeed mint=%s reason=%s "
                    "tokens_sold=%.2f remaining=%.2f",
                    position.mint, decision.reason.value, tokens_sold, position.tokens_remaining,
                )
                sell_settlement = None
            else:
                logger.info(
                    "sell confirmed mint=%s reason=%s tokens_sold=%.2f "
                    "remaining=%.2f signature=%s",
                    position.mint, decision.reason.value, tokens_sold,
                    position.tokens_remaining, signature,
                )
                sell_settlement = await _settle_leg(
                    rpc, signature, wallet_pubkey, "SELL", position.mint
                )
            state.record_leg(trade_id, "SELL", sell_settlement)
            state.record_exit_reason(trade_id, decision.reason.value)
            ledger.append(
                ExitFilled(
                    mint=position.mint,
                    trade_id=trade_id,
                    signature=signature,
                    exit_reason=decision.reason.value,
                    tokens_sold=tokens_sold,
                    tokens_remaining=position.tokens_remaining,
                    curve_price_sol=curve_price_sol,
                    realizable_multiple=decision_realizable_multiple,
                    peak_multiple=decision_peak_multiple,
                    settlement=sell_settlement.to_dict() if sell_settlement is not None else None,
                )
            )
            heartbeat.record_trade()
            if position.tokens_remaining <= 0:
                if not dry_run and resolved_token_program is not None:
                    ata_signature = await close_ata_after_exit(
                        rpc, keypair, mint, resolved_token_program, settings.config.execution,
                        fees_config,
                    )
                    if ata_signature is not None:
                        ata_settlement = await _settle_leg(
                            rpc, ata_signature, wallet_pubkey, "ATA_CLOSE", position.mint
                        )
                        state.record_leg(trade_id, "ATA_CLOSE", ata_settlement)
                        ledger.append(
                            AtaClosed(
                                mint=position.mint,
                                trade_id=trade_id,
                                signature=ata_signature,
                                settlement=(
                                    ata_settlement.to_dict() if ata_settlement is not None else None
                                ),
                            )
                        )
                position_manager.close(position.mint)
                state.forget_creator_ata(position.mint)
                popped = state.pop_trade(position.mint)
                if popped is not None:
                    _, legs, exit_reasons, entry_wall = popped
                    ledger.append(
                        _build_trade_closed(
                            position.mint, trade_id, legs, exit_reasons, entry_wall, dry_run
                        )
                    )
            _persist(position_manager, state)


async def run_reconciliation(settings: Settings, rpc: RpcClient, wallet_pubkey: Pubkey) -> None:
    interval = settings.config.reconciliation.interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            balance = await rpc.get_balance_sol(str(wallet_pubkey))
            logger.info(
                "reconciliation: wallet=%s balance=%.5f SOL credits_spent_today=%d",
                wallet_pubkey, balance, rpc.credits_spent_today,
            )
        except Exception:
            logger.exception("reconciliation check failed")


def count_real_closed_trades(events) -> int:
    """Counts TradeClosed events from real (non-dry-run) runs -- the only
    trades that carry real expectancy evidence. Dry-run "trades" never
    touch the chain and can't inform whether raising position_sol is
    supported by data. Used by the startup sizing-discipline guard
    (MILESTONE-3-HANDOFF.md Section 5.6)."""
    return sum(1 for e in events if e.get("event") == "TradeClosed" and e.get("dry_run") is False)


async def _on_heartbeat(report: HeartbeatReport) -> None:
    level = logging.WARNING if report.idle_alarm else logging.INFO
    logger.log(
        level,
        "heartbeat tick=%d candidates=%d trades=%d positions_open=%d idle_alarm=%s",
        report.tick, report.candidates_seen, report.trades_executed,
        report.positions_open, report.idle_alarm,
    )


async def main() -> None:
    settings = load_settings()
    keypair = _load_keypair(settings.secrets.wallet_keypair_path)

    async with RpcClient(settings) as rpc, httpx.AsyncClient() as tier2_http_client:
        wallet_pubkey = await preflight(settings, rpc, keypair)

        creator_blocklist = load_creator_blocklist(
            PROJECT_ROOT / settings.config.filters.creator_blocklist_path
        )
        name_symbol_blocklist = load_name_symbol_blocklist(
            PROJECT_ROOT / settings.config.filters.name_symbol_blocklist_path
        )
        tier1_filter = Tier1Filter(
            settings.config.filters.tier1, creator_blocklist, name_symbol_blocklist
        )
        tier2_filter = Tier2Filter(settings.config.filters.tier2, tier2_http_client)

        run_id = new_run_id()
        ledger_path = PROJECT_ROOT / settings.config.ledger.path
        ledger = Ledger(
            ledger_path,
            run_id,
            settings.secrets.dry_run,
            enabled=settings.config.ledger.enabled,
        )
        shadow_tracker = ShadowTracker(settings.config.shadow, rpc, ledger)

        if settings.config.ledger.enabled:
            orphans = find_orphans(read_events(ledger_path.parent), run_id)
            for orphan in orphans:
                ledger.append(orphan)
            if orphans:
                logger.warning(
                    "orphan scan: %d trade(s) from a previous run had no TradeClosed -- "
                    "appended TradeOrphaned, excluded from statistics",
                    len(orphans),
                )

            baseline_position_sol = settings.config.trading.baseline_position_sol
            min_closed_trades_before_sizeup = settings.config.trading.min_closed_trades_before_sizeup
            if settings.config.trading.position_sol > baseline_position_sol:
                real_closed_trades = count_real_closed_trades(read_events(ledger_path.parent))
                if real_closed_trades < min_closed_trades_before_sizeup:
                    # Never refuses to start -- it's the operator's money and
                    # their call (MILESTONE-3-HANDOFF.md Section 5.6), but a
                    # position size raised ahead of the data that would
                    # justify it needs to be loud, not silent.
                    logger.critical(
                        "sizing discipline: position_sol=%.4f exceeds baseline_position_sol=%.4f "
                        "with only %d/%d real closed trades -- expectancy at this size is "
                        "UNMEASURED, starting anyway",
                        settings.config.trading.position_sol, baseline_position_sol,
                        real_closed_trades, min_closed_trades_before_sizeup,
                    )

        queue: asyncio.Queue[Candidate] = asyncio.Queue(maxsize=CANDIDATE_QUEUE_MAXSIZE)
        listener = MintListener(tier1_filter, queue, ledger=ledger, shadow_tracker=shadow_tracker)
        position_manager = PositionManager(settings.config.trading.max_concurrent_positions)
        heartbeat = Heartbeat(settings.config.heartbeat)
        state = TradingState()

        restored_positions, restored_creators, restored_token_programs = load_state(STATE_PATH)
        for position in restored_positions:
            try:
                position_manager.open(position)
            except PositionLimitReachedError:
                logger.critical(
                    "restored position for mint=%s exceeds max_concurrent_positions "
                    "(%d) -- left out of tracking, reconcile manually",
                    position.mint, settings.config.trading.max_concurrent_positions,
                )
        for mint, creator in restored_creators.items():
            state.remember_creator(mint, creator)
        for mint, token_program_id in restored_token_programs.items():
            state.remember_token_program(mint, token_program_id)
        if restored_positions:
            logger.warning(
                "restored %d open position(s) from %s -- carried over from a "
                "previous run",
                len(restored_positions), STATE_PATH,
            )

        if settings.secrets.dry_run:
            logger.info(
                "starting pumpbot: wallet=%s dry_run=True position_sol=%.4f max_concurrent=%d",
                wallet_pubkey,
                settings.config.trading.position_sol, settings.config.trading.max_concurrent_positions,
            )
        else:
            logger.warning(
                "*** LIVE MODE -- DRY_RUN=false: this run WILL sign and send real "
                "transactions and CAN move real SOL *** wallet=%s position_sol=%.4f "
                "max_concurrent=%d",
                wallet_pubkey,
                settings.config.trading.position_sol, settings.config.trading.max_concurrent_positions,
            )

        try:
            await asyncio.gather(
                listener.run(),
                run_trader(
                    settings, rpc, keypair, wallet_pubkey, queue, position_manager, heartbeat,
                    state, tier2_filter, ledger, shadow_tracker,
                ),
                run_position_monitor(
                    settings, rpc, keypair, wallet_pubkey, position_manager, heartbeat, state,
                    ledger,
                ),
                run_reconciliation(settings, rpc, wallet_pubkey),
                heartbeat.run_forever(lambda: position_manager.open_count, _on_heartbeat),
                shadow_tracker.run(),
            )
        finally:
            ledger.close()


if __name__ == "__main__":
    asyncio.run(main())
