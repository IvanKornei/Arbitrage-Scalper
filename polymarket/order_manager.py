"""
polymarket/order_manager.py – Tracks open positions and manages order lifecycle.

Responsibilities:
  - Record each bet placed (size, price, token, direction)
  - Avoid placing duplicate bets on the same market
  - Provide P&L summary
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from polymarket.client import OrderResult, PolymarketClient, PolyMarket
from utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class Position:
    condition_id: str
    question: str
    direction: str           # "YES" | "NO"
    token_id: str
    entry_price: float       # price paid per share
    size_usdc: float         # total USDC spent
    shares: float            # shares received = size_usdc / entry_price
    order_id: str
    opened_at: float = field(default_factory=time.time)
    closed: bool = False
    exit_price: float = 0.0
    pnl_usdc: float = 0.0

    def __str__(self) -> str:
        return (
            f"[{self.direction}] {self.question[:50]} | "
            f"size=${self.size_usdc:.2f} @ {self.entry_price:.4f} | "
            f"pnl=${self.pnl_usdc:+.2f}"
        )


class OrderManager:
    """Tracks positions and drives order placement."""

    def __init__(self, client: PolymarketClient, dry_run: bool = False) -> None:
        self._client  = client
        self._dry_run = dry_run
        self._positions: Dict[str, Position] = {}   # condition_id → Position

    @property
    def open_positions(self) -> List[Position]:
        return [p for p in self._positions.values() if not p.closed]

    @property
    def open_count(self) -> int:
        return len(self.open_positions)

    def has_position(self, condition_id: str) -> bool:
        p = self._positions.get(condition_id)
        return p is not None and not p.closed

    async def place_bet(
        self,
        market: PolyMarket,
        direction: str,         # "YES" | "NO"
        size_usdc: float,
        price: float,
        market_order: bool = True,
    ) -> Optional[Position]:
        """
        Place a bet and record the position.

        Returns Position on success, None on failure.
        """
        if self.has_position(market.condition_id):
            log.warning(
                "[OrderMgr] Already have open position on %s – skipping",
                market.condition_id,
            )
            return None

        token_id = (
            market.yes_token_id if direction == "YES" else market.no_token_id
        )

        log.info(
            "[OrderMgr] %s %s @ %.4f – $%.2f USDC | %s",
            "DRY" if self._dry_run else "BUY",
            direction, price, size_usdc, market.question[:60],
        )

        if self._dry_run:
            order_id = f"dry_{market.condition_id[:8]}_{int(time.time())}"
            result   = OrderResult(
                order_id=order_id, status="dry_run",
                size_matched=size_usdc / price,
                price=price, side="BUY", token_id=token_id,
            )
        else:
            if direction == "YES":
                result = await self._client.buy_yes(
                    token_id, size_usdc, price, market_order
                )
            else:
                result = await self._client.buy_no(
                    token_id, size_usdc, price, market_order
                )

        if result is None:
            log.error("[OrderMgr] Order placement failed for %s", market.question[:60])
            return None

        position = Position(
            condition_id = market.condition_id,
            question     = market.question,
            direction    = direction,
            token_id     = token_id,
            entry_price  = price,
            size_usdc    = size_usdc,
            shares       = result.size_matched or size_usdc / price,
            order_id     = result.order_id,
        )
        self._positions[market.condition_id] = position
        log.info("[OrderMgr] Position opened: %s", position)
        return position

    def mark_resolved(
        self, condition_id: str, exit_price: float
    ) -> Optional[Position]:
        """Mark a position as resolved (market settled) and compute P&L."""
        pos = self._positions.get(condition_id)
        if pos is None or pos.closed:
            return None

        pos.closed     = True
        pos.exit_price = exit_price
        # Shares pay out $1 if won, $0 if lost
        pos.pnl_usdc   = pos.shares * exit_price - pos.size_usdc
        log.info(
            "[OrderMgr] Position closed: %s | exit=%.4f | pnl=$%+.2f",
            pos.question[:50], exit_price, pos.pnl_usdc,
        )
        return pos

    def total_pnl(self) -> float:
        return sum(p.pnl_usdc for p in self._positions.values() if p.closed)

    def summary(self) -> str:
        lines = [
            "── Position Summary ──────────────────────────────",
            f"  Open:   {self.open_count}",
            f"  Closed: {sum(1 for p in self._positions.values() if p.closed)}",
            f"  Total P&L: ${self.total_pnl():+.2f}",
        ]
        for p in self.open_positions:
            lines.append(f"  {p}")
        return "\n".join(lines)
