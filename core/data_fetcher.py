"""
core/data_fetcher.py – Owns all WebSocket connections and routes data
to the SignalGenerator.

Architecture:
  DataFetcher
  ├── BinanceFuturesFeed   (oracle – price leader)
  └── WeexFuturesFeed      (lagging – execution exchange)

Both feeds run as concurrent asyncio tasks.  DataFetcher exposes a
unified callback interface so SignalGenerator never touches an exchange
class directly.

Adding a 3rd exchange: instantiate its feed here and wire callbacks.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Coroutine, Dict, List, Optional

from exchanges.binance_futures import BinanceFuturesFeed
from exchanges.weex_futures import WeexFuturesFeed
from models.order_book import OrderBook
from utils.logger import get_logger

log = get_logger(__name__)


# Callback type: async def handler(ob_binance, ob_weex, trade_ticks) -> None
# But we use per-event callbacks for flexibility:
OrderBookPairCallback = Callable[
    [OrderBook, OrderBook], Coroutine
]


class MarketState:
    """
    Shared mutable snapshot of the latest market data for one symbol.
    Updated by DataFetcher, read by SignalGenerator.
    """

    __slots__ = (
        "symbol",
        "binance_ob",
        "weex_ob",
        "binance_ticks",   # list of (price, qty, side, ts)
        "weex_ticks",
        "_lock",
    )

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.binance_ob: Optional[OrderBook] = None
        self.weex_ob: Optional[OrderBook] = None
        # Rolling buffer: last N trade ticks from each exchange
        self.binance_ticks: List[tuple] = []
        self.weex_ticks: List[tuple] = []
        self._lock = asyncio.Lock()

    def is_ready(self) -> bool:
        """Both order books have been populated at least once."""
        return (
            self.binance_ob is not None
            and self.weex_ob is not None
            and self.binance_ob.best_bid is not None
            and self.weex_ob.best_bid is not None
        )


class DataFetcher:
    """
    Manages exchange feeds and maintains per-symbol MarketState.
    Notifies the SignalGenerator via callback on every order book update.
    """

    _TICK_BUFFER_SIZE = 200   # max ticks kept per symbol per exchange

    def __init__(self, symbols: List[str]) -> None:
        self.symbols = [s.upper() for s in symbols]

        self.binance = BinanceFuturesFeed(self.symbols)
        self.weex = WeexFuturesFeed(self.symbols)

        # Per-symbol state shared with SignalGenerator
        self.state: Dict[str, MarketState] = {
            sym: MarketState(sym) for sym in self.symbols
        }

        # Registered callback: called when BOTH books for a symbol are fresh
        self._on_update_callbacks: List[OrderBookPairCallback] = []

        # Wire internal callbacks
        self.binance.on_order_book_update(self._on_binance_ob)
        self.binance.on_trade_tick(self._on_binance_tick)
        self.weex.on_order_book_update(self._on_weex_ob)
        self.weex.on_trade_tick(self._on_weex_tick)

    def on_market_update(self, cb: OrderBookPairCallback) -> None:
        """Register a coroutine to be called on every dual-book update."""
        self._on_update_callbacks.append(cb)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Launch both feeds concurrently.
        This coroutine runs until cancelled.
        """
        log.info("[DataFetcher] Starting feeds for symbols: %s", self.symbols)
        await asyncio.gather(
            self.binance.connect(),
            self.weex.connect(),
        )

    async def stop(self) -> None:
        await asyncio.gather(
            self.binance.disconnect(),
            self.weex.disconnect(),
        )
        log.info("[DataFetcher] All feeds stopped.")

    # ── Internal callbacks ────────────────────────────────────────────────────

    async def _on_binance_ob(self, ob: OrderBook) -> None:
        ms = self.state[ob.symbol]
        async with ms._lock:
            ms.binance_ob = ob
        await self._maybe_emit(ob.symbol)

    async def _on_weex_ob(self, ob: OrderBook) -> None:
        ms = self.state[ob.symbol]
        async with ms._lock:
            ms.weex_ob = ob
        await self._maybe_emit(ob.symbol)

    async def _on_binance_tick(
        self, symbol: str, price: float, qty: float, side: str
    ) -> None:
        ms = self.state[symbol]
        entry = (price, qty, side, time.time())
        async with ms._lock:
            ms.binance_ticks.append(entry)
            if len(ms.binance_ticks) > self._TICK_BUFFER_SIZE:
                ms.binance_ticks = ms.binance_ticks[-self._TICK_BUFFER_SIZE :]

    async def _on_weex_tick(
        self, symbol: str, price: float, qty: float, side: str
    ) -> None:
        ms = self.state[symbol]
        entry = (price, qty, side, time.time())
        async with ms._lock:
            ms.weex_ticks.append(entry)
            if len(ms.weex_ticks) > self._TICK_BUFFER_SIZE:
                ms.weex_ticks = ms.weex_ticks[-self._TICK_BUFFER_SIZE :]

    async def _maybe_emit(self, symbol: str) -> None:
        """Emit update to registered callbacks only when both books are ready."""
        ms = self.state[symbol]
        if not ms.is_ready():
            return
        for cb in self._on_update_callbacks:
            try:
                await cb(ms.binance_ob, ms.weex_ob)
            except Exception as exc:
                log.error(
                    "[DataFetcher] Callback error for %s: %s", symbol, exc, exc_info=True
                )
