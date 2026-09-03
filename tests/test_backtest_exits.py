"""Tests for scripts/backtest_exits.py -- loaded via importlib since it lives
under scripts/, not the pumpbot package (mirrors tests/test_report.py's
loading pattern).
"""

import importlib.util
import sys
from pathlib import Path

from pumpbot.config import ExitsConfig
from pumpbot.curve import LAMPORTS_PER_SOL, TOKEN_DECIMALS, spot_price_sol_per_token

_BACKTEST_PATH = Path(__file__).resolve().parent.parent / "scripts" / "backtest_exits.py"
_spec = importlib.util.spec_from_file_location("pumpbot_backtest_exits", _BACKTEST_PATH)
backtest = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = backtest
_spec.loader.exec_module(backtest)


def _exits_config(**overrides) -> ExitsConfig:
    defaults = {
        "take_profit_1_multiple": 1.5,
        "take_profit_1_fraction": 0.5,
        "take_profit_2_multiple": 2.5,
        "stop_loss_fraction": -0.35,
        "timeout_seconds": 300,
        "trailing_enabled": False,
        "trailing_arm_multiple": 1.3,
        "trailing_drawdown_fraction": 0.2,
        "creator_sell_enabled": True,
    }
    defaults.update(overrides)
    return ExitsConfig(**defaults)


def _tick(ts: float, price_sol_raw: float) -> "backtest.ShadowTick":
    return backtest.ShadowTick(ts_monotonic=ts, price_sol_raw=price_sol_raw)


def _trade(
    ticks,
    *,
    entry_price_sol: float,
    entry_tokens: float,
    k: int,
    recorded_reason: str = "timeout",
    recorded_multiple: float | None = None,
    creator_sold_at: float | None = None,
    opened_at: float = 0.0,
    position_sol: float = 0.001,
    trade_id: str = "t1",
    mint: str = "mint1",
) -> "backtest.ReplayTrade":
    return backtest.ReplayTrade(
        trade_id=trade_id,
        mint=mint,
        entry_price_sol=entry_price_sol,
        entry_tokens=entry_tokens,
        position_sol=position_sol,
        opened_at=opened_at,
        k=k,
        ticks=tuple(ticks),
        recorded_reason=recorded_reason,
        recorded_multiple=recorded_multiple,
        creator_sold_at=creator_sold_at,
    )


# --- reconstruction round-trip (acceptance item 4) --------------------------


def test_reconstruct_curve_round_trips_to_the_same_spot_price():
    # A plausible pump.fun curve: 30 virtual SOL, ~1.07B virtual tokens.
    virtual_sol_reserves = 30_000_000_000
    virtual_token_reserves = 1_073_000_000 * 10**TOKEN_DECIMALS
    k = virtual_sol_reserves * virtual_token_reserves
    p = virtual_sol_reserves / virtual_token_reserves

    curve = backtest.reconstruct_curve(k, p)
    reconstructed_p = curve.virtual_sol_reserves / curve.virtual_token_reserves

    assert reconstructed_p == p_approx(p)


def p_approx(value: float, tol: float = 1e-9):
    class _Approx:
        def __eq__(self, other):
            return abs(other - value) <= tol * max(1.0, abs(value))

    return _Approx()


def test_reconstruct_curve_matches_spot_price_sol_per_token_for_a_moved_curve():
    """After a hypothetical buy moves the curve, k stays invariant and the
    new spot price still round-trips through reconstruct_curve -- this is
    the trick EXIT-RETUNE-HANDOFF.md Section 2.3 relies on for every tick,
    not just the first one."""
    from pumpbot.curve import BondingCurveState

    original = BondingCurveState(
        virtual_sol_reserves=30_000_000_000,
        virtual_token_reserves=1_073_000_000 * 10**TOKEN_DECIMALS,
        real_sol_reserves=0,
        real_token_reserves=793_100_000 * 10**TOKEN_DECIMALS,
        token_total_supply=1_000_000_000 * 10**TOKEN_DECIMALS,
        complete=False,
    )
    k = original.virtual_sol_reserves * original.virtual_token_reserves

    # Simulate a buy: sol in, new virtual sol reserves, tokens out via k.
    new_virtual_sol = original.virtual_sol_reserves + 5_000_000_000
    new_virtual_token = k // new_virtual_sol
    moved = BondingCurveState(
        virtual_sol_reserves=new_virtual_sol,
        virtual_token_reserves=new_virtual_token,
        real_sol_reserves=0,
        real_token_reserves=0,
        token_total_supply=0,
        complete=False,
    )
    p_moved = spot_price_sol_per_token(moved)

    reconstructed = backtest.reconstruct_curve(k, p_moved)
    assert reconstructed.virtual_sol_reserves == p_approx(new_virtual_sol, tol=1e-6)
    assert reconstructed.virtual_token_reserves == p_approx(new_virtual_token, tol=1e-6)


# --- units trap (acceptance item 5) -----------------------------------------


def test_shadow_price_sol_and_exit_filled_curve_price_sol_differ_by_1000x():
    """Section 2.4: ShadowPrice.price_sol is lamports-per-raw-token (the
    reserve ratio); ExitFilled.curve_price_sol is SOL-per-whole-token. The
    conversion factor is exactly 10**TOKEN_DECIMALS / LAMPORTS_PER_SOL."""
    virtual_sol_in_curve = 30.488808424128365
    virtual_tokens_in_curve = 1055797247.04903

    shadow_price_sol = (virtual_sol_in_curve * LAMPORTS_PER_SOL) / (
        virtual_tokens_in_curve * 10**TOKEN_DECIMALS
    )
    exit_filled_curve_price_sol = virtual_sol_in_curve / virtual_tokens_in_curve

    conversion_factor = 10**TOKEN_DECIMALS / LAMPORTS_PER_SOL
    assert exit_filled_curve_price_sol == p_approx(
        shadow_price_sol * conversion_factor, tol=1e-9
    )
    assert conversion_factor == 1e-3


# --- synthetic ladder-ordering test (Task 1) --------------------------------


def test_synthetic_price_path_fires_take_profit_1_then_take_profit_2():
    """A hand-built path that climbs straight through both rungs must fire
    take_profit_1 (partial) then take_profit_2 (the remainder) -- exercising
    the real evaluate_exit ordering, not a reimplementation of it."""
    entry_tokens = 1000.0
    entry_price_sol = 0.00003  # SOL per whole token
    k = 30_000_000_000 * 1_073_000_000 * 10**TOKEN_DECIMALS

    def price_for_multiple(multiple: float) -> float:
        # crude but sufficient: spot price scaled by the target multiple,
        # ignoring price impact -- good enough to cross clean rung boundaries.
        return entry_price_sol * multiple * LAMPORTS_PER_SOL / 10**TOKEN_DECIMALS

    ticks = [
        _tick(10.0, price_for_multiple(1.0)),
        _tick(25.0, price_for_multiple(1.6)),  # crosses take_profit_1 (1.5)
        _tick(40.0, price_for_multiple(3.0)),  # crosses take_profit_2 (2.5)
    ]
    trade = _trade(
        ticks,
        entry_price_sol=entry_price_sol,
        entry_tokens=entry_tokens,
        k=k,
        opened_at=0.0,
    )
    cfg = _exits_config(
        take_profit_1_multiple=1.5,
        take_profit_1_fraction=0.5,
        take_profit_2_multiple=2.5,
        trailing_enabled=False,
    )

    result = backtest.replay_trade(trade, cfg)
    assert result.exit_reason == "take_profit_2"
    assert result.ticks_consumed == 3


def test_synthetic_price_path_fires_stop_loss():
    entry_tokens = 1000.0
    entry_price_sol = 0.00003
    k = 30_000_000_000 * 1_073_000_000 * 10**TOKEN_DECIMALS

    def price_for_multiple(multiple: float) -> float:
        return entry_price_sol * multiple * LAMPORTS_PER_SOL / 10**TOKEN_DECIMALS

    ticks = [
        _tick(10.0, price_for_multiple(1.0)),
        _tick(25.0, price_for_multiple(0.5)),  # below stop_loss_fraction=-0.35 -> multiple<=0.65
    ]
    trade = _trade(ticks, entry_price_sol=entry_price_sol, entry_tokens=entry_tokens, k=k)
    cfg = _exits_config(stop_loss_fraction=-0.35, trailing_enabled=False)

    result = backtest.replay_trade(trade, cfg)
    assert result.exit_reason == "stop_loss"


def test_creator_sold_outranks_a_simultaneous_take_profit():
    """Ladder ordering: creator_sold beats every price rung, even when the
    price at that same tick also clears take_profit_2 (positions.py's own
    ordering, driven here rather than re-asserted against a mock)."""
    entry_tokens = 1000.0
    entry_price_sol = 0.00003
    k = 30_000_000_000 * 1_073_000_000 * 10**TOKEN_DECIMALS

    def price_for_multiple(multiple: float) -> float:
        return entry_price_sol * multiple * LAMPORTS_PER_SOL / 10**TOKEN_DECIMALS

    ticks = [_tick(10.0, price_for_multiple(3.0))]  # would clear take_profit_2 alone
    trade = _trade(
        ticks,
        entry_price_sol=entry_price_sol,
        entry_tokens=entry_tokens,
        k=k,
        creator_sold_at=10.0,
    )
    cfg = _exits_config(take_profit_2_multiple=2.5, trailing_enabled=False)

    result = backtest.replay_trade(trade, cfg)
    assert result.exit_reason == "creator_sold"


def test_suppress_creator_sold_lets_the_price_ladder_decide():
    entry_tokens = 1000.0
    entry_price_sol = 0.00003
    k = 30_000_000_000 * 1_073_000_000 * 10**TOKEN_DECIMALS

    def price_for_multiple(multiple: float) -> float:
        return entry_price_sol * multiple * LAMPORTS_PER_SOL / 10**TOKEN_DECIMALS

    ticks = [_tick(10.0, price_for_multiple(3.0))]
    trade = _trade(
        ticks,
        entry_price_sol=entry_price_sol,
        entry_tokens=entry_tokens,
        k=k,
        creator_sold_at=10.0,
    )
    cfg = _exits_config(take_profit_2_multiple=2.5, trailing_enabled=False)

    result = backtest.replay_trade(trade, cfg, suppress_creator_sold=True)
    assert result.exit_reason == "take_profit_2"


# --- join / diagnostics -----------------------------------------------------


def _envelope(event: str, dry_run: bool, ts: float, **fields):
    return {
        "event": event,
        "schema": 1,
        "ts_wall": ts,
        "ts_monotonic": ts,
        "run_id": "run-1",
        "dry_run": dry_run,
        **fields,
    }


def test_load_trades_reports_missing_shadow_path_rather_than_silently_dropping():
    events = [
        _envelope(
            "CandidateSeen", False, 0.0, mint="m1", creator="c1", name="n", symbol="s",
            uri="u", creator_supply_fraction=0.1, virtual_sol_in_curve=30.0,
            virtual_tokens_in_curve=1_073_000_000.0, notified_at_wall=0.0,
        ),
        _envelope(
            "EntryFilled", True, 1.0, mint="m1", trade_id="t1", signature=None,
            position_sol=0.001, tokens_bought=1000.0, entry_price_sol=0.00003,
            latency_seconds=0.1, settlement=None,
        ),
        # No ShadowPrice rows for t1 -- join should report it, not drop silently.
        _envelope(
            "TradeClosed", True, 300.0, mint="m1", trade_id="t1",
            realized_pnl_lamports=None, rent_recovered_lamports=None,
            total_fee_lamports=None, gross_turnover_lamports=None,
            hold_seconds=299.0, leg_count=2, exit_reasons=["timeout"],
            settlement_complete=True,
        ),
    ]
    trades, diagnostics = backtest.load_trades(events)
    assert trades == []
    assert diagnostics["missing_shadow_path"] == 1
    assert diagnostics["replayable_trades"] == 0


def test_load_trades_joins_a_complete_trade():
    events = [
        _envelope(
            "CandidateSeen", False, 0.0, mint="m1", creator="c1", name="n", symbol="s",
            uri="u", creator_supply_fraction=0.1, virtual_sol_in_curve=30.0,
            virtual_tokens_in_curve=1_073_000_000.0, notified_at_wall=0.0,
        ),
        _envelope(
            "EntryFilled", True, 1.0, mint="m1", trade_id="t1", signature=None,
            position_sol=0.001, tokens_bought=1000.0, entry_price_sol=0.00003,
            latency_seconds=0.1, settlement=None,
        ),
        _envelope(
            "ShadowPrice", True, 5.0, mint="m1", arm="bought", reject_reason=None,
            horizon_elapsed_seconds=4.0, price_sol=2.8e-8, return_from_first_seen=None,
            curve_complete=False, trade_id="t1",
        ),
        _envelope(
            "ShadowPrice", True, 20.0, mint="m1", arm="bought", reject_reason=None,
            horizon_elapsed_seconds=19.0, price_sol=2.9e-8, return_from_first_seen=0.03,
            curve_complete=False, trade_id="t1",
        ),
        _envelope(
            "ExitFilled", True, 300.0, mint="m1", trade_id="t1", signature=None,
            exit_reason="timeout", tokens_sold=1000.0, tokens_remaining=0.0,
            curve_price_sol=2.9e-8, realizable_multiple=0.98, peak_multiple=1.01,
            settlement=None,
        ),
        _envelope(
            "TradeClosed", True, 300.0, mint="m1", trade_id="t1",
            realized_pnl_lamports=None, rent_recovered_lamports=None,
            total_fee_lamports=None, gross_turnover_lamports=None,
            hold_seconds=299.0, leg_count=2, exit_reasons=["timeout"],
            settlement_complete=True,
        ),
    ]
    trades, diagnostics = backtest.load_trades(events)
    assert diagnostics["replayable_trades"] == 1
    assert len(trades) == 1
    trade = trades[0]
    assert trade.trade_id == "t1"
    assert trade.recorded_reason == "timeout"
    assert trade.recorded_multiple == 0.98
    assert trade.creator_sold_at is None
    assert len(trade.ticks) == 2


# --- net breakeven ------------------------------------------------------------


def test_net_breakeven_multiple_matches_the_handoffs_worked_example():
    """Section 2.10: at position_sol=0.001 SOL, a two-signature round trip
    at 5,000 lamports each is 1.0% of the position -- breakeven ~1.01."""
    trade = _trade([], entry_price_sol=0.00003, entry_tokens=1000.0, k=1, position_sol=0.001)
    assert backtest.net_breakeven_multiple(trade) == p_approx(1.01, tol=1e-9)
