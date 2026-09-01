"""Tier-1 filters: fast, cheap rejects applied to every new-mint candidate
before it's considered for a buy.

No RPC calls here -- the caller (listener.py) gathers the inputs (creator
supply fraction, bonding-curve state, recent mint timestamps) and this
module just decides pass/reject. Keeping the filter pure makes it directly
unit-testable without a live connection.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pumpbot.config import Tier1FilterConfig
from pumpbot.curve import BondingCurveState, curve_completion_fraction


class RejectionReason(str, Enum):
    CREATOR_BLOCKED = "creator_blocked"
    NAME_OR_SYMBOL_BLOCKED = "name_or_symbol_blocked"
    CREATOR_SUPPLY_TOO_HIGH = "creator_supply_too_high"
    CURVE_TOO_COMPLETE = "curve_too_complete"
    MINT_RATE_TOO_HIGH = "mint_rate_too_high"
    MAYHEM_MODE_UNAUTHORIZED = "mayhem_mode_unauthorized"


@dataclass(frozen=True)
class FilterResult:
    passed: bool
    reason: RejectionReason | None = None


@dataclass(frozen=True)
class Candidate:
    mint: str
    creator: str
    name: str
    symbol: str
    creator_supply_fraction: float  # creator's held fraction of total supply, 0..1
    curve: BondingCurveState
    # PumpPortal's "mayhem mode" launch flag. Confirmed root cause of the
    # intermittent fee_recipient NotAuthorized (Custom 6000) failures: a
    # paced, 6-mint live simulateTransaction test (not the earlier invalidated
    # rapid-fire batch) found this predicts authorization outcome 6/6 --
    # every is_mayhem_mode=True mint rejected ALL of NORMAL_FEE_RECIPIENTS,
    # every is_mayhem_mode=False mint accepted them. Mayhem-mode mints
    # evidently require a different, gated fee_recipient this project's
    # plain wallet doesn't have -- see filters/tier1.py's evaluate().
    is_mayhem_mode: bool = False
    # Metadata JSON URI, unused by tier1 (no I/O here) -- carried through
    # for tier2.py's social-metadata gate. See filters/tier2.py.
    uri: str = ""
    # time.time() at the moment the listener parsed this create event --
    # unused by tier1 itself, carried through so ledger.py's EntryFilled
    # can compute latency_seconds. Wall-clock (not monotonic) so latency
    # is comparable across restarts.
    notified_at_wall: float = 0.0


def load_creator_blocklist(path: str | Path) -> set[str]:
    """Creator addresses to always reject. Missing file = empty blocklist --
    this is local, gitignored state (data/), not something every checkout has."""
    p = Path(path)
    if not p.exists():
        return set()
    return set(json.loads(p.read_text(encoding="utf-8")))


def load_name_symbol_blocklist(path: str | Path) -> set[str]:
    """Lowercased name/symbol strings to always reject, one per line."""
    p = Path(path)
    if not p.exists():
        return set()
    return {
        line.strip().lower()
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


class Tier1Filter:
    def __init__(
        self,
        config: Tier1FilterConfig,
        creator_blocklist: set[str],
        name_symbol_blocklist: set[str],
    ) -> None:
        self._config = config
        self._creator_blocklist = creator_blocklist
        self._name_symbol_blocklist = name_symbol_blocklist

    def evaluate(
        self,
        candidate: Candidate,
        recent_mint_timestamps: Sequence[float],
        now: float,
    ) -> FilterResult:
        if candidate.is_mayhem_mode:
            return FilterResult(False, RejectionReason.MAYHEM_MODE_UNAUTHORIZED)

        if candidate.creator in self._creator_blocklist:
            return FilterResult(False, RejectionReason.CREATOR_BLOCKED)

        if (
            candidate.name.strip().lower() in self._name_symbol_blocklist
            or candidate.symbol.strip().lower() in self._name_symbol_blocklist
        ):
            return FilterResult(False, RejectionReason.NAME_OR_SYMBOL_BLOCKED)

        if candidate.creator_supply_fraction > self._config.max_creator_supply_fraction:
            return FilterResult(False, RejectionReason.CREATOR_SUPPLY_TOO_HIGH)

        if (
            curve_completion_fraction(candidate.curve)
            > self._config.curve_completion_guard_fraction
        ):
            return FilterResult(False, RejectionReason.CURVE_TOO_COMPLETE)

        recent_count = sum(1 for t in recent_mint_timestamps if now - t <= 1.0)
        if recent_count > self._config.max_mints_per_second:
            return FilterResult(False, RejectionReason.MINT_RATE_TOO_HIGH)

        return FilterResult(True)
