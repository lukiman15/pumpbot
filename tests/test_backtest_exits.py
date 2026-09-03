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


# --- TAIL-ANALYSIS-HANDOFF.md Task 1: objective is a parameter, not hardcoded ---


def test_objective_value_covers_all_four_objectives():
    summary = backtest.ComboSummary(
        params={},
        n=4,
        median_multiple=1.0,
        mean_multiple=1.1,
        total_return=0.4,
        net_mean=0.05,
        frac_above_gross_breakeven=0.5,
        frac_above_net_breakeven=0.5,
        exit_reason_counts={},
        multiples=(0.9, 1.0, 1.1, 1.4),
    )
    assert backtest.objective_value(summary, "median") == 1.0
    assert backtest.objective_value(summary, "mean") == 1.1
    assert backtest.objective_value(summary, "total_return") == 0.4
    assert backtest.objective_value(summary, "net_mean") == 0.05


def test_objective_value_rejects_an_unknown_objective():
    summary = backtest.ComboSummary(
        params={}, n=0, median_multiple=0, mean_multiple=0, total_return=0, net_mean=0,
        frac_above_gross_breakeven=0, frac_above_net_breakeven=0, exit_reason_counts={},
        multiples=(),
    )
    try:
        backtest.objective_value(summary, "sharpe")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_find_rank_ranks_by_the_requested_objective_not_always_median():
    """A combo that ranks last by median but first by mean must be found at
    rank 1 when objective='mean' -- this is the whole point of Task 1: median
    and mean can disagree, and find_rank must not silently prefer median."""
    low_median_high_mean = {"id": "a"}
    high_median_low_mean = {"id": "b"}
    summaries = [
        backtest.ComboSummary(
            params=low_median_high_mean, n=1, median_multiple=0.5, mean_multiple=5.0,
            total_return=0.0, net_mean=0.0, frac_above_gross_breakeven=0, frac_above_net_breakeven=0,
            exit_reason_counts={}, multiples=(),
        ),
        backtest.ComboSummary(
            params=high_median_low_mean, n=1, median_multiple=2.0, mean_multiple=1.0,
            total_return=0.0, net_mean=0.0, frac_above_gross_breakeven=0, frac_above_net_breakeven=0,
            exit_reason_counts={}, multiples=(),
        ),
    ]
    assert backtest.find_rank(summaries, low_median_high_mean, objective="median") == 2
    assert backtest.find_rank(summaries, low_median_high_mean, objective="mean") == 1
    assert backtest.find_rank(summaries, high_median_low_mean, objective="median") == 1
    assert backtest.find_rank(summaries, high_median_low_mean, objective="mean") == 2


def test_summarize_combo_computes_total_return_and_net_mean():
    trade_a = _trade(
        [_tick(10.0, 1.0)], entry_price_sol=0.00003, entry_tokens=1000.0, k=1,
        position_sol=0.001, trade_id="a",
    )
    trade_b = _trade(
        [_tick(10.0, 1.0)], entry_price_sol=0.00003, entry_tokens=1000.0, k=1,
        position_sol=0.001, trade_id="b",
    )
    cfg = _exits_config(timeout_seconds=5)  # fires immediately via timeout at tick 10s

    class _FakeReplay:
        def __init__(self, multiple):
            self.final_multiple = multiple
            self.exit_reason = "timeout"

    orig_replay_trade = backtest.replay_trade
    try:
        multiples_by_id = {"a": 1.2, "b": 0.8}
        backtest.replay_trade = lambda trade, cfg, **kw: _FakeReplay(multiples_by_id[trade.trade_id])
        summary = backtest.summarize_combo({}, cfg, [trade_a, trade_b])
    finally:
        backtest.replay_trade = orig_replay_trade

    assert summary.multiples == (1.2, 0.8)
    assert summary.total_return == p_approx(0.2 + (-0.2), tol=1e-9)
    net_breakeven = backtest.net_breakeven_multiple(trade_a)  # same for both, same position_sol
    expected_net_mean = ((1.2 - net_breakeven) + (0.8 - net_breakeven)) / 2
    assert summary.net_mean == p_approx(expected_net_mean, tol=1e-9)


# --- Task 2: tail diagnostics -----------------------------------------------


def test_trimmed_mean_sensitivity_drops_the_largest_values():
    multiples = [1.0, 1.0, 1.0, 1.0, 1.0, 5.0, 4.0, 3.0]
    result = backtest.trimmed_mean_sensitivity(multiples)
    assert result["mean_all"] == p_approx(sum(multiples) / len(multiples))
    remaining_minus_1 = sorted(multiples, reverse=True)[1:]
    assert result["mean_minus_top_1"] == p_approx(sum(remaining_minus_1) / len(remaining_minus_1))
    remaining_minus_3 = sorted(multiples, reverse=True)[3:]
    assert result["mean_minus_top_3"] == p_approx(sum(remaining_minus_3) / len(remaining_minus_3))


def test_bootstrap_ci_mean_is_reproducible_and_brackets_a_constant_series():
    constant = [1.5] * 50
    ci_a = backtest.bootstrap_ci_mean(constant, n_resamples=500, seed=42)
    ci_b = backtest.bootstrap_ci_mean(constant, n_resamples=500, seed=42)
    assert ci_a == ci_b  # same seed -> reproducible
    assert ci_a["lo"] == p_approx(1.5, tol=1e-9)
    assert ci_a["hi"] == p_approx(1.5, tol=1e-9)
    assert ci_a["contains_1_0"] is False
    assert ci_a["contains_net_breakeven"] is False


def test_bootstrap_ci_mean_empty_series_is_n_zero_safe():
    ci = backtest.bootstrap_ci_mean([], n_resamples=100)
    assert ci == {"lo": None, "hi": None, "contains_1_0": None, "contains_net_breakeven": None}


def test_tail_event_rates_counts_and_rates_at_each_threshold():
    multiples = [0.5, 1.0, 1.6, 2.1, 2.6, 3.0]
    result = backtest.tail_event_rates(multiples)
    assert result["n"] == 6
    assert result["counts"] == {1.5: 4, 2.0: 3, 2.5: 2}
    assert result["rates"][1.5] == p_approx(4 / 6)
    assert result["rates"][2.0] == p_approx(3 / 6)
    assert result["rates"][2.5] == p_approx(2 / 6)


def test_required_n_for_tail_events_scales_inversely_with_rate():
    assert backtest.required_n_for_tail_events(0.02, target_events=30) == p_approx(1500.0)
    assert backtest.required_n_for_tail_events(0.0, target_events=30) is None


# --- Task 3: extended cap grid ------------------------------------------------


def test_extended_sweep_grid_includes_the_uncapped_value_and_disables_via_config_only():
    """1e9 must disable the take_profit_2 rung purely through the config value
    evaluate_exit already reads (`multiple >= config.take_profit_2_multiple`) --
    no special case anywhere in positions.py."""
    assert backtest.UNCAPPED_TAKE_PROFIT_2_MULTIPLE in backtest.EXTENDED_SWEEP_GRID[
        "take_profit_2_multiple"
    ]
    assert 6.0 in backtest.EXTENDED_SWEEP_GRID["take_profit_2_multiple"]
    assert 10.0 in backtest.EXTENDED_SWEEP_GRID["take_profit_2_multiple"]

    entry_price_sol = 0.00003
    k = 30_000_000_000 * 1_073_000_000 * 10**TOKEN_DECIMALS

    def price_for_multiple(multiple: float) -> float:
        return entry_price_sol * multiple * LAMPORTS_PER_SOL / 10**TOKEN_DECIMALS

    ticks = [_tick(10.0, price_for_multiple(3.0))]  # would have cleared tp2=2.5
    trade = _trade(ticks, entry_price_sol=entry_price_sol, entry_tokens=1000.0, k=k)
    cfg = _exits_config(
        take_profit_2_multiple=backtest.UNCAPPED_TAKE_PROFIT_2_MULTIPLE, trailing_enabled=False
    )
    result = backtest.replay_trade(trade, cfg)
    assert result.exit_reason != "take_profit_2"


def test_iter_sweep_configs_accepts_an_explicit_grid():
    base = _exits_config()
    combos, skipped = backtest.iter_sweep_configs(base, grid=backtest.EXTENDED_SWEEP_GRID)
    tp2_values = {p["take_profit_2_multiple"] for p, _ in combos}
    assert backtest.UNCAPPED_TAKE_PROFIT_2_MULTIPLE in tp2_values
    assert skipped > 0  # tp2<=tp1 combinations still skipped in the extended grid
