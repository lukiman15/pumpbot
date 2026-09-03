"""Offline tooling for the extended-horizon observation run: does the tail
live outside the current 300s shadow window? Pure analysis over data/*.jsonl
already on disk -- no network access, no run of the trading loop. See
.claude/plans/HORIZON-RUN-HANDOFF.md for the full design rationale; only what
is needed to read this file is repeated here.

Reuses scripts/backtest_exits.py's curve reconstruction (Section 4 Task 1:
"do not build a third curve-reconstruction path") rather than duplicating it.
This script does NOT import scripts/entry_signals.py's TrackedMint, because
that dataclass doesn't carry the ShadowPrice.curve_complete flag needed for
graduation-aware classification -- it defines its own loader below, built the
same way (join CandidateSeen/ShadowPrice by mint), so both scripts can be
used side by side without one silently invalidating the other's cached data.

GRADUATION IS A CENSORING EVENT, NOT A PRICE (Section 3.4): a curve_complete
tick's price_sol is a degenerate decode artifact (migration terminal state),
never a real price and never a -100% return. It is excluded from every price
computation below and instead recorded as its own outcome category with the
last VALID on-curve price and the graduation time kept alongside it.

UNITS TRAP (carried forward): ShadowPrice.price_sol is lamports per RAW token
unit (the reserve ratio); ExitFilled.curve_price_sol is SOL per WHOLE token.
They differ by exactly 1000x. This script only ever touches ShadowPrice's
scale via CandidateSeen-derived k/p0, same as backtest_exits.py and
entry_signals.py.
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_exits as bx

from pumpbot.config import PROJECT_ROOT, load_settings
from pumpbot.curve import (
    INITIAL_VIRTUAL_SOL,
    LAMPORTS_PER_SOL,
    MIGRATION_SOL_LAMPORTS,
    TOKEN_DECIMALS,
)
from pumpbot.ledger import read_events

MIN_SAMPLE_FOR_STATS = bx.MIN_SAMPLE_FOR_STATS  # reused, one evidence bar per project

# Section 2.3's pre-registered cohort cutoff, tested against the existing
# 300s data before the operator starts anything (Task 1's own acceptance bar).
CENSORED_COHORT_COMPLETION_THRESHOLD = 0.25


# --- data model ---------------------------------------------------------


@dataclass(frozen=True)
class HorizonTick:
    elapsed: float
    ts_monotonic: float
    price_sol_raw: float
    curve_complete: bool


@dataclass(frozen=True)
class HorizonMint:
    mint: str
    arm: str  # "bought" | "rejected" | "skipped"
    reject_reason: str | None
    trade_id: str | None
    k: int
    p0: float  # t=0 reserve ratio, from CandidateSeen -- never return_from_first_seen
    creator_supply_fraction: float
    ticks: tuple[HorizonTick, ...]  # ascending elapsed, EVERY row including curve_complete ones


def load_horizon_mints(events: list[dict[str, Any]]) -> tuple[list[HorizonMint], dict[str, int]]:
    """Joins CandidateSeen / ShadowPrice by mint. Keeps curve_complete ticks
    in the sequence (unlike entry_signals.TrackedMint) so graduation timing
    and last-valid-price can both be recovered from one pass."""
    candidate_seen_by_mint: dict[str, dict[str, Any]] = {}
    shadow_by_mint: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in events:
        event = row.get("event")
        if event == "CandidateSeen":
            candidate_seen_by_mint.setdefault(row["mint"], row)
        elif event == "ShadowPrice":
            shadow_by_mint[row["mint"]].append(row)

    diagnostics = {
        "shadow_tracked_mints": len(shadow_by_mint),
        "missing_candidate_seen": 0,
        "usable_mints": 0,
    }

    mints: list[HorizonMint] = []
    for mint, rows in shadow_by_mint.items():
        candidate = candidate_seen_by_mint.get(mint)
        if candidate is None:
            diagnostics["missing_candidate_seen"] += 1
            continue

        rows_sorted = sorted(rows, key=lambda r: r["ts_monotonic"])
        candidate_ts = candidate["ts_monotonic"]
        vs_lamports = round(candidate["virtual_sol_in_curve"] * LAMPORTS_PER_SOL)
        vt_raw = round(candidate["virtual_tokens_in_curve"] * 10**TOKEN_DECIMALS)
        k = vs_lamports * vt_raw
        p0 = vs_lamports / vt_raw

        ticks = tuple(
            HorizonTick(
                elapsed=r["ts_monotonic"] - candidate_ts,
                ts_monotonic=r["ts_monotonic"],
                price_sol_raw=r["price_sol"],
                curve_complete=bool(r["curve_complete"]),
            )
            for r in rows_sorted
        )

        mints.append(
            HorizonMint(
                mint=mint,
                arm=rows_sorted[0]["arm"],
                reject_reason=rows_sorted[0]["reject_reason"],
                trade_id=rows_sorted[0].get("trade_id"),
                k=k,
                p0=p0,
                creator_supply_fraction=candidate["creator_supply_fraction"],
                ticks=ticks,
            )
        )

    diagnostics["usable_mints"] = len(mints)
    return mints, diagnostics


# --- Task 1.1: curve-progress computation -----------------------------------

_INITIAL_VIRTUAL_SOL_LAMPORTS = round(INITIAL_VIRTUAL_SOL * LAMPORTS_PER_SOL)


def completion_fraction(k: int, price_sol_raw: float) -> float:
    """How far toward migration a tick's reconstructed curve sits, by real
    SOL raised so far (Section 2.3). Same virtual-reserves algebra as
    bx.reconstruct_curve (vs=sqrt(k*p)) -- kept unrounded here (bx's
    reconstruct_curve rounds to the nearest lamport/raw-token for
    BondingCurveState's int fields, which is right for feeding tokens_to_sol
    but adds needless noise to a float percentile computation).
    real_sol_reserves is then derived the same way
    curve.from_virtual_reserves derives it: from the fixed
    INITIAL_VIRTUAL_SOL offset."""
    if k <= 0 or price_sol_raw <= 0:
        raise ValueError(f"invalid completion_fraction inputs: k={k} price_sol_raw={price_sol_raw}")
    virtual_sol_lamports = math.sqrt(k * price_sol_raw)
    real_sol_lamports = max(0.0, virtual_sol_lamports - _INITIAL_VIRTUAL_SOL_LAMPORTS)
    return min(1.0, real_sol_lamports / MIGRATION_SOL_LAMPORTS)


def peak_completion_fraction(mint: HorizonMint) -> float:
    """Max completion_fraction across the mint's own VALID on-curve ticks
    (price > 0, not curve_complete -- Section 3.4). A curve_complete tick's
    degenerate price would raise CurveCompleteError-shaped nonsense if fed
    through reconstruct_curve/completion_fraction, so it is skipped here,
    same as every other price computation in this file."""
    peak = 0.0
    for tick in mint.ticks:
        if tick.curve_complete or tick.price_sol_raw <= 0:
            continue
        peak = max(peak, completion_fraction(mint.k, tick.price_sol_raw))
    return peak


def completion_percentiles(mints: list[HorizonMint]) -> dict[str, Any]:
    """Reproduces Section 2.3's percentile table exactly on the existing 300s
    data -- Task 1's own acceptance bar, checked before the operator starts
    anything."""
    peaks = sorted(peak_completion_fraction(m) for m in mints)
    n = len(peaks)

    def pct(p: float) -> float:
        # Nearest-rank percentile. NOTE (Section 6.3 item 6, honesty over a
        # clean match): this reproduces Section 2.3's p95, p99, max, and
        # both threshold counts EXACTLY, but p50/p75/p90 land within ~0.3
        # percentage points rather than bang on (0.53% vs 0.56%, 3.54% vs
        # 3.61%, 14.57% vs 14.81%) -- every percentile-interpolation
        # convention tried (linear, exclusive, inclusive, weibull-style)
        # reproduces a different subset exactly, none reproduces all five.
        # The two numbers the rest of this analysis actually keys off --
        # the >=25% cohort size (29) and the max (91.83%) -- match exactly,
        # which is strong evidence the underlying per-mint peak dataset is
        # identical; the mismatch is in percentile *display* convention for
        # the three unused-downstream figures, not in the data.
        if n == 0:
            return 0.0
        idx = min(n - 1, max(0, round(p * (n - 1))))
        return peaks[idx]

    return {
        "n": n,
        "p50": pct(0.50),
        "p75": pct(0.75),
        "p90": pct(0.90),
        "p95": pct(0.95),
        "p99": pct(0.99),
        "max": peaks[-1] if peaks else 0.0,
        "count_ge_10pct": sum(1 for p in peaks if p >= 0.10),
        "count_ge_25pct": sum(1 for p in peaks if p >= 0.25),
    }


# --- Task 1.2: graduation-aware outcome classification ----------------------


@dataclass(frozen=True)
class MintOutcome:
    category: str  # "graduated" | "faded" | "still_open_at_horizon"
    last_valid_price: float | None
    last_valid_elapsed: float | None
    graduation_elapsed: float | None
    peak_multiple: float  # relative to p0, over valid on-curve ticks only
    peak_elapsed: float | None
    last_tick_elapsed: float | None


def classify_outcome(mint: HorizonMint) -> MintOutcome:
    """Section 3.4: a graduated mint is never scored as -100% and never
    silently dropped -- it gets its own category plus the last valid
    on-curve price and the graduation time.

    Among non-graduated mints, "still_open_at_horizon" vs "faded" is decided
    by whether the mint's peak (relative to p0) fell exactly on its last
    observed tick: if so, tracking stopped while the price was still at its
    highest observed point -- the trajectory is censored, not resolved. If
    the peak occurred earlier and the price had already receded by the last
    tick, the full rise-and-fall was observed inside the window, so it is
    labelled "faded" rather than left ambiguous.
    """
    graduation_elapsed: float | None = None
    last_valid_price: float | None = None
    last_valid_elapsed: float | None = None
    peak_multiple = 0.0
    peak_elapsed: float | None = None
    last_tick_elapsed: float | None = None

    for tick in mint.ticks:
        last_tick_elapsed = tick.elapsed
        if tick.curve_complete:
            if graduation_elapsed is None:
                graduation_elapsed = tick.elapsed
            continue
        if tick.price_sol_raw <= 0:
            continue
        last_valid_price = tick.price_sol_raw
        last_valid_elapsed = tick.elapsed
        multiple = (tick.price_sol_raw / mint.p0) if mint.p0 > 0 else 0.0
        if multiple >= peak_multiple:
            peak_multiple = multiple
            peak_elapsed = tick.elapsed

    if graduation_elapsed is not None:
        category = "graduated"
    elif peak_elapsed is not None and last_tick_elapsed is not None and peak_elapsed >= last_tick_elapsed:
        category = "still_open_at_horizon"
    else:
        category = "faded"

    return MintOutcome(
        category=category,
        last_valid_price=last_valid_price,
        last_valid_elapsed=last_valid_elapsed,
        graduation_elapsed=graduation_elapsed,
        peak_multiple=peak_multiple,
        peak_elapsed=peak_elapsed,
        last_tick_elapsed=last_tick_elapsed,
    )


def assert_never_priced_as_total_loss(outcome: MintOutcome) -> None:
    """A defensive check callers can use before feeding an outcome's price
    into any return computation -- a graduated mint's last VALID price is
    always positive; the degenerate curve_complete price never leaks in."""
    if outcome.category == "graduated" and outcome.last_valid_price is not None and outcome.last_valid_price <= 0:
        raise AssertionError(
            "graduated mint's last_valid_price must be a real positive "
            "on-curve price, never the degenerate curve_complete tick"
        )


# --- Task 1.3: peak-and-time-to-peak -----------------------------------------


def time_to_peak_fraction_of_window(outcome: MintOutcome, horizon_seconds: float) -> float | None:
    """peak_elapsed as a fraction of the tracking horizon -- the statistic
    that answers whether the OLD 300s window was hiding a later tail (peaks
    clustering near 1.0 under the old horizon would mean truncation, not a
    genuine plateau)."""
    if outcome.peak_elapsed is None or horizon_seconds <= 0:
        return None
    return outcome.peak_elapsed / horizon_seconds


# --- Task 1.4: concurrency and saturation reporting --------------------------


@dataclass(frozen=True)
class ConcurrencyReport:
    peak_concurrent: int
    tracked_mint_count: int
    bought_mints_missing_from_shadow: tuple[str, ...]


def concurrency_report(
    mints: list[HorizonMint], events: list[dict[str, Any]]
) -> ConcurrencyReport:
    """Sweep-line peak concurrency over [start, end] per tracked mint, where
    start is the first shadow tick's arrival time (ts_monotonic - elapsed)
    and end is its last tick's ts_monotonic -- shadow.py only ever stops
    tracking a mint at curve.complete or elapsed >= horizon_seconds, both of
    which show up as that mint's final row. Also cross-checks every dry-run
    bought trade_id against the shadow join (Section 2.5's dropped-bought-
    mint risk)."""
    boundaries: list[tuple[float, int]] = []
    for m in mints:
        if not m.ticks:
            continue
        start = m.ticks[0].ts_monotonic - m.ticks[0].elapsed
        end = m.ticks[-1].ts_monotonic
        boundaries.append((start, 1))
        boundaries.append((end, -1))

    boundaries.sort(key=lambda b: (b[0], b[1]))  # process departures before arrivals on ties
    current = 0
    peak = 0
    for _, delta in boundaries:
        current += delta
        peak = max(peak, current)

    shadow_bought_mints = {m.mint for m in mints if m.arm == "bought"}
    bought_trade_mints = {
        row["mint"] for row in events if row.get("event") == "EntryFilled" and row.get("dry_run")
    }
    missing = tuple(sorted(bought_trade_mints - shadow_bought_mints))

    return ConcurrencyReport(
        peak_concurrent=peak,
        tracked_mint_count=len(mints),
        bought_mints_missing_from_shadow=missing,
    )


# --- CLI / report ------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline tooling for the extended-horizon observation run"
    )
    parser.add_argument("--ledger", type=str, default=None)
    args = parser.parse_args()

    settings = load_settings()
    ledger_dir = Path(args.ledger) if args.ledger else (PROJECT_ROOT / settings.config.ledger.path).parent
    horizon_seconds = settings.config.shadow.horizon_seconds

    events = list(read_events(ledger_dir))
    mints, diagnostics = load_horizon_mints(events)

    print("=== Assembly ===")
    for k, v in diagnostics.items():
        print(f"  {k}: {v}")
    print()

    print("=== Section 2.3 reproduction: peak curve-completion fraction ===")
    pct = completion_percentiles(mints)
    print(
        f"  n={pct['n']}  p50={pct['p50']:.2%}  p75={pct['p75']:.2%}  "
        f"p90={pct['p90']:.2%}  p95={pct['p95']:.2%}  p99={pct['p99']:.2%}  "
        f"max={pct['max']:.2%}"
    )
    print(f"  >=10% completion: {pct['count_ge_10pct']} mints")
    print(f"  >=25% completion: {pct['count_ge_25pct']} mints")
    print()

    print("=== Graduation-aware outcome classification ===")
    outcomes = {m.mint: classify_outcome(m) for m in mints}
    for outcome in outcomes.values():
        assert_never_priced_as_total_loss(outcome)
    from collections import Counter

    category_counts = Counter(o.category for o in outcomes.values())
    for category, count in category_counts.most_common():
        print(f"  {category}: {count}")
    graduated = [o for o in outcomes.values() if o.category == "graduated"]
    if graduated:
        grad_times = sorted(o.graduation_elapsed for o in graduated)
        print(
            f"  graduation-time distribution (elapsed seconds): "
            f"min={grad_times[0]:.1f} median={statistics.median(grad_times):.1f} "
            f"max={grad_times[-1]:.1f}"
        )
    print()

    print("=== Peak-and-time-to-peak ===")
    ttp_fractions = [
        f
        for o in outcomes.values()
        if (f := time_to_peak_fraction_of_window(o, horizon_seconds)) is not None
    ]
    if ttp_fractions:
        ttp_sorted = sorted(ttp_fractions)
        print(
            f"  time-to-peak as fraction of the tracking window: n={len(ttp_sorted)} "
            f"median={statistics.median(ttp_sorted):.2%} "
            f"p90={ttp_sorted[min(len(ttp_sorted) - 1, round(0.90 * (len(ttp_sorted) - 1)))]:.2%}"
        )
        clustered_at_end = sum(1 for f in ttp_fractions if f >= 0.95)
        print(
            f"  peaks landing in the final 5% of the window "
            f"(consistent with truncation, not a plateau): {clustered_at_end}/{len(ttp_fractions)}"
        )
    print()

    print("=== Concurrency and saturation ===")
    concurrency = concurrency_report(mints, events)
    print(f"  peak_concurrent_tracked: {concurrency.peak_concurrent}")
    print(f"  tracked_mint_count: {concurrency.tracked_mint_count}")
    print(
        f"  bought_mints_missing_from_shadow_join: "
        f"{len(concurrency.bought_mints_missing_from_shadow)} {concurrency.bought_mints_missing_from_shadow}"
    )


if __name__ == "__main__":
    main()
