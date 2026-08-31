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

## Next: fee_recipient authorization looks mint/creator-scoped, not just Reserved-vs-Normal

With slippage and `bonding_curve_v2` both fixed, live buy simulations
against brand-new mints still intermittently fail with `NotAuthorized`
(`Custom: 6000`) at pump.fun's own `fee_recipient.rs:19`. Two things were
ruled out: it isn't which pool the address is drawn from (`pick_fee_recipient`
now only draws from `NORMAL_FEE_RECIPIENTS`, since `RESERVED_FEE_RECIPIENTS`
consistently failed authorization in a controlled test against one mint —
that fix stands), and it isn't which specific Normal address is picked
(testing all 8 Normal addresses against the same mint gives the same
result for all 8). What actually varies is the **mint**: some brand-new
mints reject every Normal fee recipient with `NotAuthorized`, others accept
every one of them (and then correctly proceed to real slippage checks).
Not yet understood — plausibly creator- or mint-config-specific
authorization pump.fun applies before this project's wallet is
"authorized" to buy at all. Not guessed further this pass; next step is
probably decoding the `global` config account or the mint's own bonding
curve account for a field that predicts this, rather than continuing to
sample blind.
