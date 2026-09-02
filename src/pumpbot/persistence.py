"""Persists open positions across restarts.

Without this, a position opened by one process is invisible to the next --
`PositionManager` and `TradingState` are pure in-memory state. This was a
real, live-confirmed gap: the first real trade's sell bug forced a restart
while a position was open, and a fresh process would have had no record of
it at all (the position was recovered manually instead, see
pumpbot-sniper-status.md). Restarting mid-position is exactly when this
matters most, so state is saved after every open/exit and reloaded at
startup before the trading loops start.

`opened_at` on `Position` is a `time.monotonic()` value, which is only
meaningful within one process's lifetime -- it's converted to/from wall-clock
time (`time.time()`) at the save/load boundary so a restored position's
timeout countdown reflects real elapsed time, not time since this new
process started.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from pumpbot.positions import Position, PositionManager


def save_state(
    path: Path,
    position_manager: PositionManager,
    creators: dict[str, str],
    token_programs: dict[str, str],
) -> None:
    now_monotonic = time.monotonic()
    now_wall = time.time()
    positions = [
        {
            "mint": position.mint,
            "entry_price_sol": position.entry_price_sol,
            "entry_tokens": position.entry_tokens,
            "tokens_remaining": position.tokens_remaining,
            "take_profit_1_hit": position.take_profit_1_hit,
            "trailing_peak_multiple": position.trailing_peak_multiple,
            "trailing_armed": position.trailing_armed,
            "opened_at_wall": now_wall - (now_monotonic - position.opened_at),
            "creator": creators.get(position.mint),
            "token_program_id": token_programs.get(position.mint),
        }
        for position in position_manager.all_open()
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"positions": positions}, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_state(path: Path) -> tuple[list[Position], dict[str, str], dict[str, str]]:
    """Returns (positions, creators_by_mint, token_programs_by_mint). Returns
    empty results if no state file exists yet -- that's the normal case for
    a fresh start with nothing open, not an error."""
    if not path.exists():
        return [], {}, {}

    data = json.loads(path.read_text(encoding="utf-8"))
    now_monotonic = time.monotonic()
    now_wall = time.time()
    positions: list[Position] = []
    creators: dict[str, str] = {}
    token_programs: dict[str, str] = {}
    for row in data.get("positions", []):
        position = Position(
            mint=row["mint"],
            entry_price_sol=row["entry_price_sol"],
            entry_tokens=row["entry_tokens"],
            opened_at=now_monotonic - (now_wall - row["opened_at_wall"]),
        )
        position.tokens_remaining = row["tokens_remaining"]
        position.take_profit_1_hit = row["take_profit_1_hit"]
        # .get() with fallbacks -- a state file written before Milestone 4
        # lacks these keys entirely. Peak defaults to 0.0 rather than
        # guessing a value: the very next evaluate_exit() call sets it to
        # the real current multiple regardless (multiple > 0.0 always holds
        # for a live position), and armed=False means trailing can't
        # falsely fire before that first live tick re-establishes it.
        position.trailing_peak_multiple = row.get("trailing_peak_multiple", 0.0)
        position.trailing_armed = row.get("trailing_armed", False)
        positions.append(position)
        if row.get("creator"):
            creators[row["mint"]] = row["creator"]
        if row.get("token_program_id"):
            token_programs[row["mint"]] = row["token_program_id"]
    return positions, creators, token_programs
