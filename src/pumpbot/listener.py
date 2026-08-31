"""Production PumpPortal new-mint listener.

Subscribes to the free wss://pumpportal.fun/api/data new-token feed (see
scripts/probe.py's Phase 0 rationale for why PumpPortal over QuickNode's raw
logsSubscribe), converts each `create` event into a Tier1 Candidate, runs it
through Tier1Filter, and pushes survivors onto a queue for the executor.

PumpPortal's create message (verified against 2 live samples, see git log):
    {
      "signature": "...", "mint": "...", "traderPublicKey": "...",
      "txType": "create", "initialBuy": 35323.892708,
      "solAmount": 0.000987653, "bondingCurveKey": "...",
      "vTokensInBondingCurve": 1072964676.107292,
      "vSolInBondingCurve": 30.00098765299999,
      "marketCapSol": 27.96..., "name": "...", "symbol": "...",
      "uri": "...", "pool": "pump"
    }
No official schema is published for this -- if PumpPortal changes field
names or types, parse_create_message returns None (see the .get() defaults
below) rather than raising, and the mint is silently skipped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass

import websockets

from pumpbot.curve import TOTAL_SUPPLY_WHOLE_TOKENS, from_virtual_reserves
from pumpbot.filters.tier1 import Candidate, Tier1Filter

logger = logging.getLogger(__name__)

PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"

# How far back to keep mint timestamps for the tier1 mint-rate check.
MINT_RATE_WINDOW_SECONDS = 5.0


@dataclass(frozen=True)
class NewMintEvent:
    signature: str
    mint: str
    creator: str
    name: str
    symbol: str
    initial_buy_whole_tokens: float
    virtual_sol_in_curve: float
    virtual_tokens_in_curve: float
    notified_at: float


def parse_create_message(msg: dict, notified_at: float) -> NewMintEvent | None:
    """Returns None for anything that isn't a well-formed create event --
    non-create messages (subscription acks, trade events), or a create
    event missing the mint address we need to act on it at all."""
    if msg.get("txType") != "create" or not msg.get("mint"):
        return None
    return NewMintEvent(
        signature=msg.get("signature", ""),
        mint=msg["mint"],
        creator=msg.get("traderPublicKey", ""),
        name=msg.get("name", ""),
        symbol=msg.get("symbol", ""),
        initial_buy_whole_tokens=msg.get("initialBuy", 0.0),
        virtual_sol_in_curve=msg.get("vSolInBondingCurve", 30.0),
        virtual_tokens_in_curve=msg.get("vTokensInBondingCurve", 1_073_000_000.0),
        notified_at=notified_at,
    )


def event_to_candidate(event: NewMintEvent) -> Candidate:
    curve = from_virtual_reserves(event.virtual_sol_in_curve, event.virtual_tokens_in_curve)
    # initialBuy is the creator's own buy in the create tx -- a fast, free
    # proxy for creator supply share. Doesn't catch a creator topping up
    # afterward; that's a tier-2 concern, not tier-1's.
    creator_supply_fraction = event.initial_buy_whole_tokens / TOTAL_SUPPLY_WHOLE_TOKENS
    return Candidate(
        mint=event.mint,
        creator=event.creator,
        name=event.name,
        symbol=event.symbol,
        creator_supply_fraction=creator_supply_fraction,
        curve=curve,
    )


class MintListener:
    """Subscribes to PumpPortal, filters through Tier1Filter, pushes
    survivors onto `queue`. Reconnects with exponential backoff + jitter
    (same pattern as scripts/probe.py's Phase 0 gate), resetting the backoff
    only once a connection survives 30s.
    """

    def __init__(self, tier1_filter: Tier1Filter, queue: asyncio.Queue[Candidate]) -> None:
        self._filter = tier1_filter
        self._queue = queue
        self._recent_mint_timestamps: list[float] = []

    async def run(self) -> None:
        backoff = 2.0
        max_backoff = 60.0
        while True:
            connected_at = time.monotonic()
            survived_this_connection = False
            try:
                async with websockets.connect(PUMPPORTAL_WS_URL, ping_interval=20) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    logger.info("subscribed to PumpPortal new-token feed")
                    async for raw in ws:
                        if time.monotonic() - connected_at > 30:
                            survived_this_connection = True
                        self._handle_raw_message(raw)
            except (websockets.ConnectionClosed, OSError) as exc:
                if survived_this_connection:
                    backoff = 2.0
                sleep_for = min(max_backoff, backoff) + random.uniform(0, 1)
                logger.warning(
                    "PumpPortal websocket dropped (%s), reconnecting in %.1fs",
                    exc, sleep_for,
                )
                await asyncio.sleep(sleep_for)
                backoff = min(max_backoff, backoff * 2)

    def _handle_raw_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        now = time.monotonic()
        event = parse_create_message(msg, now)
        if event is None:
            return

        self._recent_mint_timestamps.append(now)
        cutoff = now - MINT_RATE_WINDOW_SECONDS
        self._recent_mint_timestamps = [
            t for t in self._recent_mint_timestamps if t >= cutoff
        ]

        candidate = event_to_candidate(event)
        result = self._filter.evaluate(candidate, self._recent_mint_timestamps, now)
        if not result.passed:
            logger.info("reject mint=%s reason=%s", candidate.mint, result.reason.value)
            return

        logger.info("candidate accepted mint=%s", candidate.mint)
        try:
            self._queue.put_nowait(candidate)
        except asyncio.QueueFull:
            logger.warning("candidate queue full, dropping mint=%s", candidate.mint)
