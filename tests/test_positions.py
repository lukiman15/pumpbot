import pytest

from pumpbot.config import ExitsConfig
from pumpbot.curve import (
    LAMPORTS_PER_SOL,
    TOKEN_DECIMALS,
    BondingCurveState,
    sol_to_tokens,
    spot_price_sol_per_token,
    tokens_to_sol,
)
from pumpbot.positions import (
    DuplicatePositionError,
    ExitReason,
    Position,
    PositionLimitReachedError,
    PositionManager,
)

BASE_CONFIG = ExitsConfig(
    take_profit_1_multiple=1.5,
    take_profit_1_fraction=0.5,
    take_profit_2_multiple=2.5,
    stop_loss_fraction=-0.35,
    timeout_seconds=300,
    trailing_enabled=False,
    trailing_arm_multiple=1.3,
    trailing_drawdown_fraction=0.25,
    creator_sell_enabled=True,
)

TRAILING_CONFIG = BASE_CONFIG.model_copy(update={"trailing_enabled": True})


def make_position(opened_at: float = 0.0) -> Position:
    return Position(
        mint="Mint1",
        entry_price_sol=0.0001,
        entry_tokens=1000.0,
        opened_at=opened_at,
    )


def realizable_for_multiple(pos: Position, multiple: float) -> float:
    """The realizable SOL value of tokens_remaining that produces exactly
    `multiple` against the position's current cost basis -- lets tests
    express intent directly ("a decision at 1.5x") instead of hand-deriving
    curve output."""
    return multiple * pos.cost_basis_of_remaining_sol


def test_no_exit_when_flat():
    pos = make_position()
    decision = pos.evaluate_exit(realizable_for_multiple(pos, 1.0), now=10.0, config=BASE_CONFIG)
    assert decision is None


def test_take_profit_1_triggers_partial_exit():
    pos = make_position()
    decision = pos.evaluate_exit(realizable_for_multiple(pos, 1.5001), now=10.0, config=BASE_CONFIG)
    assert decision is not None
    assert decision.reason == ExitReason.TAKE_PROFIT_1
    assert decision.fraction == 0.5


def test_apply_take_profit_1_halves_remaining_and_sets_flag():
    pos = make_position()
    decision = pos.evaluate_exit(realizable_for_multiple(pos, 1.5001), now=10.0, config=BASE_CONFIG)
    sold = pos.apply_exit(decision)
    assert sold == 500.0
    assert pos.tokens_remaining == 500.0
    assert pos.take_profit_1_hit is True


def test_take_profit_1_does_not_retrigger_after_hit():
    pos = make_position()
    pos.apply_exit(pos.evaluate_exit(realizable_for_multiple(pos, 1.5001), 10.0, BASE_CONFIG))
    # Still above tp1 threshold but below tp2 -- should not fire again.
    decision = pos.evaluate_exit(realizable_for_multiple(pos, 1.6), now=20.0, config=BASE_CONFIG)
    assert decision is None


def test_take_profit_2_after_take_profit_1_sells_full_remainder():
    pos = make_position()
    pos.apply_exit(pos.evaluate_exit(realizable_for_multiple(pos, 1.5001), 10.0, BASE_CONFIG))  # tp1: 500 remain
    decision = pos.evaluate_exit(realizable_for_multiple(pos, 2.5), now=20.0, config=BASE_CONFIG)
    assert decision.reason == ExitReason.TAKE_PROFIT_2
    assert decision.fraction == 1.0
    sold = pos.apply_exit(decision)
    assert sold == 500.0
    assert pos.tokens_remaining == 0.0


def test_price_spike_past_take_profit_2_skips_ladder_and_exits_fully():
    pos = make_position()
    # Jumps straight to 3x in one tick, never observed at 1.5x.
    decision = pos.evaluate_exit(realizable_for_multiple(pos, 3.0), now=10.0, config=BASE_CONFIG)
    assert decision.reason == ExitReason.TAKE_PROFIT_2
    assert decision.fraction == 1.0
    assert pos.take_profit_1_hit is False


def test_stop_loss_triggers_full_exit():
    pos = make_position()
    decision = pos.evaluate_exit(realizable_for_multiple(pos, 0.64), now=10.0, config=BASE_CONFIG)  # -36%
    assert decision.reason == ExitReason.STOP_LOSS
    assert decision.fraction == 1.0


def test_stop_loss_at_exact_threshold_triggers():
    pos = make_position()
    decision = pos.evaluate_exit(realizable_for_multiple(pos, 0.65), now=10.0, config=BASE_CONFIG)  # -35% exactly
    assert decision.reason == ExitReason.STOP_LOSS


def test_timeout_triggers_full_exit_of_remaining_tokens():
    pos = make_position(opened_at=0.0)
    decision = pos.evaluate_exit(realizable_for_multiple(pos, 1.0), now=300.0, config=BASE_CONFIG)
    assert decision.reason == ExitReason.TIMEOUT
    assert decision.fraction == 1.0


def test_timeout_after_partial_take_profit_only_sells_remainder():
    pos = make_position(opened_at=0.0)
    pos.apply_exit(pos.evaluate_exit(realizable_for_multiple(pos, 1.5001), 10.0, BASE_CONFIG))  # tp1: 500 remain
    decision = pos.evaluate_exit(realizable_for_multiple(pos, 1.2), now=300.0, config=BASE_CONFIG)
    assert decision.reason == ExitReason.TIMEOUT
    sold = pos.apply_exit(decision)
    assert sold == 500.0
    assert pos.tokens_remaining == 0.0


def test_no_exit_when_tokens_remaining_is_zero():
    pos = make_position()
    pos.tokens_remaining = 0.0
    decision = pos.evaluate_exit(realizable_for_multiple(pos, 3.0), now=10.0, config=BASE_CONFIG)
    assert decision is None


def test_zero_entry_price_forces_stop_loss_instead_of_crashing():
    pos = Position(mint="Mint1", entry_price_sol=0.0, entry_tokens=1000.0, opened_at=0.0)
    decision = pos.evaluate_exit(1.0, now=10.0, config=BASE_CONFIG)
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


# --- Task 1: realizable proceeds vs spot price (MILESTONE-4-HANDOFF.md 3.1) ---


def test_spot_1_5x_does_not_trigger_take_profit_1_after_fees():
    """The whole point of Task 1: a spot move to 1.5x must NOT trigger
    take-profit-1, because realizable proceeds (after the sell fee and the
    sell's own price impact) are lower. This fails against the old
    spot-based evaluate_exit and passes against the new realizable-proceeds
    one."""
    curve_at_entry = BondingCurveState(
        virtual_token_reserves=1_073_000_000_000_000,
        virtual_sol_reserves=30_000_000_000,
        real_token_reserves=793_100_000_000_000,
        real_sol_reserves=0,
        token_total_supply=1_000_000_000_000_000,
        complete=False,
    )
    position_sol_lamports = 1_000_000  # 0.001 SOL, mirrors config.yaml's default
    tokens_out_raw = sol_to_tokens(curve_at_entry, position_sol_lamports)
    tokens_out_whole = tokens_out_raw / 10**TOKEN_DECIMALS
    # Mirrors main.py's real entry_price_sol computation exactly: an
    # all-in execution price, buy fee and impact already baked in.
    entry_price_sol = (position_sol_lamports / LAMPORTS_PER_SOL) / tokens_out_whole

    pos = Position(
        mint="Mint1", entry_price_sol=entry_price_sol, entry_tokens=tokens_out_whole, opened_at=0.0
    )

    # A later curve state whose SPOT price is exactly 1.5x entry_price_sol.
    entry_price_raw_units = entry_price_sol * LAMPORTS_PER_SOL / 10**TOKEN_DECIMALS
    target_spot_raw = entry_price_raw_units * 1.5
    scaled_virtual_sol = round(target_spot_raw * curve_at_entry.virtual_token_reserves)
    curve_later = BondingCurveState(
        virtual_token_reserves=curve_at_entry.virtual_token_reserves,
        virtual_sol_reserves=scaled_virtual_sol,
        real_token_reserves=curve_at_entry.real_token_reserves,
        real_sol_reserves=curve_at_entry.real_sol_reserves,
        token_total_supply=curve_at_entry.token_total_supply,
        complete=False,
    )
    spot_multiple = (
        spot_price_sol_per_token(curve_later) * 10**TOKEN_DECIMALS / LAMPORTS_PER_SOL
    ) / entry_price_sol
    assert spot_multiple == pytest.approx(1.5, rel=1e-6)

    tokens_remaining_raw = round(pos.tokens_remaining * 10**TOKEN_DECIMALS)
    realizable_sol_value = tokens_to_sol(curve_later, tokens_remaining_raw) / LAMPORTS_PER_SOL

    decision = pos.evaluate_exit(realizable_sol_value, now=10.0, config=BASE_CONFIG)
    assert decision is None


# --- Task 2: trailing drawdown ---


def test_trailing_peak_tracks_rise_then_fall_without_firing_on_the_fall_alone():
    # Stays below take_profit_1_multiple (1.5) throughout so only trailing
    # is exercised -- this is exactly the "stalls short of the first rung"
    # gap trailing_arm_multiple=1.3 is meant to cover.
    pos = make_position()
    pos.evaluate_exit(realizable_for_multiple(pos, 1.35), now=1.0, config=TRAILING_CONFIG)
    assert pos.trailing_peak_multiple == pytest.approx(1.35)
    pos.evaluate_exit(realizable_for_multiple(pos, 1.45), now=2.0, config=TRAILING_CONFIG)
    assert pos.trailing_peak_multiple == pytest.approx(1.45)
    # Falls to 1.4 -- above drawdown_fraction*peak (1.45*0.75=1.0875), no fire.
    decision = pos.evaluate_exit(realizable_for_multiple(pos, 1.4), now=3.0, config=TRAILING_CONFIG)
    assert decision is None
    # Peak must not have been dragged down by the fall.
    assert pos.trailing_peak_multiple == pytest.approx(1.45)


def test_trailing_does_not_arm_below_arm_multiple():
    pos = make_position()
    # Rises to 1.2, below trailing_arm_multiple (1.3), then falls hard.
    pos.evaluate_exit(realizable_for_multiple(pos, 1.2), now=1.0, config=TRAILING_CONFIG)
    assert pos.trailing_armed is False
    decision = pos.evaluate_exit(realizable_for_multiple(pos, 0.9), now=2.0, config=TRAILING_CONFIG)
    assert decision is None  # not stop-loss (-0.35) or anything else either


def test_trailing_does_not_fire_on_monotonic_rise():
    # Stays below take_profit_1_multiple (1.5) so tp1 can't preempt this --
    # a monotonic rise should never trigger the trailing stop regardless.
    pos = make_position()
    for i, multiple in enumerate([1.30, 1.35, 1.40, 1.45, 1.49], start=1):
        decision = pos.evaluate_exit(
            realizable_for_multiple(pos, multiple), now=float(i), config=TRAILING_CONFIG
        )
        assert decision is None


def test_trailing_fires_on_drawdown_from_armed_peak():
    # Peak stays below take_profit_1_multiple (1.5) so tp1 can't preempt --
    # isolates the trailing-stop fire condition itself.
    pos = make_position()
    pos.evaluate_exit(realizable_for_multiple(pos, 1.45), now=1.0, config=TRAILING_CONFIG)  # arms, peak=1.45
    # Drawdown fraction 0.25 of peak 1.45 -> fires at or below 1.0875.
    decision = pos.evaluate_exit(realizable_for_multiple(pos, 1.08), now=2.0, config=TRAILING_CONFIG)
    assert decision.reason == ExitReason.TRAILING_STOP
    assert decision.fraction == 1.0


def test_trailing_disabled_never_fires_even_past_drawdown():
    pos = make_position()
    pos.evaluate_exit(realizable_for_multiple(pos, 1.6), now=1.0, config=BASE_CONFIG)
    decision = pos.evaluate_exit(realizable_for_multiple(pos, 1.0), now=2.0, config=BASE_CONFIG)
    assert decision is None
    assert pos.trailing_armed is False


def test_trailing_peak_survives_partial_take_profit_1_fill_uncorrupted():
    # A peak expressed as a MULTIPLE (not absolute SOL) must not be
    # corrupted when take_profit_1 halves tokens_remaining -- absolute
    # proceeds would drop on the very next tick, which would otherwise look
    # like a drawdown that never happened.
    pos = make_position()
    pos.evaluate_exit(realizable_for_multiple(pos, 1.45), now=1.0, config=TRAILING_CONFIG)  # arms, peak=1.45
    assert pos.trailing_peak_multiple == pytest.approx(1.45)

    decision = pos.evaluate_exit(realizable_for_multiple(pos, 1.5001), now=2.0, config=TRAILING_CONFIG)
    assert decision.reason == ExitReason.TAKE_PROFIT_1
    # Peak updates to 1.5001 on this same tick (1.5001 > 1.45) before the
    # ladder is even evaluated.
    assert pos.trailing_peak_multiple == pytest.approx(1.5001)
    pos.apply_exit(decision)  # halves tokens_remaining -- cost_basis_of_remaining halves too

    # Re-evaluate at the SAME multiple (1.5001) post-fill: peak must still
    # read 1.5001, not have been silently reset by tokens_remaining halving.
    decision = pos.evaluate_exit(realizable_for_multiple(pos, 1.5001), now=3.0, config=TRAILING_CONFIG)
    assert pos.trailing_peak_multiple == pytest.approx(1.5001)
    assert decision is None  # tp1 already hit, tp2 not reached, no drawdown from peak


# --- Task 3: creator-sell exit ---


def test_creator_sold_outranks_everything_including_stop_loss():
    pos = make_position()
    decision = pos.evaluate_exit(
        realizable_for_multiple(pos, 0.5), now=10.0, config=BASE_CONFIG, creator_sold=True
    )
    assert decision.reason == ExitReason.CREATOR_SOLD
    assert decision.fraction == 1.0


def test_creator_sold_false_falls_through_to_normal_ladder():
    pos = make_position()
    decision = pos.evaluate_exit(
        realizable_for_multiple(pos, 1.5001), now=10.0, config=BASE_CONFIG, creator_sold=False
    )
    assert decision.reason == ExitReason.TAKE_PROFIT_1


def test_take_profit_2_outranks_trailing_stop_when_both_conditions_hold():
    # peak=10 puts the trailing-fire floor at 7.5 (10 * 0.75) -- well above
    # take_profit_2_multiple (2.5), so a drop to 7.0 satisfies BOTH the
    # trailing-fire condition and the tp2 condition simultaneously. Ladder
    # order says tp2 wins (a spike past the cap takes the cap).
    pos = make_position()
    pos.evaluate_exit(realizable_for_multiple(pos, 10.0), now=1.0, config=TRAILING_CONFIG)
    assert pos.trailing_peak_multiple == pytest.approx(10.0)
    decision = pos.evaluate_exit(realizable_for_multiple(pos, 7.0), now=2.0, config=TRAILING_CONFIG)
    assert decision.reason == ExitReason.TAKE_PROFIT_2


def test_trailing_stop_outranks_take_profit_1_when_both_conditions_hold():
    # peak=2.4 puts the trailing-fire floor at 1.8 (2.4 * 0.75) -- above
    # take_profit_1_multiple (1.5), so a drop to 1.7 satisfies BOTH the
    # trailing-fire condition and the (not yet hit) tp1 condition
    # simultaneously. Ladder order says trailing wins (a run that rolls
    # over should exit fully, not take a half-profit on the way down).
    pos = make_position()
    pos.evaluate_exit(realizable_for_multiple(pos, 2.4), now=1.0, config=TRAILING_CONFIG)
    assert pos.trailing_peak_multiple == pytest.approx(2.4)
    decision = pos.evaluate_exit(realizable_for_multiple(pos, 1.7), now=2.0, config=TRAILING_CONFIG)
    assert decision.reason == ExitReason.TRAILING_STOP
    assert decision.fraction == 1.0
    assert pos.take_profit_1_hit is False


def test_creator_sold_outranks_trailing_stop():
    pos = make_position()
    pos.evaluate_exit(realizable_for_multiple(pos, 1.6), now=1.0, config=TRAILING_CONFIG)  # arms
    decision = pos.evaluate_exit(
        realizable_for_multiple(pos, 1.5), now=2.0, config=TRAILING_CONFIG, creator_sold=True
    )
    assert decision.reason == ExitReason.CREATOR_SOLD
