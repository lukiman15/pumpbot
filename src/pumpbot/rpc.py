"""QuickNode client: RPS limiting, credit metering, retry/backoff.

Everything else in this project talks to the chain through this module so the
rate limit and credit budget are enforced in exactly one place.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from datetime import date, datetime, timezone
from typing import Any

import httpx
from aiolimiter import AsyncLimiter

from pumpbot.config import Settings

logger = logging.getLogger(__name__)


class RpcError(RuntimeError):
    pass


class CreditHaltError(RuntimeError):
    """Raised when the daily credit budget is exhausted for non-exempt calls."""


class DailyLimitReachedError(RuntimeError):
    """QuickNode's plan-level daily request cap (JSON-RPC error -32003) is hit.

    This resets on QuickNode's clock, not ours -- retrying burns wall-clock
    time for nothing until then, so this is raised immediately rather than
    going through the normal retry/backoff loop.
    """


# Methods that must never be blocked by the daily credit halt: stranding a
# position to save a credit is prohibited by design (see positions.py).
CREDIT_EXEMPT_METHODS = {
    "getSignatureStatuses",
    "getTokenAccountBalance",
    "getAccountInfo",
    "getBalance",
}


class RpcClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._limiter = AsyncLimiter(settings.config.rpc.rps_limit, time_period=1.0)
        self._http = httpx.AsyncClient(timeout=15.0)
        self._id_counter = itertools.count(1)
        self._credits_spent_today = 0
        self._credits_day: date = datetime.now(timezone.utc).date()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "RpcClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def _roll_credit_day(self) -> None:
        # QuickNode resets its own daily cap on its own clock (unknown to us);
        # UTC midnight is a documented, unambiguous approximation rather than
        # tying the halt to wherever this process happens to run.
        today = datetime.now(timezone.utc).date()
        if today != self._credits_day:
            self._credits_day = today
            self._credits_spent_today = 0

    @property
    def credits_spent_today(self) -> int:
        self._roll_credit_day()
        return self._credits_spent_today

    def _cost(self, method: str) -> int:
        return self._settings.config.rpc.credit_costs.get(method, 1)

    def _check_credit_budget(self, method: str) -> None:
        if method in CREDIT_EXEMPT_METHODS:
            return
        self._roll_credit_day()
        halt = self._settings.config.rpc.daily_credit_halt
        if self._credits_spent_today >= halt:
            raise CreditHaltError(
                f"Daily credit halt ({halt}) reached; new entries are blocked. "
                "Exits and reconciliation remain unaffected."
            )

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        """Send one JSON-RPC call, rate-limited, credit-metered, retried."""
        self._check_credit_budget(method)
        cfg = self._settings.config.rpc
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._id_counter),
            "method": method,
            "params": params or [],
        }

        last_exc: Exception | None = None
        for attempt in range(cfg.max_retries + 1):
            async with self._limiter:
                try:
                    resp = await self._http.post(
                        self._settings.secrets.quicknode_http_url, json=payload
                    )
                except httpx.HTTPError as exc:
                    last_exc = exc
                else:
                    if resp.status_code == 429:
                        try:
                            body = resp.json()
                        except ValueError:
                            body = {}
                        if body.get("error", {}).get("code") == -32003:
                            reset_seconds = resp.headers.get("x-ratelimit-reset", "?")
                            raise DailyLimitReachedError(
                                f"QuickNode daily request limit reached; "
                                f"resets in ~{reset_seconds}s. Not retrying."
                            )
                        last_exc = RpcError(f"{method} -> HTTP 429")
                    elif resp.status_code >= 500:
                        last_exc = RpcError(f"{method} -> HTTP {resp.status_code}")
                    else:
                        body = resp.json()
                        if "error" in body:
                            raise RpcError(f"{method} -> {body['error']}")
                        self._roll_credit_day()
                        self._credits_spent_today += self._cost(method)
                        return body["result"]

            backoff = cfg.backoff_base_seconds * (2**attempt)
            logger.warning("rpc retry %s attempt=%s backoff=%.2fs", method, attempt, backoff)
            await asyncio.sleep(backoff)

        raise RpcError(f"{method} failed after {cfg.max_retries} retries") from last_exc

    async def get_balance_sol(self, pubkey: str) -> float:
        result = await self.call("getBalance", [pubkey])
        lamports = result["value"]
        return lamports / 1_000_000_000
