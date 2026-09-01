# pumpbot

pump.fun new-mint sniper, v1 scope (see project plan for full rationale). This
is an instrumentation run at 0.001 SOL/position, not a profit-seeking one —
fees structurally exceed any plausible edge at this size.

## Setup

```bash
uv sync --extra dev
cp .env.example .env
# fill in QUICKNODE_HTTP_URL / QUICKNODE_WSS_URL
# generate a burner wallet and point WALLET_KEYPAIR_PATH at it
```

## Phase 0 — run this first

Nothing else in this repo should be trusted or extended until this passes:

```bash
python scripts/probe.py --hours 1
```

Read the printed report for:
1. Buy-queue rank (are we even competitive for a 1.5x scalp?)
2. The live account ordering pulled from a real `buy` transaction, to
   compare against `pumpbot/program.py` before writing `executor.py`'s
   instruction encoding.
3. Fee-drag rejection rate against `config.yaml`'s `fees.*` thresholds — if
   this rejects almost everything, do not proceed to build the trading
   loop under this configuration.

## Real signing/submission: built, not yet wired into the live loop

`src/pumpbot/submit.py` is the first piece of the actual-send path:
`get_recent_blockhash` (real `getLatestBlockhash`, `confirmed` commitment),
`sign_transaction` (real `Keypair.new_signed_with_payer` signing --
single-signer only, since this project is never CPI'd the way the
historical live samples in `program.py` were), `send_transaction` (real
`sendTransaction`), `confirm_transaction` (polls `getSignatureStatuses`
until confirmed, raising `OnChainFailureError` immediately on an on-chain
error or `ConfirmationTimeoutError` if nothing is ever observed), and
`send_and_confirm` wiring all four together. Covered by
`tests/test_submit.py` against a scripted fake RPC -- no real network call,
no real funds, in any test.

This is a genuinely different kind of action from everything else in this
repo: everywhere else, `simulateTransaction` is read-only and nothing is
ever signed or broadcast. `submit.py` CAN move real SOL on mainnet.
Deliberately, nothing in `main.py`'s current `DRY_RUN=true` path calls it
yet -- wiring it into `run_trader`/`run_position_monitor` and flipping
`DRY_RUN=false` is a separate step for whoever operates this bot to do
themselves, not something this codebase should do on its own. `main()`
still refuses to start with `DRY_RUN=false` until that wiring exists.

Still needed before that wiring makes sense:
- ATA lifecycle: `submit.py` doesn't yet decide *when* to close a position's
  ATA after a full exit (rent recovery) -- that's a `main.py`-level policy
  question, not a `submit.py` one.
- Blockhash-expiry handling: `send_and_confirm` currently just waits out
  `confirm_timeout_seconds`; it doesn't yet check `lastValidBlockHeight` to
  know when a transaction can no longer land at all (vs. hasn't landed
  *yet*) and decide whether to resubmit with a fresh blockhash.
- Real integration into `run_trader`/`run_position_monitor` in place of the
  `_simulate` calls, once the two items above exist.

## Status

- [x] Scaffold, `config.py`, `rpc.py`, `program.py` (constants, PDAs, and the
      full verified account ordering for both `buy` and `sell`)
- [x] `scripts/probe.py` (Phase 0 gate) — passed with a clear GO signal
- [x] `curve.py`, `listener.py`, `filters/tier1.py`, `positions.py`,
      `heartbeat.py`, `executor.py`, `main.py` — all built, tested, wired
- [x] `main.py` runs the full pipeline live in dry-run mode: real mints from
      PumpPortal, real Tier1 filtering, real bonding-curve sizing, and every
      buy/sell decision built into a real instruction and checked with
      `simulateTransaction` (read-only, zero risk — nothing is signed or sent)

## Resolved: the "RPC propagation lag" was a missing `commitment` parameter

Live dry-run testing first surfaced what looked like a real infrastructure
problem: a brand-new mint's account was routinely not visible via
`getAccountInfo` at the moment PumpPortal notified us of it — 100% of
candidates across multiple live runs, not intermittent. The initial
hypothesis was QuickNode-specific replica lag, so a second provider
(constant-k's Kaldera/Nexus plan, a bare-metal Geyser/shred-fed RPC) was
brought in to test side-by-side.

Racing both providers on real new mints showed near-identical ~10s lag on
*both* — QuickNode was actually marginally faster. That ruled out "bad
provider" and pointed at something both shared: every `getAccountInfo` and
`simulateTransaction` call in this codebase omitted the `commitment`
parameter, which defaults to `finalized` — the level that genuinely does
take ~10s+ to reach, regardless of node quality. Explicitly requesting
`confirmed` commitment made the same account visible within ~0.1s of
PumpPortal's own notification, measured repeatedly. Fixed in
`executor.resolve_token_program_id`, `main._simulate`, and the position
monitor's curve-state refresh.

A second, unrelated bug surfaced once mints became visible: simulating the
bare `buy` instruction failed with `AccountNotInitialized` on
`associated_user`, since the wallet's own token account for a brand-new
mint has never been created. Real pump.fun clients bundle an idempotent
create-ATA instruction ahead of `buy` in the same transaction; `main.py`
now does the same.

With both fixed, buy simulations now reach pump.fun's own program logic
and fail (or succeed) on legitimate business-logic grounds instead of
infrastructure noise — see "Next: slippage tolerance" below for the
current failure mode observed there.

No RPC provider change was needed in the end; the constant-k trial was
useful for isolating the cause (ruling out "bad node") but the actual fix
was two lines of request parameters.

## Resolved: slippage tolerance, and buy's mystery account [16]

`config.yaml` now has `trading.slippage_tolerance_fraction` (default 5%),
padding buy's `max_sol_cost` up and sell's `min_sol_output` down (derived
from `curve.tokens_to_sol()` against live curve state) instead of using an
exact, zero-buffer target. Wired into `main.py`'s `_simulate_buy` /
`_simulate_sell`.

Fetching pump.fun's actual IDL (not just the docs site) also identified the
Anchor error codes seen while testing this: `Custom: 6000` is
`NotAuthorized`, `Custom: 6002` is `TooMuchSolRequired` (real slippage),
and a live simulate surfaced a third, `Custom: 6074` /
`InvalidBondingCurveV2` — "bonding_curve_v2 remaining account is missing or
invalid." That error message directly named the account this project had
been calling "unresolved account [16]" since Phase 0 and had only ever
confirmed as *safe to submit an arbitrary value for* (via simulation
against an already-established mint) — never actually identified. Brand
new mints require the real value: `derive_bonding_curve_v2_pda(mint)`,
seeds `[b"bonding-curve-v2", mint]`. Confirmed two ways — it reproduces
this project's own committed historical buy sample's account [16] exactly
(impossible by chance for a 32-byte PDA), and live `simulateTransaction`
against two different brand-new mints passes `InvalidBondingCurveV2`
cleanly when given this value. Sell's analogous slot (account [14]) is
**not** resolved by the same formula — a real, checked mismatch against
this project's committed historical sell sample, not just unverified — and
is left alone rather than guessed.

## Resolved: fee_recipient authorization is gated by PumpPortal's `is_mayhem_mode` flag

With slippage and `bonding_curve_v2` both fixed, live buy simulations
against brand-new mints still intermittently failed with `NotAuthorized`
(`Custom: 6000`) at pump.fun's own `fee_recipient.rs:19`. Two things were
ruled out first: it isn't which pool the address is drawn from
(`pick_fee_recipient` now only draws from `NORMAL_FEE_RECIPIENTS`, since
`RESERVED_FEE_RECIPIENTS` consistently failed authorization — that fix
stands), and it isn't which specific Normal address is picked (all 8 give
the same result against a given mint).

An initial 8-mint batch script aimed at finding the real cause produced
100% spurious `IncorrectProgramId` errors — traced to a connection-reuse bug
(rapid back-to-back differently-shaped RPC calls on one shared connection),
*not* a real pump.fun/pumpbot issue, and that batch's data was discarded.

A corrected, paced re-run (delay between every RPC call, fresh `RpcClient`
per mint, and capturing PumpPortal's full create-message payload alongside
each result) against 6 fresh live mints found a clean predictor:
PumpPortal's `is_mayhem_mode` flag. Both `is_mayhem_mode: false` mints
passed fee_recipient authorization cleanly (one then hit an unrelated
`Custom 6063` further downstream); all three `is_mayhem_mode: true` mints
failed with the exact `NotAuthorized` (`Custom 6000`) error at
`fee_recipient.rs:19`. 6/6 consistent. This is a *different* correlation
than the one already ruled out for token-program selection (see
`program.py`'s "Token-2022 finding" — that check found `is_mayhem_mode`
does NOT predict which token program a mint uses; this is a separate
question it does predict).

Mayhem-mode mints evidently require a different, gated fee_recipient this
project's plain wallet isn't authorized for. Rather than guess at what that
fee_recipient might be, `filters/tier1.py`'s `Tier1Filter` now rejects
`is_mayhem_mode=true` candidates outright (`RejectionReason.
MAYHEM_MODE_UNAUTHORIZED`) before they ever reach a buy simulation — see
`Candidate.is_mayhem_mode`'s docstring. Wired through `listener.py`'s
`NewMintEvent`/`event_to_candidate`.

## Resolved: sell's account [14] is confirmed unvalidated (identity still unknown)

The last open account-ordering item. ~10 PDA/ATA hypotheses were tried
against this module's committed historical sell sample (every plausible
seed combination of mint/user/creator, including buy's exact
`bonding_curve_v2` formula and per-(user,mint) variants) — none reproduced
it. Rather than keep guessing, this was resolved the same way buy's `[16]`
was: force an error and read pump.fun's own program logs.

A live sell `simulateTransaction` against a brand-new mint (with the
create-ATA instruction bundled first, since a fresh wallet has no token
account for a mint it's never bought), with a completely random, unrelated
pubkey placed at `[14]`, ran straight past that slot with **zero error**
and proceeded into real business logic — `NotEnoughTokensToSell` (`Custom:
6023`), the genuine balance check. Confirmed twice independently, on two
different brand-new mints with two different random pubkeys.

This closes the caveat from the earlier pass: the *old* "safe to submit
anything" claim for this slot was tested only against an already-established
mint, before the discovery that buy's equivalent slot (`bonding_curve_v2`)
turned out to matter specifically for brand-new mints. Sell's slot does
**not** have that same brand-new-mint-specific behavior — it's confirmed
unvalidated by the current on-chain program regardless of mint age. Its
real identity is still unknown, but that no longer matters for safety: an
arbitrary value is verified safe to submit for a real sell.
