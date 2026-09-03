# Tail-Aware Re-Analysis — Multi-Objective Ranking, Tail Diagnostics, and a Cap Test

**Pure offline analysis**, extending [`scripts/backtest_exits.py`](../../scripts/backtest_exits.py)
(no run of `python -m pumpbot`, no network, no capital risk) over the same
103 replayable dry-run trades from `data/ledger-2026-09-02.jsonl` and
`data/ledger-2026-09-03.jsonl` used in the prior exit-retune analysis.

---

## 0. Correction to Section 1's own headline figures

Before anything else: Section 1.1 of the handoff quoted `mean = 1.0353` over
"n = 107 exit legs," computed directly from `ExitFilled.realizable_multiple`
rows. That figure is **leg-weighted, not trade-weighted** — it counts each of
the 4 trades that partially filled at `take_profit_1` **twice**: once for
the partial-fill leg (itself ~1.55–1.85×) and once for the final closing leg.
Recomputed at the trade level (one number per trade — the correct unit for
comparing exit *configurations*, since a sweep must produce exactly one
modelled outcome per trade):

```
n = 103 trades (final leg only)
mean = 1.0115      median = 0.9800
mean minus top 1 = 0.9949
mean minus top 3 = 0.9750
mean minus top 5 = 0.9706
```

This harness's own replayed baseline (§2 below) gives mean=1.0134,
minus-top-1=0.9976, minus-top-3=0.9734, minus-top-5=0.9669 — close to the
corrected trade-level figures, with the residual gap consistent with the
15s-resolution replay noise already documented in the prior fidelity report.

**The qualitative finding in Section 1 survives this correction — median and
mean genuinely disagree, and the mean is fragile on a handful of trades —
but the magnitude was overstated.** The "average trade gains 3.5%" framing
should read closer to "average trade gains ~1.1–1.3%," not 3.5%. Everything
below uses the trade-level (harness-replayed) figures, which is what the
sweep can actually act on.

---

## 1. Multi-objective ranking (Task 1)

All four objectives computed over the full pre-registered grid (3,120
combinations, same as the prior task):

| Objective | Baseline value | Baseline rank |
|---|---|---|
| `median` | 0.9800 | **2,760 / 3,120** |
| `mean` | 1.0134 | **600 / 3,120** |
| `total_return` | 1.3807 | 600 / 3,120 |
| `net_mean` | 0.0034 | 600 / 3,120 |

**The baseline's rank swings from the bottom 12% (median) to the top 19%
(mean/total_return/net_mean).** This is the disagreement Section 1.1
predicted, confirmed directly: ranking by median makes the current config
look bad; ranking by mean makes it look reasonably good. Both are computed
from the identical 103 modelled outcomes — the difference is entirely in how
the objective aggregates them.

**Top-10 overlap between objectives:**

| Pair | Shared param sets |
|---|---|
| median vs mean | 0/10 |
| median vs total_return | 0/10 |
| median vs net_mean | 0/10 |
| mean vs total_return | **10/10** |
| mean vs net_mean | **10/10** |
| total_return vs net_mean | **10/10** |

`mean`, `total_return`, and `net_mean` agree completely on their top-10 —
unsurprising, since all three are driven by the same tail trades. `median`
picks an entirely disjoint set. The two winners:

- **Median-optimal**: `tp1=1.2×/0%` (a no-op — sells nothing), `tp2=1.5×`,
  `stop=-15%`, `timeout=60s` → median=0.9800, **but mean=1.0051 and
  total_return=0.5241 — both worse than baseline's 1.0134 / 1.3807.** This
  is Section 1.1's warning realized concretely: optimizing the median
  produces a config that is worse on the metric that compounds.
- **Mean/total_return/net_mean-optimal**: `tp1=2.0×/50%`, `tp2=2.5×`
  (baseline's own cap), `stop=-15%`, `timeout=120s` → mean=1.0195,
  total_return=2.0108, net_mean=0.0095, median=0.9800 (identical median to
  baseline).

No combination clears net-of-base-fee breakeven for a majority of trades
under any objective (max `frac_above_net_breakeven` across the whole grid:
12.6%, unchanged from the prior report — this finding does not depend on
which objective is used to rank).

---

## 2. Split-half stability, by objective

| Objective | First half's #1 rank in second half | Top-5 overlap |
|---|---|---|
| `median` | **1 / 3,120** | 1/5 |
| `mean` | **2,945 / 3,120** | 0/5 |
| `total_return` | 2,945 / 3,120 | 0/5 |
| `net_mean` | 2,945 / 3,120 | 0/5 |

**The median objective's #1 combo replicates almost perfectly across
halves. The mean-family objectives do not replicate at all** — the
first-half mean-winner falls to the bottom 6% when scored against the
second half.

Investigated directly (not just reported from the numbers): in the first
half (51 trades), the *tight-cap* combo (`tp2=1.5`) happens to have both the
best median **and** the best mean in that sub-sample. In the second half (52
trades), the mean-winner is the *looser-cap* combo (`tp2=2.5`, matching the
full-sample mean-winner exactly). **This is not a contradiction — it's
direct evidence that at n≈52 per half, which trades happen to fall in which
half changes which shape looks best on the mean.** The full-sample
mean-winner is essentially inherited from whichever half contains the
largest tail trades, which in this data is the second half.

**Stated plainly, per Section 3.4:** the median ranking is stable at this n;
the mean ranking is not. Neither finding should be read as "median is the
right objective" — Section 1 already showed median-optimization actively
selects against the tail thesis. It means: **this sample is not yet large
enough to say which shape is best on the metric that matters for a tail
strategy**, which is exactly what Task 4 exists to fix.

---

## 3. Tail diagnostics (Task 2)

Computed for baseline and for each objective's winner. Full detail (bootstrap
seed=0, 10,000 resamples):

| Config | Trimmed mean (all / −1 / −3 / −5) | Bootstrap 95% CI on mean | Contains 1.0 / ~1.01 |
|---|---|---|---|
| Baseline | 1.0134 / 0.9976 / 0.9734 / 0.9669 | [0.9726, 1.0664] | Yes / Yes |
| Winner[median] | 1.0051 / 0.9965 / 0.9823 / 0.9733 | [0.9781, 1.0375] | Yes / Yes |
| Winner[mean/total_return/net_mean] | 1.0195 / 1.0038 / 0.9797 / 0.9734 | [0.9782, 1.0726] | Yes / Yes |

**Every single interval contains both 1.0 and the base-fee-adjusted
breakeven (~1.01).** At this n, this data cannot distinguish "this strategy
has a positive edge," "this strategy is exactly breakeven," and "this
strategy has a small negative edge" from each other, under *any* of the
configs examined. Per Section 3.4, this is the answer — reporting it
plainly is the success condition, not a reason to keep looking for a
config that resolves it.

**Trimmed-mean sensitivity, restated as a warning:** for every config
examined, dropping just the top 3 trades (out of 103) pulls the mean below
1.0 gross of base fees. The tail-optimal winner's mean survives slightly
better than baseline's (1.0195 → 0.9797 vs 1.0134 → 0.9734, both
minus-top-3), but neither survives dropping 5.

**Tail-event rates:**

| Config | n>1.5× | n>2.0× | n>2.5× | rate>2.0× |
|---|---|---|---|---|
| Baseline | 3 | 2 | 2 | 1.9% |
| Winner[median] | 4 | **0** | **0** | 0.0% |
| Winner[mean family] | 3 | 2 | 2 | 1.9% |

The median-optimal winner produces **zero** trades exceeding 2.0× — its
tight `tp2=1.5` cap mechanically prevents the exact outcomes the tail thesis
depends on. This is the clearest illustration in this report of Section
1.1's warning: optimizing the median doesn't just fail to capture the tail,
it actively eliminates it from the modelled outcome set.

**Required n for ~30 tail events**, at the baseline/mean-family rate of
1.9% (>2.0×): **≈1,545 trades**. At observed throughput (§5 below,
~20/hour), that is roughly **77 more hours** of observation for the >2.0×
threshold alone — the >2.5× threshold is at the same rate here (2/103) and
would need a similar n. This number directly sizes the Task 4 instructions.

---

## 4. Does removing the cap help the mean? (Task 3)

Extended grid: `take_profit_2_multiple` ∈ {1.5, 2.0, 2.5, 4.0, 6.0, 10.0,
1e9}. **6,000 combinations run, 720 skipped** (tp2≤tp1, same skip logic,
proportionally more because the axis is longer).

**Result: no. Under every objective (mean, total_return, net_mean), the top
5 combinations in the extended grid still use `take_profit_2_multiple =
2.5` — the same cap as baseline, not a looser one.** The median objective's
top-5 is unchanged too (still `tp2=1.5`).

This was checked directly, not inferred from the ranking alone. Holding the
mean-winner's other parameters fixed (`tp1=2.0×/50%, stop=-15%, timeout=120s,
trailing_arm=1.15`) and varying only `take_profit_2_multiple`:

| `tp2` | mean | total_return |
|---|---|---|
| 2.5 | **1.0195** | **2.0108** |
| 4.0 | 1.0155 | 1.5961 |
| 6.0 | 1.0155 | 1.5961 |
| 10.0 | 1.0155 | 1.5961 |
| 1e9 (uncapped) | 1.0155 | 1.5961 |

**Loosening the cap makes the mean and total_return worse, not better**, for
this parameter shape. This is not simply "the 300s horizon binds before the
cap does," though that is part of the picture — checked directly, with
`take_profit_2` and `stop_loss` effectively disabled, only **2 of 103 trades**
ever peak above 2.5× within the 300s horizon (one to 2.5377, one to
**3.5566** — genuinely higher than the capped 2.7042 originally recorded).
So there *is* room above 2.5× within the horizon on at least one trade. The
reason loosening the cap still hurts the mean is that letting the position
ride past 2.5× **exposes it to giving back the gain** before whatever
eventually closes it (trailing stop, timeout, or creator_sold) fires — the
fixed 2.5× cap locks in value that a looser cap risks losing to reversion,
*even within this same 5-minute window*.

**Section 2.4's horizon limit, stated as required:** this is "uncapped
within 300 seconds," not uncapped. The one trade that peaks at 3.5566 is
truncated there by the shadow horizon — there is no data on whether it kept
climbing, gave back further, or did something else after 300s. Nothing here
says a looser cap would fail over a longer horizon; it says a looser cap
does not help over *this* horizon, on *this* sample of 2 tail-qualifying
trades. That is a real answer, but it is an answer about 300 seconds, not
about the strategy's tail in general — which is exactly why Task 4's longer
horizon matters.

---

## 5. Task 4 — Instructions for the operator's next observation window

**You (the operator) run this. The agent that produced this report did not
start it and does not decide whether to.**

### Why, and how much

Section 3's headline number: at the observed >2.0× tail rate (1.9%), reaching
~30 tail events needs **roughly n≈1,545 trades**. Observed throughput in the
Phase 1 re-run was **104 entries in 5h10m ≈ 20 entries/hour**. At that rate:

| Target n | Additional hours (approx.) |
|---|---|
| 400 | ~15 more hours |
| 1,000 | ~45 more hours |
| 1,545 (the Task 2 target) | ~72 more hours (~3 days) |

Decide how much of that to spend; none of it is mandatory to make this
report useful — even a partial run tightens the bootstrap CI in §3.

### Rule 1 — Config must not change, with exactly one exception

Any change to entry gates, exit thresholds, or `max_concurrent_positions`
alters the data-generating process and makes new data **non-poolable** with
the existing 103 trades — you would not be able to combine the two windows
for a bigger n, only replace one with the other. **Do not** raise
`max_concurrent_positions` to collect trades faster; it is the single
largest lever in this whole system (it discarded 55.6% of the candidate
funnel in the prior window) and changing it is explicitly a separate,
future decision — not something to fold into this run.

### Rule 2 — The one permitted change, and its trap

Raise `shadow.horizon_seconds` from 300 to (e.g.) 900. This extends
*observation* only — `exits.timeout_seconds` stays 300, so real entries and
exits are completely unaffected, and the resulting trade data still pools
cleanly with the existing 103. **This is the only way to learn whether the
tail develops after 5 minutes** (§4's open question above).

**The trap:** peak shadow concurrency was ~23 against `max_tracked: 50` at a
300s horizon. Roughly tripling the horizon to 900s roughly triples how long
each mint stays tracked, so peak concurrency would rise to roughly **~69** —
past the current cap. `shadow.py`'s `track()` drops mints once the tracker
is full, **before** the sampling roll — so once saturated, the "bought" arm
(sampled at 1.0, per `shadow.py`'s own docstring) could start silently
losing entries, and the sample stops being random. **If you raise the
horizon, raise `max_tracked` with it — to roughly 150 for a 900s horizon.**
This is the single most important operational detail in this section; get it
wrong and the extended-horizon data is unusable for exactly the question it
was collected to answer.

Note the asymmetry this creates: the *trade* data (entries, exits, modelled
multiples) pools cleanly across the horizon change since trading behavior is
untouched. The *shadow forward-return* comparisons (rejected/skipped arms
vs. bought) need more care, since the observation window itself gets longer
— treat any adverse-selection comparison spanning the change as two windows,
not one, even though the exit-ladder replay data can be pooled.

### Rule 3 — Credit budget

The 5h10m re-run used roughly `19,278` credits (from the ledger's own
`credits_spent_today` reconciliation log) against a `daily_credit_halt` of
9,000,000 — under 0.3%. At a 900s horizon and ~3x the tracked-mint count,
expect shadow-polling credit burn to roughly scale with tracked count (a
rough 3x on the shadow-polling component specifically, not the whole budget)
— still trivially within headroom even over several days.

### What to check after the run

- **Halted-time percentage.** Compare against the two prior windows'
  67.7% (first attempt) and 1.6% (re-run) — should stay low; a return to
  frequent halting would indicate a regression, not new information about
  the tail.
- **Orphan count.** Should stay 0, matching every prior window.
- **Bought-arm n.** Compare against the §5 throughput table above to gauge
  progress toward the tail-event target.
- **Whether shadow saturation was reached** — check the peak-concurrency
  estimate the same way the Phase-1 evidence pack did (sweep-line over
  `ShadowPrice` timestamps) against the raised `max_tracked`. If it
  saturated anyway, say so before drawing any conclusion from the extended
  window — a saturated sample is not a random one.

---

## 6. What this does not establish

- **The 300-second horizon wall.** Every figure in this report — the sweep,
  the tail diagnostics, the cap test — is bounded by 300 seconds of recorded
  price data per mint. Section 4's finding that loosening the cap doesn't
  help is a finding about this horizon specifically, not about the
  strategy's tail behavior in general. Nothing here says what happens to a
  position after 300 seconds, because no such data exists yet.
- **The Section 2.3 fidelity caveat.** The prior task's "100% exit-reason
  match" is real but should not be read as certifying the price-path
  reconstruction: 100 of 103 recorded exits are reproduced by construction
  (`creator_sold` is fed in from its recorded timestamp; `timeout` is just
  the clock). Only 3 exits in the whole dataset were genuinely
  price-driven, so the curve-reconstruction machinery is validated on n=3,
  not n=103. The baseline replay's mean/median matching the (corrected)
  ground truth closely is real evidence, but it is not the same claim as
  "100% fidelity," and this report does not repeat that framing.
- **The 15-second vs. 2-second resolution gap.** Still live, still
  one-sided (can only understate threshold hits and add noise to
  time-triggered exits' modelled prices — see the prior exit-retune report's
  §1 for the specific worked example).
- **No capital was ever at risk** in producing any figure in this report —
  every "buy," "sell," and modelled multiple comes from
  `simulateTransaction`-derived price paths and curve arithmetic, never
  execution.
- **This is not a go-live recommendation, and none is implied.** The
  headline finding — median and mean disagree sharply, the mean is
  statistically indistinguishable from breakeven at this n, and the tail
  needs roughly 15x more data to characterize — is evidence about
  measurement uncertainty, not a verdict on whether real trading should be
  enabled. That decision belongs to the operator alone.

---

## 7. Acceptance checklist

| # | Item | Result |
|---|---|---|
| 1 | Ranking metric is a parameter; `find_rank`/`split_half_stability` no longer hardcode `median_multiple` | **PASS** — both take `objective: str = "median"`; `test_find_rank_ranks_by_the_requested_objective_not_always_median` demonstrates the median/mean disagreement directly |
| 2 | All four objectives reported over the full grid | **PASS** — §1 |
| 3 | Baseline's rank reported under each objective | **PASS** — §1 table (2,760 / 600 / 600 / 600) |
| 4 | Top-10 overlap between objectives reported | **PASS** — §1 |
| 5 | Trimmed-mean sensitivity reported for baseline and each objective's winner | **PASS** — §3 table |
| 6 | Bootstrap CI on the mean reported, explicit 1.0/~1.01 statements | **PASS** — §3, all three intervals contain both |
| 7 | Tail-event counts at 1.5×/2.0×/2.5× reported as rates | **PASS** — §3 table |
| 8 | Required n for ~30 tail events estimated, used to size Task 4 | **PASS** — §3 (≈1,545), used directly in §5's throughput table |
| 9 | `take_profit_2_multiple` extended with 6.0/10.0/1e9; sweep re-run; skip count reported | **PASS** — §4, 6,000 run / 720 skipped |
| 10 | Cap result reported under each objective, §2.4 horizon limit stated in that section | **PASS** — §4 |
| 11 | `1e9` uncapped case is config-only, no `positions.py` special case | **PASS** — `test_extended_sweep_grid_includes_the_uncapped_value_and_disables_via_config_only`; `git diff` confirms `positions.py` untouched |
| 12 | `positions.py`, `config.yaml`, `ledger.py`, `report.py` unmodified | **PASS** — verified via `git diff --stat` before each commit |
| 13 | Every headline figure carries an uncertainty statement | **PASS** — every table in §1–4 is paired with a stability/CI/sample-size caveat in prose |
| 14 | §2.3 fidelity caveat restated; "100% fidelity" not presented as validating price machinery | **PASS** — §6, explicit |
| 15 | Task 4 instructions written, self-contained, incl. poolability rule, `max_tracked` trap, throughput arithmetic, credit estimate | **PASS** — §5 |
| 16 | `.claude/reports/tail-analysis.md` written with "What this does not establish" | **PASS** (this file, §6) |
| 17 | No modelled figure in any ledger file; no new PnL-like field in `ledger.py` | **PASS** — all output is stdout/markdown; `ledger.py` untouched |
| 18 | `data/` still gitignored, no ledger/mint/wallet data committed | **PASS** — verified via `git status` before each commit |
| 19 | `uv run pytest` passes; new test count stated; ruff shows no new findings | **PASS** — 263 tests pass (was 252, +11 new), ruff still exactly the 8-item baseline |
| 20 | `DRY_RUN` never touched; `.env`/`wallet.json` never read; no trading-loop run; no go-live recommendation | **PASS** |

---

## 8. What to report back (handoff §6.3)

1. **Does the baseline's rank change materially between median and mean?**
   Yes, dramatically: 2,760/3,120 (bottom 12%) under median vs. 600/3,120
   (top 19%) under mean/total_return/net_mean.
2. **Does the bootstrap CI on the mean contain 1.0? Contain 1.01?** Yes to
   both, for baseline and for every objective's winner examined. This data
   cannot currently distinguish an edge from breakeven from a small loss.
3. **Does loosening/removing the 2.5x cap improve the mean, and does the
   300s horizon bind before the cap does?** No, it makes the mean worse for
   the best-performing shape found. The horizon does bind in the narrow
   sense that only 2 of 103 trades ever exceed 2.5× within it — but the
   mechanism isn't simply "the horizon prevents reaching the cap," it's that
   riding past the cap exposes the position to giving back gains before
   whatever eventually closes it fires, even within the same window.
4. **Roughly what n is needed for ~30 tail events?** ≈1,545 trades at the
   observed 1.9% rate for >2.0× — about 77 more hours at current throughput.
5. **Recommended horizon and `max_tracked` for the next run:** 900s horizon
   (3x current), `max_tracked` raised to ~150 (3x current, to stay under the
   now-~69 estimated peak concurrency) — see §5, Rule 2.
6. **Anything in §1/§2 found wrong:** Yes — §1.1's headline mean (1.0353)
   was leg-weighted, double-counting 4 trades' partial take-profit-1 fills.
   The correct trade-level ground truth is ≈1.0115–1.0134 depending on
   whether it's read from the raw ledger or this harness's replay. The
   qualitative finding (median/mean disagreement, tail fragility) holds; the
   magnitude was overstated. See §0.
7. **Acceptance checklist:** §7 above, all PASS.
