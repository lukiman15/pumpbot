from datetime import UTC, datetime

from pumpbot.ledger import (
    CandidateRejected,
    CandidateSeen,
    Ledger,
    TradeOrphaned,
    find_orphans,
    new_trade_id,
    read_events,
)
from pumpbot.main import _build_trade_closed, _LegRecord
from pumpbot.settlement import Settlement


def make_candidate_seen(mint: str = "MintAddr111") -> CandidateSeen:
    return CandidateSeen(
        mint=mint,
        creator="CreatorAddr111",
        name="Some Coin",
        symbol="SOME",
        uri="https://meta.example.com/a.json",
        creator_supply_fraction=0.01,
        virtual_sol_in_curve=30.0,
        virtual_tokens_in_curve=1_073_000_000.0,
        notified_at_wall=1000.0,
    )


# --- schema round-trip -----------------------------------------------------


def test_append_and_read_round_trip(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="run-abc", dry_run=True)
    ledger.append(make_candidate_seen())
    ledger.close()

    rows = list(read_events(tmp_path))
    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "CandidateSeen"
    assert row["schema"] == 1
    assert row["run_id"] == "run-abc"
    assert row["dry_run"] is True
    assert row["mint"] == "MintAddr111"
    assert row["creator"] == "CreatorAddr111"
    assert row["notified_at_wall"] == 1000.0
    assert "ts_wall" in row and "ts_monotonic" in row


def test_rotated_filename_contains_today_utc(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="run-abc", dry_run=False)
    ledger.append(make_candidate_seen())
    ledger.close()

    today = datetime.now(UTC).date().isoformat()
    expected = tmp_path / f"ledger-{today}.jsonl"
    assert expected.exists()


# --- durability: written rows are flushed, readable without closing -------


def test_append_flushes_without_closing(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="run-1", dry_run=False)
    ledger.append(make_candidate_seen(mint="MintA"))
    ledger.append(CandidateRejected(mint="MintA", reason="creator_supply_too_high"))

    rows = list(read_events(tmp_path))  # ledger is still open, not closed
    assert [r["event"] for r in rows] == ["CandidateSeen", "CandidateRejected"]
    ledger.close()


def test_read_events_skips_malformed_lines(tmp_path):
    path = tmp_path / "ledger-2026-01-01.jsonl"
    path.write_text(
        '{"event": "CandidateSeen", "mint": "A"}\n'
        "not json at all\n"
        '{"event": "CandidateSeen", "mint": "B"}\n',
        encoding="utf-8",
    )
    rows = list(read_events(tmp_path))
    assert [r["mint"] for r in rows] == ["A", "B"]


def test_read_events_missing_directory_yields_nothing(tmp_path):
    assert list(read_events(tmp_path / "does_not_exist")) == []


def test_disabled_ledger_never_writes(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="run-1", dry_run=False, enabled=False)
    ledger.append(make_candidate_seen())
    assert list(tmp_path.glob("*.jsonl")) == []


# --- orphan scan ------------------------------------------------------------


def test_find_orphans_classifies_closed_open_orphaned():
    trade_a = new_trade_id()  # CLOSED
    trade_b = new_trade_id()  # OPEN (current run)
    trade_c = new_trade_id()  # ORPHANED (old run, no TradeClosed)

    events = [
        {"event": "EntryFilled", "trade_id": trade_a, "run_id": "run-old", "mint": "A", "tokens_bought": 100.0},
        {"event": "TradeClosed", "trade_id": trade_a, "run_id": "run-old", "mint": "A"},
        {"event": "EntryFilled", "trade_id": trade_b, "run_id": "run-current", "mint": "B", "tokens_bought": 50.0},
        {"event": "EntryFilled", "trade_id": trade_c, "run_id": "run-old", "mint": "C", "tokens_bought": 75.0},
        {"event": "ExitFilled", "trade_id": trade_c, "run_id": "run-old", "mint": "C", "tokens_remaining": 25.0},
    ]

    orphans = find_orphans(events, current_run_id="run-current")

    assert len(orphans) == 1
    orphan = orphans[0]
    assert isinstance(orphan, TradeOrphaned)
    assert orphan.trade_id == trade_c
    assert orphan.mint == "C"
    assert orphan.orphaned_from_run_id == "run-old"
    assert orphan.last_known_tokens_remaining == 25.0


def test_find_orphans_does_not_reorphan_already_orphaned_trade():
    trade_id = new_trade_id()
    events = [
        {"event": "EntryFilled", "trade_id": trade_id, "run_id": "run-old", "mint": "A", "tokens_bought": 10.0},
        {"event": "TradeOrphaned", "trade_id": trade_id, "run_id": "run-old", "mint": "A"},
    ]
    assert find_orphans(events, current_run_id="run-current") == []


def test_find_orphans_empty_ledger_yields_nothing():
    assert find_orphans([], current_run_id="run-current") == []


# --- dry-run isolation -------------------------------------------------------


def test_dry_run_trade_closed_has_null_pnl_fields():
    legs = [_LegRecord("BUY", None), _LegRecord("SELL", None)]
    closed = _build_trade_closed(
        mint="MintA", trade_id="t1", legs=legs, exit_reasons=["timeout"],
        entry_ts_wall=1000.0, dry_run=True,
    )
    assert closed.realized_pnl_lamports is None
    assert closed.rent_recovered_lamports is None
    assert closed.total_fee_lamports is None
    assert closed.gross_turnover_lamports is None
    assert closed.settlement_complete is False


def test_dry_run_trade_closed_round_trips_through_ledger_as_null_not_zero(tmp_path):
    legs = [_LegRecord("BUY", None)]
    closed = _build_trade_closed(
        mint="MintA", trade_id="t1", legs=legs, exit_reasons=["stop_loss"],
        entry_ts_wall=1000.0, dry_run=True,
    )
    ledger = Ledger(tmp_path / "ledger.jsonl", run_id="run-1", dry_run=True)
    ledger.append(closed)
    ledger.close()

    row = next(read_events(tmp_path))
    assert row["realized_pnl_lamports"] is None
    assert row["rent_recovered_lamports"] is None
    assert row["total_fee_lamports"] is None
    assert row["gross_turnover_lamports"] is None


# --- PnL math (Accounting Rules) --------------------------------------------


def test_realized_pnl_is_plain_sum_of_leg_deltas():
    buy = Settlement(fee_lamports=5000, sol_delta_lamports=-4_925_858, slot=1, leg_kind="BUY")
    sell = Settlement(fee_lamports=5000, sol_delta_lamports=972_366, slot=2, leg_kind="SELL")
    legs = [_LegRecord("BUY", buy), _LegRecord("SELL", sell)]

    closed = _build_trade_closed(
        mint="MintA", trade_id="t1", legs=legs, exit_reasons=["stop_loss"],
        entry_ts_wall=1000.0, dry_run=False,
    )
    assert closed.realized_pnl_lamports == -4_925_858 + 972_366
    assert closed.total_fee_lamports == 10_000
    assert closed.gross_turnover_lamports == 4_925_858
    assert closed.rent_recovered_lamports == 0  # no ATA_CLOSE leg landed
    assert closed.settlement_complete is True
    assert closed.leg_count == 2


def test_rent_recovered_reported_separately_never_netted_into_realized_pnl():
    buy = Settlement(fee_lamports=5000, sol_delta_lamports=-5_000_000, slot=1, leg_kind="BUY")
    sell = Settlement(fee_lamports=5000, sol_delta_lamports=4_000_000, slot=2, leg_kind="SELL")
    ata_close = Settlement(fee_lamports=5000, sol_delta_lamports=2_000_000, slot=3, leg_kind="ATA_CLOSE")
    legs = [_LegRecord("BUY", buy), _LegRecord("SELL", sell), _LegRecord("ATA_CLOSE", ata_close)]

    closed = _build_trade_closed(
        mint="MintA", trade_id="t1", legs=legs, exit_reasons=["take_profit_1", "take_profit_2"],
        entry_ts_wall=1000.0, dry_run=False,
    )
    # realized_pnl includes the ATA_CLOSE leg's delta (it's a real sol_delta,
    # summed like any other leg) but rent_recovered_lamports is ALSO reported
    # on its own -- never silently chosen instead of the other.
    assert closed.realized_pnl_lamports == -5_000_000 + 4_000_000 + 2_000_000
    assert closed.rent_recovered_lamports == 2_000_000
    assert closed.total_fee_lamports == 15_000


def test_settlement_incomplete_when_a_leg_never_settled():
    buy = Settlement(fee_lamports=5000, sol_delta_lamports=-5_000_000, slot=1, leg_kind="BUY")
    legs = [_LegRecord("BUY", buy), _LegRecord("SELL", None)]  # SELL settlement never arrived

    closed = _build_trade_closed(
        mint="MintA", trade_id="t1", legs=legs, exit_reasons=["timeout"],
        entry_ts_wall=1000.0, dry_run=False,
    )
    assert closed.settlement_complete is False
    # Still reports what's available, rather than nulling out everything.
    assert closed.realized_pnl_lamports == -5_000_000
    assert closed.gross_turnover_lamports == 5_000_000


def test_gross_turnover_none_when_buy_leg_never_settled():
    legs = [_LegRecord("BUY", None), _LegRecord("SELL", Settlement(1000, 900_000, 2, "SELL"))]
    closed = _build_trade_closed(
        mint="MintA", trade_id="t1", legs=legs, exit_reasons=["timeout"],
        entry_ts_wall=1000.0, dry_run=False,
    )
    assert closed.gross_turnover_lamports is None
    assert closed.settlement_complete is False
