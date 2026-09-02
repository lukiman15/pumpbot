import json
import time

from pumpbot.persistence import load_state, save_state
from pumpbot.positions import ExitDecision, ExitReason, Position, PositionManager


def make_position(mint: str = "MintAAA", opened_ago_seconds: float = 0.0) -> Position:
    return Position(
        mint=mint,
        entry_price_sol=0.0001,
        entry_tokens=1000.0,
        opened_at=time.monotonic() - opened_ago_seconds,
    )


def test_load_state_missing_file_returns_empty(tmp_path):
    positions, creators, token_programs = load_state(tmp_path / "does_not_exist.json")
    assert positions == []
    assert creators == {}
    assert token_programs == {}


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "state" / "positions.json"
    manager = PositionManager(max_concurrent_positions=2)
    manager.open(make_position("MintAAA"))

    save_state(path, manager, creators={"MintAAA": "CreatorXYZ"}, token_programs={"MintAAA": "TokenProgABC"})
    assert path.exists()

    positions, creators, token_programs = load_state(path)
    assert len(positions) == 1
    restored = positions[0]
    assert restored.mint == "MintAAA"
    assert restored.entry_price_sol == 0.0001
    assert restored.entry_tokens == 1000.0
    assert restored.tokens_remaining == 1000.0
    assert restored.take_profit_1_hit is False
    assert creators == {"MintAAA": "CreatorXYZ"}
    assert token_programs == {"MintAAA": "TokenProgABC"}


def test_save_and_load_preserves_partial_exit_and_elapsed_time(tmp_path):
    path = tmp_path / "positions.json"
    manager = PositionManager(max_concurrent_positions=1)
    position = make_position("MintAAA", opened_ago_seconds=120.0)
    manager.open(position)
    position.apply_exit(ExitDecision(ExitReason.TAKE_PROFIT_1, fraction=0.5))

    save_state(path, manager, creators={}, token_programs={})
    positions, _, _ = load_state(path)

    restored = positions[0]
    assert restored.tokens_remaining == 500.0
    assert restored.take_profit_1_hit is True
    # opened_at is a time.monotonic() value re-based on wall-clock elapsed
    # time -- it should reflect the ~120s that already passed, not reset to
    # "just now" the way a naive restore would.
    elapsed = time.monotonic() - restored.opened_at
    assert 119.0 <= elapsed <= 122.0


def test_save_with_no_open_positions_writes_empty_list(tmp_path):
    path = tmp_path / "positions.json"
    manager = PositionManager(max_concurrent_positions=1)

    save_state(path, manager, creators={}, token_programs={})
    positions, creators, token_programs = load_state(path)

    assert positions == []
    assert creators == {}
    assert token_programs == {}


def test_save_and_load_round_trip_preserves_trailing_peak_and_armed(tmp_path):
    path = tmp_path / "positions.json"
    manager = PositionManager(max_concurrent_positions=1)
    position = make_position("MintAAA")
    position.trailing_peak_multiple = 1.8
    position.trailing_armed = True
    manager.open(position)

    save_state(path, manager, creators={}, token_programs={})
    positions, _, _ = load_state(path)

    restored = positions[0]
    assert restored.trailing_peak_multiple == 1.8
    assert restored.trailing_armed is True


def test_load_state_tolerates_pre_milestone_4_file_missing_trailing_keys(tmp_path):
    # A state file written before Milestone 4 has no trailing_peak_multiple
    # or trailing_armed keys at all -- the first restart after deploying
    # this milestone must not raise KeyError with a real position open.
    path = tmp_path / "positions.json"
    path.write_text(
        json.dumps(
            {
                "positions": [
                    {
                        "mint": "MintAAA",
                        "entry_price_sol": 0.0001,
                        "entry_tokens": 1000.0,
                        "tokens_remaining": 1000.0,
                        "take_profit_1_hit": False,
                        "opened_at_wall": time.time(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    positions, _, _ = load_state(path)

    restored = positions[0]
    assert restored.trailing_peak_multiple == 0.0
    assert restored.trailing_armed is False


def test_closed_position_disappears_from_next_save(tmp_path):
    path = tmp_path / "positions.json"
    manager = PositionManager(max_concurrent_positions=1)
    manager.open(make_position("MintAAA"))
    save_state(path, manager, creators={"MintAAA": "CreatorXYZ"}, token_programs={})

    manager.close("MintAAA")
    save_state(path, manager, creators={"MintAAA": "CreatorXYZ"}, token_programs={})

    positions, _, _ = load_state(path)
    assert positions == []
