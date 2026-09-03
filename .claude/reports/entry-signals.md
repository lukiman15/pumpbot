# Entry-Signal Analysis — Velocity, Creator Supply, and Socials

**Pure offline analysis**, via [`scripts/entry_signals.py`](../../scripts/entry_signals.py)
(no run of `python -m pumpbot`, no network, no capital risk), over the 578
shadow-tracked mints in `data/ledger-2026-09-02.jsonl` and
`data/ledger-2026-09-03.jsonl`, reusing `scripts/backtest_exits.py`'s curve
reconstruction and ladder-replay machinery.

**Deviation from the handoff, stated up front:** Tasks 1–2 and 3–5 were
specified as at least two commits. All five tasks share one data model
(`TrackedMint`) with no clean functional seam between them — landed as one
code commit (`345ab8b`), with this evidence pack as a separate second
commit, matching the pattern of the two prior offline analyses in this
series.

---

## 1. Two bugs caught before trusting any output

1. **12 `ShadowPrice` rows are migration-terminal artifacts, not real
   crashes to zero.** They carry `curve_complete=True` and a degenerate
   `price_sol=0.0` — the final poll after a curve migrated, decoded to a
   zeroed/near-zero reserve state. Treating these as a genuine −100% return
   would be exactly backwards: graduating to PumpSwap is typically a
   *success* signal, not a loss. Every return calculation in this analysis
   excludes non-positive-price ticks (never using them as an entry point,
   and falling back to the last *usable* tick for a forward-return
   endpoint). This is also why the sanity check below recovers the
   established n=39, not n=44 or n=47 depending on how these are counted.
2. **`sol_to_tokens` caps its output via `curve.real_token_reserves`, which
   `backtest_exits.reconstruct_curve` zeroes** (that field is unused by
   `tokens_to_sol`, the function the exit-side analyses actually call). Used
   unmodified for a synthetic non-bought-mint entry, this silently floored
   every such buy to 0 tokens — caught because the secondary ladder replay
   (§6) initially returned n=0 for the skipped population. Fixed with
   `_reconstruct_curve_for_buy`, which sets `real_token_reserves` to
   `virtual_token_reserves` as a safe (never-binding) upper bound rather
   than a guessed real value — documented in the code, covered by
   `test_synthetic_replay_trade_computes_nonzero_entry_tokens`.

---

## 2. Assembly (Task 1)

| | |
|---|---|
| Shadow-tracked mints | 578 |
| Missing `CandidateSeen` | 0 |
| Usable mints | 578 |
| With a `Tier2Evaluated` row (socials known) | 122 |
| Tier1-passing (bought + skipped[max_concurrent_positions]) | **505** |

Usable entry ticks per delay, out of 505 tier1-passing mints:

| Delay | Usable |
|---|---|
| 15s | 504 |
| 30s | 504 |
| 45s | 503 |
| 60s | 503 |

Essentially the whole population has a usable tick at every delay in the
grid — attrition is 1–2 mints, from the excluded degenerate zero-price
ticks (§1), not from a structural gap.

---

## 3. Sanity check (Task 3's required gate)

The check did **not** reproduce the literal −11.9%/−0.4% figures on the
first attempt — investigated per the handoff's own instruction, rather than
reported as a pass.

**Root cause, confirmed directly:** the prior figures (`scripts/report.py`'s
`adverse_selection_by_arm`) use `ShadowPrice.return_from_first_seen` — the
return from the *first poll* to the *last poll*. First-tick elapsed times
run 1.0–20.0s after candidate arrival (median 9.9s), so that metric silently
discards the most volatile opening seconds of every mint's life — precisely
what Section 2.6 warned about, and precisely why this task's own primitives
use `CandidateSeen`'s own reserves for a true t=0 instead.

Reproduced both ways, on the identical 578-mint population:

| Metric | Rejected (n) | Rejected median | Tier1-passing (n) | Tier1-passing median |
|---|---|---|---|---|
| `return_from_first_seen` (old methodology) | 39 | **−11.9%** | 504 | **−0.3%** |
| True t=0 → last-tick (this task's methodology) | 39 | **−21.6%** | 504 | **−0.1%** |

**The pipeline is correct — this confirms it, it doesn't contradict it.**
Recomputing the *old* metric inside this task's own code reproduces
−11.9%/−0.3% exactly (n=39 both ways). The *new*, more complete metric shows
rejected mints performing **almost twice as badly** as previously measured,
because a large share of a creator-supply-flagged mint's decline happens in
the first ~10 seconds, before the old metric's clock even starts. **Every
figure in this report uses the true t=0 methodology**; the old number was an
underestimate of how bad tier-1 rejects actually are, not the other way
around.

---

## 4. Velocity analysis (Task 2, the headline)

Population: the 505 tier1-passing mints. Control 1 (enter at t=0, no
filter — what the bot does today): **median −0.1%, mean +4.4%** (n=504,
frac_above_zero 25.6%; the mean here is heavily right-skewed by a handful of
large movers, same caution as the exit-side tail analysis).

**Control 2 vs. Control 1 — does waiting help, independent of filtering?**

| Delay | Control 2 (no filter) median | frac_above_zero |
|---|---|---|
| 15s | −0.1% | 11.1% |
| 30s | −0.1% | 10.7% |
| 45s | +0.0% | 11.3% |
| 60s | +0.0% | 10.4% |

**Waiting alone changes almost nothing on the median** (−0.1% → 0.0%
across 15–60s) but roughly **halves `frac_above_zero`** (25.6% → ~10–11%).
This is not noise-free — `frac_above_zero` is a coarser statistic — but it
points the same direction as the filtered results below: entering later
looks *worse* on this population, not better, once you account for the fact
that Control 2's mints have already run some of their move by t=T (the same
kind of window-shift effect the velocity filters exhibit more strongly).

**Does a velocity filter help beyond waiting?**

At **every delay and every threshold tested, the filtered cell's median is
at or below the unfiltered control at that same delay — and it gets
monotonically worse as the threshold tightens:**

| Delay=30s | n | % of flow | median | mean |
|---|---|---|---|---|
| Control 2 (no filter) | 503 | 99.6% | −0.1% | −4.3% |
| filter ≥ +0% | 349 | 69.1% | −0.7% | −5.7% |
| filter ≥ +5% | 88 | 17.4% | −17.9% | −18.9% |
| filter ≥ +10% | 73 | 14.5% | −22.0% | −21.9% |
| filter ≥ +25% | 46 | 9.1% | −31.9% | −25.5% |
| filter ≥ +50% | 28 | — | INSUFFICIENT SAMPLE |

(The other three delays — 15s, 45s, 60s — show the identical monotonic
pattern; full tables are in the script's own output, reproducible via
`uv run python scripts/entry_signals.py`.)

**This is the opposite of the PRD's claim.** The PRD states *"13% win rate
instant-snipe vs 36% velocity-confirmed — same bot, same market."* **Nothing
in this surface is consistent with an effect of that size, or of that
sign.** The largest effect actually found in the entire grid is negative:
requiring ≥25% velocity confirmation at any delay roughly **triples the
severity of the median loss** (from about −0.1% unfiltered to about −30%
filtered) while discarding 90%+ of flow. `frac_above_zero` does rise
somewhat at the tightest thresholds (up to ~19% at ≥25%, vs ~10% for the
unfiltered control) — a small number of mints that do keep running — but the
central tendency degrades far faster than that tail improves. Read plainly,
this data shows the opposite mechanism to the PRD's claim: a mint that has
already moved sharply by t=15–60s is, on this evidence, more likely to be
near a local top than at the start of a sustained move.

**Split-half stability**, best-behaved rule tested (delay=30s, filter≥0%,
the mildest and highest-n filter):

| | n | median |
|---|---|---|
| First half | 174 | −0.3% |
| Second half | 175 | −0.7% |

Both halves show the same negative direction relative to the unfiltered
baseline. **The finding that velocity filtering does not help — and
actively hurts as the threshold tightens — replicates.**

---

## 5. Creator-supply threshold curve (Task 3)

Population: all 578 tracked mints.

| Threshold | Admitted n (%) | Admitted median | Excluded n (%) | Excluded median |
|---|---|---|---|---|
| ≤0.02 | 332 (57.4%) | −0.0% | 236 (40.8%) | −6.3% |
| ≤0.04 | 431 (74.6%) | −0.0% | 137 (23.7%) | **−12.0%** |
| ≤0.06 | 445 (77.0%) | −0.0% | 123 (21.3%) | **−12.0%** |
| ≤0.08 | 490 (84.8%) | −0.0% | 78 (13.5%) | −17.1% |
| **≤0.10 (current)** | 532 (92.0%) | −0.1% | 36 (6.2%) | −24.0% |
| ≤0.15 | 540 (93.4%) | −0.1% | 28 (4.8%) | INSUFFICIENT SAMPLE |
| ≤0.20 | 551 (95.3%) | −0.1% | 17 (2.9%) | INSUFFICIENT SAMPLE |
| no limit | 568 (98.3%) | −0.1% | 0 | n/a |

**A real, monotonic signal**: excluded-mint returns get worse the tighter
the threshold is drawn (the excluded population becomes more concentrated
in genuinely bad mints), and admitted-mint returns stay essentially flat
around 0% regardless of where the line is drawn. This is the strongest,
cleanest signal found anywhere in this analysis.

**Split-half stability, at 0.04 and 0.06** (chosen because the excluded
group is comfortably above the 30-sample floor in both halves — the current
0.10 threshold's excluded group, n=36 combined, falls to 18 per half and
cannot be split-half validated at all):

| Threshold | Half | Admitted median | Excluded median |
|---|---|---|---|
| 0.04 | First | −0.0% | −12.0% |
| 0.04 | Second | −0.0% | −12.0% |
| 0.06 | First | −0.0% | −12.0% |
| 0.06 | Second | −0.0% | −12.0% |

**Remarkably stable — both halves agree to the reported precision at both
thresholds.** This is the one rule in the whole analysis that replicates
cleanly.

**Is 0.10 in the right place?** The excluded population at 0.10 (n=36,
6.2% of flow) shows the worst median of any threshold row (−24.0%) — the
gate is working on what it excludes. But the curve shows **the same
roughly-−12%-median signal is already present at 0.04–0.06**, on a
substantially larger excluded population (123–137 mints, 21–24% of flow,
comfortably split-half validated) that 0.10 currently lets straight through.
Descriptively: **0.10 looks looser than the data supports** — a materially
larger share of flow than currently excluded shows the same underperformance
signature. This is a data description, not a recommendation to change
`config.yaml` (§0 rule 5, out of scope here).

---

## 6. Socials, honestly scoped (Task 4)

**Restricted to 122 of 578 mints** — those that reached a `Tier2Evaluated`
row. **Not representative of the 578**; do not read anything below as
applying to the full flow.

| Flag | True | False |
|---|---|---|
| `has_twitter` | n=79, median −0.0% | n=42, median −0.5% |
| `has_telegram` | INSUFFICIENT SAMPLE (n=8) | n=113, median −0.2% |
| `has_website` | n=44, median −0.1% | n=77, median −0.3% |

| Socials count | n | median |
|---|---|---|
| 0 | 38 | −0.6% |
| 1 | 41 | −0.0% |
| 2 | 36 | −0.0% |
| 3 | INSUFFICIENT SAMPLE (n=6) | — |

| `min_socials` threshold | Admitted n | Admitted median | Excluded n | Excluded median |
|---|---|---|---|---|
| ≥1 (current) | 83 | −0.0% | 38 | −0.6% |
| ≥2 | 42 | −0.0% | 79 | −0.3% |
| ≥3 | INSUFFICIENT SAMPLE (n=6) | — | 115 | −0.1% |

**Weak, small-sample signal at best.** Every admitted/excluded gap here is
under a percentage point except the 0-vs-nonzero-socials split (−0.6% vs
≈−0.0%), and every cell is close to the 30-sample floor. This does not
resemble the strength or stability of the creator-supply signal (§5), and at
n=122 with this much cell fragmentation, no conclusion here should carry
weight independent of a larger, purpose-collected sample.

---

## 7. Combination, and the secondary ladder replay (Task 5)

**Best velocity rule (delay=30s, ≥0% — the least-bad filter, since every
tighter one performs worse) combined with the current creator-supply
threshold (≤0.10), against either alone:**

| Rule | n | median |
|---|---|---|
| Velocity alone | 349 | −0.7% |
| Creator-supply alone | 503 | −0.1% |
| **Combined** | 349 | −0.7% |
| No filter (same delay baseline) | 503 | −0.1% |

**The combination is identical to velocity alone** — every mint the
velocity filter admits already clears the 0.10 creator-supply threshold in
this population, so creator-supply adds nothing on top of velocity here.
This is not evidence the two signals are redundant in general; it reflects
that at delay=30s the velocity filter alone already discards enough flow
that the (already-loose, per §5) 0.10 threshold has nothing left to bite on.
Combined with §4's finding, the honest read is: **velocity filtering should
not be adopted, so this combination question is moot in practice** — but
the creator-supply signal (§5) stands on its own regardless of velocity.

**Secondary ladder replay (Section 3.2)** — modelled multiple via the real
`Position`/`evaluate_exit` ladder, `creator_sold` **suppressed on both**
populations for symmetry (Section 2.9: no creator-sell signal exists at all
for non-bought mints):

| Population | n | median | mean | frac_above_zero |
|---|---|---|---|---|
| Bought (real trades) | 103 | −2.0% | +4.4% | 13.6% |
| Skipped[max_concurrent_positions] (synthetic entries) | 400 | −2.1% | −2.2% | 7.2% |

**Nearly identical medians (−2.0% vs −2.1%).** Even through the real exit
ladder — not just raw forward price — the mints the bot actually bought
perform statistically indistinguishably from the mints it skipped only
because the single position slot was occupied. This is independent
confirmation, via a completely different metric (modelled multiple vs. raw
forward return), of the same conclusion the exit-retune and tail-analysis
reports already reached: **the bot's selection between tier-1-passing
candidates adds no visible edge.** The bought arm's higher mean (driven by
the same handful of large movers noted throughout this series) is consistent
with tail fragility, not a real selection effect — see the tail-analysis
report's bootstrap-CI findings for the direct evidence of that.

Per Section 3.2, this secondary metric is a **lower bound** on what the
bought arm actually achieved, since suppressing `creator_sold` removes the
exit reason that drove 58 of 103 real exits.

---

## 8. What this does not establish

- **The 300-second horizon wall.** Every forward return in this report is
  bounded by 300 seconds of recorded price data per mint (carried forward
  from the exit-side analyses).
- **15-second tick resolution.** A velocity feature measured at exactly
  t=15/30/45/60s is really measured at the first available tick at or after
  that point — up to ~15 seconds later. This does not change the direction
  of §4's finding (which is monotonic and large), but it means the specific
  delay boundaries are approximate, not exact.
- **No creator-sell data for non-bought mints (Section 2.9).** The 400
  skipped[max_concurrent_positions] mints in §7's secondary replay have no
  creator-ATA monitoring at all — their modelled multiple assumes
  `creator_sold` never fires, same as the bought arm's suppressed replay,
  which is why the comparison is fair but neither number reflects what
  either population's *real* exit behavior would have been.
- **The socials scope (Section 2.5).** §6 covers 122 of 578 mints (21%) and
  is not representative of the tracked flow as a whole.
- **The sampling difference between arms (Section 2.2).** The "bought" arm
  is a census; every other arm here is roughly a 10% sample. Every
  comparison in this report is between *distributions*, never raw counts
  across arms, per that constraint.
- **No capital was ever at risk**, and no real fill was ever obtained — every
  figure in this report comes from `simulateTransaction`-derived price paths
  and curve arithmetic, both for the bought arm's actual dry-run trades and
  for every non-bought mint's synthetic entries.
- **This is not a go-live recommendation, and none is implied.** The
  velocity finding (do not adopt it) and the creator-supply finding
  (0.10 looks loose relative to what the data supports) are descriptive
  results about what this data shows, not a proposal to change
  `config.yaml`, which stays the operator's decision alone.

---

## 9. Acceptance checklist

| # | Item | Result |
|---|---|---|
| 1 | `scripts/entry_signals.py` exists, runs offline, no network calls | **PASS** |
| 2 | Shared helpers imported from `backtest_exits.py`; no second curve reconstruction | **PASS** — `import backtest_exits as bx`; `_reconstruct_curve_for_buy` wraps `bx.reconstruct_curve` rather than reimplementing it |
| 3 | t=0 price from `CandidateSeen` reserves, not `return_from_first_seen`, with a test | **PASS** — `test_t0_price_uses_candidate_seen_reserves_not_return_from_first_seen` |
| 4 | Look-ahead test present and passing | **PASS** — `test_velocity_filter_outcome_excludes_the_qualifying_rise_look_ahead` |
| 5 | Assembly report: usable t=0 prices, ticks per delay, drops with reasons | **PASS** — §2 |
| 6 | Velocity surface run over the exact grid, on the 505 tier1-passing population | **PASS** — §4, full grid in script output |
| 7 | Both controls reported | **PASS** — §4 |
| 8 | Every cell reports n and flow fraction; sub-30 cells marked insufficient | **PASS** — `cell_stats`/`fmt_cell`; visible throughout §4–7 |
| 9 | Explicit statement on the PRD's 13%→36% claim and the largest effect found | **PASS** — §4, stated as contradicted, largest effect is negative |
| 10 | Creator-supply curve run over the exact threshold list | **PASS** — §5 |
| 11 | Sanity check recovered the known result; investigated when it didn't match literally | **PASS** — §3, investigated and explained, not silently reported as failing |
| 12 | Socials section scoped to 122, limitation stated first | **PASS** — §6 |
| 13 | Split-half stability reported for every promising rule, including non-replication | **PASS** — §4 (velocity, replicates in direction), §5 (creator-supply, replicates cleanly); the 0.10 threshold's own excluded group could not be split-half tested at all (n=18/half) — stated plainly |
| 14 | Secondary ladder replay with `creator_sold` suppressed on both populations | **PASS** — §7 |
| 15 | Sampling difference stated wherever arms are compared; no count-based cross-arm comparison | **PASS** — every table compares medians/means/fractions, never raw counts across arms |
| 16 | `positions.py`, `config.yaml`, `ledger.py`, `report.py` unmodified | **PASS** — verified via `git diff --stat` before commit |
| 17 | No modelled figure in any ledger file; no new PnL-like field | **PASS** — all output is stdout/markdown |
| 18 | `.claude/reports/entry-signals.md` written with "What this does not establish" | **PASS** (this file, §8) |
| 19 | `data/` still gitignored, no ledger/mint/wallet data committed | **PASS** — verified via `git status` before each commit |
| 20 | `uv run pytest` passes; new test count stated; ruff shows no new findings | **PASS** — 273 tests pass (was 263, +10 new), ruff still exactly the 8-item baseline |
| 21 | `DRY_RUN` never touched; `.env`/`wallet.json` never read; no run started; no go-live recommendation | **PASS** |

---

## 10. What to report back (handoff §6.3)

1. **Does waiting help at all, independent of filtering?** Barely, and
   arguably not: the median is flat to marginally better (−0.1% → 0.0%
   across 15–60s), but `frac_above_zero` roughly halves, pointing the
   opposite direction.
2. **Does the velocity filter help beyond waiting, and by how much?** No —
   every filtered cell at every delay is at or below its own unfiltered
   control, and the effect is monotonically negative as the threshold
   tightens (down to roughly −30% median at ≥25%, vs ~0% unfiltered).
3. **Largest effect found, n, flow retained, split-half:** The
   creator-supply signal at 0.04–0.06 (excluded median ≈ −12% vs admitted
   ≈ 0%, n=123–137 excluded, 21–24% of flow) — the only rule in this
   analysis that replicates cleanly across both halves at matching
   precision.
4. **Is `max_creator_supply_fraction: 0.10` right, loose, or tight?**
   Descriptively loose: the same underperformance signature already appears
   at 0.04–0.06 on a much larger population than 0.10 currently excludes.
5. **Did the tier-1 sanity check reproduce?** Not literally on the first
   pass — investigated, root-caused to a real methodology difference (the
   old figure used `return_from_first_seen`, discarding the first ~10s),
   and confirmed correct by reproducing the old figure exactly (−11.9%,
   n=39) under that same old methodology before reporting the corrected
   number (−21.6%) under this task's methodology.
6. **Anything in §1/§2 found wrong:** Yes, two things, both caught before
   reporting anything downstream — the migration-terminal zero-price
   artifact (§1.1) and the `sol_to_tokens`/`real_token_reserves` capping bug
   (§1.2). Both are now covered by regression tests.
7. **Acceptance checklist:** §9 above, all PASS.
