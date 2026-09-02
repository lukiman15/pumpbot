"""Offline report over the trade ledger (ledger.py).

    python scripts/report.py [--ledger data/ledger.jsonl]

Reads every rotated ledger file, filters to dry_run == false for anything
PnL-related, and prints: trade counts by status, win rate / PnL (both
including and excluding rent), fee drag, entry-latency distribution,
exit-reason breakdown, the funnel (one CandidateSeen denominator, never a
sum of two event types), and adverse-selection comparisons across the
shadow log's bought/rejected/skipped arms.

Structural prevention, not convention: load_closed_real_trades() is the
only way real PnL data enters this script. It filters to
event == "TradeClosed" and not dry_run, and asserts every PnL field is
non-null (a null there is a settlement bug, not a statistic to skip).
ClosedRealTrade's PnL fields are non-Optional and it carries no dry_run
flag, so there is no code path by which a dry-run row or a modeled figure
can reach an average -- every statistics function below takes
list[ClosedRealTrade] and nothing else.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pumpbot.config import PROJECT_ROOT, load_settings
from pumpbot.ledger import read_events
from pumpbot.submit import BASE_FEE_LAMPORTS_PER_SIGNATURE

MIN_SAMPLE_FOR_STATS = 30

# Reject reasons tier1 emits pre-queue vs the reasons run_trader emits
# post-queue -- kept as separate funnel stages, per the plan, rather than
# merged into one bucket.
TIER1_REJECT_REASONS = frozenset(
    {
        "creator_blocked",
        "name_or_symbol_blocked",
        "creator_supply_too_high",
        "curve_too_complete",
        "mint_rate_too_high",
        "mayhem_mode_unauthorized",
    }
)


@dataclass(frozen=True)
class ClosedRealTrade:
    """The ONLY shape real PnL statistics may be computed from -- see
    load_closed_real_trades()'s docstring. No dry_run flag, no Optional
    PnL field: there is nothing to check before using one, by construction."""

    trade_id: str
    mint: str
    realized_pnl_lamports: int
    rent_recovered_lamports: int
    total_fee_lamports: int
    gross_turnover_lamports: int
    hold_seconds: float
    leg_count: int
    exit_reasons: list[str]
    settlement_complete: bool


def load_closed_real_trades(events: Iterable[dict[str, Any]]) -> list[ClosedRealTrade]:
    trades = []
    for row in events:
        if row.get("event") != "TradeClosed" or row.get("dry_run"):
            continue
        required = (
            "realized_pnl_lamports", "rent_recovered_lamports",
            "total_fee_lamports", "gross_turnover_lamports",
        )
        for field_name in required:
            if row.get(field_name) is None:
                raise ValueError(
                    f"TradeClosed trade_id={row.get('trade_id')} has null "
                    f"{field_name} but dry_run=False -- this is a settlement "
                    "bug, not a statistic to silently skip"
                )
        trades.append(
            ClosedRealTrade(
                trade_id=row["trade_id"],
                mint=row["mint"],
                realized_pnl_lamports=row["realized_pnl_lamports"],
                rent_recovered_lamports=row["rent_recovered_lamports"],
                total_fee_lamports=row["total_fee_lamports"],
                gross_turnover_lamports=row["gross_turnover_lamports"],
                hold_seconds=row["hold_seconds"],
                leg_count=row["leg_count"],
                exit_reasons=row.get("exit_reasons", []),
                settlement_complete=row["settlement_complete"],
            )
        )
    return trades


# --- statistics functions: list[ClosedRealTrade] in, nothing else --------


def win_rate(trades: list[ClosedRealTrade]) -> tuple[float | None, int]:
    n = len(trades)
    if n < MIN_SAMPLE_FOR_STATS:
        return None, n
    wins = sum(1 for t in trades if t.realized_pnl_lamports > 0)
    return wins / n, n


def pnl_stats(
    trades: list[ClosedRealTrade], include_rent: bool
) -> tuple[float | None, float | None, int]:
    n = len(trades)
    if n < MIN_SAMPLE_FOR_STATS:
        return None, None, n
    values = [
        t.realized_pnl_lamports if include_rent else t.realized_pnl_lamports - t.rent_recovered_lamports
        for t in trades
    ]
    return statistics.mean(values), statistics.median(values), n


def fee_drag(trades: list[ClosedRealTrade]) -> tuple[float | None, int]:
    n = len(trades)
    if n == 0:
        return None, 0
    total_fees = sum(t.total_fee_lamports for t in trades)
    total_turnover = sum(t.gross_turnover_lamports for t in trades)
    if total_turnover == 0:
        return None, n
    return total_fees / total_turnover, n


def exit_reason_breakdown(trades: list[ClosedRealTrade]) -> dict[str, tuple[int, float]]:
    """Groups by each trade's LAST exit reason (how it ultimately closed --
    a trade can carry more than one, e.g. a partial take_profit_1 followed
    by a timeout on the remainder). Returns {reason: (count, mean_pnl)}."""
    by_reason: dict[str, list[int]] = defaultdict(list)
    for t in trades:
        reason = t.exit_reasons[-1] if t.exit_reasons else "unknown"
        by_reason[reason].append(t.realized_pnl_lamports)
    return {
        reason: (len(pnls), statistics.mean(pnls))
        for reason, pnls in by_reason.items()
    }


def latency_percentiles(latencies: list[float]) -> tuple[float, float, float] | None:
    if not latencies:
        return None
    ordered = sorted(latencies)

    def pct(p: float) -> float:
        idx = min(len(ordered) - 1, round(p * (len(ordered) - 1)))
        return ordered[idx]

    return pct(0.50), pct(0.90), ordered[-1]


# --- funnel: one CandidateSeen denominator --------------------------------


def compute_funnel(events: list[dict[str, Any]]) -> dict[str, Any]:
    seen = 0
    rejected_by_reason: Counter[str] = Counter()
    skipped_by_reason: Counter[str] = Counter()
    filled = 0

    for row in events:
        event = row.get("event")
        if event == "CandidateSeen":
            seen += 1
        elif event == "CandidateRejected":
            rejected_by_reason[row.get("reason", "unknown")] += 1
        elif event == "CandidateSkipped":
            skipped_by_reason[row.get("reason", "unknown")] += 1
        elif event == "EntryFilled":
            filled += 1

    return {
        "seen": seen,
        "rejected_by_reason": rejected_by_reason,
        "skipped_by_reason": skipped_by_reason,
        "filled": filled,
    }


def trade_status_counts(events: list[dict[str, Any]]) -> Counter[str]:
    """CLOSED if a TradeClosed exists, ORPHANED if a TradeOrphaned exists,
    else OPEN (entry recorded, neither yet). See the plan's Accounting
    Rules for the full status definitions -- this mirrors find_orphans'
    classification for reporting rather than re-emitting events."""
    has_entry: set[str] = set()
    closed: set[str] = set()
    orphaned: set[str] = set()
    for row in events:
        trade_id = row.get("trade_id")
        if not trade_id:
            continue
        event = row.get("event")
        if event == "EntryFilled":
            has_entry.add(trade_id)
        elif event == "TradeClosed":
            closed.add(trade_id)
        elif event == "TradeOrphaned":
            orphaned.add(trade_id)

    counts: Counter[str] = Counter()
    for trade_id in has_entry:
        if trade_id in closed:
            counts["CLOSED"] += 1
        elif trade_id in orphaned:
            counts["ORPHANED"] += 1
        else:
            counts["OPEN"] += 1
    return counts


# --- adverse selection: shadow log by arm ---------------------------------


def _latest_shadow_price_by_mint(events: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The last ShadowPrice per mint (highest horizon_elapsed_seconds) is
    that mint's at-horizon (or at-graduation) forward return. A mint is
    tracked under exactly one arm for its whole life (ShadowTracker.track
    is idempotent once a mint is admitted), so keying by mint alone is
    safe -- (arm, mint) would be equivalent."""
    latest: dict[str, dict[str, Any]] = {}
    for row in events:
        if row.get("event") != "ShadowPrice":
            continue
        mint = row["mint"]
        existing = latest.get(mint)
        if existing is None or row["horizon_elapsed_seconds"] > existing["horizon_elapsed_seconds"]:
            latest[mint] = row
    return latest


def adverse_selection_by_arm(
    events: Iterable[dict[str, Any]],
) -> dict[tuple[str, str | None], list[float]]:
    """Groups the final ShadowPrice per mint by (arm, reject_reason) --
    NOT arm alone. The `skipped` arm mixes four unrelated reasons
    (entries_halted, max_concurrent_positions, tier2_rejected,
    sim_would_fail_race/structural) that answer different questions, and
    lumping them together produced a misread evidence pack
    (PHASE-1-RERUN-HANDOFF.md Section 2.4): bought vs all-of-rejected looks
    like a tier-2 result, but the two arms differ by tier-1, tier-2, AND
    simulation all at once. See tier1_effect/tier2_marginal_effect below
    for the comparisons that actually isolate one question each."""
    by_arm_reason: dict[tuple[str, str | None], list[float]] = defaultdict(list)
    for row in _latest_shadow_price_by_mint(events).values():
        ret = row.get("return_from_first_seen")
        if ret is not None:
            by_arm_reason[(row["arm"], row.get("reject_reason"))].append(ret)
    return dict(by_arm_reason)


# The tier-2 gate hasn't judged a candidate that was skipped for one of
# these two reasons -- it never reached tier-2 at all. Deliberately an
# allowlist: `tier2_rejected` is the thing being measured (excluding it),
# and `sim_would_fail_race`/`sim_would_fail_structural` are plausibly
# biased toward CONTESTED tokens (something else wanted them badly enough
# to move the price first), so folding them into the control would bias
# it. PHASE-1-RERUN-HANDOFF.md Section 3, Task 3.
TIER2_UNJUDGED_SKIP_REASONS = frozenset({"entries_halted", "max_concurrent_positions"})


def tier1_effect(
    by_arm_reason: dict[tuple[str, str | None], list[float]],
) -> dict[str, list[float]]:
    """Control: tier-1-passing flow (bought + skipped, any reason -- both
    arms passed tier-1 by construction). Answers whether tier-1's own
    reject reasons (creator_supply_too_high, mint_rate_too_high --
    mayhem_mode_unauthorized is excluded upstream, see shadow.py) are
    filtering out mints that would have done worse anyway."""
    rejected = [r for (arm, _reason), rets in by_arm_reason.items() if arm == "rejected" for r in rets]
    tier1_passing = [
        r for (arm, _reason), rets in by_arm_reason.items() if arm in ("bought", "skipped") for r in rets
    ]
    return {"rejected": rejected, "tier1_passing_control": tier1_passing}


def tier2_marginal_effect(
    by_arm_reason: dict[tuple[str, str | None], list[float]],
) -> dict[str, list[float]]:
    """Control: TIER2_UNJUDGED_SKIP_REASONS only -- flow that passed tier-1
    but never reached a tier-2 verdict, not the general skipped bucket.
    Answers whether the tier-2 social gate adds anything ON TOP of tier-1,
    which `rejected` (tier-1-failing) cannot isolate."""
    bought = [r for (arm, _reason), rets in by_arm_reason.items() if arm == "bought" for r in rets]
    control = [
        r
        for (arm, reason), rets in by_arm_reason.items()
        if arm == "skipped" and reason in TIER2_UNJUDGED_SKIP_REASONS
        for r in rets
    ]
    return {"bought": bought, "tier2_unjudged_control": control}


def shadow_saturation_estimate(
    events: Iterable[dict[str, Any]], max_tracked: int
) -> dict[str, Any]:
    """Estimates peak CONCURRENT shadow-tracked mints from ShadowPrice
    rows' (ts_wall, horizon_elapsed_seconds) pairs. ShadowTracker itself
    (shadow.py) keeps no record of track() calls silently dropped for
    len(_tracked) >= max_tracked (PHASE-1-RERUN-HANDOFF.md Section 2.5),
    so this reconstructs each mint's active interval from the ledger
    alone -- [first_ts_wall - its own horizon offset, last_ts_wall] -- and
    sweeps for the maximum overlap. Once that estimated peak reaches
    max_tracked, further admissions in that stretch were dropped and the
    sample stops being random (first-come instead)."""
    per_mint_rows: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in events:
        if row.get("event") != "ShadowPrice":
            continue
        mint = row.get("mint")
        ts_wall = row.get("ts_wall")
        horizon = row.get("horizon_elapsed_seconds")
        if mint is None or ts_wall is None or horizon is None:
            continue
        per_mint_rows[mint].append((ts_wall, horizon))

    if not per_mint_rows:
        return {"n_mints": 0, "peak_concurrent": 0, "saturated": False}

    boundaries: list[tuple[float, int]] = []
    for rows in per_mint_rows.values():
        start = min(ts - h for ts, h in rows)
        end = max(ts for ts, _h in rows)
        boundaries.append((start, 1))
        boundaries.append((end, -1))
    boundaries.sort()

    concurrent = 0
    peak = 0
    for _ts, delta in boundaries:
        concurrent += delta
        peak = max(peak, concurrent)

    return {
        "n_mints": len(per_mint_rows),
        "peak_concurrent": peak,
        "saturated": peak >= max_tracked,
    }


def tier2_outcome_report(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Fill rate and median forward return split by tier-2 outcome -- the
    local test of the 8.94x/17.4x graduation-rate claim Milestone 1 cites."""
    outcomes: dict[str, dict[str, int]] = defaultdict(lambda: {"evaluated": 0, "filled": 0})
    mint_to_outcome: dict[str, str] = {}
    for row in events:
        if row.get("event") == "Tier2Evaluated":
            outcomes[row["outcome"]]["evaluated"] += 1
            mint_to_outcome[row["mint"]] = row["outcome"]
        elif row.get("event") == "EntryFilled":
            outcome = mint_to_outcome.get(row["mint"])
            if outcome is not None:
                outcomes[outcome]["filled"] += 1

    shadow_by_mint_return: dict[str, float] = {}
    latest_horizon: dict[str, float] = {}
    for row in events:
        if row.get("event") != "ShadowPrice":
            continue
        mint = row["mint"]
        ret = row.get("return_from_first_seen")
        if ret is None:
            continue
        if row["horizon_elapsed_seconds"] >= latest_horizon.get(mint, -1.0):
            latest_horizon[mint] = row["horizon_elapsed_seconds"]
            shadow_by_mint_return[mint] = ret

    returns_by_outcome: dict[str, list[float]] = defaultdict(list)
    for mint, outcome in mint_to_outcome.items():
        if mint in shadow_by_mint_return:
            returns_by_outcome[outcome].append(shadow_by_mint_return[mint])

    report: dict[str, dict[str, Any]] = {}
    for outcome, counts in outcomes.items():
        n = counts["evaluated"]
        fill_rate = counts["filled"] / n if n else None
        returns = returns_by_outcome.get(outcome, [])
        median_return = statistics.median(returns) if returns else None
        report[outcome] = {
            "n": n,
            "filled": counts["filled"],
            "fill_rate": fill_rate,
            "median_forward_return": median_return,
            "return_n": len(returns),
        }
    return report


def fee_composition(trades: list[ClosedRealTrade]) -> dict[str, Any]:
    """Splits total_fee_lamports into a modeled base-fee component
    (BASE_FEE_LAMPORTS_PER_SIGNATURE * leg_count, mirroring submit.py's
    estimate_entry_fee_lamports) and a priority-fee residual -- the ledger
    only records the real total paid, not the two components separately,
    so this is a model over that figure, not an independently-recorded
    one. Reports rent recovered alongside it, and fee drag both including
    and excluding that recovery."""
    n = len(trades)
    if n == 0:
        return {"n": 0}
    base_fee_total = sum(t.leg_count * BASE_FEE_LAMPORTS_PER_SIGNATURE for t in trades)
    total_fee_total = sum(t.total_fee_lamports for t in trades)
    priority_fee_total = max(0, total_fee_total - base_fee_total)
    rent_recovered_total = sum(t.rent_recovered_lamports for t in trades)
    turnover_total = sum(t.gross_turnover_lamports for t in trades)
    drag_excluding_rent = total_fee_total / turnover_total if turnover_total else None
    drag_including_rent = (
        (total_fee_total - rent_recovered_total) / turnover_total if turnover_total else None
    )
    return {
        "n": n,
        "base_fee_total": base_fee_total,
        "priority_fee_total": priority_fee_total,
        "rent_recovered_total": rent_recovered_total,
        "drag_excluding_rent": drag_excluding_rent,
        "drag_including_rent": drag_including_rent,
    }


def trailing_exit_stats(real_events: list[dict[str, Any]]) -> dict[str, Any]:
    """Mean peak multiple vs mean realized multiple for trailing-stop exits
    -- the gap between them is the giveback, and it's the number that says
    whether exits.trailing_drawdown_fraction is set sanely. Pulled from
    ExitFilled rows directly (not ClosedRealTrade/TradeClosed) since
    peak_multiple/realizable_multiple are per-leg fields, not per-trade
    ones -- see ledger.py's ExitFilled docstring."""
    rows = [
        e for e in real_events
        if e.get("event") == "ExitFilled" and e.get("exit_reason") == "trailing_stop"
    ]
    n = len(rows)
    if n == 0:
        return {"n": 0}
    return {
        "n": n,
        "mean_peak_multiple": statistics.mean(r["peak_multiple"] for r in rows),
        "mean_realized_multiple": statistics.mean(r["realizable_multiple"] for r in rows),
    }


def filter_since(events: Iterable[dict[str, Any]], since: str | None) -> list[dict[str, Any]]:
    """Filters to events at/after 00:00 UTC on `since` (YYYY-MM-DD). None
    passes everything through unfiltered. The ledger rotates daily and
    read_events reads an entire directory, so without this a report over a
    later window stays permanently polluted by an earlier window's rows
    (MEASUREMENT-RUN-HANDOFF.md Section 4, Phase 0 item 4)."""
    events = list(events)
    if since is None:
        return events
    cutoff_ts = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
    return [e for e in events if e.get("ts_wall", 0) >= cutoff_ts]


def dry_run_exit_reason_counts(events: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Exit-reason COUNTS from dry-run TradeClosed rows only -- deliberately
    separate from exit_reason_breakdown/ClosedRealTrade (MEASUREMENT-RUN-
    HANDOFF.md Section 3.1: dry-run and real statistics never mix). No PnL
    here: a dry-run PnL was never realized, so there is nothing to average."""
    counts: Counter[str] = Counter()
    for row in events:
        if row.get("event") != "TradeClosed" or not row.get("dry_run"):
            continue
        exit_reasons = row.get("exit_reasons") or []
        reason = exit_reasons[-1] if exit_reasons else "unknown"
        counts[reason] += 1
    return dict(counts)


def dry_run_observation(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """What a dry run CAN legitimately show: it exercises the same
    websocket -> filter -> simulate path as a real run, minus only the
    send, so entry count/latency and simulated exit shape are real
    observations about that path -- just not about realized PnL, slippage,
    fill rate, or fee drag, none of which exist without a real send."""
    events = list(events)
    entry_count = sum(1 for e in events if e.get("event") == "EntryFilled" and e.get("dry_run"))
    closed_count = sum(1 for e in events if e.get("event") == "TradeClosed" and e.get("dry_run"))
    latencies = [
        e["latency_seconds"] for e in events if e.get("event") == "EntryFilled" and e.get("dry_run")
    ]
    return {
        "entry_count": entry_count,
        "closed_count": closed_count,
        "latency_pcts": latency_percentiles(latencies),
        "latency_n": len(latencies),
        "exit_reason_counts": dry_run_exit_reason_counts(events),
    }


# --- printing --------------------------------------------------------------


def _fmt_lamports(n: float | None) -> str:
    if n is None:
        return "n/a"
    return f"{n / 1_000_000_000:.6f} SOL ({n:.0f} lamports)"


def _print_arm_comparison(label: str, returns: list[float]) -> None:
    """Reuses MIN_SAMPLE_FOR_STATS as the same insufficient-sample floor
    used everywhere else in this script -- two different evidence bars in
    one project invites arguing about which applies (see
    DEFERRED-exit-threshold-retune.md's identical reasoning for reusing
    min_closed_trades_before_sizeup)."""
    n = len(returns)
    if n < MIN_SAMPLE_FOR_STATS:
        print(f"    {label}: INSUFFICIENT SAMPLE (n={n})")
    else:
        print(f"    {label}: median={statistics.median(returns):+.1%} (n={n})")


def print_report(all_events: list[dict[str, Any]]) -> None:
    real_events = [e for e in all_events if not e.get("dry_run")]
    closed_trades = load_closed_real_trades(all_events)
    dry_run_count = sum(1 for e in all_events if e.get("dry_run"))
    real_count = len(all_events) - dry_run_count

    if dry_run_count:
        print(
            f"NOTE: this ledger contains {dry_run_count} dry-run event(s) and "
            f"{real_count} real event(s). Sections differ in which population "
            "they count -- see each heading. A dry-run event never reaches a "
            "real-trade statistic (MEASUREMENT-RUN-HANDOFF.md Section 3.1)."
        )
        print()

    print("=== Trade status (ALL events, incl. dry-run) ===")
    statuses = trade_status_counts(all_events)
    total = sum(statuses.values())
    orphan_rate = statuses["ORPHANED"] / total if total else 0.0
    for status in ("CLOSED", "OPEN", "ORPHANED"):
        print(f"  {status}: {statuses.get(status, 0)}")
    print(f"  orphan_rate: {orphan_rate:.1%} (n={total})")

    if dry_run_count:
        print()
        print("=== Dry-run observation (SIMULATED fills only -- no order was ever sent) ===")
        obs = dry_run_observation(all_events)
        print(f"  dry-run entries: {obs['entry_count']}  dry-run closed: {obs['closed_count']}")
        if obs["latency_pcts"] is None:
            print(f"  entry latency: n/a (n={obs['latency_n']})")
        else:
            p50, p90, p_max = obs["latency_pcts"]
            print(
                f"  entry latency: p50={p50:.3f}s p90={p90:.3f}s max={p_max:.3f}s "
                f"(n={obs['latency_n']})"
            )
        if not obs["exit_reason_counts"]:
            print("  exit-reason breakdown (SIMULATED): n/a (n=0)")
        else:
            for reason, count in sorted(obs["exit_reason_counts"].items(), key=lambda kv: -kv[1]):
                print(f"  exit-reason (SIMULATED) {reason}: n={count}")

    print()
    print("=== Win rate & PnL (CLOSED real trades only) ===")
    rate, n = win_rate(closed_trades)
    if rate is None:
        print(f"  win_rate: INSUFFICIENT SAMPLE (n={n})")
    else:
        print(f"  win_rate: {rate:.1%} (n={n})")

    for label, include_rent in (("including rent", True), ("excluding rent", False)):
        mean, median, n = pnl_stats(closed_trades, include_rent)
        if mean is None:
            print(f"  realized_pnl ({label}): INSUFFICIENT SAMPLE (n={n})")
        else:
            print(
                f"  realized_pnl ({label}): mean={_fmt_lamports(mean)} "
                f"median={_fmt_lamports(median)} (n={n})"
            )

    print()
    print("=== Fee drag (CLOSED real trades only) ===")
    drag, n = fee_drag(closed_trades)
    if drag is None:
        print(f"  fee_drag: n/a (n={n})")
    else:
        print(f"  fee_drag: {drag:.2%} of gross turnover (n={n} trades)")

    print()
    print("=== Entry latency (REAL fills only) ===")
    latencies = [
        e["latency_seconds"] for e in real_events if e.get("event") == "EntryFilled"
    ]
    pcts = latency_percentiles(latencies)
    if pcts is None:
        print(f"  n/a (n={len(latencies)})")
    else:
        p50, p90, p_max = pcts
        print(f"  p50={p50:.3f}s p90={p90:.3f}s max={p_max:.3f}s (n={len(latencies)})")

    print()
    print("=== Exit-reason breakdown (CLOSED real trades) ===")
    breakdown = exit_reason_breakdown(closed_trades)
    if not breakdown:
        print("  n/a (n=0)")
    else:
        for reason, (count, mean_pnl) in sorted(breakdown.items(), key=lambda kv: -kv[1][0]):
            print(f"  {reason}: n={count} mean_pnl={_fmt_lamports(mean_pnl)}")

    print()
    print("=== Funnel (ALL events, incl. dry-run; one CandidateSeen denominator) ===")
    funnel = compute_funnel(all_events)
    seen = funnel["seen"]
    print(f"  seen: {seen}")
    if seen == 0:
        print("  n/a -- no CandidateSeen rows in this ledger")
    else:
        for reason, count in funnel["rejected_by_reason"].most_common():
            print(f"    rejected[tier1] {reason}: {count} ({count / seen:.1%})")
        for reason, count in funnel["skipped_by_reason"].most_common():
            print(f"    skipped {reason}: {count} ({count / seen:.1%})")
        print(f"    filled: {funnel['filled']} ({funnel['filled'] / seen:.1%})")

    print()
    print("=== Tier-2 outcome: fill rate & forward return (ALL events, incl. dry-run) ===")
    tier2_report = tier2_outcome_report(all_events)
    if not tier2_report:
        print("  n/a -- no Tier2Evaluated rows in this ledger")
    else:
        for outcome, stats in sorted(tier2_report.items(), key=lambda kv: -kv[1]["n"]):
            fill_rate = stats["fill_rate"]
            fill_rate_str = f"{fill_rate:.1%}" if fill_rate is not None else "n/a"
            median_return = stats["median_forward_return"]
            median_str = f"{median_return:+.1%}" if median_return is not None else "n/a"
            print(
                f"  {outcome}: n={stats['n']} fill_rate={fill_rate_str} "
                f"median_forward_return={median_str} (return_n={stats['return_n']})"
            )

    print()
    print("=== Fee composition (CLOSED real trades) ===")
    composition = fee_composition(closed_trades)
    if composition["n"] == 0:
        print("  n/a (n=0)")
    else:
        n = composition["n"]
        print(f"  base_fee (modeled): {_fmt_lamports(composition['base_fee_total'])} (n={n})")
        print(f"  priority_fee (modeled residual): {_fmt_lamports(composition['priority_fee_total'])} (n={n})")
        print(f"  rent_recovered: {_fmt_lamports(composition['rent_recovered_total'])} (n={n})")
        drag_ex = composition["drag_excluding_rent"]
        drag_in = composition["drag_including_rent"]
        drag_ex_str = f"{drag_ex:.2%}" if drag_ex is not None else "n/a"
        drag_in_str = f"{drag_in:.2%}" if drag_in is not None else "n/a"
        print(f"  fee_drag excluding rent recovery: {drag_ex_str} (n={n})")
        print(f"  fee_drag including rent recovery: {drag_in_str} (n={n})")

    print()
    print("=== Sizing readiness (CLOSED real trades only) ===")
    settings = load_settings()
    position_sol = settings.config.trading.position_sol
    baseline_position_sol = settings.config.trading.baseline_position_sol
    threshold = settings.config.trading.min_closed_trades_before_sizeup
    real_closed = len(closed_trades)
    print(f"  real closed trades: {real_closed} (threshold: {threshold})")
    if position_sol <= baseline_position_sol:
        print(
            f"  position_sol={position_sol:.4f} is at or below "
            f"baseline_position_sol={baseline_position_sol:.4f} -- sizing "
            "discipline not in play"
        )
    elif real_closed >= threshold:
        print(
            f"  position_sol={position_sol:.4f} exceeds baseline -- "
            f"DATA-SUPPORTED ({real_closed}>={threshold} real closed trades)"
        )
    else:
        print(
            f"  position_sol={position_sol:.4f} exceeds baseline -- "
            f"NOT DATA-SUPPORTED ({real_closed}<{threshold} real closed trades, "
            "expectancy at this size is unmeasured)"
        )

    print()
    print("=== Exit mechanism (CLOSED real trades only) ===")
    exit_breakdown = exit_reason_breakdown(closed_trades)
    trailing_stats = trailing_exit_stats(real_events)
    if trailing_stats["n"] == 0:
        print("  trailing_stop giveback: n/a (n=0)")
    else:
        peak = trailing_stats["mean_peak_multiple"]
        realized = trailing_stats["mean_realized_multiple"]
        print(
            f"  trailing_stop: mean_peak_multiple={peak:.3f}x "
            f"mean_realized_multiple={realized:.3f}x giveback={peak - realized:.3f}x "
            f"(n={trailing_stats['n']})"
        )

    creator_sold_entry = exit_breakdown.get("creator_sold")
    if creator_sold_entry is None:
        print("  creator_sold: n/a (n=0)")
    else:
        cs_count, cs_mean_pnl = creator_sold_entry
        all_mean_pnl = (
            statistics.mean(t.realized_pnl_lamports for t in closed_trades) if closed_trades else None
        )
        print(
            f"  creator_sold: n={cs_count} mean_pnl={_fmt_lamports(cs_mean_pnl)} "
            f"vs all-trades mean_pnl={_fmt_lamports(all_mean_pnl)}"
        )

    print()
    print("=== Adverse selection (ALL events, incl. dry-run) ===")
    by_arm_reason = adverse_selection_by_arm(all_events)
    if not by_arm_reason:
        print("  n/a -- no ShadowPrice rows in this ledger (shadow.enabled: false?)")
    else:
        print("  -- full breakdown by (arm, reject_reason) --")
        for (arm, reason), returns in sorted(
            by_arm_reason.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")
        ):
            label = f"{arm}[{reason}]" if reason is not None else arm
            print(f"  {label}: median={statistics.median(returns):+.1%} (n={len(returns)})")

        print()
        print(
            "  -- TIER-1 effect: control = tier1_passing (bought + skipped, any "
            "reason -- both passed tier-1) --"
        )
        t1 = tier1_effect(by_arm_reason)
        _print_arm_comparison("rejected", t1["rejected"])
        _print_arm_comparison("tier1_passing_control", t1["tier1_passing_control"])

        print()
        print(
            "  -- TIER-2 marginal effect: control = tier2_unjudged (skipped: "
            "entries_halted/max_concurrent_positions only -- excludes "
            "tier2_rejected and sim_would_fail_*) --"
        )
        t2 = tier2_marginal_effect(by_arm_reason)
        _print_arm_comparison("bought", t2["bought"])
        _print_arm_comparison("tier2_unjudged_control", t2["tier2_unjudged_control"])

    print()
    saturation = shadow_saturation_estimate(all_events, settings.config.shadow.max_tracked)
    if saturation["n_mints"] == 0:
        print("  shadow saturation: n/a (no ShadowPrice rows)")
    else:
        verdict = (
            "LIKELY -- sample is first-come, not random"
            if saturation["saturated"]
            else "not reached -- sample should be representative"
        )
        print(
            f"  shadow saturation: peak_concurrent~={saturation['peak_concurrent']} vs "
            f"max_tracked={settings.config.shadow.max_tracked} "
            f"(n_mints_tracked={saturation['n_mints']}) -- {verdict}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline report over pumpbot's trade ledger")
    parser.add_argument(
        "--ledger", type=str, default=None,
        help="Path to the ledger base file or directory (default: config.yaml's ledger.path)",
    )
    parser.add_argument(
        "--since", type=str, default=None,
        help="Only include events at/after 00:00 UTC on this date (YYYY-MM-DD). "
        "The ledger rotates daily and this script reads the whole directory, so "
        "use this to isolate one window (e.g. a live run) from an earlier one "
        "(e.g. a dry-run measurement window).",
    )
    args = parser.parse_args()

    if args.ledger is not None:
        ledger_path = Path(args.ledger)
    else:
        settings = load_settings()
        ledger_path = PROJECT_ROOT / settings.config.ledger.path

    directory = ledger_path if ledger_path.is_dir() else ledger_path.parent
    events = filter_since(read_events(directory), args.since)
    if not events:
        since_note = f" since {args.since}" if args.since else ""
        print(f"No ledger events found under {directory}{since_note}")
        return

    print_report(events)


if __name__ == "__main__":
    main()
