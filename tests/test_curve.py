import struct

import pytest

from pumpbot.curve import (
    FEE_BPS,
    BondingCurveState,
    CurveCompleteError,
    curve_completion_fraction,
    decode_bonding_curve,
    from_virtual_reserves,
    price_impact_fraction,
    sol_to_tokens,
    spot_price_sol_per_token,
    tokens_to_sol,
)

FRESH_CURVE = BondingCurveState(
    virtual_token_reserves=1_073_000_000_000_000,
    virtual_sol_reserves=30_000_000_000,
    real_token_reserves=793_100_000_000_000,
    real_sol_reserves=0,
    token_total_supply=1_000_000_000_000_000,
    complete=False,
)


def test_sol_to_tokens_positive_for_positive_input():
    tokens_out = sol_to_tokens(FRESH_CURVE, 1_000_000_000)  # 1 SOL
    assert tokens_out > 0
    assert tokens_out < FRESH_CURVE.real_token_reserves


def test_sol_to_tokens_zero_for_zero_input():
    assert sol_to_tokens(FRESH_CURVE, 0) == 0


def test_round_trip_buy_then_sell_loses_money_to_fees_only():
    sol_in = 1_000_000_000
    fee = sol_in * FEE_BPS // 10_000
    net_sol_in = sol_in - fee
    tokens_out = sol_to_tokens(FRESH_CURVE, sol_in)

    # Sell back into the *post-buy* curve state -- selling into the unchanged
    # original reserves isn't a valid inverse of a constant-product trade.
    post_buy = BondingCurveState(
        **{
            **FRESH_CURVE.__dict__,
            "virtual_sol_reserves": FRESH_CURVE.virtual_sol_reserves + net_sol_in,
            "virtual_token_reserves": FRESH_CURVE.virtual_token_reserves - tokens_out,
        }
    )
    sol_back = tokens_to_sol(post_buy, tokens_out)

    # Two 1% fees compound to ~2% loss, with no curve drift since reserves
    # exactly reverse under the constant-product invariant.
    assert sol_back < sol_in
    assert sol_back > sol_in * 0.97


def test_larger_buy_has_worse_price_impact():
    small = price_impact_fraction(FRESH_CURVE, 100_000_000)
    large = price_impact_fraction(FRESH_CURVE, 10_000_000_000)
    assert large > small


def test_spot_price_positive():
    assert spot_price_sol_per_token(FRESH_CURVE) > 0


def test_complete_curve_raises_on_trade():
    completed = BondingCurveState(**{**FRESH_CURVE.__dict__, "complete": True})
    with pytest.raises(CurveCompleteError):
        sol_to_tokens(completed, 1_000_000_000)
    with pytest.raises(CurveCompleteError):
        tokens_to_sol(completed, 1_000_000)


def test_curve_completion_fraction_bounds():
    assert curve_completion_fraction(FRESH_CURVE) == 0.0
    nearly_done = BondingCurveState(
        **{**FRESH_CURVE.__dict__, "real_sol_reserves": 85_000_000_000}
    )
    assert curve_completion_fraction(nearly_done) == 1.0
    overshoot = BondingCurveState(
        **{**FRESH_CURVE.__dict__, "real_sol_reserves": 999_000_000_000}
    )
    assert curve_completion_fraction(overshoot) == 1.0


def test_decode_bonding_curve_round_trip():
    raw = struct.pack(
        "<8s5QB",
        b"\x00" * 8,
        FRESH_CURVE.virtual_token_reserves,
        FRESH_CURVE.virtual_sol_reserves,
        FRESH_CURVE.real_token_reserves,
        FRESH_CURVE.real_sol_reserves,
        FRESH_CURVE.token_total_supply,
        0,
    )
    decoded = decode_bonding_curve(raw)
    assert decoded == FRESH_CURVE


def test_decode_bonding_curve_too_short_raises():
    with pytest.raises(ValueError):
        decode_bonding_curve(b"\x00" * 10)


def test_from_virtual_reserves_fresh_mint_matches_known_defaults():
    # A brand-new mint with no dev buy: virtual reserves are exactly the
    # documented initial defaults.
    curve = from_virtual_reserves(virtual_sol=30.0, virtual_tokens=1_073_000_000.0)
    assert curve.virtual_sol_reserves == 30_000_000_000
    assert curve.virtual_token_reserves == 1_073_000_000_000_000
    assert curve.real_sol_reserves == 0
    assert curve.real_token_reserves == 793_100_000_000_000
    assert not curve.complete


def test_from_virtual_reserves_live_sample_with_dev_buy():
    # Captured live from PumpPortal: a create with a small creator dev buy.
    curve = from_virtual_reserves(
        virtual_sol=30.00098765299999, virtual_tokens=1072964676.107292
    )
    # SOL raised so far should be ~0.001 SOL (solAmount from the same sample).
    assert curve.real_sol_reserves == pytest.approx(987653, abs=10)
    # Real token reserves should have dropped by the same raw amount sold.
    assert curve.real_token_reserves < 793_100_000_000_000
    assert curve.token_total_supply == 1_000_000_000_000_000
