"""Tests for scripts/entry_signals.py -- loaded via importlib since it lives
under scripts/, not the pumpbot package (mirrors tests/test_backtest_exits.py's
loading pattern). backtest_exits.py is loaded first and registered under the
plain module name entry_signals.py imports it by (`backtest_exits`), so its
`sys.path.insert` + `import backtest_exits` resolves against this loaded copy
rather than re-executing the file a second time.
"""

import importlib.util
import sys
from pathlib import Path

from pumpbot.curve import LAMPORTS_PER_SOL, TOKEN_DECIMALS

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

_bx_spec = importlib.util.spec_from_file_location("backtest_exits", _SCRIPTS_DIR / "backtest_exits.py")
backtest_exits = importlib.util.module_from_spec(_bx_spec)
sys.modules["backtest_exits"] = backtest_exits
_bx_spec.loader.exec_module(backtest_exits)

_es_spec = importlib.util.spec_from_file_location("pumpbot_entry_signals", _SCRIPTS_DIR / "entry_signals.py")
entry_signals = importlib.util.module_from_spec(_es_spec)
sys.modules[_es_spec.name] = entry_signals
_es_spec.loader.exec_module(entry_signals)


def p_approx(value: float, tol: float = 1e-9):
    class _Approx:
        def __eq__(self, other):
            return abs(other - value) <= tol * max(1.0, abs(value))

    return _Approx()


def _mint(
    ticks,
    *,
    k: int,
    p0: float,
    arm: str = "bought",
    reject_reason: str | None = None,
    creator_supply_fraction: float = 0.05,
    socials=None,
    mint: str = "mint1",
) -> "entry_signals.TrackedMint":
    return entry_signals.TrackedMint(
        mint=mint,
        arm=arm,
        reject_reason=reject_reason,
        k=k,
        p0=p0,
        creator_supply_fraction=creator_supply_fraction,
        ticks=tuple(ticks),
        socials=socials,
    )


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


# --- Section 2.6: t=0 price comes from CandidateSeen, not return_from_first_seen ---


def test_t0_price_uses_candidate_seen_reserves_not_return_from_first_seen():
    events = [
        _envelope(
            "CandidateSeen", False, 0.0, mint="m1", creator="c1", name="n", symbol="s",
            uri="u", creator_supply_fraction=0.05, virtual_sol_in_curve=30.0,
            virtual_tokens_in_curve=1_073_000_000.0, notified_at_wall=0.0,
        ),
        # First shadow poll arrives 9.9s later, itself already 10% above t=0 --
        # return_from_first_seen on this row would show 0.0 (it's the first seen
        # tick), silently discarding that first 9.9s of real movement.
        _envelope(
            "ShadowPrice", True, 9.9, mint="m1", arm="bought", reject_reason=None,
            horizon_elapsed_seconds=9.9, price_sol=30.0 * LAMPORTS_PER_SOL * 1.10
            / (1_073_000_000.0 * 10**TOKEN_DECIMALS),
            return_from_first_seen=0.0, curve_complete=False, trade_id="t1",
        ),
    ]
    mints, diagnostics = entry_signals.load_tracked_mints(events)
    assert diagnostics["usable_mints"] == 1
    mint = mints[0]
    # full_horizon_return must show the true +10% move from t=0, not 0%
    # (what return_from_first_seen would report for a single-tick mint).
    ret = entry_signals.full_horizon_return(mint)
    assert ret == p_approx(0.10, tol=1e-6)


# --- Section 3.1: look-ahead trap ------------------------------------------


def test_velocity_filter_outcome_excludes_the_qualifying_rise_look_ahead():
    """A mint that rises sharply through t=30s then crashes must show a
    NEGATIVE outcome under a filter selecting on that rise -- if the rise
    leaked into the reported outcome, this would show a spurious positive."""
    k = 30_000_000_000 * 1_073_000_000 * 10**TOKEN_DECIMALS
    p0 = 30_000_000_000 / (1_073_000_000 * 10**TOKEN_DECIMALS)

    ticks = [
        (10.0, p0),  # flat until the entry tick
        (31.0, p0 * 1.50),  # +50% by the entry point (this qualifies the filter)
        (300.0, p0 * 0.50),  # then crashes hard, well below the entry price
    ]
    mint = _mint(ticks, k=k, p0=p0)

    vr = entry_signals.velocity_return(mint, delay=30.0)
    assert vr is not None
    _, ret_at_30 = vr
    assert ret_at_30 == p_approx(0.50, tol=1e-6)  # the qualifying rise, correctly measured

    outcome = entry_signals.outcome_from_entry_tick(mint, delay=30.0)
    assert outcome is not None
    # Measured from the T=30s entry price (p0*1.5) to the crash (p0*0.5):
    # 0.5/1.5 - 1 = -66.7%. Must NOT be positive, and must NOT equal the
    # (t=0 -> t=300s) full-horizon return, which would blend in the rise.
    assert outcome.forward_return < 0
    assert outcome.forward_return == p_approx((0.50 / 1.50) - 1.0, tol=1e-6)
    full_horizon = entry_signals.full_horizon_return(mint)
    assert full_horizon == p_approx(-0.50, tol=1e-6)  # t=0 -> t=300s: 0.5x - 1 = -50%
    assert outcome.forward_return != full_horizon


# --- units relationship (carried forward) -----------------------------------


def test_p0_and_shadow_price_sol_share_the_same_scale():
    """p0 is computed the same way ShadowPrice.price_sol is (the raw reserve
    ratio) -- both lamports-per-raw-token, so velocity_return's ratio is a
    same-units comparison, never the 1000x-off ExitFilled.curve_price_sol
    scale (backtest_exits.py's units trap, carried forward per Section 2.7)."""
    virtual_sol_in_curve = 30.488808424128365
    virtual_tokens_in_curve = 1055797247.04903
    vs_lamports = round(virtual_sol_in_curve * LAMPORTS_PER_SOL)
    vt_raw = round(virtual_tokens_in_curve * 10**TOKEN_DECIMALS)
    p0 = vs_lamports / vt_raw

    # Matches the worked example's ShadowPrice.price_sol scale (~2.8e-05),
    # not ExitFilled.curve_price_sol's scale (~2.9e-08).
    assert p0 == p_approx(2.801438778811648e-05, tol=1e-3)


# --- entry/outcome primitives -----------------------------------------------


def test_entry_tick_at_delay_skips_non_positive_price_ticks():
    """A curve_complete migration terminal tick can decode to price_sol=0.0
    -- a degenerate artifact, not a real price of zero (graduating is
    typically a success signal, not a crash). Must never be used as an
    entry point."""
    k = 30_000_000_000 * 1_073_000_000 * 10**TOKEN_DECIMALS
    p0 = 30_000_000_000 / (1_073_000_000 * 10**TOKEN_DECIMALS)
    ticks = [(5.0, 0.0), (20.0, p0 * 1.1)]
    mint = _mint(ticks, k=k, p0=p0)

    entry = entry_signals.entry_tick_at_delay(mint, delay=0.0)
    assert entry == (20.0, p_approx(p0 * 1.1, tol=1e-9))


def test_full_horizon_return_skips_a_trailing_zero_price_tick():
    k = 30_000_000_000 * 1_073_000_000 * 10**TOKEN_DECIMALS
    p0 = 30_000_000_000 / (1_073_000_000 * 10**TOKEN_DECIMALS)
    ticks = [(10.0, p0 * 1.2), (300.0, 0.0)]  # terminal tick is the degenerate artifact
    mint = _mint(ticks, k=k, p0=p0)

    ret = entry_signals.full_horizon_return(mint)
    assert ret == p_approx(0.20, tol=1e-6)  # uses the last USABLE tick, not the literal last one


def test_outcome_from_entry_tick_none_when_no_forward_data_remains():
    k = 30_000_000_000 * 1_073_000_000 * 10**TOKEN_DECIMALS
    p0 = 30_000_000_000 / (1_073_000_000 * 10**TOKEN_DECIMALS)
    ticks = [(10.0, p0), (30.0, p0 * 1.1)]
    mint = _mint(ticks, k=k, p0=p0)

    # Entry at delay=30 lands on the LAST tick -- nothing left to measure forward.
    assert entry_signals.outcome_from_entry_tick(mint, delay=30.0) is None


# --- population split (Section 2.3) -----------------------------------------


def test_tier1_passing_includes_bought_and_skipped_max_concurrent_only():
    mints = [
        _mint([], k=1, p0=1.0, arm="bought", mint="a"),
        _mint([], k=1, p0=1.0, arm="skipped", reject_reason="max_concurrent_positions", mint="b"),
        _mint([], k=1, p0=1.0, arm="skipped", reject_reason="tier2_rejected", mint="c"),
        _mint([], k=1, p0=1.0, arm="rejected", reject_reason="creator_supply_too_high", mint="d"),
    ]
    passing = entry_signals.tier1_passing(mints)
    assert {m.mint for m in passing} == {"a", "b"}


# --- cell_stats / sample floor -----------------------------------------------


def test_cell_stats_marks_insufficient_sample_below_the_floor():
    stats = entry_signals.cell_stats([0.1] * (entry_signals.MIN_SAMPLE_FOR_STATS - 1), population_n=100)
    assert stats.median is None
    assert stats.mean is None
    assert stats.frac_above_zero is None
    assert stats.n == entry_signals.MIN_SAMPLE_FOR_STATS - 1


def test_cell_stats_reports_stats_at_the_floor():
    returns = [0.1] * entry_signals.MIN_SAMPLE_FOR_STATS
    stats = entry_signals.cell_stats(returns, population_n=100)
    assert stats.median == p_approx(0.1)
    assert stats.frac_above_zero == p_approx(1.0)
    assert stats.frac_of_population == p_approx(entry_signals.MIN_SAMPLE_FOR_STATS / 100)


# --- synthetic ladder replay for non-bought mints ---------------------------


def test_synthetic_replay_trade_computes_nonzero_entry_tokens():
    """Regression: sol_to_tokens caps its output via
    curve.real_token_reserves, which bx.reconstruct_curve zeroes (it's
    unused by tokens_to_sol). Using that curve unmodified for a synthetic
    BUY silently floors every entry to 0 tokens. _reconstruct_curve_for_buy
    must avoid this."""
    k = 30_000_000_000 * 1_073_000_000 * 10**TOKEN_DECIMALS
    p0 = 30_000_000_000 / (1_073_000_000 * 10**TOKEN_DECIMALS)
    mint = _mint([(10.0, p0)], k=k, p0=p0)

    trade = entry_signals.synthetic_replay_trade(mint, entry_elapsed=10.0, entry_price_raw=p0, position_sol=0.001)
    assert trade.entry_tokens > 0
    assert trade.entry_price_sol > 0
