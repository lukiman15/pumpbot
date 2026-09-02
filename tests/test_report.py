"""Tests for scripts/report.py -- loaded via importlib since it lives under
scripts/, not the pumpbot package, and its own module-level sys.path hack
(for `from pumpbot... import`) only needs to run once regardless of how many
times this test module is imported.
"""

import importlib.util
import sys
import time
from pathlib import Path

_REPORT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "report.py"
_spec = importlib.util.spec_from_file_location("pumpbot_report", _REPORT_PATH)
report = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = report  # dataclass() needs the module registered to resolve annotations
_spec.loader.exec_module(report)


def _envelope(event: str, dry_run: bool, ts_wall: float, **fields):
    return {
        "event": event,
        "schema": 1,
        "ts_wall": ts_wall,
        "ts_monotonic": ts_wall,
        "run_id": "run-1",
        "dry_run": dry_run,
        **fields,
    }


def make_dry_run_events(ts_wall: float = 1000.0) -> list[dict]:
    return [
        _envelope("CandidateSeen", True, ts_wall, mint="M1", creator="C1", name="n", symbol="s", uri="u",
                  creator_supply_fraction=0.01, virtual_sol_in_curve=30.0, virtual_tokens_in_curve=1e9,
                  notified_at_wall=ts_wall),
        _envelope("EntryFilled", True, ts_wall + 1, mint="M1", trade_id="t1", signature=None,
                  position_sol=0.001, tokens_bought=900.0, entry_price_sol=0.0000011,
                  latency_seconds=0.42, settlement=None),
        _envelope("TradeClosed", True, ts_wall + 2, mint="M1", trade_id="t1",
                  realized_pnl_lamports=None, rent_recovered_lamports=None, total_fee_lamports=None,
                  gross_turnover_lamports=None, hold_seconds=30.0, leg_count=2,
                  exit_reasons=["timeout"], settlement_complete=False),
    ]


def make_real_events(ts_wall: float = 2000.0) -> list[dict]:
    return [
        _envelope("CandidateSeen", False, ts_wall, mint="M2", creator="C2", name="n", symbol="s", uri="u",
                  creator_supply_fraction=0.01, virtual_sol_in_curve=30.0, virtual_tokens_in_curve=1e9,
                  notified_at_wall=ts_wall),
        _envelope("EntryFilled", False, ts_wall + 1, mint="M2", trade_id="t2", signature="sig1",
                  position_sol=0.001, tokens_bought=850.0, entry_price_sol=0.0000012,
                  latency_seconds=0.35, settlement=None),
        _envelope("ExitFilled", False, ts_wall + 2, mint="M2", trade_id="t2", signature="sig2",
                  exit_reason="trailing_stop", tokens_sold=850.0, tokens_remaining=0.0,
                  curve_price_sol=0.0000014, realizable_multiple=1.3, peak_multiple=1.6, settlement=None),
        _envelope("TradeClosed", False, ts_wall + 3, mint="M2", trade_id="t2",
                  realized_pnl_lamports=100000, rent_recovered_lamports=2000000, total_fee_lamports=60000,
                  gross_turnover_lamports=2000000, hold_seconds=40.0, leg_count=3,
                  exit_reasons=["trailing_stop"], settlement_complete=True),
    ]


# --- filter_since ---


def test_filter_since_none_passes_everything_through():
    events = make_dry_run_events() + make_real_events()
    assert report.filter_since(events, None) == events


def test_filter_since_excludes_events_before_cutoff():
    # 2026-01-01T00:00:00Z
    cutoff_ts = 1767225600.0
    before = _envelope("CandidateSeen", False, cutoff_ts - 100, mint="Old", creator="C",
                        name="n", symbol="s", uri="u", creator_supply_fraction=0.0,
                        virtual_sol_in_curve=30.0, virtual_tokens_in_curve=1e9, notified_at_wall=0.0)
    after = _envelope("CandidateSeen", False, cutoff_ts + 100, mint="New", creator="C",
                       name="n", symbol="s", uri="u", creator_supply_fraction=0.0,
                       virtual_sol_in_curve=30.0, virtual_tokens_in_curve=1e9, notified_at_wall=0.0)
    filtered = report.filter_since([before, after], "2026-01-01")
    assert filtered == [after]


def test_filter_since_across_a_ledger_rotation_boundary(tmp_path):
    # Two rotated files, mirroring ledger.py's daily <stem>-<date>.jsonl
    # naming -- read_events reads the whole directory, so --since is the
    # only thing that separates these two windows.
    import json

    day1_ts = 1767139200.0  # 2025-12-31T00:00:00Z + a bit
    day2_ts = 1767225600.0  # 2026-01-01T00:00:00Z + a bit
    (tmp_path / "ledger-2025-12-31.jsonl").write_text(
        json.dumps(_envelope("CandidateSeen", False, day1_ts + 60, mint="Day1", creator="C",
                              name="n", symbol="s", uri="u", creator_supply_fraction=0.0,
                              virtual_sol_in_curve=30.0, virtual_tokens_in_curve=1e9,
                              notified_at_wall=0.0))
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "ledger-2026-01-01.jsonl").write_text(
        json.dumps(_envelope("CandidateSeen", False, day2_ts + 60, mint="Day2", creator="C",
                              name="n", symbol="s", uri="u", creator_supply_fraction=0.0,
                              virtual_sol_in_curve=30.0, virtual_tokens_in_curve=1e9,
                              notified_at_wall=0.0))
        + "\n",
        encoding="utf-8",
    )

    all_events = list(report.read_events(tmp_path))
    assert len(all_events) == 2

    since_events = report.filter_since(all_events, "2026-01-01")
    assert len(since_events) == 1
    assert since_events[0]["mint"] == "Day2"


# --- dry_run_observation / dry_run_exit_reason_counts ---


def test_dry_run_observation_empty_ledger_is_n_zero_safe():
    obs = report.dry_run_observation([])
    assert obs["entry_count"] == 0
    assert obs["closed_count"] == 0
    assert obs["latency_pcts"] is None
    assert obs["exit_reason_counts"] == {}


def test_dry_run_observation_counts_and_latency_from_dry_run_rows_only():
    events = make_dry_run_events() + make_real_events()
    obs = report.dry_run_observation(events)
    assert obs["entry_count"] == 1
    assert obs["closed_count"] == 1
    assert obs["latency_pcts"] == (0.42, 0.42, 0.42)
    assert obs["exit_reason_counts"] == {"timeout": 1}


# --- print_report: dry-run-only / real-only / mixed ---


def test_print_report_dry_run_only_does_not_crash_any_real_statistic_section(capsys):
    report.print_report(make_dry_run_events())
    out = capsys.readouterr().out
    assert "Dry-run observation" in out
    assert "INSUFFICIENT SAMPLE (n=0)" in out or "n/a (n=0)" in out
    # No real trade in this ledger -- must never raise inside
    # load_closed_real_trades or any downstream section.


def test_print_report_real_only_shows_no_dry_run_banner_or_section(capsys):
    report.print_report(make_real_events())
    out = capsys.readouterr().out
    assert "NOTE: this ledger contains" not in out
    assert "Dry-run observation" not in out


def test_print_report_mixed_keeps_populations_separate(capsys):
    events = make_dry_run_events() + make_real_events()
    report.print_report(events)
    out = capsys.readouterr().out

    assert "NOTE: this ledger contains 3 dry-run event(s) and 4 real event(s)" in out
    assert "Dry-run observation" in out
    # Trade status counts BOTH trades (dry-run + real) -- ALL events.
    assert "CLOSED: 2" in out
    # But the dry-run trade's null PnL must never reach a real statistic --
    # load_closed_real_trades silently drops it (it isn't dry_run=False),
    # so only the one real trade contributes to win_rate/PnL sample sizes.
    assert "(n=1)" in out or "n=1" in out


def test_load_closed_real_trades_still_raises_on_null_pnl_for_a_real_trade():
    # Guards against a future edit accidentally loosening this: a REAL
    # TradeClosed with a null PnL field is a settlement bug, not a
    # statistic to skip (report.py's own module docstring).
    bad_real_event = _envelope(
        "TradeClosed", False, time.time(), mint="M3", trade_id="t3",
        realized_pnl_lamports=None, rent_recovered_lamports=1, total_fee_lamports=1,
        gross_turnover_lamports=1, hold_seconds=1.0, leg_count=1, exit_reasons=["timeout"],
        settlement_complete=True,
    )
    try:
        report.load_closed_real_trades([bad_real_event])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a real TradeClosed with null PnL")
