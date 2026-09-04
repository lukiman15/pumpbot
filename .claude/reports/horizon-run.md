# Extended-Horizon Observation Run — Results

Per `.claude/plans/HORIZON-RUN-HANDOFF.md` Tasks 4–5. Analysis only — `DRY_RUN`
stayed `true` throughout, no capital was ever at risk, and no ledger file,
`config.yaml` field outside §3.1, or trading-path code was touched to produce
this report.

## Run confirmation

- `run_id=4c10b851d9824d24823f359e860c699c`
- Started **2026-09-03 18:06:06** (matches the operator's pasted startup log
  exactly), stopped **2026-09-04 06:31:56** by Ctrl-C — **~12h 26m**, well
  past the 5h floor and the "overnight" recommendation.
- Config diff applied exactly as specified: `poll_interval_seconds: 15→60`,
  `horizon_seconds: 300→3600`, `max_tracked: 50→200`. `exits.timeout_seconds`
  confirmed unchanged at `300` (`git diff config.yaml`).
- `shadow_tracked_mints=707`, `missing_candidate_seen=0` — full join, no
  orphaned shadow rows.
- Old and new runs are cleanly separable by `run_id` (a per-process ledger
  field), not by file or timestamp heuristic — the old 300s window
  (`run_id=82aa36e0...`, 2026-09-02, n=578) is entirely distinct from this
  run. All figures below that say "NEW window" use only
  `4c10b851...`'s 707 mints unless stated otherwise.

## Operator checklist (§4, Task 2 item 5)

| Item | Value |
|---|---|
| Halted-time | 0 threshold-halt events found in the ledger |
| Orphan count | 1 (`TradeOrphaned`) — expected: the operator's Ctrl-C interrupted an open position from the *previous* run's process, caught by the standard orphan scan |
| Entries (dry-run buys) | 156 |
| Peak concurrent tracked mints | **125** vs `max_tracked: 200` — 62.5% of capacity, no saturation |
| Bought mints missing from shadow join | **0** |

No saturation risk materialized (§2.5). Randomness was not compromised by a
full tracker at any point in this run.

## Section 2.3 reproduction, NEW window (full 3600s, own percentile table)

`n=707  p50=0.20%  p75=1.75%  p90=9.76%  p95=23.62%  p99=62.98%  max=96.53%`
`>=10% completion: 71 mints   >=25% completion: 34 mints`

(Same nearest-rank percentile convention as Task 1, same caveat: display
convention, not data, drives sub-percentage-point disagreement with other
interpolation methods. Not decision-relevant here — the ≥25% count and max are the load-bearing figures.)

## Graduation-aware outcome classification, NEW window (full 3600s)

```
faded:               470
still_open_at_horizon: 217
graduated:             20
```

Graduation timing (elapsed seconds, n=20): `min=20.5  median=59.6  p90=308.8  max=862.6`

**17 of 20 graduations (85%) occurred within the first 300 seconds** — the
old window's own horizon. Only 3 graduations, across the entire 707-mint,
12.4-hour run, happened *after* t=300s (at roughly 309s, and two more inside
the ≤300s-to-863s range implied by the max). This is a strong, low-noise
signal in favor of §2.2's alternative hypothesis: graduations are not a
"long tail beyond 300s" phenomenon at these settings — they cluster right at
the front of a mint's life, and a 12x longer window barely surfaced a
handful more.

## Peak-and-time-to-peak, NEW window (full 3600s)

`n=690  median time-to-peak=6.81% of window (≈245s)  p90=101.57%`
(p90 slightly over 100% is 60s-poll-interval overshoot past the 3600s wall, not a data error)

- Peaks landing in the final 5% of the tracked window (consistent with the
  window truncating an ongoing rise, not a real plateau): **206/690 (29.9%)**
- Peaks landing at or before t=300s — i.e., visible even under the OLD
  window: **360/690 (52.2%)**

Read together with the graduation numbers: roughly half of all peaks (by
count) occur within the timeframe the old window already covered, and the
tail that peaks very late (last-5%-of-window) is a real ~30% minority, not a
majority. The horizon extension surfaces *some* later peaks, but they are not
where most of the graduation activity or peak-price activity lives.

## Censored ≥25%-completion cohort: fate from t=300s to t=3600s

Cohort definition applied exactly as in the handoff: mints with a valid
on-curve tick within ±50s of t=300s whose curve-completion fraction at that
tick was ≥25%.

**Cohort size: n=12.** (0 excluded as run-truncated arrivals — the run ran
long enough that every cohort member got a full window.)

| Fate | Count |
|---|---|
| Graduated between t=300s and t=3600s | 2 |
| Faded (full rise-and-fall observed) | 10 |
| Still open at t=3600s | 0 |

**Graduation rate within the resolved cohort: 2/12 = 16.7%** — nominally
clears §3.5's ≥15% "continue" threshold, but **n=12 is far below any
sample size that threshold-crossing should be trusted at**: one mint's
outcome either way moves the rate by 8.3 percentage points, which would flip
the letter of the condition (1/12=8.3%, would fail; 3/12=25%, comfortably
clear). This is flagged, not resolved — see "Applying §3.5" below.

Forward return from the t=300s reference tick to t=3600s/fade, for the 10
non-graduating cohort members:

`median=-76.5%  p25=-78.4%  p75=-66.8%  positive-return count: 0/10`

The cohort members that don't graduate collapse hard after t=300s — there is
no sign of a "held near peak, drifted sideways" middle outcome in this
sample.

## Subpopulation search (§3.5's second continue-condition)

Population: all NEW-window mints with a valid t=300s reference tick that
were tracked to a full 3600s or graduated after t=300s (n=635, of which 2
graduated after the reference tick — reported separately, not folded into a
numeric return).

| Split | Cell | n | Median forward return (t=300s→3600s/fade) |
|---|---|---|---|
| Creator supply (terciles) | low (≤0.18%) | 212 | 0.00% |
| | mid (0.18–3.42%) | 220 | 0.00% |
| | high (>3.42%) | 201 (+2 grad) | 0.00% |
| Curve completion @ t=300s | <5% | 598 | 0.00% |
| | 5–25% | 25 | -42.2% *(n<30, insufficient)* |
| | ≥25% | 10 (+2 grad) | -76.5% *(n<30, insufficient)* |
| Socials (Tier2Evaluated, n=149/635 with data) | has_twitter | 89 | 0.00% |
| | no_twitter | 60 | 0.00% |
| | has_telegram | 4 | -0.4% *(n<30)* |
| | no_telegram | 145 | 0.00% |
| | has_website | 43 | 0.00% |
| | no_website | 106 | 0.00% |
| | tier2_passed | 142 | 0.00% |
| | tier2_failed | 7 | -0.1% *(n<30)* |

**No cell at n≥30 shows a positive median return.** Every n≥30 cell reads
exactly 0.00% — this is not rounding: the large majority of tracked mints go
completely dead after early activity (no further buys or sells means the
bonding-curve reserve ratio literally does not move), so their forward
return from t=300s is flat, not merely small. All non-zero movement is
concentrated in the two <30-n cells that had already made real curve
progress by t=300s, and that movement is uniformly negative.

Since no subpopulation reached the n≥30 bar at all (let alone a positive
median), split-half replication — the condition's second clause — is moot;
there is nothing promising to replicate.

## Comparison to the OLD window (§3.3)

Chosen method: **truncate the NEW data to its first 300 seconds**, rather
than downsample the OLD data to 60s (stated per §3.3's requirement).

| | OLD (native 15s poll, 300s) | NEW truncated to 300s (60s poll) |
|---|---|---|
| n | 578 | 707 |
| p50 | 0.52% | 0.19% |
| p75 | 3.54% | 1.53% |
| p90 | 14.57% | 8.18% |
| p95 | 23.09% | 21.31% |
| p99 | 50.58% | 56.03% |
| max | 91.83% | 89.62% |
| ≥25% completion | 29/578 | 30/707 |
| faded | 380 | 349 |
| still_open_at_horizon | 186 | 341 |
| graduated (within 300s) | 12 | 17 |

The lower p50/p75/p90 under the coarser 60s grid is exactly what §3.3
predicts: a 60s interval misses short-lived spikes a 15s interval catches,
so median/mid-percentile peak estimates read low under coarser polling. The
tail (p95/p99/max) is comparable across both — big, sustained moves survive
a coarser grid; small transient ones don't. The `still_open_at_horizon`
count nearly doubling (186→341) is also a resolution artifact of the
classification method (fewer, sparser ticks make ties at "last tick = peak"
more likely) and should not be read as more mints genuinely still rising —
it is a measurement-granularity effect, not a behavior change between runs.

## Applying §3.5's pre-registered stop condition

Quoted as written, not reinterpreted:

- **Continue if:** ≥15% of the ≥25%-completion cohort graduates t=300s→3600s
  (**observed: 16.7%, but n=12**), **or** some subpopulation shows a positive
  median forward return at n≥30, replicated split-half (**observed: no cell
  at n≥30 is positive — this clause is not met**).
- **Stop if:** graduations cluster inside 300s (**observed: 85%, 17/20, do**)
  **and** no subpopulation reaches positive median at n≥30 (**observed: true,
  none do**).

**This result lands between the two branches and is reported as ambiguous,
per the handoff's own instruction not to resolve an ambiguous result toward
more work.** The "stop" branch's both conditions are literally satisfied.
The "continue" branch's graduation-rate clause is also literally satisfied
by the letter of the number (16.7% ≥ 15%), but on a cohort of n=12 — a
sample where the outcome of a single mint determines whether the condition
reads as met or failed. The subpopulation clause of "continue" is cleanly
not met. On balance: one of two independent "continue" triggers is nominally
tripped, but by a margin the sample size cannot support; the other trigger,
along with both "stop" conditions, are unambiguous. The honest summary is
that the data leans stop, with one fragile, low-n data point pointing the
other way that should not be treated as a real signal until n is larger.

## What this does not establish

- **Resolution asymmetry**: NEW-window figures use 60s polling vs OLD's 15s.
  Confirmed and quantified above (§3.3 comparison) — coarser polling reads
  lower peaks at the median/upper-mid percentiles specifically, which is a
  measurement artifact, not evidence prices actually rose less.
- **The 3600s wall**: this run cannot speak to anything that might happen
  beyond t=3600s. The extended horizon is still finite.
- **The truncated final hour**: not applicable here — the run ran long
  enough (12.4h) that 0 cohort members needed to be excluded for arriving
  too late to observe a full window. Worth noting explicitly since it was a
  real risk at the minimum 5h recommendation and did not materialize.
- **§2.5 saturation risk**: checked and did not materialize. Peak concurrency
  was 125/200 (62.5%), and 0 bought mints were dropped from the shadow join.
  `sample_fraction` was never touched, so representativeness is unchanged
  from the OLD window.
- **No real fill, no capital at risk**: `DRY_RUN=true` throughout (confirmed
  by the operator before starting, and every buy/sell in the ledger is
  `[DRY RUN] simulated`). Nothing in this report reflects actual execution,
  slippage, or real order-book interaction — only bonding-curve state as
  observed by polling.
- **Small-n fragility**: both cohort-level figures that matter most for the
  stop-condition decision (the ≥25% cohort's graduation rate, and the two
  cells with non-zero forward returns) rest on n=10–12. Wider claims than
  "this is suggestive, not conclusive" are not supported by this run.
- **No go-live recommendation is made here or implied.**

## Acceptance checklist status (§6.2, items relevant to Tasks 4–5)

- Old/new comparison honors §3.3: truncated NEW to 300s (stated above). ✓
- Peak concurrency reported against `max_tracked: 200` (125, no saturation);
  0 dropped bought mints. ✓
- §3.5 stop condition applied as pre-registered, branch stated prominently,
  ambiguous result reported as ambiguous, not resolved toward more work. ✓
- `positions.py`, `ledger.py`, `report.py` unmodified; `config.yaml` changes
  limited to the operator's §3.1 diff (confirmed via `git diff`). ✓
- No modelled figure written to any ledger file; all figures in this report
  are derived offline from existing ledger data, nothing appended. ✓
- This report includes "What this does not establish." ✓
- `data/` remains gitignored; no ledger rows, mint addresses, or wallet data
  are reproduced in this report beyond aggregate counts/percentiles. ✓
- `DRY_RUN` never touched, `.env`/`wallet.json` never read by Claude, the run
  was started and stopped entirely by the operator. ✓
