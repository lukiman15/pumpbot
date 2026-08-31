import asyncio

import pytest

from pumpbot.config import HeartbeatConfig
from pumpbot.heartbeat import Heartbeat

CONFIG = HeartbeatConfig(interval_seconds=300, idle_alarm_heartbeats=3)


def test_quiet_market_does_not_alarm():
    hb = Heartbeat(CONFIG)
    for _ in range(5):
        report = hb.tick(positions_open=0)
        assert not report.idle_alarm
        assert report.candidates_seen == 0
        assert report.trades_executed == 0


def test_candidates_with_trades_does_not_alarm():
    hb = Heartbeat(CONFIG)
    for _ in range(5):
        hb.record_candidate()
        hb.record_trade()
        report = hb.tick(positions_open=1)
        assert not report.idle_alarm


def test_candidates_without_trades_alarms_after_threshold():
    hb = Heartbeat(CONFIG)
    reports = []
    for _ in range(CONFIG.idle_alarm_heartbeats):
        hb.record_candidate()
        reports.append(hb.tick(positions_open=0))

    # Alarm should not fire before the threshold, only at it.
    assert not reports[0].idle_alarm
    assert not reports[1].idle_alarm
    assert reports[2].idle_alarm


def test_alarm_keeps_firing_while_condition_persists():
    hb = Heartbeat(CONFIG)
    for _ in range(CONFIG.idle_alarm_heartbeats):
        hb.record_candidate()
        hb.tick(positions_open=0)

    hb.record_candidate()
    report = hb.tick(positions_open=0)
    assert report.idle_alarm


def test_trade_resets_alarm_streak():
    hb = Heartbeat(CONFIG)
    for _ in range(CONFIG.idle_alarm_heartbeats):
        hb.record_candidate()
        hb.tick(positions_open=0)

    hb.record_candidate()
    hb.record_trade()
    report = hb.tick(positions_open=1)
    assert not report.idle_alarm

    # Streak should have reset -- takes the full threshold again to re-alarm.
    hb.record_candidate()
    report = hb.tick(positions_open=0)
    assert not report.idle_alarm


def test_counters_reset_between_ticks():
    hb = Heartbeat(CONFIG)
    hb.record_candidate()
    hb.record_candidate()
    hb.record_trade()
    report = hb.tick(positions_open=1)
    assert report.candidates_seen == 2
    assert report.trades_executed == 1

    report2 = hb.tick(positions_open=1)
    assert report2.candidates_seen == 0
    assert report2.trades_executed == 0


def test_tick_number_increments():
    hb = Heartbeat(CONFIG)
    assert hb.tick(positions_open=0).tick == 1
    assert hb.tick(positions_open=0).tick == 2
    assert hb.tick(positions_open=0).tick == 3


def test_report_carries_positions_open():
    hb = Heartbeat(CONFIG)
    report = hb.tick(positions_open=1)
    assert report.positions_open == 1


@pytest.mark.asyncio
async def test_run_forever_calls_sync_callback_each_interval():
    fast_config = HeartbeatConfig(interval_seconds=0, idle_alarm_heartbeats=3)
    hb = Heartbeat(fast_config)
    reports = []

    task = asyncio.create_task(hb.run_forever(lambda: 0, reports.append))
    await asyncio.sleep(0.035)
    task.cancel()

    assert len(reports) >= 2


@pytest.mark.asyncio
async def test_run_forever_calls_async_callback():
    fast_config = HeartbeatConfig(interval_seconds=0, idle_alarm_heartbeats=3)
    hb = Heartbeat(fast_config)
    reports = []

    async def on_report(report):
        reports.append(report)

    task = asyncio.create_task(hb.run_forever(lambda: 0, on_report))
    await asyncio.sleep(0.025)
    task.cancel()

    assert len(reports) >= 1
