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

## Next: slippage tolerance on buy's `max_sol_cost`

With the above fixed, live buy simulations now fail with pump.fun's own
custom Anchor errors (`Custom: 6000` / `Custom: 6002`, seen at the `buy`
instruction itself) instead of infrastructure errors. The likely cause:
`max_sol_cost_lamports` is currently set to the position size with zero
slippage buffer, so any price movement between sizing the trade and the
simulated/real execution (even a few hundred milliseconds) can push the
required cost past an exact cap. `config.yaml` has no slippage-tolerance
setting yet — `_simulate_sell` already flagged the equivalent gap on the
sell side (`min_sol_output_lamports=0`, a placeholder, not a real floor).
Next step: add a configurable slippage tolerance and pad both sides
accordingly, then confirm the exact meaning of 6000 vs 6002 against
pump.fun's error list if a copy can be found, rather than guessing.
