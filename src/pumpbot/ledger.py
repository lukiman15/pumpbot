"""Append-only JSONL trade ledger.

The durable record every trading decision produces: one JSON object per
line, rotated daily under data/ (gitignored -- see .gitignore's data/
entry). No module in this project has written runtime state to disk
before this (persistence.py's positions.json is the one exception, and it
overwrites a single snapshot rather than appending an event history) --
this append-only format is a new pattern, stated here rather than implied.

Event dataclasses mirror heartbeat.py's frozen-report style: plain data,
no RPC, no I/O. Ledger itself is the only thing that touches a file
handle. Every event line gets a five-field envelope (event, schema,
ts_wall, ts_monotonic, run_id, dry_run) merged with the event's own
to_dict() -- see the plan's Event Schema for the full field list per type.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def new_run_id() -> str:
    return uuid.uuid4().hex


def new_trade_id() -> str:
    return uuid.uuid4().hex


class _EventMixin:
    """Not a dataclass itself -- just supplies to_dict() via
    dataclasses.asdict(), which works on any dataclass instance regardless
    of what else it inherits from. EVENT is a plain class attribute (no
    type annotation), so the dataclass decorator on subclasses does not
    turn it into a field."""

    EVENT: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)  # type: ignore[call-overload]


@dataclass(frozen=True)
class CandidateSeen(_EventMixin):
    EVENT = "CandidateSeen"
    mint: str
    creator: str
    name: str
    symbol: str
    uri: str
    creator_supply_fraction: float
    virtual_sol_in_curve: float
    virtual_tokens_in_curve: float
    notified_at_wall: float


@dataclass(frozen=True)
class CandidateRejected(_EventMixin):
    EVENT = "CandidateRejected"
    mint: str
    reason: str


@dataclass(frozen=True)
class CandidateSkipped(_EventMixin):
    EVENT = "CandidateSkipped"
    mint: str
    reason: str
    detail: str | None


@dataclass(frozen=True)
class Tier2Evaluated(_EventMixin):
    EVENT = "Tier2Evaluated"
    mint: str
    outcome: str
    has_twitter: bool
    has_telegram: bool
    has_website: bool
    passed: bool
    mode: str
    fetch_seconds: float


@dataclass(frozen=True)
class EntryFilled(_EventMixin):
    EVENT = "EntryFilled"
    mint: str
    trade_id: str
    signature: str | None
    position_sol: float
    tokens_bought: float
    entry_price_sol: float
    latency_seconds: float
    settlement: dict[str, Any] | None


@dataclass(frozen=True)
class ExitFilled(_EventMixin):
    EVENT = "ExitFilled"
    mint: str
    trade_id: str
    signature: str | None
    exit_reason: str
    tokens_sold: float
    tokens_remaining: float
    curve_price_sol: float  # spot, pre-fee -- unchanged meaning from Milestone 2
    # Realizable proceeds / cost basis at decision time (Milestone 4 Task 1)
    # and the trailing high-water mark at that same moment (Task 2) -- what
    # curve_price_sol alone can't reconstruct: a trailing exit you can't
    # later distinguish from a lucky one isn't evidence.
    realizable_multiple: float
    peak_multiple: float
    settlement: dict[str, Any] | None


@dataclass(frozen=True)
class AtaClosed(_EventMixin):
    EVENT = "AtaClosed"
    mint: str
    trade_id: str
    signature: str | None
    settlement: dict[str, Any] | None


@dataclass(frozen=True)
class TradeClosed(_EventMixin):
    EVENT = "TradeClosed"
    mint: str
    trade_id: str
    realized_pnl_lamports: int | None
    rent_recovered_lamports: int | None
    total_fee_lamports: int | None
    gross_turnover_lamports: int | None
    hold_seconds: float
    leg_count: int
    exit_reasons: list[str]
    settlement_complete: bool


@dataclass(frozen=True)
class TradeOrphaned(_EventMixin):
    EVENT = "TradeOrphaned"
    mint: str
    trade_id: str
    orphaned_from_run_id: str
    last_known_tokens_remaining: float


@dataclass(frozen=True)
class ShadowPrice(_EventMixin):
    EVENT = "ShadowPrice"
    mint: str
    arm: str  # "bought" | "rejected" | "skipped"
    reject_reason: str | None
    horizon_elapsed_seconds: float
    price_sol: float
    return_from_first_seen: float | None
    curve_complete: bool
    trade_id: str | None


class Ledger:
    """Owns one open file handle, rotated daily. `path` is the configured
    base path (e.g. data/ledger.jsonl); rotated files are written as
    <stem>-<YYYY-MM-DD><suffix> beside it, in UTC (matching rpc.py's own
    UTC-midnight convention for its daily credit-halt roll)."""

    def __init__(self, path: Path, run_id: str, dry_run: bool, enabled: bool = True) -> None:
        self._directory = path.parent
        self._stem = path.stem
        self._suffix = path.suffix or ".jsonl"
        self._run_id = run_id
        self._dry_run = dry_run
        self._enabled = enabled
        self._current_date: str | None = None
        self._handle = None

    def _path_for(self, day: str) -> Path:
        return self._directory / f"{self._stem}-{day}{self._suffix}"

    def _ensure_handle_for_today(self) -> None:
        today = datetime.now(UTC).date().isoformat()
        if today == self._current_date and self._handle is not None:
            return
        if self._handle is not None:
            self._handle.close()
        self._directory.mkdir(parents=True, exist_ok=True)
        self._handle = self._path_for(today).open("a", encoding="utf-8")
        self._current_date = today

    def append(self, event: _EventMixin) -> None:
        """Writes one JSON line and flushes. Deliberately a plain `def`,
        not `async def`: the event loop is single-threaded and this
        function performs no `await`, so it is atomic by construction --
        no other task can interleave between the read of the current file
        handle and the write. Making this `async def` later would
        reintroduce exactly the interleaving hazard this comment exists to
        prevent, even though nothing here would obviously require it.

        An I/O error here must never propagate into the trading path -- a
        full disk logs loudly and drops the row rather than crashing or
        corrupting position state."""
        if not self._enabled:
            return
        try:
            self._ensure_handle_for_today()
            row: dict[str, Any] = {
                "event": event.EVENT,
                "schema": SCHEMA_VERSION,
                "ts_wall": time.time(),
                "ts_monotonic": time.monotonic(),
                "run_id": self._run_id,
                "dry_run": self._dry_run,
                **event.to_dict(),
            }
            self._handle.write(json.dumps(row) + "\n")
            self._handle.flush()
        except OSError:
            logger.exception(
                "ledger append failed for event=%s mint=%s",
                getattr(event, "EVENT", "?"), getattr(event, "mint", "?"),
            )

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def read_events(path: Path) -> Iterator[dict[str, Any]]:
    """Yields parsed dicts from the ledger, skipping malformed lines. If
    `path` is a directory, reads every *.jsonl file inside it in filename
    order -- the YYYY-MM-DD rotation naming sorts chronologically for
    free. If `path` is a single file, reads just that file. A path that
    doesn't exist yields nothing -- a fresh start with no ledger yet is
    the normal case, not an error."""
    if path.is_dir():
        files = sorted(path.glob("*.jsonl"))
    elif path.exists():
        files = [path]
    else:
        files = []

    for file in files:
        with file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("skipping malformed ledger line in %s", file)


def find_orphans(events: Iterable[dict[str, Any]], current_run_id: str) -> list[TradeOrphaned]:
    """Pure function over already-read events implementing the trade
    status rules: CLOSED (a TradeClosed exists), OPEN (the owning run_id
    is the current process), ORPHANED (owning run_id is not current, and
    no TradeClosed exists yet). Returns one TradeOrphaned per orphaned
    trade -- the caller is responsible for appending them to the ledger."""
    entries: dict[str, dict[str, Any]] = {}
    tokens_remaining: dict[str, float] = {}
    closed: set[str] = set()
    already_orphaned: set[str] = set()

    for row in events:
        event = row.get("event")
        trade_id = row.get("trade_id")
        if not trade_id:
            continue
        if event == "EntryFilled":
            entries[trade_id] = {
                "run_id": row.get("run_id"),
                "mint": row.get("mint"),
                "tokens_bought": row.get("tokens_bought", 0.0),
            }
            tokens_remaining[trade_id] = row.get("tokens_bought", 0.0)
        elif event == "ExitFilled":
            if "tokens_remaining" in row:
                tokens_remaining[trade_id] = row["tokens_remaining"]
        elif event == "TradeClosed":
            closed.add(trade_id)
        elif event == "TradeOrphaned":
            already_orphaned.add(trade_id)

    orphans = []
    for trade_id, entry in entries.items():
        if trade_id in closed or trade_id in already_orphaned:
            continue
        if entry["run_id"] == current_run_id:
            continue  # OPEN, not orphaned
        orphans.append(
            TradeOrphaned(
                mint=entry["mint"],
                trade_id=trade_id,
                orphaned_from_run_id=entry["run_id"],
                last_known_tokens_remaining=tokens_remaining.get(
                    trade_id, entry["tokens_bought"]
                ),
            )
        )
    return orphans
