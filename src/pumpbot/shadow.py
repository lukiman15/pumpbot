"""Shadow log: fixed-horizon forward-price tracking for both bought and
declined mints -- the PRD's adverse-selection question (do the mints the
bot rejects/skips out- or under-perform the ones it buys, on the same
clock?) cannot be answered without a forward-price series for all three
arms.

Ships disabled (config.yaml's shadow.enabled: false) -- polling costs real
RPC budget even at a low sample_fraction, and this is meant to be switched
on deliberately after observing credit burn under normal trading.

Rate-limiting, measured: AsyncLimiter(12, 1.0) is a token bucket that
permits an instant burst of 12 then refills at 12/s, with a strict FIFO
waiter queue. At max_tracked=50 polled in one tick, 38 acquisitions queue
behind the first 12 and take ~3.2s to drain under one shared limiter -- a
live buy issued during that window would wait behind all of them,
regardless of how the rate is tuned. That is why every RPC call this
module makes uses rpc.py's pool="shadow" -- a disjoint token bucket from
trading's own, so a shadow burst can never queue ahead of a live trading
call.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from solders.pubkey import Pubkey

from pumpbot.config import ShadowConfig
from pumpbot.curve import (
    BondingCurveState,
    decode_bonding_curve,
    spot_price_sol_per_token,
)
from pumpbot.ledger import Ledger, ShadowPrice
from pumpbot.program import derive_bonding_curve_pda
from pumpbot.rpc import RpcClient

logger = logging.getLogger(__name__)

# PumpPortal's own "mayhem mode" launch flag -- a deterministic external
# gate, not a judgment this project makes (see filters/tier1.py's
# Candidate.is_mayhem_mode docstring). Measured live at 75% of all
# rejects; sampling it into the rejected arm would dilute the
# adverse-selection signal from creator_supply_too_high and
# mint_rate_too_high, the two reject reasons that ARE this project's own
# decisions and the only ones this arm can actually inform.
EXCLUDED_REJECTED_REASONS = frozenset({"mayhem_mode_unauthorized"})


@dataclass
class _TrackedMint:
    mint: str
    arm: str  # "bought" | "rejected" | "skipped"
    reject_reason: str | None
    trade_id: str | None
    first_seen_price_sol: float | None = None
    started_at: float = field(default_factory=time.monotonic)


class ShadowTracker:
    def __init__(
        self,
        config: ShadowConfig,
        rpc: RpcClient,
        ledger: Ledger,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._config = config
        self._rpc = rpc
        self._ledger = ledger
        self._rng = rng
        self._tracked: dict[str, _TrackedMint] = {}

    @property
    def tracked_count(self) -> int:
        return len(self._tracked)

    def track(
        self,
        mint: str,
        arm: str,
        reject_reason: str | None = None,
        trade_id: str | None = None,
    ) -> None:
        """Admits `mint` for shadow tracking if a sampling draw passes and
        capacity allows. Idempotent -- a mint already tracked is left
        alone. The "bought" arm is always sampled at 1.0 regardless of
        config.sample_fraction: it's small (bounded by trade count) and
        sampling it would destroy the comparison it exists to enable."""
        if not self._config.enabled:
            return
        if mint in self._tracked:
            return
        if arm == "rejected" and reject_reason in EXCLUDED_REJECTED_REASONS:
            return
        if len(self._tracked) >= self._config.max_tracked:
            return

        sample_fraction = 1.0 if arm == "bought" else self._config.sample_fraction
        if self._rng() >= sample_fraction:
            return

        self._tracked[mint] = _TrackedMint(
            mint=mint, arm=arm, reject_reason=reject_reason, trade_id=trade_id
        )

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._config.poll_interval_seconds)
            for mint in list(self._tracked.keys()):
                try:
                    await self._poll_one(mint)
                except Exception:
                    logger.exception("shadow poll crashed for mint=%s", mint)

    async def _poll_one(self, mint: str) -> None:
        tracked = self._tracked.get(mint)
        if tracked is None:
            return

        elapsed = time.monotonic() - tracked.started_at
        curve = await self._fetch_curve(mint)
        if curve is None:
            # Bonding curve account not (yet) visible to this RPC node --
            # skip this poll, keep tracking. Distinct from curve.complete
            # below: this is "try again later", that is "done, for real".
            return

        price_sol = spot_price_sol_per_token(curve)
        if tracked.first_seen_price_sol is None:
            tracked.first_seen_price_sol = price_sol

        if tracked.first_seen_price_sol:
            return_from_first_seen = (price_sol / tracked.first_seen_price_sol) - 1.0
        else:
            return_from_first_seen = None

        self._ledger.append(
            ShadowPrice(
                mint=tracked.mint,
                arm=tracked.arm,
                reject_reason=tracked.reject_reason,
                horizon_elapsed_seconds=elapsed,
                price_sol=price_sol,
                return_from_first_seen=return_from_first_seen,
                # A graduated token is a result, not an error -- this is
                # the final ShadowPrice for it either way.
                curve_complete=curve.complete,
                trade_id=tracked.trade_id,
            )
        )

        if curve.complete or elapsed >= self._config.horizon_seconds:
            del self._tracked[mint]

    async def _fetch_curve(self, mint: str) -> BondingCurveState | None:
        bonding_curve_pda = derive_bonding_curve_pda(Pubkey.from_string(mint))
        info = await self._rpc.call(
            "getAccountInfo",
            [str(bonding_curve_pda), {"encoding": "base64", "commitment": "confirmed"}],
            pool="shadow",
        )
        value = info.get("value")
        if value is None:
            return None
        raw = base64.b64decode(value["data"][0])
        return decode_bonding_curve(raw)
