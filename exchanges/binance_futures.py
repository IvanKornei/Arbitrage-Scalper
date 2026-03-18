"""
exchanges/binance_futures.py – Binance USDⓈ-M Futures market-data feed.

Streams used (combined WebSocket):
  • <symbol>@depth@100ms  – order book diff stream (100 ms cadence)
  • <symbol>@aggTrade     – aggregated trade ticks (sub-ms latency)

Order-book management follows Binance's official diff-depth protocol:
  1. Fetch REST snapshot.
  2. Buffer incoming diffs.
  3. Discard diffs with lastUpdateId <= snapshot.lastUpdateId.
  4. Apply remaining diffs in sequence order.

Reference: https://binance-docs.github.io/apidocs/futures/en/#diff-book-depth-streams
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional
from urllib.parse import urlencode

import aiohttp
try:
    import orjson as json_lib
    def _loads(s): return json_lib.loads(s)
except ImportError:
    import json as json_lib
    def _loads(s): return json_lib.loads(s)
import websockets
from websockets.exceptions import ConnectionClosed

from config import CONFIG
from exchanges.base_exchange import BaseMarketDataFeed
from models.order_book import OrderBook
from utils.logger import get_logger

log = get_logger(__name__)

_SNAPSHOT_URL = "https://fapi.binance.com/fapi/v1/depth"
_SNAPSHOT_LIMIT = 1000   # levels to fetch in initial snapshot


class BinanceFuturesFeed(BaseMarketDataFeed):
    """
    Single combined-stream WebSocket for all configured symbols.
    Reconnects automatically with exponential back-off.
    """

    def __init__(self, symbols: List[str]) -> None:
        super().__init__("Binance", symbols)
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        # Per-symbol: buffer of diffs received before snapshot arrives
        self._pending_diffs: Dict[str, list] = {s: [] for s in self.symbols}
        self._snap_applied: Dict[str, bool] = {s: False for s in self.symbols}
        self._snap_last_id: Dict[str, int] = {s: 0 for s in self.symbols}

    # ── Build WebSocket URL ───────────────────────────────────────────────────

    def _build_ws_url(self) -> str:
        streams = []
        for sym in self.symbols:
            s = sym.lower()
            streams.append(f"{s}@depth@100ms")
            streams.append(f"{s}@aggTrade")
        combined = "/".join(streams)
        return f"{CONFIG.binance.ws_base}/stream?streams={combined}"

    # ── Public interface ──────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._running = True
        attempt = 0
        while self._running:
            url = self._build_ws_url()
            log.info("[Binance] Connecting to %s ...", url[:80] + "...")
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=10 * 1024 * 1024,  # 10 MB
                ) as ws:
                    self._ws = ws
                    attempt = 0
                    log.info("[Binance] WebSocket connected. Fetching snapshots ...")
                    # Fetch REST snapshots concurrently for all symbols
                    await asyncio.gather(
                        *[self._fetch_snapshot(sym) for sym in self.symbols]
                    )
                    log.info("[Binance] All snapshots applied. Streaming diffs ...")
                    async for raw in ws:
                        t0 = time.perf_counter()
                        await self._handle_message(raw)
                        elapsed_ms = (time.perf_counter() - t0) * 1000
                        if elapsed_ms > 5:
                            log.debug(
                                "[Binance] Message processing took %.2f ms", elapsed_ms
                            )
            except ConnectionClosed as exc:
                log.warning("[Binance] WS closed: %s", exc)
            except Exception as exc:
                log.error("[Binance] Unexpected error: %s", exc, exc_info=True)
            finally:
                self._ws = None
                # Reset snapshot state for reconnection
                for sym in self.symbols:
                    self._snap_applied[sym] = False
                    self._pending_diffs[sym] = []

            if not self._running:
                break
            delay = self._backoff(attempt)
            log.info("[Binance] Reconnecting in %.1f s (attempt %d) ...", delay, attempt)
            await asyncio.sleep(delay)
            attempt += 1

    async def disconnect(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    # ── Message dispatch ──────────────────────────────────────────────────────

    async def _handle_message(self, raw: bytes | str) -> None:
        msg = _loads(raw)
        stream: str = msg.get("stream", "")
        data: dict = msg.get("data", msg)

        if "@depth" in stream:
            await self._handle_depth(data)
        elif "@aggTrade" in stream:
            await self._handle_agg_trade(data)

    # ── Order book management ─────────────────────────────────────────────────

    async def _fetch_snapshot(self, symbol: str) -> None:
        """Fetch REST depth snapshot and seed the local order book."""
        params = urlencode({"symbol": symbol, "limit": _SNAPSHOT_LIMIT})
        url = f"{_SNAPSHOT_URL}?{params}"
        t0 = time.perf_counter()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    snap = await resp.json(content_type=None)
        except Exception as exc:
            log.error("[Binance] Snapshot fetch failed for %s: %s", symbol, exc)
            return
        elapsed_ms = (time.perf_counter() - t0) * 1000
        log.info(
            "[Binance] Snapshot fetched for %s in %.1f ms (lastUpdateId=%s)",
            symbol,
            elapsed_ms,
            snap.get("lastUpdateId"),
        )
        last_id: int = snap["lastUpdateId"]
        self._snap_last_id[symbol] = last_id

        ob = self.order_books[symbol]
        ob.apply_snapshot(snap["bids"], snap["asks"])

        # Apply buffered diffs that arrived during snapshot fetch
        for diff in self._pending_diffs[symbol]:
            if diff["U"] <= last_id + 1 <= diff["u"]:
                ob.apply_delta(diff["b"], diff["a"], sequence=diff["u"])
            elif diff["u"] > last_id:
                ob.apply_delta(diff["b"], diff["a"], sequence=diff["u"])

        self._pending_diffs[symbol] = []
        self._snap_applied[symbol] = True
        log.debug("[Binance] OrderBook initialised: %s", ob)

    async def _handle_depth(self, data: dict) -> None:
        symbol: str = data["s"].upper()
        if symbol not in self.order_books:
            return

        recv_ts = time.time()

        if not self._snap_applied[symbol]:
            # Buffer until snapshot is ready
            self._pending_diffs[symbol].append(data)
            return

        ob = self.order_books[symbol]
        ob.apply_delta(
            bids=data["b"],
            asks=data["a"],
            ts=recv_ts,
            sequence=data["u"],
        )
        await self._emit_order_book(ob)

    # ── Trade ticks ───────────────────────────────────────────────────────────

    async def _handle_agg_trade(self, data: dict) -> None:
        symbol: str = data["s"].upper()
        price = float(data["p"])
        qty = float(data["q"])
        side = "SELL" if data["m"] else "BUY"   # m=True → maker was buyer → aggressor SELL
        await self._emit_tick(symbol, price, qty, side)
