"""Tests for scripts/horizon_run.py -- loaded via importlib since it lives
under scripts/, not the pumpbot package (mirrors tests/test_backtest_exits.py
and tests/test_entry_signals.py's loading pattern). backtest_exits.py is
loaded first and registered under the plain module name horizon_run.py
imports it by (`backtest_exits`), so its `sys.path.insert` +
`import backtest_exits` resolves against this loaded copy.
"""

import importlib.util
import sys
from pathlib import Path

from pumpbot.curve import LAMPORTS_PER_SOL, MIGRATION_SOL_LAMPORTS, TOKEN_DECIMALS
from pumpbot.ledger import read_events

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

_bx_spec = importlib.util.spec_from_file_location("backtest_exits", _SCRIPTS_DIR / "backtest_exits.py")
backtest_exits = importlib.util.module_from_spec(_bx_spec)
sys.modules["backtest_exits"] = backtest_exits
_bx_spec.loader.exec_module(backtest_exits)

_hr_spec = importlib.util.spec_from_file_location("pumpbot_horizon_run", _SCRIPTS_DIR / "horizon_run.py")
horizon_run = importlib.util.module_from_spec(_hr_spec)
sys.modules[_hr_spec.name] = horizon_run
_hr_spec.loader.exec_module(horizon_run)


def p_approx(value: float, tol: float = 1e-9):
    class _Approx:
        def __eq__(self, other):
            return abs(other - value) <= tol * max(1.0, abs(value))

    return _Approx()


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


def _candidate_seen(mint, ts, *, virtual_sol=30.0, virtual_tokens=1_073_000_000.0, creator_supply=0.05):
    return _envelope(
        "CandidateSeen", False, ts, mint=mint, creator="c1", name="n", symbol="s", uri="u",
        creator_supply_fraction=creator_supply, virtual_sol_in_curve=virtual_sol,
        virtual_tokens_in_curve=virtual_tokens, notified_at_wall=ts,
    )


def _shadow_price(mint, ts, *, arm="rejected", reject_reason="creator_supply_too_high",
                   price_sol, curve_complete=False, trade_id=None, elapsed=None):
    return _envelope(
        "ShadowPrice", True, ts, mint=mint, arm=arm, reject_reason=reject_reason,
        horizon_elapsed_seconds=elapsed if elapsed is not None else ts,
        price_sol=price_sol, return_from_first_seen=0.0, curve_complete=curve_complete,
        trade_id=trade_id,
    )


def _p0(virtual_sol=30.0, virtual_tokens=1_073_000_000.0):
    vs_lamports = round(virtual_sol * LAMPORTS_PER_SOL)
    vt_raw = round(virtual_tokens * 10**TOKEN_DECIMALS)
    return vs_lamports / vt_raw


# --- Section 2.3 percentile reproduction ------------------------------------


def test_completion_percentiles_reproduce_section_2_3_on_real_ledger_data():
    from pumpbot.config import PROJECT_ROOT as ROOT
    from pumpbot.config import load_settings

    settings = load_settings()
    ledger_dir = (ROOT / settings.config.ledger.path).parent
    events = list(read_events(ledger_dir))
    mints, diagnostics = horizon_run.load_horizon_mints(events)

    assert diagnostics["usable_mints"] == 578
    assert diagnostics["missing_candidate_seen"] == 0

    pct = horizon_run.completion_percentiles(mints)
    assert pct["n"] == 578
    # The two figures the rest of the analysis keys off (cohort size, max)
    # reproduce exactly.
    assert pct["count_ge_10pct"] == 76
    assert pct["count_ge_25pct"] == 29
    assert pct["max"] == p_approx(0.9183, tol=1e-3)
    assert pct["p95"] == p_approx(0.2309, tol=1e-3)
    assert pct["p99"] == p_approx(0.5058, tol=1e-3)
    # p50/p75/p90 land close (percentile-convention noise, documented in
    # horizon_run.completion_percentiles) but are not required to match to
    # the same precision as the cohort-defining figures above.
    assert 0.0 < pct["p50"] < 0.01
    assert 0.03 < pct["p75"] < 0.04
    assert 0.13 < pct["p90"] < 0.16


def test_completion_fraction_matches_curve_completion_fraction_formula():
    """completion_fraction's real_sol derivation must agree with
    curve.curve_completion_fraction's own real_sol_reserves/MIGRATION math,
    just computed from a reconstructed (k, price) pair instead of a decoded
    on-chain account."""
    from pumpbot.curve import curve_completion_fraction

    virtual_sol = 50.0  # partway to migration
    virtual_tokens = 700_000_000.0
    vs_lamports = round(virtual_sol * LAMPORTS_PER_SOL)
    vt_raw = round(virtual_tokens * 10**TOKEN_DECIMALS)
    k = vs_lamports * vt_raw
    price = vs_lamports / vt_raw

    curve = backtest_exits.reconstruct_curve(k, price)
    real_sol_reserves = max(0, curve.virtual_sol_reserves - round(30.0 * LAMPORTS_PER_SOL))
    expected = curve_completion_fraction(
        curve.__class__(
            virtual_sol_reserves=curve.virtual_sol_reserves,
            virtual_token_reserves=curve.virtual_token_reserves,
            real_sol_reserves=real_sol_reserves,
            real_token_reserves=0,
            token_total_supply=0,
            complete=False,
        )
    )
    assert horizon_run.completion_fraction(k, price) == p_approx(expected, tol=1e-6)


def test_completion_fraction_capped_at_1_0_past_migration():
    k = round(200.0 * LAMPORTS_PER_SOL) * round(300_000_000.0 * 10**TOKEN_DECIMALS)
    price = (200.0 * LAMPORTS_PER_SOL) / (300_000_000.0 * 10**TOKEN_DECIMALS)
    assert horizon_run.completion_fraction(k, price) == 1.0


# --- graduation-aware outcome classification (Section 3.4) ------------------


def test_graduated_mint_is_not_scored_as_total_loss_and_keeps_last_valid_price():
    p0 = _p0()
    events = [
        _candidate_seen("m1", 0.0),
        _shadow_price("m1", 10.0, price_sol=p0 * 2.0, elapsed=10.0),
        _shadow_price("m1", 40.0, price_sol=p0 * 5.0, elapsed=40.0),
        # Migration terminal tick: degenerate zero price, curve_complete=True.
        _shadow_price("m1", 55.0, price_sol=0.0, curve_complete=True, elapsed=55.0),
    ]
    mints, _ = horizon_run.load_horizon_mints(events)
    assert len(mints) == 1
    outcome = horizon_run.classify_outcome(mints[0])

    assert outcome.category == "graduated"
    assert outcome.graduation_elapsed == p_approx(55.0)
    # Last valid on-curve price must be the pre-graduation tick, never the
    # degenerate curve_complete row's price_sol=0.0.
    assert outcome.last_valid_price == p_approx(p0 * 5.0)
    assert outcome.last_valid_price > 0
    assert outcome.peak_multiple == p_approx(5.0)
    # Must not raise -- the whole point of the defensive check.
    horizon_run.assert_never_priced_as_total_loss(outcome)


def test_graduated_mint_is_not_silently_dropped_from_the_join():
    events = [
        _candidate_seen("m1", 0.0),
        _shadow_price("m1", 5.0, price_sol=0.0, curve_complete=True, elapsed=5.0),
    ]
    mints, diagnostics = horizon_run.load_horizon_mints(events)
    assert diagnostics["usable_mints"] == 1
    assert len(mints) == 1
    outcome = horizon_run.classify_outcome(mints[0])
    assert outcome.category == "graduated"


def test_still_open_at_horizon_when_peak_is_the_last_tick():
    p0 = _p0()
    events = [
        _candidate_seen("m1", 0.0),
        _shadow_price("m1", 100.0, price_sol=p0 * 1.2, elapsed=100.0),
        _shadow_price("m1", 300.0, price_sol=p0 * 3.0, elapsed=300.0),  # still rising at cutoff
    ]
    mints, _ = horizon_run.load_horizon_mints(events)
    outcome = horizon_run.classify_outcome(mints[0])
    assert outcome.category == "still_open_at_horizon"
    assert outcome.peak_elapsed == p_approx(300.0)


def test_faded_when_price_recedes_from_an_earlier_peak():
    p0 = _p0()
    events = [
        _candidate_seen("m1", 0.0),
        _shadow_price("m1", 60.0, price_sol=p0 * 4.0, elapsed=60.0),  # the peak
        _shadow_price("m1", 300.0, price_sol=p0 * 1.5, elapsed=300.0),  # receded by cutoff
    ]
    mints, _ = horizon_run.load_horizon_mints(events)
    outcome = horizon_run.classify_outcome(mints[0])
    assert outcome.category == "faded"
    assert outcome.peak_elapsed == p_approx(60.0)
    assert outcome.peak_multiple == p_approx(4.0)


# --- CurveCompleteError path --------------------------------------------------


def test_curve_complete_ticks_never_reach_curve_math_that_would_raise():
    """A curve_complete=True tick's price_sol is a degenerate decode
    artifact, sometimes literally 0.0 -- feeding it through
    reconstruct_curve/completion_fraction would raise ValueError (price<=0)
    or, if fed through tokens_to_sol/sol_to_tokens on a real completed
    curve, CurveCompleteError. peak_completion_fraction and classify_outcome
    must both skip these ticks entirely rather than letting either error
    surface."""
    p0 = _p0()
    events = [
        _candidate_seen("m1", 0.0),
        _shadow_price("m1", 10.0, price_sol=p0 * 2.0, elapsed=10.0),
        _shadow_price("m1", 20.0, price_sol=0.0, curve_complete=True, elapsed=20.0),
    ]
    mints, _ = horizon_run.load_horizon_mints(events)
    mint = mints[0]

    # Must not raise.
    peak = horizon_run.peak_completion_fraction(mint)
    outcome = horizon_run.classify_outcome(mint)

    assert peak >= 0.0
    assert outcome.category == "graduated"

    import pytest

    from pumpbot.curve import CurveCompleteError, from_virtual_reserves, tokens_to_sol

    completed_curve = from_virtual_reserves(virtual_sol=90.0, virtual_tokens=500_000_000.0)
    completed_curve = completed_curve.__class__(
        virtual_sol_reserves=completed_curve.virtual_sol_reserves,
        virtual_token_reserves=completed_curve.virtual_token_reserves,
        real_sol_reserves=completed_curve.real_sol_reserves,
        real_token_reserves=completed_curve.real_token_reserves,
        token_total_supply=completed_curve.token_total_supply,
        complete=True,
    )
    with pytest.raises(CurveCompleteError):
        tokens_to_sol(completed_curve, 1000)


# --- concurrency and saturation (Section 2.5) --------------------------------


def test_concurrency_report_finds_overlap_peak():
    p0 = _p0()
    events = [
        _candidate_seen("m1", 0.0),
        _shadow_price("m1", 5.0, price_sol=p0, elapsed=5.0),
        _shadow_price("m1", 15.0, price_sol=p0, elapsed=15.0),  # m1 tracked [0, 15]
        _candidate_seen("m2", 10.0),
        _shadow_price("m2", 20.0, price_sol=p0, elapsed=10.0),  # m2 tracked [10, 20] -- overlaps m1
        _candidate_seen("m3", 100.0),
        _shadow_price("m3", 110.0, price_sol=p0, elapsed=10.0),  # m3 tracked [100, 110] -- no overlap
    ]
    mints, _ = horizon_run.load_horizon_mints(events)
    report = horizon_run.concurrency_report(mints, events)
    assert report.tracked_mint_count == 3
    assert report.peak_concurrent == 2  # m1 and m2 overlap; m3 does not


def test_concurrency_report_flags_a_bought_mint_missing_from_the_shadow_join():
    p0 = _p0()
    events = [
        _candidate_seen("m1", 0.0),
        _shadow_price("m1", 5.0, arm="bought", reject_reason=None, price_sol=p0, elapsed=5.0, trade_id="t1"),
        _envelope(
            "EntryFilled", True, 6.0, trade_id="t1", mint="m1", entry_price_sol=p0,
            tokens_bought=1000.0, position_sol=0.001,
        ),
        # m2 was bought (has an EntryFilled) but never made it into the shadow join
        # (Section 2.5: capacity checked before the sampling roll, can drop a bought mint).
        _envelope(
            "EntryFilled", True, 7.0, trade_id="t2", mint="m2", entry_price_sol=p0,
            tokens_bought=1000.0, position_sol=0.001,
        ),
    ]
    mints, _ = horizon_run.load_horizon_mints(events)
    report = horizon_run.concurrency_report(mints, events)
    assert report.bought_mints_missing_from_shadow == ("m2",)


def test_concurrency_report_no_missing_bought_mints_when_join_is_complete():
    p0 = _p0()
    events = [
        _candidate_seen("m1", 0.0),
        _shadow_price("m1", 5.0, arm="bought", reject_reason=None, price_sol=p0, elapsed=5.0, trade_id="t1"),
        _envelope(
            "EntryFilled", True, 6.0, trade_id="t1", mint="m1", entry_price_sol=p0,
            tokens_bought=1000.0, position_sol=0.001,
        ),
    ]
    mints, _ = horizon_run.load_horizon_mints(events)
    report = horizon_run.concurrency_report(mints, events)
    assert report.bought_mints_missing_from_shadow == ()


# --- peak-and-time-to-peak ----------------------------------------------------


def test_time_to_peak_fraction_of_window():
    p0 = _p0()
    events = [
        _candidate_seen("m1", 0.0),
        _shadow_price("m1", 90.0, price_sol=p0 * 3.0, elapsed=90.0),
        _shadow_price("m1", 300.0, price_sol=p0 * 1.0, elapsed=300.0),
    ]
    mints, _ = horizon_run.load_horizon_mints(events)
    outcome = horizon_run.classify_outcome(mints[0])
    frac = horizon_run.time_to_peak_fraction_of_window(outcome, horizon_seconds=300.0)
    assert frac == p_approx(0.30)


def test_time_to_peak_fraction_none_without_a_peak():
    outcome = horizon_run.MintOutcome(
        category="graduated", last_valid_price=None, last_valid_elapsed=None,
        graduation_elapsed=1.0, peak_multiple=0.0, peak_elapsed=None, last_tick_elapsed=1.0,
    )
    assert horizon_run.time_to_peak_fraction_of_window(outcome, horizon_seconds=300.0) is None


# sanity: MIGRATION_SOL_LAMPORTS import path stays intact
def test_migration_constant_imported_for_completion_math():
    assert MIGRATION_SOL_LAMPORTS == 85_000_000_000
