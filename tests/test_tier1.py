import json

from pumpbot.config import Tier1FilterConfig
from pumpbot.curve import BondingCurveState
from pumpbot.filters.tier1 import (
    Candidate,
    RejectionReason,
    Tier1Filter,
    load_creator_blocklist,
    load_name_symbol_blocklist,
)

CONFIG = Tier1FilterConfig(
    max_creator_supply_fraction=0.10,
    max_mints_per_second=5,
    curve_completion_guard_fraction=0.80,
)

FRESH_CURVE = BondingCurveState(
    virtual_token_reserves=1_073_000_000_000_000,
    virtual_sol_reserves=30_000_000_000,
    real_token_reserves=793_100_000_000_000,
    real_sol_reserves=0,
    token_total_supply=1_000_000_000_000_000,
    complete=False,
)


def make_candidate(**overrides) -> Candidate:
    defaults = {
        "mint": "MintAddr111",
        "creator": "CreatorAddr111",
        "name": "Some Coin",
        "symbol": "SOME",
        "creator_supply_fraction": 0.01,
        "curve": FRESH_CURVE,
    }
    defaults.update(overrides)
    return Candidate(**defaults)


def make_filter(creator_blocklist=None, name_symbol_blocklist=None) -> Tier1Filter:
    return Tier1Filter(
        CONFIG, creator_blocklist or set(), name_symbol_blocklist or set()
    )


def test_clean_candidate_passes():
    result = make_filter().evaluate(make_candidate(), recent_mint_timestamps=[], now=100.0)
    assert result.passed
    assert result.reason is None


def test_mayhem_mode_rejected():
    # Confirmed root cause of the intermittent fee_recipient NotAuthorized
    # failures -- see Candidate.is_mayhem_mode's docstring.
    candidate = make_candidate(is_mayhem_mode=True)
    result = make_filter().evaluate(candidate, recent_mint_timestamps=[], now=100.0)
    assert not result.passed
    assert result.reason == RejectionReason.MAYHEM_MODE_UNAUTHORIZED


def test_non_mayhem_mode_passes():
    candidate = make_candidate(is_mayhem_mode=False)
    result = make_filter().evaluate(candidate, recent_mint_timestamps=[], now=100.0)
    assert result.passed


def test_blocked_creator_rejected():
    f = make_filter(creator_blocklist={"CreatorAddr111"})
    result = f.evaluate(make_candidate(), recent_mint_timestamps=[], now=100.0)
    assert not result.passed
    assert result.reason == RejectionReason.CREATOR_BLOCKED


def test_blocked_name_rejected_case_insensitive():
    f = make_filter(name_symbol_blocklist={"scam coin"})
    candidate = make_candidate(name="Scam Coin")
    result = f.evaluate(candidate, recent_mint_timestamps=[], now=100.0)
    assert not result.passed
    assert result.reason == RejectionReason.NAME_OR_SYMBOL_BLOCKED


def test_blocked_symbol_rejected():
    f = make_filter(name_symbol_blocklist={"scam"})
    candidate = make_candidate(symbol="SCAM")
    result = f.evaluate(candidate, recent_mint_timestamps=[], now=100.0)
    assert not result.passed
    assert result.reason == RejectionReason.NAME_OR_SYMBOL_BLOCKED


def test_creator_supply_too_high_rejected():
    candidate = make_candidate(creator_supply_fraction=0.11)
    result = make_filter().evaluate(candidate, recent_mint_timestamps=[], now=100.0)
    assert not result.passed
    assert result.reason == RejectionReason.CREATOR_SUPPLY_TOO_HIGH


def test_creator_supply_at_exact_threshold_passes():
    candidate = make_candidate(creator_supply_fraction=0.10)
    result = make_filter().evaluate(candidate, recent_mint_timestamps=[], now=100.0)
    assert result.passed


def test_curve_too_complete_rejected():
    nearly_done_curve = BondingCurveState(
        **{**FRESH_CURVE.__dict__, "real_sol_reserves": 90_000_000_000}
    )
    candidate = make_candidate(curve=nearly_done_curve)
    result = make_filter().evaluate(candidate, recent_mint_timestamps=[], now=100.0)
    assert not result.passed
    assert result.reason == RejectionReason.CURVE_TOO_COMPLETE


def test_mint_rate_too_high_rejected():
    # 6 mints in the last second, threshold is 5
    recent = [99.9, 99.8, 99.7, 99.6, 99.5, 99.4]
    result = make_filter().evaluate(make_candidate(), recent_mint_timestamps=recent, now=100.0)
    assert not result.passed
    assert result.reason == RejectionReason.MINT_RATE_TOO_HIGH


def test_mint_rate_ignores_old_timestamps():
    # All older than the 1s window -- should not count against the rate.
    recent = [90.0, 85.0, 80.0, 70.0, 60.0, 50.0]
    result = make_filter().evaluate(make_candidate(), recent_mint_timestamps=recent, now=100.0)
    assert result.passed


def test_load_creator_blocklist_missing_file_is_empty(tmp_path):
    assert load_creator_blocklist(tmp_path / "does_not_exist.json") == set()


def test_load_creator_blocklist_reads_json_array(tmp_path):
    path = tmp_path / "creator_blocklist.json"
    path.write_text(json.dumps(["AbC123", "DeF456"]), encoding="utf-8")
    assert load_creator_blocklist(path) == {"AbC123", "DeF456"}


def test_load_name_symbol_blocklist_missing_file_is_empty(tmp_path):
    assert load_name_symbol_blocklist(tmp_path / "does_not_exist.txt") == set()


def test_load_name_symbol_blocklist_reads_lowercased_lines(tmp_path):
    path = tmp_path / "name_blocklist.txt"
    path.write_text("Scam Coin\nRUG\n\n  spaced  \n", encoding="utf-8")
    assert load_name_symbol_blocklist(path) == {"scam coin", "rug", "spaced"}
