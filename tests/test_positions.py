import pytest

from pumpbot.config import ExitsConfig
from pumpbot.positions import (
    DuplicatePositionError,
    ExitReason,
    Position,
    PositionLimitReachedError,
    PositionManager,
)

CONFIG = ExitsConfig(
    take_profit_1_multiple=1.5,
    take_profit_1_fraction=0.5,
    take_profit_2_multiple=2.5,
    stop_loss_fraction=-0.35,
    timeout_seconds=300,
)


def make_position(opened_at: float = 0.0) -> Position:
    return Position(
        mint="Mint1",
        entry_price_sol=0.0001,
        entry_tokens=1000.0,
        opened_at=opened_at,
    )


def test_no_exit_when_flat():
    pos = make_position()
    decision = pos.evaluate_exit(current_price_sol=0.0001, now=10.0, config=CONFIG)
    assert decision is None


def test_take_profit_1_triggers_partial_exit():
    pos = make_position()
    decision = pos.evaluate_exit(current_price_sol=0.00015001, now=10.0, config=CONFIG)  # 1.5x
    assert decision is not None
    assert decision.reason == ExitReason.TAKE_PROFIT_1
    assert decision.fraction == 0.5


def test_apply_take_profit_1_halves_remaining_and_sets_flag():
    pos = make_position()
    decision = pos.evaluate_exit(current_price_sol=0.00015001, now=10.0, config=CONFIG)
    sold = pos.apply_exit(decision)
    assert sold == 500.0
    assert pos.tokens_remaining == 500.0
    assert pos.take_profit_1_hit is True


def test_take_profit_1_does_not_retrigger_after_hit():
    pos = make_position()
    pos.apply_exit(pos.evaluate_exit(0.00015001, 10.0, CONFIG))
    # Still above tp1 threshold but below tp2 -- should not fire again.
    decision = pos.evaluate_exit(current_price_sol=0.00016, now=20.0, config=CONFIG)
    assert decision is None


def test_take_profit_2_after_take_profit_1_sells_full_remainder():
    pos = make_position()
    pos.apply_exit(pos.evaluate_exit(0.00015001, 10.0, CONFIG))  # tp1: 500 remain
    decision = pos.evaluate_exit(current_price_sol=0.00025, now=20.0, config=CONFIG)  # 2.5x
    assert decision.reason == ExitReason.TAKE_PROFIT_2
    assert decision.fraction == 1.0
    sold = pos.apply_exit(decision)
    assert sold == 500.0
    assert pos.tokens_remaining == 0.0


def test_price_spike_past_take_profit_2_skips_ladder_and_exits_fully():
    pos = make_position()
    # Jumps straight to 3x in one tick, never observed at 1.5x.
    decision = pos.evaluate_exit(current_price_sol=0.0003, now=10.0, config=CONFIG)
    assert decision.reason == ExitReason.TAKE_PROFIT_2
    assert decision.fraction == 1.0
    assert pos.take_profit_1_hit is False


def test_stop_loss_triggers_full_exit():
    pos = make_position()
    decision = pos.evaluate_exit(current_price_sol=0.000064, now=10.0, config=CONFIG)  # -36%
    assert decision.reason == ExitReason.STOP_LOSS
    assert decision.fraction == 1.0


def test_stop_loss_at_exact_threshold_triggers():
    pos = make_position()
    # -35% exactly: multiple = 0.65
    decision = pos.evaluate_exit(current_price_sol=0.000065, now=10.0, config=CONFIG)
    assert decision.reason == ExitReason.STOP_LOSS


def test_timeout_triggers_full_exit_of_remaining_tokens():
    pos = make_position(opened_at=0.0)
    decision = pos.evaluate_exit(current_price_sol=0.0001, now=300.0, config=CONFIG)
    assert decision.reason == ExitReason.TIMEOUT
    assert decision.fraction == 1.0


def test_timeout_after_partial_take_profit_only_sells_remainder():
    pos = make_position(opened_at=0.0)
    pos.apply_exit(pos.evaluate_exit(0.00015001, 10.0, CONFIG))  # tp1: 500 remain
    decision = pos.evaluate_exit(current_price_sol=0.00012, now=300.0, config=CONFIG)
    assert decision.reason == ExitReason.TIMEOUT
    sold = pos.apply_exit(decision)
    assert sold == 500.0
    assert pos.tokens_remaining == 0.0


def test_no_exit_when_tokens_remaining_is_zero():
    pos = make_position()
    pos.tokens_remaining = 0.0
    decision = pos.evaluate_exit(current_price_sol=0.0003, now=10.0, config=CONFIG)
    assert decision is None


def test_zero_entry_price_forces_stop_loss_instead_of_crashing():
    pos = Position(mint="Mint1", entry_price_sol=0.0, entry_tokens=1000.0, opened_at=0.0)
    decision = pos.evaluate_exit(current_price_sol=0.0001, now=10.0, config=CONFIG)
    assert decision.reason == ExitReason.STOP_LOSS


def test_position_manager_enforces_max_concurrent():
    mgr = PositionManager(max_concurrent_positions=1)
    mgr.open(make_position())
    assert not mgr.can_open_new()
    with pytest.raises(PositionLimitReachedError):
        mgr.open(Position(mint="Mint2", entry_price_sol=0.0001, entry_tokens=500.0, opened_at=0.0))


def test_position_manager_rejects_duplicate_mint():
    mgr = PositionManager(max_concurrent_positions=5)
    mgr.open(make_position())
    with pytest.raises(DuplicatePositionError):
        mgr.open(make_position())


def test_position_manager_close_frees_slot():
    mgr = PositionManager(max_concurrent_positions=1)
    mgr.open(make_position())
    mgr.close("Mint1")
    assert mgr.can_open_new()
    assert mgr.get("Mint1") is None


def test_position_manager_all_open():
    mgr = PositionManager(max_concurrent_positions=2)
    mgr.open(make_position())
    assert [p.mint for p in mgr.all_open()] == ["Mint1"]
