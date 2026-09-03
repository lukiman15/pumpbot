# Exit-Threshold Retune — Offline Replay Against Recorded Dry-Run Data

**Pure offline analysis.** No run of `python -m pumpbot`, no network access,
no capital risk of any kind. Replays 103 closed dry-run trades from
`data/ledger-2026-09-02.jsonl` and `data/ledger-2026-09-03.jsonl` (the Phase
1 re-run window, 2026-09-02 17:57:51 → 23:07:53 EDT) through the real
`Position`/`evaluate_exit` decision function via
[`scripts/backtest_exits.py`](../../scripts/backtest_exits.py).

---

## 1. Fidelity result (Task 2)

**Match rate: 100.0% (n=103) → TRUSTWORTHY band (≥90%).**

Every recorded exit reason reproduced exactly:

| Recorded → Replayed | Count |
|---|---|
| creator_sold → creator_sold | 58 |
| timeout → timeout | 42 |
| take_profit_2 → take_profit_2 | 2 |
| trailing_stop → trailing_stop | 1 |

No mismatches in either direction — no missed rungs (the expected §2.5
resolution-gap direction) and no spurious extra fires (which would have
indicated a units/logic bug). The harness is trustworthy; the sweep below is
reported without a leading caveat about the fidelity gate itself, though the
resolution and horizon caveats (§7) still apply to every number.

**One nuance found beyond the fidelity gate itself:** among the 103 matched
trades, |replayed − recorded| modelled multiple has median 0.0000 but a max
of **0.7691** on one trade. Investigated: that trade's exit reason was
`creator_sold`, a *time*-triggered condition (fixed at the timestamp the
live position monitor detected the creator's ATA balance drop), not a
price-triggered one. The live run detected it via 2-second-resolution
polling; the replay can only price it at the nearest 15-second shadow tick.
Both replay and live agree on *when* creator_sold happened and *that* it
happened — they disagree on the price at that instant, because the two loops
sample price at different resolutions. This means the resolution gap costs
more than missed threshold hits (§2.5's headline concern): it also adds
noise to the modelled multiple *of already-correctly-classified* trades,
specifically for time-triggered exits. Reported here because it materially
widens the uncertainty band on any single-trade modelled multiple, even
though it did not move the match rate.

---

## 2. Parameter sweep (Task 3)

Grid run: **3,120 combinations** (720 skipped for `take_profit_2_multiple <=
take_profit_1_multiple`, exactly as pre-registered).

**Baseline** (current `config.yaml` values — tp1=1.5/50%, tp2=2.5,
stop=−35%, trailing arm=1.3, timeout=300s):

- median modelled multiple: **0.9800**
- mean: 1.0134
- fraction above gross breakeven (1.0): 13.6%
- fraction above net-of-base-fee breakeven (~1.01, §5): 8.7%
- **rank: 2,760 of 3,120** — below the middle of the pack

**Top of the surface** (by median modelled multiple): a large tie cluster at
median=0.9800, all built from `take_profit_1_fraction=0.0` (i.e.
take_profit_1 fires but sells nothing — functionally a no-op),
`take_profit_2_multiple=1.5`, `stop_loss_fraction=-0.15`, and varying
trailing/timeout values that barely move the result. The single best cell by
mean (1.0051) trades a wide `net_breakeven` fraction of only 9.7–11.7%.

**Headline finding, stated as the null result it is:** across the entire
3,120-combination grid, **no combination clears net-of-base-fee breakeven
for a majority of trades.** The best cell in the whole grid reaches only
**12.6%** of trades above net breakeven. This is not "the best combo we
found was mediocre" — it is "nothing in a 3,120-point pre-registered grid
works," which is a real and useful finding, not a set-up for picking a
best-of-a-bad-set recommendation.

**Why the sweep barely moves the needle:** 100 of 103 recorded trades (97%)
exited on `creator_sold` or `timeout`, not a price-threshold rung (only 3
ever reached `take_profit_2` or `trailing_stop`). Retuning
`take_profit_1_multiple`, `take_profit_2_multiple`, or `stop_loss_fraction`
has limited leverage on the aggregate outcome while creator behavior and the
timeout clock decide almost everything. This explains the wide tie clusters
at the top of the table — many combinations differ only in parameters that
rarely get exercised at this sample.

---

## 3. Split-half stability (Task 3, §3.5)

Trades split by entry time: first half n=51, second half n=52 (both above
the 30-trade floor — not insufficient sample).

- First half's #1 combo (`tp1_frac=0.0, tp2=1.5, stop=-15%, trailing_arm=1.15,
  timeout=60`) ranks **#1 in the second half too** (median ties at 0.98 in
  both).
- But of the first half's full top-5 list, only **1 of 5** combos also
  appears in the second half's top-5 — the top-5 lists otherwise diverge
  (the second half's top-5 is the same combo with the `timeout_seconds`
  value varied, since — per §2 above — timeout barely matters here).

**Stated plainly:** the #1 spot replicates, but that replication is
consistent with the tie-cluster explanation in §2, not evidence of a robust
best strategy — a huge number of combinations are statistically
indistinguishable at n≈52 per half, so "the #1 combo agreeing across halves"
mostly demonstrates that the same *kind* of no-op-tp1, low-stop,
low-tp2 shape wins by default whenever creator_sold/timeout dominate the
sample, not that this project has found a genuinely superior exit ladder.

---

## 4. The creator-sold counterfactual (Task 4)

58 trades exited on `creator_sold` live. Replayed with it suppressed
(letting the price ladder decide against the same recorded price path,
baseline config):

| | median modelled multiple |
|---|---|
| With `creator_sold` (as recorded) | 0.9736 |
| Without `creator_sold` (price ladder only) | 0.9663 |

Without creator_sold, the counterfactual exit-reason distribution is
`{timeout: 49, trailing_stop: 4, take_profit_2: 2, horizon_end: 3}` — the
`horizon_end: 3` cases are trades where even the full 300s of recorded
shadow data never reached a price-threshold rung, so no counterfactual exit
reason exists for them at all (they are excluded from further threshold
analysis, not silently assigned one).

**Direction:** suppressing `creator_sold` produces a slightly **lower**
median (0.9663 vs 0.9736) — i.e., in this sample, reacting to a creator sale
immediately was marginally *better* than waiting for the price ladder,
though the gap (0.7 percentage points at the median) is small relative to
the noise this analysis carries (§1, §7) and should not be read as a
confident verdict either way. The mean tells a different, noisier story
(1.0304 without vs unreported-but-similar-scale with) driven by a few large
counterfactual outliers — median is the more trustworthy summary here given
the same skew concerns `scripts/report.py` already guards against elsewhere
in this project.

**Censoring caveat (Section 2.7), stated explicitly:** the prices after a
real creator sale are real observations — the shadow tracker kept polling
regardless of what the live position monitor decided. But whether the
creator sold *more* after that first detected sale, or whether other
holders reacted to information this analysis cannot see, is unobserved. This
counterfactual assumes no further unobserved creator behavior and cannot
model any market-impact difference between this bot selling and not
selling. Treat the direction above as suggestive, not conclusive.

---

## 5. Gross vs. net breakeven (Section 2.10)

`tokens_to_sol` already deducts the 1% pump.fun trading fee on both legs, so
a modelled multiple of 1.0 is breakeven **on trading fees only**. At
`position_sol: 0.001` SOL, a two-signature round trip at 5,000 lamports each
is 1.0% of the position, so the **net-of-base-fee breakeven multiple is
~1.0100**, not 1.0. Every figure in §2 above is reported both ways
(`frac_above_gross_breakeven` / `frac_above_net_breakeven`); the gap between
them (13.6% → 8.7% at baseline) is not noise — it is real capital the base
transaction fee alone removes from the sample of trades that would otherwise
have looked profitable.

This excludes ATA rent (recoverable on close, float during the trade) and
priority fee (`priority_fee_sol: 0.0` throughout this window) — neither is
modelled here at all.

---

## 6. Deviation from the handoff

The handoff specified landing Tasks 1–3 as three separate commits so a bad
step could be reverted independently. In this implementation, Tasks 2 and 3
(baseline validation, parameter sweep) are analysis functions that operate
on Task 1's harness within the same file (`scripts/backtest_exits.py`) —
they are not separate code artifacts, and there is no natural file boundary
to split across commits without writing the script three times over. Landed
as one commit (`ce9b5c6`) instead, with this deviation flagged here per the
handoff's own instruction not to substitute silently.

---

## 7. What this does not establish

- **15-second resolution bias.** The replay runs 7.5× coarser than the live
  position monitor's 2-second poll. This can only *understate*
  take-profit/stop/trailing hits (§1's confusion matrix shows zero such
  misses at this sample, but the bias remains structurally one-sided) and,
  per §1's finding, adds real noise to the modelled multiple of individual
  time-triggered exits even when correctly classified.
- **300-second horizon wall.** There is no recorded price data past 300
  seconds for any mint. Timeouts shorter than 300s are testable; nothing
  about a longer timeout can be extrapolated from this data, and this
  report does not attempt to.
- **No real fills, no real slippage.** Every "buy," "sell," and "modelled
  multiple" here comes from `simulateTransaction`-derived price paths and
  curve arithmetic, not execution. This says nothing about whether a real
  order would have filled at the modelled price, filled at all, or filled
  under real priority-fee competition.
- **No capital was ever at risk** in producing any number in this report.
- **`max_concurrent_positions: 1`** is a larger lever than anything swept
  here (it discarded 55.6% of the candidate funnel in the source window)
  and is explicitly out of scope for this analysis — noted as the obvious
  next question, not acted on.
- **This is not a go-live recommendation**, and none is implied. The
  headline null result (§2) is evidence that the currently-defined
  parameter *shape* does not clear even a fee-aware breakeven bar on this
  sample — nothing more, and nothing about whether real trading should ever
  be enabled. That decision belongs to the operator alone.

---

## 8. Acceptance checklist

| # | Item | Result |
|---|---|---|
| 1 | `scripts/backtest_exits.py` exists, runs offline, no network access | **PASS** |
| 2 | Imports `Position`/`evaluate_exit` from `positions.py`, no reimplementation | **PASS** — `from pumpbot.positions import Position` at the top; `evaluate_exit`/`apply_exit` called directly in `replay_trade` |
| 3 | `positions.py` unmodified | **PASS** — `git diff` shows no changes to it this task |
| 4 | Reserve reconstruction from `(k, p)` covered by a round-trip test | **PASS** — `test_reconstruct_curve_round_trips_to_the_same_spot_price`, `test_reconstruct_curve_matches_spot_price_sol_per_token_for_a_moved_curve` |
| 5 | `ShadowPrice.price_sol` ↔ `ExitFilled.curve_price_sol` 1000× relationship asserted | **PASS** — `test_shadow_price_sol_and_exit_filled_curve_price_sol_differ_by_1000x` |
| 6 | Join completeness reported | **PASS** — 0 missing candidate/shadow joins; 1 trade still open (excluded, not silently dropped) |
| 7 | Baseline replay run over all available trades; confusion matrix reported | **PASS** — §1 above |
| 8 | Baseline match rate reported, mapped to §3.3 band, band's consequence applied | **PASS** — 100.0% → TRUSTWORTHY, sweep proceeded |
| 9 | Mismatch direction analysed against §2.5 expectation | **PASS** — zero mismatches; noted the resolution gap still shows up as multiple-level noise on matched trades (§1) |
| 10 | Sweep run over the exact pre-registered grid; skipped count reported | **PASS** — 3,120 run, 720 skipped |
| 11 | Every result cell reports n; cells under 30 marked insufficient sample | **PASS** — full sweep n=103 throughout; split-half n=51/52, both above floor, reported as such |
| 12 | Gross and net-of-base-fee breakeven fractions both reported | **PASS** — §2, §5 |
| 13 | Baseline's rank among all combinations reported | **PASS** — 2,760/3,120 |
| 14 | Split-half stability check run, result stated plainly incl. non-replication | **PASS** — §3, explicitly notes the top-5 overlap is weak evidence given the tie cluster |
| 15 | Task 4 counterfactual run on the 58 affected trades, censoring caveat stated | **PASS** — §4 |
| 16 | 300s horizon wall stated explicitly; no extrapolation past it | **PASS** — §7 |
| 17 | `.claude/reports/exit-retune.md` written, incl. "What this does not establish" | **PASS** (this file) |
| 18 | `DEFERRED-exit-threshold-retune.md` updated; circular trigger addressed | **PASS** |
| 19 | No modelled figure in any ledger file; no PnL field added; `report.py`'s real-trade path untouched | **PASS** — `git diff` shows no changes to `ledger.py` or `report.py`'s `load_closed_real_trades` path; all output goes to stdout / this markdown file |
| 20 | `config.yaml` unmodified | **PASS** |
| 21 | `uv run pytest` passes; new count stated; ruff shows no new findings | **PASS** — 252 tests pass (was 242; +10 new), ruff still exactly the 8 pre-existing findings |
| 22 | `data/` still gitignored, no ledger/mint/wallet data committed | **PASS** — verified via `git status` before writing this file; only `scripts/backtest_exits.py`, `tests/test_backtest_exits.py`, and the two `.claude/` docs are new |
| 23 | `DRY_RUN` never touched; `.env`/`wallet.json` never read; no trading-loop run; no go-live recommendation | **PASS** |

**Tests:** 252 passed (242 baseline + 10 new). **Ruff:** 8 findings — same
pre-existing baseline, zero new.
