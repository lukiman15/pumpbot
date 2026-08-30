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

- [x] Scaffold, `config.py`, `rpc.py`, `program.py` (constants + PDAs only)
- [x] `scripts/probe.py` (Phase 0 gate)
- [ ] Everything else (`curve.py`, `listener.py`, `filters/tier1.py`,
      `executor.py`, `positions.py`, `heartbeat.py`, `main.py`) is
      deliberately not built yet — it depends on Phase 0's output.
