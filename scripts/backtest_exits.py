"""Offline replay of the exit ladder against recorded dry-run price paths.

Pure analysis over data/*.jsonl already on disk -- no network access, no run
of the trading loop. See .claude/plans/EXIT-RETUNE-HANDOFF.md for the full
design rationale; only what's needed to read this file is repeated here.

Reconstructs, per bought-arm dry-run trade, the sequence of BondingCurveState
objects the live position monitor would have seen (from the mint's invariant
k, pinned at CandidateSeen, and each ShadowPrice tick's spot price), then
drives the REAL Position/evaluate_exit from src/pumpbot/positions.py tick by
tick -- never a reimplementation of the ladder. The result is a "modelled
multiple" per trade under any exits: config, at 15s resolution (7.5x coarser
than the live loop's 2s poll) and bounded by the 300s shadow horizon. See
EXIT-RETUNE-HANDOFF.md Section 2.5 (one-sided resolution bias -- can only
UNDERSTATE take-profit/stop hits) and 2.6 (no data past 300s) before trusting
any number this script prints.

UNITS TRAP (Section 2.4): ShadowPrice.price_sol is lamports per RAW token
unit (the reserve ratio); ExitFilled.curve_price_sol is SOL per WHOLE token.
They differ by exactly 10**TOKEN_DECIMALS / LAMPORTS_PER_SOL = 1e-3. Get this
wrong and every modelled multiple is silently off by 1000x.
"""

from __future__ import annotations

import argparse
import itertools
import math
import random
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pumpbot.config import PROJECT_ROOT, ExitsConfig, load_settings
from pumpbot.curve import (
    LAMPORTS_PER_SOL,
    TOKEN_DECIMALS,
    BondingCurveState,
    tokens_to_sol,
)
from pumpbot.ledger import read_events
from pumpbot.positions import Position

# Reused from scripts/report.py -- one evidence bar per project, not two
# (DEFERRED-exit-threshold-retune.md's identical reasoning).
MIN_SAMPLE_FOR_STATS = 30

DEFAULT_DATA_DIR = PROJECT_ROOT / "data"

# Section 2.10: a two-signature round trip (buy + sell), 5,000 lamports each,
# regardless of leg count -- the handoff's own stated simplification.
BASE_FEE_LAMPORTS_PER_SIGNATURE = 5000
ROUND_TRIP_SIGNATURES = 2


# --- reconstruction ---------------------------------------------------------


def reconstruct_curve(k: int, price_sol_raw: float) -> BondingCurveState:
    """Rebuilds a BondingCurveState from a mint's invariant k (pinned at
    CandidateSeen: virtual_sol_reserves(lamports) * virtual_token_reserves(raw))
    and one tick's ShadowPrice.price_sol (the RAW reserve ratio p =
    virtual_sol/virtual_token). Since k = vs*vt and p = vs/vt:
    vs = sqrt(k*p), vt = sqrt(k/p). Only virtual_sol_reserves,
    virtual_token_reserves, and complete are ever read by tokens_to_sol --
    the other three fields are zeroed, not guessed."""
    if k <= 0 or price_sol_raw <= 0:
        raise ValueError(f"invalid curve reconstruction inputs: k={k} price_sol_raw={price_sol_raw}")
    virtual_sol = math.sqrt(k * price_sol_raw)
    virtual_token = math.sqrt(k / price_sol_raw)
    return BondingCurveState(
        virtual_sol_reserves=round(virtual_sol),
        virtual_token_reserves=round(virtual_token),
        real_sol_reserves=0,
        real_token_reserves=0,
        token_total_supply=0,
        complete=False,
    )


# --- data model --------------------------------------------------------------


@dataclass(frozen=True)
class ShadowTick:
    ts_monotonic: float
    price_sol_raw: float


@dataclass(frozen=True)
class ReplayTrade:
    trade_id: str
    mint: str
    entry_price_sol: float
    entry_tokens: float
    position_sol: float
    opened_at: float  # ts_monotonic
    k: int
    ticks: tuple[ShadowTick, ...]
    recorded_reason: str
    recorded_multiple: float | None
    creator_sold_at: float | None  # ts_monotonic of the creator_sold leg, if any


def load_trades(events: list[dict[str, Any]]) -> tuple[list[ReplayTrade], dict[str, int]]:
    """Joins CandidateSeen / EntryFilled / bought-arm ShadowPrice / ExitFilled
    / TradeClosed rows into one ReplayTrade per closed dry-run trade_id.
    Returns (trades, diagnostics) -- diagnostics reports join completeness
    per Section 2.9 rather than silently analysing a biased subset."""
    candidate_seen_by_mint: dict[str, dict[str, Any]] = {}
    entry_by_trade_id: dict[str, dict[str, Any]] = {}
    exits_by_trade_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    closed_by_trade_id: dict[str, dict[str, Any]] = {}
    shadow_by_trade_id: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in events:
        event = row.get("event")
        if event == "CandidateSeen":
            candidate_seen_by_mint.setdefault(row["mint"], row)
        elif event == "EntryFilled" and row.get("dry_run"):
            entry_by_trade_id[row["trade_id"]] = row
        elif event == "ExitFilled" and row.get("dry_run"):
            exits_by_trade_id[row["trade_id"]].append(row)
        elif event == "TradeClosed" and row.get("dry_run"):
            closed_by_trade_id[row["trade_id"]] = row
        elif event == "ShadowPrice" and row.get("arm") == "bought" and row.get("trade_id"):
            shadow_by_trade_id[row["trade_id"]].append(row)

    diagnostics = {
        "entry_filled_dry_run": len(entry_by_trade_id),
        "trade_closed_dry_run": len(closed_by_trade_id),
        "still_open_or_orphaned": 0,
        "missing_candidate_seen": 0,
        "missing_shadow_path": 0,
        "too_few_ticks": 0,
        "replayable_trades": 0,
    }

    trades: list[ReplayTrade] = []
    for trade_id, entry in entry_by_trade_id.items():
        closed = closed_by_trade_id.get(trade_id)
        if closed is None:
            diagnostics["still_open_or_orphaned"] += 1
            continue

        mint = entry["mint"]
        candidate = candidate_seen_by_mint.get(mint)
        if candidate is None:
            diagnostics["missing_candidate_seen"] += 1
            continue

        shadow_rows = sorted(shadow_by_trade_id.get(trade_id, []), key=lambda r: r["ts_monotonic"])
        if not shadow_rows:
            diagnostics["missing_shadow_path"] += 1
            continue
        if len(shadow_rows) < 2:
            diagnostics["too_few_ticks"] += 1
            continue

        virtual_sol_lamports = round(candidate["virtual_sol_in_curve"] * LAMPORTS_PER_SOL)
        virtual_tokens_raw = round(candidate["virtual_tokens_in_curve"] * 10**TOKEN_DECIMALS)
        k = virtual_sol_lamports * virtual_tokens_raw

        exit_reasons = closed.get("exit_reasons") or []
        recorded_reason = exit_reasons[-1] if exit_reasons else "unknown"

        legs = sorted(exits_by_trade_id.get(trade_id, []), key=lambda r: r["ts_monotonic"])
        recorded_multiple = legs[-1]["realizable_multiple"] if legs else None
        creator_sold_at = next(
            (leg["ts_monotonic"] for leg in legs if leg.get("exit_reason") == "creator_sold"),
            None,
        )

        trades.append(
            ReplayTrade(
                trade_id=trade_id,
                mint=mint,
                entry_price_sol=entry["entry_price_sol"],
                entry_tokens=entry["tokens_bought"],
                position_sol=entry["position_sol"],
                opened_at=entry["ts_monotonic"],
                k=k,
                ticks=tuple(
                    ShadowTick(ts_monotonic=r["ts_monotonic"], price_sol_raw=r["price_sol"])
                    for r in shadow_rows
                ),
                recorded_reason=recorded_reason,
                recorded_multiple=recorded_multiple,
                creator_sold_at=creator_sold_at,
            )
        )

    diagnostics["replayable_trades"] = len(trades)
    return trades, diagnostics


# --- replay --------------------------------------------------------------


@dataclass(frozen=True)
class ReplayResult:
    exit_reason: str
    exit_ts_monotonic: float | None
    final_multiple: float
    peak_multiple: float
    ticks_consumed: int


def replay_trade(
    trade: ReplayTrade,
    exits_cfg: ExitsConfig,
    *,
    suppress_creator_sold: bool = False,
) -> ReplayResult:
    """Drives a REAL Position through evaluate_exit tick by tick over the
    trade's recorded shadow price path. Never reimplements the ladder
    (EXIT-RETUNE-HANDOFF.md Section 3.2)."""
    position = Position(
        mint=trade.mint,
        entry_price_sol=trade.entry_price_sol,
        entry_tokens=trade.entry_tokens,
        opened_at=trade.opened_at,
    )
    peak_multiple = 0.0
    last_multiple = 0.0
    exit_reason: str | None = None
    exit_ts: float | None = None
    ticks_consumed = 0

    for tick in trade.ticks:
        if position.tokens_remaining <= 0:
            break
        tokens_remaining_raw = round(position.tokens_remaining * 10**TOKEN_DECIMALS)
        if tokens_remaining_raw <= 0:
            break

        curve = reconstruct_curve(trade.k, tick.price_sol_raw)
        realizable_sol_value = tokens_to_sol(curve, tokens_remaining_raw) / LAMPORTS_PER_SOL
        ticks_consumed += 1

        cost_basis = position.cost_basis_of_remaining_sol
        multiple = realizable_sol_value / cost_basis if cost_basis > 0 else 0.0
        peak_multiple = max(peak_multiple, multiple)
        last_multiple = multiple

        creator_sold_now = (
            not suppress_creator_sold
            and trade.creator_sold_at is not None
            and tick.ts_monotonic >= trade.creator_sold_at
        )

        decision = position.evaluate_exit(
            realizable_sol_value, tick.ts_monotonic, exits_cfg, creator_sold=creator_sold_now
        )
        if decision is not None:
            position.apply_exit(decision)
            exit_reason = decision.reason.value
            exit_ts = tick.ts_monotonic

    if exit_reason is None:
        # Ran out of ticks (bounded by the 300s shadow horizon, Section 2.6)
        # without any rung firing -- not one of the ladder's own reasons, so
        # it cannot be confused with a real exit reason in the confusion
        # matrix or sweep output.
        exit_reason = "horizon_end"

    return ReplayResult(
        exit_reason=exit_reason,
        exit_ts_monotonic=exit_ts,
        final_multiple=last_multiple,
        peak_multiple=peak_multiple,
        ticks_consumed=ticks_consumed,
    )


# --- Task 2: baseline fidelity ---------------------------------------------


@dataclass(frozen=True)
class FidelityReport:
    match_rate: float
    n: int
    confusion: dict[tuple[str, str], int]
    abs_multiple_diffs: list[float]
    mismatch_directions: Counter


def run_fidelity_check(trades: list[ReplayTrade], exits_cfg: ExitsConfig) -> FidelityReport:
    confusion: Counter = Counter()
    abs_diffs: list[float] = []
    matches = 0
    mismatch_directions: Counter = Counter()

    for trade in trades:
        result = replay_trade(trade, exits_cfg)
        confusion[(trade.recorded_reason, result.exit_reason)] += 1
        if trade.recorded_reason == result.exit_reason:
            matches += 1
            if trade.recorded_multiple is not None:
                abs_diffs.append(abs(result.final_multiple - trade.recorded_multiple))
        else:
            # Section 2.5's expected direction: replay MISSES a take-profit/
            # stop/trailing rung the live loop caught (replayed=timeout or a
            # lower rung than recorded). The opposite direction (replay fires
            # something the live run did not) would indicate a units/logic bug.
            mismatch_directions[(trade.recorded_reason, result.exit_reason)] += 1

    n = len(trades)
    return FidelityReport(
        match_rate=matches / n if n else 0.0,
        n=n,
        confusion=dict(confusion),
        abs_multiple_diffs=abs_diffs,
        mismatch_directions=mismatch_directions,
    )


def fidelity_band(match_rate: float) -> str:
    if match_rate >= 0.90:
        return "TRUSTWORTHY (>=90%)"
    if match_rate >= 0.70:
        return "PROVISIONAL (70-90%)"
    return "STOP (<70%)"


# --- Task 3: parameter sweep -------------------------------------------------

SWEEP_GRID: dict[str, list[Any]] = {
    "take_profit_1_multiple": [1.2, 1.3, 1.5, 2.0],
    "take_profit_1_fraction": [0.0, 0.5, 1.0],
    "take_profit_2_multiple": [1.5, 2.0, 2.5, 4.0],
    "stop_loss_fraction": [-0.15, -0.25, -0.35, -0.50],
    # None means trailing_enabled=False; a float is trailing_arm_multiple
    # with trailing_enabled=True (trailing_drawdown_fraction held at the
    # base config's value -- not swept, per the pre-registered grid).
    "trailing_arm_multiple": [1.15, 1.3, 1.5, None],
    "timeout_seconds": [60, 120, 180, 240, 300],
}


# TAIL-ANALYSIS-HANDOFF.md Task 3: an extended take_profit_2_multiple axis
# to test whether loosening or removing the 2.5x cap improves the mean.
# 1e9 is "effectively uncapped" -- evaluate_exit checks
# `multiple >= config.take_profit_2_multiple`, so a very large value disables
# the rung with no code change to positions.py (Section 3.2).
UNCAPPED_TAKE_PROFIT_2_MULTIPLE = 1e9
EXTENDED_SWEEP_GRID: dict[str, list[Any]] = {
    **SWEEP_GRID,
    "take_profit_2_multiple": [1.5, 2.0, 2.5, 4.0, 6.0, 10.0, UNCAPPED_TAKE_PROFIT_2_MULTIPLE],
}


def iter_sweep_configs(
    base: ExitsConfig, grid: dict[str, list[Any]] | None = None
) -> tuple[list[tuple[dict[str, Any], ExitsConfig]], int]:
    """Yields (params, config) for every coherent combination in `grid`
    (default SWEEP_GRID). Returns (combos, skipped_count) -- skipped combos
    have take_profit_2_multiple <= take_profit_1_multiple (Section 4 Task 3)."""
    grid = grid if grid is not None else SWEEP_GRID
    keys = list(grid.keys())
    combos: list[tuple[dict[str, Any], ExitsConfig]] = []
    skipped = 0
    for values in itertools.product(*(grid[k] for k in keys)):
        params = dict(zip(keys, values, strict=True))
        if params["take_profit_2_multiple"] <= params["take_profit_1_multiple"]:
            skipped += 1
            continue
        trailing_arm = params["trailing_arm_multiple"]
        update = {
            "take_profit_1_multiple": params["take_profit_1_multiple"],
            "take_profit_1_fraction": params["take_profit_1_fraction"],
            "take_profit_2_multiple": params["take_profit_2_multiple"],
            "stop_loss_fraction": params["stop_loss_fraction"],
            "timeout_seconds": params["timeout_seconds"],
            "trailing_enabled": trailing_arm is not None,
        }
        if trailing_arm is not None:
            update["trailing_arm_multiple"] = trailing_arm
        cfg = base.model_copy(update=update)
        combos.append((params, cfg))
    return combos, skipped


# TAIL-ANALYSIS-HANDOFF.md Section 1.1/Task 1: ranking by median actively
# selects against a tail strategy ("profitable snipers lose on 60-80% of
# snipes; returns come from the 20-40% that 5x+" -- the PRD). These four
# objectives are reported side by side, never collapsed to one "winner"
# (Section 3.1).
OBJECTIVES = ("median", "mean", "total_return", "net_mean")


@dataclass(frozen=True)
class ComboSummary:
    params: dict[str, Any]
    n: int
    median_multiple: float
    mean_multiple: float
    total_return: float  # sum(m - 1 for m in multiples) -- what the account does
    net_mean: float  # mean(m - net_breakeven_multiple(trade)) -- fee-aware mean
    frac_above_gross_breakeven: float
    frac_above_net_breakeven: float
    exit_reason_counts: dict[str, int]
    multiples: tuple[float, ...]  # retained per-trade for tail diagnostics (Task 2)


def objective_value(summary: ComboSummary, objective: str) -> float:
    if objective == "median":
        return summary.median_multiple
    if objective == "mean":
        return summary.mean_multiple
    if objective == "total_return":
        return summary.total_return
    if objective == "net_mean":
        return summary.net_mean
    raise ValueError(f"unknown objective: {objective!r} (expected one of {OBJECTIVES})")


def net_breakeven_multiple(trade: ReplayTrade) -> float:
    """Section 2.10: base fee is not in the curve math. A two-signature
    round trip at BASE_FEE_LAMPORTS_PER_SIGNATURE lamports each is this
    fraction of position_sol -- the modelled multiple needed to break even
    net of it, not just net of the 1% trading fee already inside
    tokens_to_sol."""
    base_fee_lamports = ROUND_TRIP_SIGNATURES * BASE_FEE_LAMPORTS_PER_SIGNATURE
    position_lamports = trade.position_sol * LAMPORTS_PER_SOL
    return 1.0 + (base_fee_lamports / position_lamports if position_lamports > 0 else 0.0)


def summarize_combo(
    params: dict[str, Any], cfg: ExitsConfig, trades: list[ReplayTrade]
) -> ComboSummary:
    multiples: list[float] = []
    net_diffs: list[float] = []
    above_gross = 0
    above_net = 0
    reasons: Counter = Counter()
    for trade in trades:
        result = replay_trade(trade, cfg)
        multiples.append(result.final_multiple)
        net_diffs.append(result.final_multiple - net_breakeven_multiple(trade))
        reasons[result.exit_reason] += 1
        if result.final_multiple > 1.0:
            above_gross += 1
        if result.final_multiple > net_breakeven_multiple(trade):
            above_net += 1
    n = len(trades)
    return ComboSummary(
        params=params,
        n=n,
        median_multiple=statistics.median(multiples) if multiples else 0.0,
        mean_multiple=statistics.mean(multiples) if multiples else 0.0,
        total_return=sum(m - 1.0 for m in multiples),
        net_mean=statistics.mean(net_diffs) if net_diffs else 0.0,
        frac_above_gross_breakeven=above_gross / n if n else 0.0,
        frac_above_net_breakeven=above_net / n if n else 0.0,
        exit_reason_counts=dict(reasons),
        multiples=tuple(multiples),
    )


def run_sweep(
    trades: list[ReplayTrade], base_cfg: ExitsConfig, grid: dict[str, list[Any]] | None = None
) -> tuple[list[ComboSummary], int]:
    combos, skipped = iter_sweep_configs(base_cfg, grid)
    summaries = [summarize_combo(params, cfg, trades) for params, cfg in combos]
    return summaries, skipped


def baseline_params(base_cfg: ExitsConfig) -> dict[str, Any]:
    return {
        "take_profit_1_multiple": base_cfg.take_profit_1_multiple,
        "take_profit_1_fraction": base_cfg.take_profit_1_fraction,
        "take_profit_2_multiple": base_cfg.take_profit_2_multiple,
        "stop_loss_fraction": base_cfg.stop_loss_fraction,
        "trailing_arm_multiple": (
            base_cfg.trailing_arm_multiple if base_cfg.trailing_enabled else None
        ),
        "timeout_seconds": base_cfg.timeout_seconds,
    }


def find_rank(
    summaries: list[ComboSummary], target_params: dict[str, Any], objective: str = "median"
) -> int | None:
    """1-indexed rank of target_params among summaries, sorted by
    `objective` descending. None if target_params isn't in the grid (e.g.
    baseline values fall outside the pre-registered grid)."""
    ranked = sorted(summaries, key=lambda s: objective_value(s, objective), reverse=True)
    for i, s in enumerate(ranked, start=1):
        if s.params == target_params:
            return i
    return None


def split_half_stability(
    trades: list[ReplayTrade],
    base_cfg: ExitsConfig,
    objective: str = "median",
    top_n: int = 5,
) -> dict[str, Any]:
    """Sorts trades by entry time, splits into first/second half, sweeps each
    independently under `objective`, and reports whether the best
    combinations agree (Section 3.5). A result that doesn't replicate is
    stated plainly, not hidden."""
    ordered = sorted(trades, key=lambda t: t.opened_at)
    mid = len(ordered) // 2
    first_half, second_half = ordered[:mid], ordered[mid:]

    combos, _ = iter_sweep_configs(base_cfg)
    first_summaries = [summarize_combo(p, c, first_half) for p, c in combos]
    second_summaries = [summarize_combo(p, c, second_half) for p, c in combos]

    def top_params(summaries: list[ComboSummary]) -> list[dict[str, Any]]:
        ranked = sorted(summaries, key=lambda s: objective_value(s, objective), reverse=True)
        return [s.params for s in ranked[:top_n]]

    first_top = top_params(first_summaries)
    second_top = top_params(second_summaries)

    first_top1_rank_in_second = (
        find_rank(second_summaries, first_top[0], objective) if first_top else None
    )
    overlap = sum(1 for p in first_top if p in second_top)
    first_half_top1_value = (
        max(objective_value(s, objective) for s in first_summaries) if first_summaries else None
    )

    return {
        "objective": objective,
        "first_half_n": len(first_half),
        "second_half_n": len(second_half),
        "first_half_top1_params": first_top[0] if first_top else None,
        "first_half_top1_value": first_half_top1_value,
        "first_top1_rank_in_second_half": first_top1_rank_in_second,
        "second_half_grid_size": len(second_summaries),
        "top5_overlap_count": overlap,
        "insufficient_sample": min(len(first_half), len(second_half)) < MIN_SAMPLE_FOR_STATS,
    }


# --- Task 2: tail diagnostics ------------------------------------------------


def trimmed_mean_sensitivity(multiples: list[float]) -> dict[str, float | None]:
    """Mean with all trades, minus the top 1/3/5 by value. A big drop means
    the mean rests on a handful of trades (Section 1.2) -- report it rather
    than picking whichever framing (median or mean) reads better."""
    ordered = sorted(multiples, reverse=True)
    result: dict[str, float | None] = {"mean_all": statistics.mean(ordered) if ordered else None}
    for k in (1, 3, 5):
        remaining = ordered[k:]
        result[f"mean_minus_top_{k}"] = statistics.mean(remaining) if remaining else None
    return result


def bootstrap_ci_mean(
    multiples: list[float], n_resamples: int = 10_000, seed: int = 0
) -> dict[str, float | None]:
    """Percentile bootstrap 95% CI on the mean (Section 3.4). A wide interval
    that straddles 1.0 IS the answer at this n -- not a reason to keep
    resampling until it doesn't."""
    n = len(multiples)
    if n == 0:
        return {"lo": None, "hi": None, "contains_1_0": None, "contains_net_breakeven": None}
    rng = random.Random(seed)
    means = []
    for _ in range(n_resamples):
        sample = [multiples[rng.randrange(n)] for _ in range(n)]
        means.append(statistics.mean(sample))
    means.sort()
    lo = means[int(0.025 * n_resamples)]
    hi = means[int(0.975 * n_resamples) - 1]
    return {
        "lo": lo,
        "hi": hi,
        "contains_1_0": lo <= 1.0 <= hi,
        # ~1.01, the Section 2.10 base-fee-adjusted bar at position_sol=0.001.
        "contains_net_breakeven": lo <= 1.01 <= hi,
    }


def tail_event_rates(multiples: list[float]) -> dict[str, Any]:
    n = len(multiples)
    counts = {
        threshold: sum(1 for m in multiples if m > threshold) for threshold in (1.5, 2.0, 2.5)
    }
    return {
        "n": n,
        "counts": counts,
        "rates": {t: (c / n if n else 0.0) for t, c in counts.items()},
    }


def required_n_for_tail_events(tail_rate: float, target_events: int = 30) -> float | None:
    """Roughly how many trades are needed to observe `target_events` tail
    events at the given rate -- directly sizes Task 4's observation window."""
    if tail_rate <= 0:
        return None
    return target_events / tail_rate


# --- Task 4: creator-sold counterfactual ------------------------------------


def creator_sold_counterfactual(
    trades: list[ReplayTrade], base_cfg: ExitsConfig
) -> dict[str, Any]:
    affected = [t for t in trades if t.recorded_reason == "creator_sold"]
    with_creator_sold = [replay_trade(t, base_cfg).final_multiple for t in affected]
    without_creator_sold = [
        replay_trade(t, base_cfg, suppress_creator_sold=True).final_multiple for t in affected
    ]
    without_reasons = Counter(
        replay_trade(t, base_cfg, suppress_creator_sold=True).exit_reason for t in affected
    )
    return {
        "n": len(affected),
        "with_creator_sold_median": statistics.median(with_creator_sold) if with_creator_sold else None,
        "without_creator_sold_median": statistics.median(without_creator_sold)
        if without_creator_sold
        else None,
        "without_creator_sold_mean": statistics.mean(without_creator_sold)
        if without_creator_sold
        else None,
        "without_creator_sold_exit_reasons": dict(without_reasons),
    }


# --- CLI / report ------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline replay of the exit ladder against recorded dry-run price paths"
    )
    parser.add_argument(
        "--ledger", type=str, default=None,
        help="Path to the ledger directory (default: config.yaml's ledger.path directory)",
    )
    args = parser.parse_args()

    settings = load_settings()
    if args.ledger:
        ledger_dir = Path(args.ledger)
    else:
        ledger_dir = (PROJECT_ROOT / settings.config.ledger.path).parent
    base_cfg = settings.config.exits

    events = list(read_events(ledger_dir))
    trades, diagnostics = load_trades(events)

    print("=== Join completeness (Section 2.9) ===")
    for k, v in diagnostics.items():
        print(f"  {k}: {v}")
    print()

    fidelity = run_fidelity_check(trades, base_cfg)
    band = fidelity_band(fidelity.match_rate)
    print("=== Task 2: Baseline fidelity ===")
    print(f"  match_rate: {fidelity.match_rate:.1%} (n={fidelity.n}) -> {band}")
    print("  confusion matrix (recorded -> replayed): count")
    for (recorded, replayed), count in sorted(fidelity.confusion.items(), key=lambda kv: -kv[1]):
        marker = "" if recorded == replayed else "  <-- MISMATCH"
        print(f"    {recorded} -> {replayed}: {count}{marker}")
    if fidelity.abs_multiple_diffs:
        print(
            f"  |replayed - recorded| multiple, matched trades: "
            f"median={statistics.median(fidelity.abs_multiple_diffs):.4f} "
            f"max={max(fidelity.abs_multiple_diffs):.4f} (n={len(fidelity.abs_multiple_diffs)})"
        )
    print()

    if fidelity.match_rate < 0.70:
        print("Match rate below 70% -- STOPPING per Section 3.3. Sweep not run.")
        return

    print("=== Task 3 (prior task): Parameter sweep ===")
    summaries, skipped = run_sweep(trades, base_cfg)
    print(f"  grid combinations run: {len(summaries)}  skipped (tp2<=tp1): {skipped}")

    base_params = baseline_params(base_cfg)
    base_summary = next((s for s in summaries if s.params == base_params), None)
    print(f"  baseline params: {base_params}")

    print()
    print("=== Task 1 (this task): multi-objective ranking ===")
    top10_by_objective: dict[str, list[dict[str, Any]]] = {}
    for objective in OBJECTIVES:
        ranked = sorted(summaries, key=lambda s: objective_value(s, objective), reverse=True)
        rank = find_rank(summaries, base_params, objective)
        top10_by_objective[objective] = [s.params for s in ranked[:10]]
        print(f"  -- objective={objective} --")
        if base_summary is not None:
            print(
                f"    baseline value={objective_value(base_summary, objective):.4f} "
                f"rank={rank}/{len(summaries)}"
            )
        print("    top 5:")
        for i, s in enumerate(ranked[:5], start=1):
            print(
                f"      #{i} {objective}={objective_value(s, objective):.4f} "
                f"median={s.median_multiple:.4f} mean={s.mean_multiple:.4f} "
                f"total_return={s.total_return:.4f} net_mean={s.net_mean:.4f} "
                f"params={s.params}"
            )

    print("  top-10 overlap between objectives (count of shared param sets):")
    for a, b in itertools.combinations(OBJECTIVES, 2):
        overlap = sum(1 for p in top10_by_objective[a] if p in top10_by_objective[b])
        print(f"    {a} vs {b}: {overlap}/10")
    print()

    any_above_net = any(s.frac_above_net_breakeven > 0.5 for s in summaries)
    max_frac_above_net = max((s.frac_above_net_breakeven for s in summaries), default=0.0)
    print(
        f"  combos where >50% of trades clear net-of-base-fee breakeven: "
        f"{'YES' if any_above_net else 'NONE'} (max frac_above_net_breakeven across whole "
        f"grid = {max_frac_above_net:.1%})"
    )
    print()

    print("=== Split-half stability, by objective ===")
    for objective in OBJECTIVES:
        stability = split_half_stability(trades, base_cfg, objective=objective)
        print(f"  -- objective={objective} --")
        for k, v in stability.items():
            print(f"    {k}: {v}")
    print()

    print("=== Task 2: Tail diagnostics ===")
    diagnostic_targets: dict[str, ComboSummary] = {}
    if base_summary is not None:
        diagnostic_targets["baseline"] = base_summary
    for objective in OBJECTIVES:
        ranked = sorted(summaries, key=lambda s: objective_value(s, objective), reverse=True)
        if ranked:
            diagnostic_targets[f"winner[{objective}]"] = ranked[0]

    for label, summary in diagnostic_targets.items():
        print(f"  -- {label}: params={summary.params} --")
        trimmed = trimmed_mean_sensitivity(list(summary.multiples))
        print(f"    trimmed-mean sensitivity: {trimmed}")
        ci = bootstrap_ci_mean(list(summary.multiples))
        print(
            f"    bootstrap 95% CI on mean: [{ci['lo']:.4f}, {ci['hi']:.4f}]  "
            f"contains_1.0={ci['contains_1_0']}  contains_net_breakeven(~1.01)="
            f"{ci['contains_net_breakeven']}"
        )
        tail = tail_event_rates(list(summary.multiples))
        print(f"    tail-event counts/rates: {tail}")
        rate_2_0 = tail["rates"][2.0]
        required_n = required_n_for_tail_events(rate_2_0)
        print(
            f"    required n for ~30 tail events at the >2.0x rate "
            f"({rate_2_0:.1%}): {required_n}"
        )
    print()

    print("=== Task 3: Extended take_profit_2_multiple sweep (looser/no cap) ===")
    extended_summaries, extended_skipped = run_sweep(trades, base_cfg, grid=EXTENDED_SWEEP_GRID)
    print(
        f"  grid combinations run: {len(extended_summaries)}  "
        f"skipped (tp2<=tp1): {extended_skipped}"
    )
    for objective in OBJECTIVES:
        ranked = sorted(
            extended_summaries, key=lambda s: objective_value(s, objective), reverse=True
        )
        print(f"  -- objective={objective}, top 5 (extended grid) --")
        for i, s in enumerate(ranked[:5], start=1):
            print(
                f"    #{i} {objective}={objective_value(s, objective):.4f} "
                f"tp2={s.params['take_profit_2_multiple']} median={s.median_multiple:.4f} "
                f"mean={s.mean_multiple:.4f} params={s.params}"
            )
    print()

    print("=== Split-half stability check (original grid, median -- kept for comparability) ===")
    stability = split_half_stability(trades, base_cfg, objective="median")
    for k, v in stability.items():
        print(f"  {k}: {v}")
    print()

    print("=== Task 4 (prior task): creator_sold counterfactual ===")
    counterfactual = creator_sold_counterfactual(trades, base_cfg)
    for k, v in counterfactual.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
