# Extended-Horizon Observation Run — Operator Instructions

Written per HORIZON-RUN-HANDOFF.md Task 2. **You (the operator) run this, not
Claude.** Everything below is the exact procedure; nothing here changes
trading behavior, only observation.

## 1. Config diff — apply exactly this, nothing else

In `config.yaml`, under `shadow:`:

```yaml
shadow:
  enabled: true
  sample_fraction: 0.10          # UNCHANGED
  poll_interval_seconds: 60      # was 15
  horizon_seconds: 3600          # was 300
  max_tracked: 200               # was 50
```

**`exits.timeout_seconds` stays at `300`.** Do not touch it, or any other
`config.yaml` field. This is what keeps the new trade-level data poolable
with the existing 103 closed trades (same entry gates, same exit ladder, same
maximum hold — only the *shadow observation* window changes). Changing
`timeout_seconds` would orphan four analyses' worth of baseline.

## 2. Before starting: confirm `DRY_RUN=true`

Open `.env` yourself and confirm the `DRY_RUN` line reads `true`. This
project's own history includes a case where it was found `false` — check it
every time, not just on trust. Claude has not read, and will not read, `.env`
or `wallet.json` beyond this instruction.

## 3. Recommended duration: ~5 hours

The pre-registered question is about the cohort that reached ≥25% curve
completion at t=300s and was then cut off — call it the "censored cohort."

- Observed rate: 29 of 578 tracked mints reached that cohort in the existing
  5h10m window ≈ **5.02% of tracked mints**.
- Tracked arrivals run at ≈157/hour (unchanged: total candidate flow ≈1,390/hour,
  sample_fraction stays 0.10, plus the always-sampled bought arm).
- Expected cohort rate: 0.0502 × 157/hour ≈ **7.9 ≈ 8 cohort-qualifying mints
  per hour**.
- For n ≥ 30 in that cohort: 30 / 8 ≈ **3.75 hours**.

**But the last hour of any run is truncated**: a mint that arrives in the
final hour cannot have a full 3600s of observation before the run ends, so
its "did it graduate by t+3600s" question is unanswered and must be excluded
from the graduation analysis (Task 4.2). That means the run needs one extra
hour on top of the 3.75h needed to *accumulate* the cohort:

**Recommended run length: 5 hours** (3.75h to reach n≈30 in the censored
cohort with a small margin, plus ~1h so the final hour's truncated arrivals
can be dropped without losing the target sample). If you can run longer,
longer is better — the cohort accumulates the whole time — but 5h is the
floor for a decision-capable sample.

## 4. Expected credit burn vs the daily halt

Per-mint poll count at the new settings: `horizon_seconds / poll_interval_seconds
= 3600 / 60 = 60` polls per mint (upper bound — a mint that graduates early
gets fewer, but only ~2% do).

Each shadow poll is one `getAccountInfo` call = 1 credit
(`config.yaml`'s `rpc.credit_costs.getAccountInfo: 1`).

Over a 5-hour run at ≈157 tracked arrivals/hour:

```
tracked arrivals  ≈ 157/hr × 5h  = 785 mints
credits           ≈ 785 mints × 60 polls/mint = 47,100 credits
```

Against `rpc.daily_credit_halt: 9,000,000`, this is **≈0.52% of one day's
halt budget** — enormous headroom. This is a shadow-polling-only estimate;
ordinary trading RPC calls (buys, sells, reconciliation) are unaffected by
this change and were never close to the halt in prior windows either.

**RPS check** (Section 2.4's binding constraint, not credits):
`max_tracked 200 / poll_interval 60s = 3.33 req/s`, under
`rpc.shadow_rps_limit: 4`. This is the ceiling — do not raise `max_tracked`
further at this poll interval, or a sweep will take longer than the interval
and shadow resolution will silently degrade.

## 5. What to check on completion

Hand these back along with the ledger file(s):

1. **Halted-time percentage** — any threshold halts, and how much of runtime
   they consumed (`scripts/report.py`'s halt log).
2. **Orphan count** — from the startup orphan scan.
3. **Entries count** — how many real (dry-run) buys executed during the
   window.
4. **Peak concurrent tracked mints vs `max_tracked: 200`** — if it saturates
   (peak at or near 200), say so; this is the main risk at a 12x longer
   horizon (Section 2.5).
5. **Any bought mint missing from the shadow join** — Section 2.5: capacity
   is checked *before* the sampling roll, so a bought mint (which should
   always be sampled at 1.0) can still be silently dropped if the tracker is
   full when it arrives.

Claude's tooling (`scripts/horizon_run.py`) computes items 4 and 5
automatically from the ledger — you don't need to compute them by hand, just
run it after the window closes and hand over its output alongside the raw
ledger files.

## 6. If saturation is observed

**The lever is `sample_fraction`, not `max_tracked`.** Section 2.4:
`max_tracked: 200` at `poll_interval_seconds: 60` is already the ceiling
under the current `shadow_rps_limit: 4` — raising `max_tracked` further would
push shadow polling over its RPS budget and degrade resolution for everyone,
not just the mints past the old cap. If tracked mints are hitting 200 at
peak, the fix for a future run is lowering `sample_fraction` below 0.10, not
raising `max_tracked`. Do not change either mid-run; note it for next time.

## 7. When you're done

Send Claude the new ledger file(s) (e.g. `data/ledger-<date>.jsonl` for each
day the run spanned). Task 4 (analysis) and Task 5 (write-up, stop-condition
application) pick up from there.

---

## Confirmation of Task 1 (analysis tooling)

Built and tested against the **existing** 300s ledger data before this run
starts, per the handoff's own requirement:

- `scripts/horizon_run.py` reproduces Section 2.3's percentile table on the
  current 578-mint dataset: `max=91.83%` and both cohort-defining counts
  (`>=10% = 76`, `>=25% = 29`) match **exactly**. `p95=23.09%` and
  `p99=50.58%` also match exactly. `p50`/`p75`/`p90` land within ~0.3
  percentage points of the handoff's stated figures (0.53% vs 0.56%, 3.54% vs
  3.61%, 14.57% vs 14.81%) — every percentile-interpolation convention tried
  reproduces a different subset of the five exactly, none reproduces all
  five, and the two figures that actually gate the cohort definition (the
  ≥25% count and the max) are exact. Documented in the script itself; not
  hidden.
- Graduation-handling test (`test_graduated_mint_is_not_scored_as_total_loss_and_keeps_last_valid_price`)
  asserts a graduated mint keeps its last valid pre-completion price and is
  never scored as −100%.
- `CurveCompleteError` path covered
  (`test_curve_complete_ticks_never_reach_curve_math_that_would_raise`).
- Concurrency/saturation reporting reproduces the previously-observed peak of
  23 concurrent tracked mints (against `max_tracked: 50`) on the existing
  data, and finds zero bought mints missing from the shadow join in that
  window.
- `uv run pytest`: **287 passed** (273 baseline + 14 new).
- `uv run ruff check .`: **8 findings**, matching the pre-existing baseline
  exactly (zero new).
- No third curve-reconstruction path: `completion_fraction` reuses
  `backtest_exits.reconstruct_curve`'s algebra; `scripts/horizon_run.py`
  imports `backtest_exits` rather than reimplementing it.
- `positions.py`, `ledger.py`, `report.py` unmodified. `config.yaml`
  unmodified by Claude — the diff above is the operator's to apply.
