"""Heartbeat: periodic liveness reporting and an idle-alarm failsafe.

Zero executed trades across idle_alarm_heartbeats consecutive heartbeats
*while candidates were arriving* means something is broken (a stuck
executor, an empty wallet, a bad filter) -- not just a quiet market. A
quiet market shows up as zero candidates too, and must not alarm.

Pure counters + decision logic -- no RPC calls. main.py (not yet built)
calls record_candidate()/record_trade() as things happen, and tick() once
per heartbeat.interval_seconds to get a report to log/alert on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pumpbot.config import HeartbeatConfig


@dataclass(frozen=True)
class HeartbeatReport:
    tick: int
    candidates_seen: int
    trades_executed: int
    positions_open: int
    idle_alarm: bool


class Heartbeat:
    def __init__(self, config: HeartbeatConfig) -> None:
        self._config = config
        self._tick_count = 0
        self._consecutive_idle_with_candidates = 0
        self._candidates_seen = 0
        self._trades_executed = 0

    def record_candidate(self) -> None:
        self._candidates_seen += 1

    def record_trade(self) -> None:
        self._trades_executed += 1

    def tick(self, positions_open: int) -> HeartbeatReport:
        """Call once every heartbeat.interval_seconds. Resets the per-interval
        candidate/trade counters and returns a report for this interval."""
        self._tick_count += 1

        if self._candidates_seen > 0 and self._trades_executed == 0:
            self._consecutive_idle_with_candidates += 1
        else:
            self._consecutive_idle_with_candidates = 0

        idle_alarm = (
            self._consecutive_idle_with_candidates >= self._config.idle_alarm_heartbeats
        )

        report = HeartbeatReport(
            tick=self._tick_count,
            candidates_seen=self._candidates_seen,
            trades_executed=self._trades_executed,
            positions_open=positions_open,
            idle_alarm=idle_alarm,
        )

        self._candidates_seen = 0
        self._trades_executed = 0
        return report

    async def run_forever(
        self,
        get_positions_open: Callable[[], int],
        on_report: Callable[[HeartbeatReport], Awaitable[None] | None],
    ) -> None:
        """Sleeps heartbeat.interval_seconds, ticks, hands the report to
        on_report (sync or async), forever. Intended for main.py to run as
        a background task alongside the listener/executor loops."""
        while True:
            await asyncio.sleep(self._config.interval_seconds)
            report = self.tick(get_positions_open())
            result = on_report(report)
            if result is not None:
                await result
