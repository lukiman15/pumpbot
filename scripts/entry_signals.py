"""Offline analysis of entry-side signals: velocity confirmation, the
creator-supply threshold, and socials -- against the shadow log's 578
tracked mints. Pure analysis over data/*.jsonl already on disk -- no
network access, no run of the trading loop.

Reuses scripts/backtest_exits.py's curve reconstruction and ladder-replay
machinery rather than duplicating it (ENTRY-SIGNALS-HANDOFF.md Section 2.8).
See that handoff for the full design rationale; only what's needed to read
this file is repeated here.

Two controls matter for the velocity question, and they must never be
conflated (Section 4 Task 2):

- Entering at t=0 with no filter: what the bot does today.
- Entering at delay T with no filter: isolates the effect of *waiting* from
  the effect of *selecting* on a rise.

LOOK-AHEAD TRAP (Section 3.1): a filter that selects on "rose >= X% by t=T"
must have its outcome measured strictly from T onward, never from t=0 -- the
qualifying rise itself must never appear inside the reported outcome.

UNITS TRAP (carried forward from backtest_exits.py): ShadowPrice.price_sol
is lamports per RAW token unit; ExitFilled.curve_price_sol is SOL per WHOLE
token. They differ by exactly 1000x.

SAMPLING TRAP (Section 2.2): the "bought" arm is a census (sample_fraction
1.0); every other arm is a 10% sample (config.yaml's shadow.sample_fraction).
Comparing *distributions* across arms is fine; comparing raw *counts* across
arms is not.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_exits as bx

from pumpbot.config import PROJECT_ROOT, load_settings
from pumpbot.curve import LAMPORTS_PER_SOL, TOKEN_DECIMALS, sol_to_tokens
from pumpbot.ledger import read_events

MIN_SAMPLE_FOR_STATS = bx.MIN_SAMPLE_FOR_STATS  # reused, one evidence bar per project

# Section 4 Task 2's pre-registered grid.
ENTRY_DELAYS = (15.0, 30.0, 45.0, 60.0)
VELOCITY_THRESHOLDS = (None, 0.0, 0.05, 0.10, 0.25, 0.50)  # None = no filter (control)

# Section 4 Task 3's pre-registered threshold list.
CREATOR_SUPPLY_THRESHOLDS = (0.02, 0.04, 0.06, 0.08, 0.10, 0.15, 0.20, None)  # None = no limit

# Section 4 Task 4.
MIN_SOCIALS_THRESHOLDS = (1, 2, 3)


# --- data model --------------------------------------------------------------


@dataclass(frozen=True)
class SocialInfo:
    has_twitter: bool
    has_telegram: bool
    has_website: bool
    count: int


@dataclass(frozen=True)
class TrackedMint:
    mint: str
    arm: str  # "bought" | "rejected" | "skipped"
    reject_reason: str | None
    k: int
    p0: float  # t=0 reserve ratio, from CandidateSeen (Section 2.6) -- NOT return_from_first_seen
    creator_supply_fraction: float
    # (elapsed_seconds_since_candidate, price_sol_raw), ascending, from ShadowPrice rows
    ticks: tuple[tuple[float, float], ...]
    socials: SocialInfo | None  # None if no Tier2Evaluated row for this mint (Section 2.5)


def load_tracked_mints(events: list[dict[str, Any]]) -> tuple[list[TrackedMint], dict[str, int]]:
    """Joins CandidateSeen / ShadowPrice / Tier2Evaluated by mint. Every
    ShadowPrice row already carries its own arm/reject_reason (Section 2.1's
    table), so no cross-reference to CandidateSkipped/CandidateRejected is
    needed."""
    candidate_seen_by_mint: dict[str, dict[str, Any]] = {}
    tier2_by_mint: dict[str, dict[str, Any]] = {}
    shadow_by_mint: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in events:
        event = row.get("event")
        if event == "CandidateSeen":
            candidate_seen_by_mint.setdefault(row["mint"], row)
        elif event == "Tier2Evaluated":
            tier2_by_mint.setdefault(row["mint"], row)
        elif event == "ShadowPrice":
            shadow_by_mint[row["mint"]].append(row)

    diagnostics = {
        "shadow_tracked_mints": len(shadow_by_mint),
        "missing_candidate_seen": 0,
        "usable_mints": 0,
        "with_socials": 0,
    }

    mints: list[TrackedMint] = []
    for mint, rows in shadow_by_mint.items():
        candidate = candidate_seen_by_mint.get(mint)
        if candidate is None:
            diagnostics["missing_candidate_seen"] += 1
            continue

        rows_sorted = sorted(rows, key=lambda r: r["ts_monotonic"])
        arm = rows_sorted[0]["arm"]
        reject_reason = rows_sorted[0]["reject_reason"]

        candidate_ts = candidate["ts_monotonic"]
        vs_lamports = round(candidate["virtual_sol_in_curve"] * LAMPORTS_PER_SOL)
        vt_raw = round(candidate["virtual_tokens_in_curve"] * 10**TOKEN_DECIMALS)
        k = vs_lamports * vt_raw
        p0 = vs_lamports / vt_raw

        ticks = tuple(
            (r["ts_monotonic"] - candidate_ts, r["price_sol"]) for r in rows_sorted
        )

        tier2 = tier2_by_mint.get(mint)
        socials = None
        if tier2 is not None:
            has_twitter = bool(tier2["has_twitter"])
            has_telegram = bool(tier2["has_telegram"])
            has_website = bool(tier2["has_website"])
            socials = SocialInfo(
                has_twitter=has_twitter,
                has_telegram=has_telegram,
                has_website=has_website,
                count=sum([has_twitter, has_telegram, has_website]),
            )
            diagnostics["with_socials"] += 1

        mints.append(
            TrackedMint(
                mint=mint,
                arm=arm,
                reject_reason=reject_reason,
                k=k,
                p0=p0,
                creator_supply_fraction=candidate["creator_supply_fraction"],
                ticks=ticks,
                socials=socials,
            )
        )

    diagnostics["usable_mints"] = len(mints)
    return mints, diagnostics


def tier1_passing(mints: list[TrackedMint]) -> list[TrackedMint]:
    """Section 2.3: bought + skipped[max_concurrent_positions] -- the 505
    mints that cleared tier-1, minus a slot being free or not, a reason
    uncorrelated with the mint's own quality."""
    return [
        m
        for m in mints
        if m.arm == "bought" or (m.arm == "skipped" and m.reject_reason == "max_concurrent_positions")
    ]


# --- entry/outcome primitives ------------------------------------------------


def entry_tick_at_delay(mint: TrackedMint, delay: float) -> tuple[float, float] | None:
    """First tick at or after `delay` seconds since the candidate was seen --
    the earliest data a real velocity rule evaluated at exactly `delay` could
    have acted on. A non-positive price is a completed/migrated curve's
    terminal tick (or a decode edge case), not a usable entry point."""
    for elapsed, price in mint.ticks:
        if elapsed >= delay and price > 0:
            return elapsed, price
    return None


def velocity_return(mint: TrackedMint, delay: float) -> tuple[float, float] | None:
    """(entry_elapsed, return from t=0 to the entry tick). Uses CandidateSeen's
    own p0, never ShadowPrice.return_from_first_seen (Section 2.6)."""
    entry = entry_tick_at_delay(mint, delay)
    if entry is None:
        return None
    entry_elapsed, entry_price = entry
    if mint.p0 <= 0:
        return None
    return entry_elapsed, (entry_price / mint.p0) - 1.0


@dataclass(frozen=True)
class OutcomeFromEntry:
    entry_elapsed: float
    exit_elapsed: float
    holding_seconds: float
    forward_return: float


def outcome_from_entry_tick(mint: TrackedMint, delay: float) -> OutcomeFromEntry | None:
    """Forward return from the entry tick (at `delay`) to the LAST recorded
    tick -- the outcome window end stays fixed at the shadow horizon for
    every arm (Section 2.10), never re-based to a full 300s from the entry
    point. Section 3.1: this is measured strictly from the entry tick
    onward, so a velocity filter's qualifying rise can never leak into it."""
    entry = entry_tick_at_delay(mint, delay)
    if entry is None:
        return None
    entry_elapsed, entry_price = entry
    exit_tick = _last_positive_price_tick(mint)
    if exit_tick is None:
        return None
    exit_elapsed, exit_price = exit_tick
    if exit_elapsed <= entry_elapsed or entry_price <= 0:
        return None  # no forward data left after this entry point
    return OutcomeFromEntry(
        entry_elapsed=entry_elapsed,
        exit_elapsed=exit_elapsed,
        holding_seconds=exit_elapsed - entry_elapsed,
        forward_return=(exit_price / entry_price) - 1.0,
    )


def _last_positive_price_tick(mint: TrackedMint) -> tuple[float, float] | None:
    for elapsed, price in reversed(mint.ticks):
        if price > 0:
            return elapsed, price
    return None


def full_horizon_return(mint: TrackedMint) -> float | None:
    """t=0 -> last tick, i.e. entering immediately with no filter and no
    delay -- what the bot does today (Control 1)."""
    if mint.p0 <= 0:
        return None
    last = _last_positive_price_tick(mint)
    if last is None:
        return None
    _, last_price = last
    return (last_price / mint.p0) - 1.0


# --- stats helpers -------------------------------------------------------


@dataclass(frozen=True)
class CellStats:
    n: int
    frac_of_population: float
    median: float | None
    mean: float | None
    frac_above_zero: float | None


def cell_stats(returns: list[float], population_n: int) -> CellStats:
    n = len(returns)
    if n < MIN_SAMPLE_FOR_STATS:
        return CellStats(
            n=n, frac_of_population=n / population_n if population_n else 0.0,
            median=None, mean=None, frac_above_zero=None,
        )
    return CellStats(
        n=n,
        frac_of_population=n / population_n if population_n else 0.0,
        median=statistics.median(returns),
        mean=statistics.mean(returns),
        frac_above_zero=sum(1 for r in returns if r > 0) / n,
    )


def fmt_cell(label: str, stats: CellStats) -> str:
    if stats.median is None:
        return f"{label}: INSUFFICIENT SAMPLE (n={stats.n}, {stats.frac_of_population:.1%} of flow)"
    return (
        f"{label}: n={stats.n} ({stats.frac_of_population:.1%} of flow) "
        f"median={stats.median:+.1%} mean={stats.mean:+.1%} "
        f"frac_above_zero={stats.frac_above_zero:.1%}"
    )


# --- Task 2: velocity analysis ------------------------------------------------


def run_velocity_grid(population: list[TrackedMint]) -> dict[str, Any]:
    population_n = len(population)

    control_1_returns = [r for m in population if (r := full_horizon_return(m)) is not None]
    control_1 = cell_stats(control_1_returns, population_n)

    grid: dict[float, dict[Any, CellStats]] = {}
    for delay in ENTRY_DELAYS:
        grid[delay] = {}
        for threshold in VELOCITY_THRESHOLDS:
            returns = []
            for m in population:
                vr = velocity_return(m, delay)
                if vr is None:
                    continue
                _, ret_at_delay = vr
                if threshold is not None and ret_at_delay < threshold:
                    continue
                outcome = outcome_from_entry_tick(m, delay)
                if outcome is None:
                    continue
                returns.append(outcome.forward_return)
            grid[delay][threshold] = cell_stats(returns, population_n)

    return {"population_n": population_n, "control_1_enter_at_0_no_filter": control_1, "grid": grid}


# --- Task 3: creator-supply threshold curve -----------------------------------


def run_creator_supply_curve(all_mints: list[TrackedMint]) -> dict[str, Any]:
    population_n = len(all_mints)
    rows = []
    for threshold in CREATOR_SUPPLY_THRESHOLDS:
        if threshold is None:
            admitted = all_mints
            excluded: list[TrackedMint] = []
        else:
            admitted = [m for m in all_mints if m.creator_supply_fraction <= threshold]
            excluded = [m for m in all_mints if m.creator_supply_fraction > threshold]
        admitted_returns = [r for m in admitted if (r := full_horizon_return(m)) is not None]
        excluded_returns = [r for m in excluded if (r := full_horizon_return(m)) is not None]
        rows.append(
            {
                "threshold": threshold,
                "admitted": cell_stats(admitted_returns, population_n),
                "excluded": cell_stats(excluded_returns, population_n),
            }
        )
    return {"population_n": population_n, "rows": rows}


def run_tier1_sanity_check(all_mints: list[TrackedMint]) -> dict[str, Any]:
    """Section 4 Task 3's required sanity check: the analysis must recover
    the already-known result (tier-1 rejects underperform, roughly -11.9%
    median vs -0.4% for tier1-passing flow) using this script's own
    full_horizon_return, before anything else here is trusted."""
    rejected = [m for m in all_mints if m.arm == "rejected"]
    passing = tier1_passing(all_mints)
    rejected_returns = [r for m in rejected if (r := full_horizon_return(m)) is not None]
    passing_returns = [r for m in passing if (r := full_horizon_return(m)) is not None]
    return {
        "rejected": cell_stats(rejected_returns, len(all_mints)),
        "tier1_passing": cell_stats(passing_returns, len(all_mints)),
    }


# --- Task 4: socials, honestly scoped -----------------------------------------


def run_socials_analysis(all_mints: list[TrackedMint]) -> dict[str, Any]:
    scoped = [m for m in all_mints if m.socials is not None]
    population_n = len(scoped)

    by_flag: dict[str, dict[bool, CellStats]] = {}
    for flag_name in ("has_twitter", "has_telegram", "has_website"):
        by_flag[flag_name] = {}
        for value in (True, False):
            group = [m for m in scoped if getattr(m.socials, flag_name) == value]
            returns = [r for m in group if (r := full_horizon_return(m)) is not None]
            by_flag[flag_name][value] = cell_stats(returns, population_n)

    by_count: dict[int, CellStats] = {}
    for count in (0, 1, 2, 3):
        group = [m for m in scoped if m.socials.count == count]
        returns = [r for m in group if (r := full_horizon_return(m)) is not None]
        by_count[count] = cell_stats(returns, population_n)

    by_min_socials: dict[int, dict[str, CellStats]] = {}
    for threshold in MIN_SOCIALS_THRESHOLDS:
        admitted = [m for m in scoped if m.socials.count >= threshold]
        excluded = [m for m in scoped if m.socials.count < threshold]
        admitted_returns = [r for m in admitted if (r := full_horizon_return(m)) is not None]
        excluded_returns = [r for m in excluded if (r := full_horizon_return(m)) is not None]
        by_min_socials[threshold] = {
            "admitted": cell_stats(admitted_returns, population_n),
            "excluded": cell_stats(excluded_returns, population_n),
        }

    return {
        "population_n": population_n,
        "by_flag": by_flag,
        "by_count": by_count,
        "by_min_socials": by_min_socials,
    }


# --- Task 5: combination, stability, secondary ladder replay ------------------


def split_half(population: list[TrackedMint], key) -> tuple[list[TrackedMint], list[TrackedMint]]:
    ordered = sorted(population, key=key)
    mid = len(ordered) // 2
    return ordered[:mid], ordered[mid:]


def evaluate_combo_returns(
    population: list[TrackedMint], delay: float, velocity_threshold: float | None,
    creator_supply_threshold: float | None,
) -> list[float]:
    returns = []
    for m in population:
        if creator_supply_threshold is not None and m.creator_supply_fraction > creator_supply_threshold:
            continue
        vr = velocity_return(m, delay)
        if vr is None:
            continue
        _, ret_at_delay = vr
        if velocity_threshold is not None and ret_at_delay < velocity_threshold:
            continue
        outcome = outcome_from_entry_tick(m, delay)
        if outcome is None:
            continue
        returns.append(outcome.forward_return)
    return returns


def _reconstruct_curve_for_buy(k: int, price_sol_raw: float) -> Any:
    """Like bx.reconstruct_curve, but with real_token_reserves set to
    virtual_token_reserves rather than 0. tokens_to_sol (used everywhere
    else in this analysis) never reads real_token_reserves, but
    sol_to_tokens -- needed here only to size a synthetic entry -- caps its
    output via min(tokens_out, real_token_reserves), and reconstruct_curve
    zeroes that field, which would silently floor every synthetic buy to 0
    tokens. Real reserves are always below virtual reserves in pump.fun's
    design, and a single position_sol-sized buy never approaches either
    bound, so this is a safe upper bound, not a guess at the true value."""
    curve = bx.reconstruct_curve(k, price_sol_raw)
    return bx.BondingCurveState(
        virtual_sol_reserves=curve.virtual_sol_reserves,
        virtual_token_reserves=curve.virtual_token_reserves,
        real_sol_reserves=0,
        real_token_reserves=curve.virtual_token_reserves,
        token_total_supply=0,
        complete=False,
    )


def synthetic_replay_trade(
    mint: TrackedMint, entry_elapsed: float, entry_price_raw: float, position_sol: float
) -> bx.ReplayTrade:
    """Builds a ReplayTrade as if the bot had bought `mint` at the tick
    nearest `entry_elapsed`, using the exact fee-inclusive math main.py uses
    at a real buy (sol_to_tokens then position_sol / tokens_out). No
    creator-sell data exists for a non-bought mint (Section 2.9) --
    creator_sold_at is always None here, and callers must still pass
    suppress_creator_sold=True explicitly for symmetry with the bought arm."""
    curve = _reconstruct_curve_for_buy(mint.k, entry_price_raw)
    position_lamports = round(position_sol * LAMPORTS_PER_SOL)
    tokens_out_raw = sol_to_tokens(curve, position_lamports)
    tokens_out_whole = tokens_out_raw / 10**TOKEN_DECIMALS
    entry_price_sol = position_sol / tokens_out_whole if tokens_out_whole > 0 else 0.0

    ticks_after_entry = tuple(
        bx.ShadowTick(ts_monotonic=elapsed, price_sol_raw=price)
        for elapsed, price in mint.ticks
        if elapsed >= entry_elapsed and price > 0
    )
    return bx.ReplayTrade(
        trade_id=mint.mint,
        mint=mint.mint,
        entry_price_sol=entry_price_sol,
        entry_tokens=tokens_out_whole,
        position_sol=position_sol,
        opened_at=entry_elapsed,
        k=mint.k,
        ticks=ticks_after_entry,
        recorded_reason="n/a",
        recorded_multiple=None,
        creator_sold_at=None,
    )


def run_secondary_ladder_replay(
    bought_trades: list[bx.ReplayTrade], skipped_population: list[TrackedMint], exits_cfg,
    position_sol: float,
) -> dict[str, Any]:
    """Section 3.2's secondary metric: modelled multiple from the real
    ladder, creator_sold suppressed on BOTH populations so the comparison is
    symmetric. A lower bound on what the bought arm actually achieved, since
    58 of 103 of its real exits were creator_sold."""
    # cell_stats/fmt_cell format their input as a RETURN (0.0 = breakeven),
    # not a multiple (1.0 = breakeven) -- convert here so a modelled
    # multiple of 0.98 prints as -2.0%, not the nonsensical +98.0%.
    bought_returns = [
        bx.replay_trade(t, exits_cfg, suppress_creator_sold=True).final_multiple - 1.0
        for t in bought_trades
    ]

    skipped_returns = []
    for m in skipped_population:
        entry = entry_tick_at_delay(m, 0.0)
        if entry is None:
            continue
        entry_elapsed, entry_price = entry
        synthetic = synthetic_replay_trade(m, entry_elapsed, entry_price, position_sol)
        if not synthetic.ticks or synthetic.entry_tokens <= 0:
            continue
        result = bx.replay_trade(synthetic, exits_cfg, suppress_creator_sold=True)
        skipped_returns.append(result.final_multiple - 1.0)

    return {
        "bought": cell_stats(bought_returns, len(bought_trades)),
        "skipped_max_concurrent_positions": cell_stats(
            skipped_returns, len(skipped_population)
        ),
    }


# --- CLI / report ------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline entry-signal analysis (velocity, creator supply, socials)"
    )
    parser.add_argument("--ledger", type=str, default=None)
    args = parser.parse_args()

    settings = load_settings()
    ledger_dir = Path(args.ledger) if args.ledger else (PROJECT_ROOT / settings.config.ledger.path).parent

    events = list(read_events(ledger_dir))
    all_mints, diagnostics = load_tracked_mints(events)
    print("=== Task 1: Assembly (Section 2) ===")
    for k, v in diagnostics.items():
        print(f"  {k}: {v}")
    population = tier1_passing(all_mints)
    print(f"  tier1_passing (bought + skipped[max_concurrent_positions]): {len(population)}")
    for delay in ENTRY_DELAYS:
        with_tick = sum(1 for m in population if entry_tick_at_delay(m, delay) is not None)
        print(f"  usable at delay={delay:.0f}s: {with_tick}/{len(population)}")
    print()

    print("=== Task 3 sanity check (must recover the known tier-1 result) ===")
    sanity = run_tier1_sanity_check(all_mints)
    print(f"  {fmt_cell('rejected', sanity['rejected'])}")
    print(f"  {fmt_cell('tier1_passing', sanity['tier1_passing'])}")
    print()

    print(f"=== Task 2: Velocity grid (population = tier1-passing, n={len(population)}) ===")
    velocity = run_velocity_grid(population)
    print(f"  {fmt_cell('Control 1: enter at t=0, no filter', velocity['control_1_enter_at_0_no_filter'])}")
    for delay in ENTRY_DELAYS:
        print(f"  -- delay={delay:.0f}s --")
        for threshold in VELOCITY_THRESHOLDS:
            label = "Control 2: no filter" if threshold is None else f"filter >= {threshold:+.0%}"
            print(f"    {fmt_cell(label, velocity['grid'][delay][threshold])}")
    print()

    print(f"=== Task 3: Creator-supply threshold curve (population = all tracked, n={len(all_mints)}) ===")
    curve_result = run_creator_supply_curve(all_mints)
    for row in curve_result["rows"]:
        label = "no limit" if row["threshold"] is None else f"<= {row['threshold']:.2f}"
        print(f"  threshold {label}:")
        print(f"    {fmt_cell('admitted', row['admitted'])}")
        print(f"    {fmt_cell('excluded', row['excluded'])}")
    print()

    print(f"=== Task 4: Socials (restricted to n={sum(1 for m in all_mints if m.socials is not None)} with a Tier2Evaluated row) ===")
    socials = run_socials_analysis(all_mints)
    for flag_name, by_value in socials["by_flag"].items():
        print(f"  -- {flag_name} --")
        for value, stats in by_value.items():
            print(f"    {fmt_cell(str(value), stats)}")
    print("  -- socials count --")
    for count, stats in socials["by_count"].items():
        print(f"    {fmt_cell(f'count={count}', stats)}")
    print("  -- min_socials threshold --")
    for threshold, cells in socials["by_min_socials"].items():
        print(f"    threshold >= {threshold}:")
        print(f"      {fmt_cell('admitted', cells['admitted'])}")
        print(f"      {fmt_cell('excluded', cells['excluded'])}")
    print()

    print("=== Task 5: Combination, stability, secondary ladder replay ===")
    # Best velocity rule (per Task 2 output, read by the report author) combined
    # with the current creator-supply threshold, vs either alone.
    best_velocity_delay, best_velocity_threshold = 30.0, 0.0
    creator_threshold = settings.config.filters.tier1.max_creator_supply_fraction
    velocity_alone = evaluate_combo_returns(population, best_velocity_delay, best_velocity_threshold, None)
    supply_alone = evaluate_combo_returns(population, best_velocity_delay, None, creator_threshold)
    combined = evaluate_combo_returns(population, best_velocity_delay, best_velocity_threshold, creator_threshold)
    no_filter = evaluate_combo_returns(population, best_velocity_delay, None, None)
    print(f"  velocity alone (delay={best_velocity_delay:.0f}s, >= {best_velocity_threshold:+.0%}): "
          f"{fmt_cell('', cell_stats(velocity_alone, len(population)))}")
    print(f"  creator-supply alone (<= {creator_threshold:.2f}): "
          f"{fmt_cell('', cell_stats(supply_alone, len(population)))}")
    print(f"  combined: {fmt_cell('', cell_stats(combined, len(population)))}")
    print(f"  no filter (same delay, same n baseline): {fmt_cell('', cell_stats(no_filter, len(population)))}")
    print()

    print("  -- split-half stability, best velocity rule --")
    first_half, second_half = split_half(population, key=lambda m: m.mint)
    for label, half in (("first_half", first_half), ("second_half", second_half)):
        returns = evaluate_combo_returns(half, best_velocity_delay, best_velocity_threshold, None)
        print(f"    {label}: {fmt_cell('', cell_stats(returns, len(half)))}")
    print()

    print("  -- secondary ladder replay, creator_sold suppressed on both populations --")
    exits_cfg = settings.config.exits
    bought_events_trades, _bt_diag = bx.load_trades(events)
    skipped_population = [
        m for m in all_mints if m.arm == "skipped" and m.reject_reason == "max_concurrent_positions"
    ]
    secondary = run_secondary_ladder_replay(
        bought_events_trades, skipped_population, exits_cfg, settings.config.trading.position_sol
    )
    print(f"    {fmt_cell('bought (real trades, creator_sold suppressed)', secondary['bought'])}")
    print(f"    {fmt_cell('skipped[max_concurrent_positions] (synthetic entries)', secondary['skipped_max_concurrent_positions'])}")


if __name__ == "__main__":
    main()
