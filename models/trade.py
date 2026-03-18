"""
models/trade.py – Mutable position state tracked by the ExecutionEngine.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from models.signal import Direction


class TradeState(str, Enum):
    PENDING = "PENDING"          # Order sent, not yet confirmed
    OPEN = "OPEN"                # Position active
    BREAKEVEN = "BREAKEVEN"      # SL moved to entry
    TRAILING = "TRAILING"        # Trailing stop engaged
    CLOSED = "CLOSED"            # Fully exited
    ERROR = "ERROR"              # Exchange error


@dataclass
class Trade:
    symbol: str
    direction: Direction
    entry_price: float
    quantity: float              # Base asset qty
    leverage: int

    # Assigned at creation
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    state: TradeState = TradeState.PENDING
    opened_at: float = field(default_factory=time.time)
    closed_at: Optional[float] = None

    # Exchange order IDs
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None

    # Stop levels (set after entry confirmed)
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None     # Optional; bot primarily uses trailing
    trailing_stop: Optional[float] = None   # Absolute price of trailing stop

    # P&L tracking
    highest_price: Optional[float] = None   # For LONG trailing (peak)
    lowest_price: Optional[float] = None    # For SHORT trailing (trough)
    realised_pnl: Optional[float] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def duration_sec(self) -> float:
        end = self.closed_at or time.time()
        return end - self.opened_at

    def unrealised_pnl(self, current_price: float) -> float:
        if self.direction == Direction.LONG:
            return (current_price - self.entry_price) * self.quantity * self.leverage
        else:
            return (self.entry_price - current_price) * self.quantity * self.leverage

    def unrealised_pct(self, current_price: float) -> float:
        if self.direction == Direction.LONG:
            return (current_price - self.entry_price) / self.entry_price * 100.0
        else:
            return (self.entry_price - current_price) / self.entry_price * 100.0

    def __str__(self) -> str:
        return (
            f"Trade({self.id} {self.symbol} {self.direction.value} "
            f"qty={self.quantity} entry={self.entry_price} "
            f"state={self.state.value} "
            f"dur={self.duration_sec():.1f}s)"
        )
