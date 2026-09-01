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


def adverse_selection_by_arm(events: list[dict[str, Any]]) -> dict[str, list[float]]:
    """The last ShadowPrice per mint (highest horizon_elapsed_seconds) is
    that mint's at-horizon (or at-graduation) forward return."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in events:
        if row.get("event") != "ShadowPrice":
            continue
        key = (row["arm"], row["mint"])
        existing = latest.get(key)
        if existing is None or row["horizon_elapsed_seconds"] > existing["horizon_elapsed_seconds"]:
            latest[key] = row

    by_arm: dict[str, list[float]] = defaultdict(list)
    for (arm, _mint), row in latest.items():
        ret = row.get("return_from_first_seen")
        if ret is not None:
            by_arm[arm].append(ret)
    return by_arm


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


# --- printing --------------------------------------------------------------


def _fmt_lamports(n: float | None) -> str:
    if n is None:
        return "n/a"
    return f"{n / 1_000_000_000:.6f} SOL ({n:.0f} lamports)"


def print_report(all_events: list[dict[str, Any]]) -> None:
    real_events = [e for e in all_events if not e.get("dry_run")]
    closed_trades = load_closed_real_trades(all_events)

    print("=== Trade status ===")
    statuses = trade_status_counts(all_events)
    total = sum(statuses.values())
    orphan_rate = statuses["ORPHANED"] / total if total else 0.0
    for status in ("CLOSED", "OPEN", "ORPHANED"):
        print(f"  {status}: {statuses.get(status, 0)}")
    print(f"  orphan_rate: {orphan_rate:.1%} (n={total})")

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
    print("=== Fee drag ===")
    drag, n = fee_drag(closed_trades)
    if drag is None:
        print(f"  fee_drag: n/a (n={n})")
    else:
        print(f"  fee_drag: {drag:.2%} of gross turnover (n={n} trades)")

    print()
    print("=== Entry latency (real fills) ===")
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
    print("=== Funnel (one CandidateSeen denominator) ===")
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
    print("=== Tier-2 outcome: fill rate & forward return ===")
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
    print("=== Sizing readiness ===")
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
    print("=== Adverse selection: median forward return by arm ===")
    by_arm = adverse_selection_by_arm(all_events)
    if not by_arm:
        print("  n/a -- no ShadowPrice rows in this ledger (shadow.enabled: false?)")
    else:
        for arm in ("bought", "rejected", "skipped"):
            returns = by_arm.get(arm, [])
            if not returns:
                print(f"  {arm}: n/a (n=0)")
            else:
                print(f"  {arm}: median={statistics.median(returns):+.1%} (n={len(returns)})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline report over pumpbot's trade ledger")
    parser.add_argument(
        "--ledger", type=str, default=None,
        help="Path to the ledger base file or directory (default: config.yaml's ledger.path)",
    )
    args = parser.parse_args()

    if args.ledger is not None:
        ledger_path = Path(args.ledger)
    else:
        settings = load_settings()
        ledger_path = PROJECT_ROOT / settings.config.ledger.path

    directory = ledger_path if ledger_path.is_dir() else ledger_path.parent
    events = list(read_events(directory))
    if not events:
        print(f"No ledger events found under {directory}")
        return

    print_report(events)


if __name__ == "__main__":
    main()
