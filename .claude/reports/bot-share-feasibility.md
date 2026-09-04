# Bot-Share Backfill — Task 1 Feasibility Gate

Per `.claude/plans/BOT-SHARE-HANDOFF.md` Section 3. **Result: gate FAILED. Stopping here per
Section 3.2 — no partial analysis, no forward-collection run started, no full backfill run.**

## What happened

- 20 mints were sampled with a fixed seed (`20260904`) from the `4c10b851d9824d24823f359e860c699c`
  run's `ShadowPrice` events.
- For each, the bonding curve PDA was derived with the existing
  `program.derive_bonding_curve_pda(mint)` (no new derivation written), and
  `getSignaturesForAddress` was called through the existing rate-limited `RpcClient`
  (`pool="shadow"`, so a concurrently running bot would not have been affected).
- **All 20 of 20 calls failed with the same error before returning any data:**

  ```
  DailyLimitReachedError: QuickNode daily request limit reached; resets in ~4179s. Not retrying.
  ```

  (JSON-RPC error `-32003`, QuickNode's plan-level daily request cap — this is a different,
  stricter limit than this project's own `rpc.daily_credit_halt: 9_000_000` credit tracker, and
  it was already exhausted before this script made its first call.)

## Why this is not the retention-boundary failure the handoff anticipated

Section 3.2's designed failure mode is **data no longer available** — an RPC provider's
retention window closing on transaction history that is a day old. That is a real, closed
question about whether the data still exists.

What actually happened is different and more mundane: the **account's daily request quota**
was already spent — almost certainly by the extended-horizon shadow run analyzed in the
previous handoff (55,671 ledger events over ~12.4 hours) plus ordinary trading RPC traffic
earlier today, on top of by this feasibility probe's own first call landing after the cap
was already hit. **This tells us nothing about whether the transaction history itself is
still retained** — the gate never got far enough to find out. QuickNode's own documented
reset for this cap is time-based (`resets in ~4179s` ≈ **70 minutes** from the run at the
time of writing) rather than tied to any data-retention boundary.

## Fraction succeeded

**0/20 (0%).** Every call in the sample failed identically before any signature data was
returned. `n_complete=0`, `n_truncated=0`, `n_empty=0`, `n_error=20`.

## What is needed to actually run this gate

Nothing structural — the code path is sound (PDA derivation, rate-limited client, pagination
logic are all in place and unexercised bugs cannot be ruled out but no bug surfaced, since
every call failed identically at the RPC-provider layer before reaching pagination logic).
The only requirement is **waiting for QuickNode's daily request cap to reset** (~70 minutes
from this run) and re-running `scripts/backfill_feasibility.py` with the same fixed seed
(`20260904`) so the sample is identical.

No credit was meaningfully spent: the daily-limit error is raised before the request is
billed, and this project's own credit tracker (`rpc.daily_credit_halt`) was not touched by
this attempt.

## Recommendation to the operator

Per Section 3.2, this task **stops here** rather than retrying in a loop or switching to a
forward-collection alternative on its own initiative, both of which the handoff explicitly
prohibits. Two options once you're ready:

1. **Re-run the gate after QuickNode's daily cap resets.** Command:
   `uv run python scripts/backfill_feasibility.py` (same fixed seed, so results are directly
   comparable to this attempt). If it hits the same daily cap again, that indicates the
   account's plan tier may be too low for this project's combined trading + shadow + backfill
   RPC volume, which is worth knowing regardless of this specific task.
2. **Check whether QuickNode's plan tier needs raising** if this daily cap is being hit
   during normal operation (not just this probe) — that's a capacity question independent of
   whether the bot-share analysis proceeds at all.

Task 2 (`scripts/backfill_trades.py`), Task 3 (`scripts/bot_share_analysis.py`), and Task 4
(the velocity-contradiction check) are **not started**. No code beyond the feasibility probe
itself (`scripts/backfill_feasibility.py`) was written this session for this handoff.

## Constraints honored

- `DRY_RUN` never touched; `.env`/`wallet.json` never read.
- No trading-path code, `ledger.py`, `positions.py`, or `config.yaml` modified.
- `data/backfill_feasibility_sample.json` (raw per-mint results, mint addresses included) was
  written for later debugging — it is gitignored under `data/` and is **not** reproduced here;
  this report contains only aggregate counts.
- No go-live recommendation made.
