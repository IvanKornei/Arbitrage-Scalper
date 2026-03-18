"""
exchanges/base_exchange.py – Abstract contracts for all exchange adapters.

To add a new exchange:
  1. Subclass BaseMarketDataFeed  → implement connect / _handle_message
  2. Subclass BaseExecutionClient → implement place_order / cancel_order / get_position
  3. Register in core/data_fetcher.py

Nothing in the core pipeline imports a concrete exchange class directly.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Dict, List, Optional

from models.order_book import OrderBook
from models.trade import Trade


# ── Callback type aliases ──────────────────────────────────────────────────────

# Called every time the order book is updated
OrderBookCallback = Callable[[OrderBook], Awaitable[None]]

# Called every time an aggressor trade (tick) arrives
TradeTickCallback = Callable[[str, float, float, str], Awaitable[None]]
#                                    symbol price   qty  side ("BUY"/"SELL")


# ── Market data feed ──────────────────────────────────────────────────────────

class BaseMarketDataFeed(ABC):
    """
    Manages a persistent WebSocket connection for one exchange.
    Owns order books for N symbols and broadcasts updates to callbacks.
    """

    def __init__(self, exchange_name: str, symbols: List[str]) -> None:
        self.exchange_name = exchange_name
        self.symbols = [s.upper() for s in symbols]

        # Shared state: symbol → live order book
        self.order_books: Dict[str, OrderBook] = {
            sym: OrderBook(symbol=sym, exchange=exchange_name)
            for sym in self.symbols
        }

        self._ob_callbacks: List[OrderBookCallback] = []
        self._tick_callbacks: List[TradeTickCallback] = []
        self._running = False

    # ── Public API ────────────────────────────────────────────────────────────

    def on_order_book_update(self, cb: OrderBookCallback) -> None:
        """Register a coroutine called on every order-book delta."""
        self._ob_callbacks.append(cb)

    def on_trade_tick(self, cb: TradeTickCallback) -> None:
        """Register a coroutine called on every aggressor trade."""
        self._tick_callbacks.append(cb)

    @abstractmethod
    async def connect(self) -> None:
        """
        Open WebSocket(s), subscribe to streams, and loop forever.
        Must handle reconnection internally with exponential back-off.
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully close the WebSocket connection."""

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _emit_order_book(self, ob: OrderBook) -> None:
        for cb in self._ob_callbacks:
            await cb(ob)

    async def _emit_tick(
        self, symbol: str, price: float, qty: float, side: str
    ) -> None:
        for cb in self._tick_callbacks:
            await cb(symbol, price, qty, side)

    @staticmethod
    def _backoff(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
        """Exponential back-off: base * 2^attempt, capped at `cap` seconds."""
        return min(base * (2 ** attempt), cap)


# ── Execution client ──────────────────────────────────────────────────────────

class OrderResult:
    """Thin wrapper around exchange order response."""

    def __init__(
        self,
        order_id: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        status: str,
        raw: Optional[dict] = None,
    ) -> None:
        self.order_id = order_id
        self.symbol = symbol
        self.side = side
        self.qty = qty
        self.price = price
        self.status = status
        self.raw = raw or {}

    def __repr__(self) -> str:
        return (
            f"OrderResult(id={self.order_id} {self.symbol} {self.side} "
            f"qty={self.qty} price={self.price} status={self.status})"
        )


class BaseExecutionClient(ABC):
    """
    Sends and manages orders on a single exchange.
    All methods are async and must be safe to call concurrently.
    """

    def __init__(self, exchange_name: str) -> None:
        self.exchange_name = exchange_name

    @abstractmethod
    async def place_market_order(
        self,
        symbol: str,
        side: str,        # "BUY" | "SELL"
        quantity: float,
        reduce_only: bool = False,
    ) -> OrderResult:
        """Submit a market order and return immediately with order metadata."""

    @abstractmethod
    async def place_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        reduce_only: bool = False,
        post_only: bool = False,
    ) -> OrderResult:
        """Submit a limit order."""

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an open order. Returns True if successfully cancelled."""

    @abstractmethod
    async def get_open_position(self, symbol: str) -> Optional[dict]:
        """
        Return position dict or None if flat.
        Expected keys: side, size, entry_price, unrealised_pnl, liq_price
        """

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set cross-margin leverage for a symbol before trading."""

    @abstractmethod
    async def get_account_balance(self) -> float:
        """Return available USDT balance."""
