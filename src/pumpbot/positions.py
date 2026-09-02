"""Position tracking and exit-decision logic.

Pure state + decision logic -- no RPC calls, no transaction building. The
executor (not yet built) fetches current curve state, calls
Position.evaluate_exit() each tick, and acts on whatever ExitDecision comes
back by calling apply_exit() once the sell actually lands.

Exit ladder per config.yaml's exits.*: sell take_profit_1_fraction at
take_profit_1_multiple, the rest at take_profit_2_multiple, or bail earlier
on stop_loss_fraction or timeout_seconds -- whichever comes first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from pumpbot.config import ExitsConfig


class ExitReason(str, Enum):
    TAKE_PROFIT_1 = "take_profit_1"
    TAKE_PROFIT_2 = "take_profit_2"
    STOP_LOSS = "stop_loss"
    TIMEOUT = "timeout"
    TRAILING_STOP = "trailing_stop"
    CREATOR_SOLD = "creator_sold"


class PositionLimitReachedError(RuntimeError):
    """Raised when opening a position would exceed max_concurrent_positions."""


class DuplicatePositionError(RuntimeError):
    """Raised when a position is already open for a mint."""


@dataclass(frozen=True)
class ExitDecision:
    reason: ExitReason
    fraction: float  # fraction of the CURRENT remaining token balance to sell, 0..1


@dataclass
class Position:
    mint: str
    entry_price_sol: float  # SOL per whole token, cost basis at entry
    entry_tokens: float  # whole tokens acquired at entry
    opened_at: float  # time.monotonic() at open
    tokens_remaining: float = field(init=False)
    take_profit_1_hit: bool = field(default=False, init=False)
    # Gross SOL originally paid (entry_price_sol already includes the buy fee
    # and its price impact -- see main.py's entry_price_sol computation).
    # Stored once rather than recomputed as entry_price_sol * entry_tokens in
    # three places.
    entry_cost_sol: float = field(init=False)
    # High-water mark for the trailing-drawdown exit (Task 2), expressed as a
    # MULTIPLE of cost basis, not an absolute SOL figure -- absolute proceeds
    # shrink when a partial take-profit-1 fill reduces tokens_remaining, which
    # would otherwise corrupt the peak on the very next tick after that fill.
    trailing_peak_multiple: float = field(default=0.0, init=False)
    trailing_armed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.tokens_remaining = self.entry_tokens
        self.entry_cost_sol = self.entry_price_sol * self.entry_tokens

    @property
    def cost_basis_of_remaining_sol(self) -> float:
        """Gross SOL basis attributable to tokens_remaining -- entry_cost_sol
        scaled down proportionally as partial exits reduce the position."""
        if self.entry_tokens <= 0:
            return 0.0
        return self.entry_cost_sol * (self.tokens_remaining / self.entry_tokens)

    def evaluate_exit(
        self,
        realizable_sol_value: float,
        now: float,
        config: ExitsConfig,
        *,
        creator_sold: bool = False,
    ) -> ExitDecision | None:
        """Returns the highest-priority exit action due right now, or None.

        `realizable_sol_value` is what selling tokens_remaining would actually
        net right now (fee and price impact already deducted -- see
        curve.py's tokens_to_sol), NOT the marginal spot price. Comparing
        realizable proceeds against cost basis is what makes `multiple` mean
        "what would I actually get back per SOL I actually put in" (see
        MILESTONE-4-HANDOFF.md Section 4.1).
        """
        if self.tokens_remaining <= 0:
            return None

        cost_basis = self.cost_basis_of_remaining_sol
        multiple = realizable_sol_value / cost_basis if cost_basis > 0 else 0.0
        pnl_fraction = multiple - 1.0

        if config.trailing_enabled:
            self.trailing_peak_multiple = max(self.trailing_peak_multiple, multiple)
            if not self.trailing_armed and multiple >= config.trailing_arm_multiple:
                self.trailing_armed = True

        # Ladder order (MILESTONE-4-HANDOFF.md Section 4.4): creator sold
        # outranks everything -- it's information about *why* the price is
        # about to move, and unlike a threshold on price, it doesn't recover.
        if creator_sold:
            return ExitDecision(ExitReason.CREATOR_SOLD, fraction=1.0)

        # Stop loss next: capital preservation beats letting a ladder play out.
        if pnl_fraction <= config.stop_loss_fraction:
            return ExitDecision(ExitReason.STOP_LOSS, fraction=1.0)

        # A spike straight past take_profit_2 is strictly better fully exited
        # now than partially exited at take_profit_1's lower price and left
        # waiting -- check the higher rung first regardless of tp1 status.
        if multiple >= config.take_profit_2_multiple:
            return ExitDecision(ExitReason.TAKE_PROFIT_2, fraction=1.0)

        # Trailing sits below tp2 (a spike past the cap should take the cap,
        # not the trailed price) and above tp1 (a run that rolls over should
        # exit fully rather than take a half-profit on the way down).
        if (
            config.trailing_enabled
            and self.trailing_armed
            and multiple <= self.trailing_peak_multiple * (1 - config.trailing_drawdown_fraction)
        ):
            return ExitDecision(ExitReason.TRAILING_STOP, fraction=1.0)

        if not self.take_profit_1_hit and multiple >= config.take_profit_1_multiple:
            return ExitDecision(
                ExitReason.TAKE_PROFIT_1, fraction=config.take_profit_1_fraction
            )

        if now - self.opened_at >= config.timeout_seconds:
            return ExitDecision(ExitReason.TIMEOUT, fraction=1.0)

        return None

    def apply_exit(self, decision: ExitDecision) -> float:
        """Reduce tokens_remaining by the decided fraction; returns tokens sold.

        Call only after the sell actually lands -- this mutates position state.
        """
        tokens_sold = self.tokens_remaining * decision.fraction
        self.tokens_remaining = max(0.0, self.tokens_remaining - tokens_sold)
        if decision.reason == ExitReason.TAKE_PROFIT_1:
            self.take_profit_1_hit = True
        return tokens_sold


class PositionManager:
    """Tracks open positions and enforces max_concurrent_positions."""

    def __init__(self, max_concurrent_positions: int) -> None:
        self._max_concurrent = max_concurrent_positions
        self._positions: dict[str, Position] = {}

    @property
    def open_count(self) -> int:
        return len(self._positions)

    def can_open_new(self) -> bool:
        return self.open_count < self._max_concurrent

    def open(self, position: Position) -> None:
        if position.mint in self._positions:
            raise DuplicatePositionError(f"position already open for mint {position.mint}")
        if not self.can_open_new():
            raise PositionLimitReachedError(
                f"max_concurrent_positions ({self._max_concurrent}) reached"
            )
        self._positions[position.mint] = position

    def get(self, mint: str) -> Position | None:
        return self._positions.get(mint)

    def close(self, mint: str) -> None:
        self._positions.pop(mint, None)

    def all_open(self) -> list[Position]:
        return list(self._positions.values())
