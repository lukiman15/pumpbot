# Deferred: exit threshold retune

Recorded per MILESTONE-4-HANDOFF.md Section 2.2/Task 6, so this decision
doesn't get quietly lost.

## The question

Is a 2.5x cap and a -35% stop the right *shape* at all? The PRD notes the
current config sits between two coherent strategies and executes neither:

- A **tail strategy** needs many concurrent positions and uncapped upside.
  This bot has `max_concurrent_positions: 1` and a hard 2.5x cap
  (`take_profit_2_multiple`).
- A **grind strategy** needs a tight stop and quick small profits. This bot
  has a -35% stop (`stop_loss_fraction`).

Peer post-mortems suggest -15% and 35% respectively; that is someone else's
data, not this project's.

## Why deferred

n=1. There is essentially no accumulated ledger data (`data/` does not exist
in a fresh checkout, and the only real completed round trip in this
project's history is a single trade). Retuning thresholds against one
observation is fitting noise, which is precisely the failure mode the
ledger (Milestone 2) was built to prevent.

Milestone 4 changed the *mechanisms* a position can react to (realizable-
proceeds pricing, trailing drawdown, creator-sell detection) but deliberately
left every existing threshold value untouched:
`take_profit_1_multiple`, `take_profit_1_fraction`, `take_profit_2_multiple`,
`stop_loss_fraction`, `timeout_seconds`.

## Trigger to revisit — addressed offline, 2026-09-02

The `>= 30 real closed trades` trigger below was circular: it required going
live to learn whether going live was sensible. That circularity was broken by
replaying the question offline against 103 recorded dry-run price paths, at
zero capital risk. See `.claude/reports/exit-retune.md` for the full
analysis (harness: `scripts/backtest_exits.py`).

**Headline result:** the replay harness reproduced all 103 recorded outcomes
exactly (100% match rate — well inside the trustworthy band). Sweeping the
pre-registered grid found **no combination where a majority of trades clear
even the net-of-base-fee breakeven multiple** (best case: 12.6% of trades,
across the whole 3,120-combination grid). The current config's own result
ranks 2,760th of 3,120. This is a genuine null result, not an underpowered
one — n=103 clears the project's 30-trade floor by a wide margin.

The deeper reason the sweep barely moves the needle: 97% of recorded exits
(100 of 103) were `creator_sold` or `timeout`, not a price-threshold rung.
Retuning `take_profit_1_multiple`, `take_profit_2_multiple`, or
`stop_loss_fraction` has limited leverage while creator behavior and the
timeout clock decide almost everything. **What remains genuinely dependent
on live data, and this offline analysis cannot resolve:**

- Real slippage and fill rate — this bot's own real buy/sell orders were
  never sent; the replay's "realizable proceeds" assumes a fill at the
  modelled curve state, not what actually happens under real network
  competition.
- Whether `max_concurrent_positions: 1` (which throttled 55.6% of the
  candidate funnel in the same window) is a bigger lever than any exit
  threshold — flagged as the obvious next question, explicitly out of scope
  for this analysis.
- Anything past the 300-second shadow horizon, which this data cannot see
  at all.

The original `>= 30 real closed trades` trigger, preserved for reference:

`scripts/report.py` reports **>= 30 real closed trades**. This is the same
bar `trading.min_closed_trades_before_sizeup` already encodes for position
sizing (Milestone 3) -- reused deliberately, so this project doesn't end up
arguing about which of two evidence bars applies.

## What to look at when the trigger fires

- The exit-reason breakdown and the new `=== Exit mechanism ===` section
  from Milestone 4 Task 5 (`scripts/report.py`) -- in particular the
  trailing-stop giveback (mean peak multiple vs mean realized multiple) and
  whether creator-sell exits are systematically worse than the all-trades
  mean (a late signal, not a broken one).
- The shadow log's adverse-selection comparison (`=== Adverse selection ===`),
  which shows what rejected and bought candidates did over the same horizon.

## The confound

Milestone 4 Task 1 changed what these thresholds *measure*: exits now
decide on realizable proceeds (after both the buy and sell fee, and both
sides' price impact) rather than marginal spot price. This makes the
existing thresholds effectively slightly tighter on the upside and slightly
looser on the downside than they were pre-Milestone-4, even with the numbers
unchanged. Only trades closed *after* Milestone 4 are comparable to each
other -- do not pool them with anything from before it.
