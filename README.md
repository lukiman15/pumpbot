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

## Known blocker: RPC propagation lag

Live dry-run testing surfaced a real infrastructure problem, not a code bug:
on the QuickNode endpoint currently configured, a brand-new mint's account is
routinely **not yet visible** via `getAccountInfo` at the moment PumpPortal
notifies us of it — confirmed systematic (100% of candidates in multiple
~30-45s live runs), not intermittent. `executor.resolve_token_program_id`
retries a few times over ~2-3 seconds, but that isn't enough to close the
gap, and Phase 0's own measured buy-queue rank (median 0) means a sniper
can't afford to wait it out the way `scripts/probe.py` does (a full 20s)
without losing all competitiveness anyway.

This is the real remaining blocker before this bot could ever land a
competitive buy — not the unresolved account slot (see `program.py`), which
turned out not to matter. Options, none yet evaluated: a faster/geyser-fed
RPC provider, a different account-visibility strategy (e.g. websocket
`accountSubscribe`, though that's still bounded by the same node's view of
the chain), or accepting a slower entry and re-scoping away from "sniping."
