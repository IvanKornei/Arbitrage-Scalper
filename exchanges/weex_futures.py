"""
exchanges/weex_futures.py – WEEX Futures market-data feed AND execution client.

WebSocket documentation: https://docs.weex.com/en/futures/websocket.html
REST API documentation:  https://docs.weex.com/en/futures/rest.html

Authentication uses HMAC-SHA256 signature over:
    timestamp + method + requestPath + body
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import List, Optional
from urllib.parse import urlencode

import aiohttp
import orjson
import websockets
from websockets.exceptions import ConnectionClosed

from config import CONFIG
from exchanges.base_exchange import (
    BaseExecutionClient,
    BaseMarketDataFeed,
    OrderResult,
)
from models.order_book import OrderBook
from utils.logger import get_logger

log = get_logger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sign(secret: str, timestamp: str, method: str, path: str, body: str = "") -> str:
    message = timestamp + method.upper() + path + body
    return hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _headers(api_key: str, api_secret: str, passphrase: str, method: str, path: str, body: str = "") -> dict:
    ts = str(int(time.time() * 1000))
    sig = _sign(api_secret, ts, method, path, body)
    return {
        "Content-Type": "application/json",
        "ACCESS-KEY": api_key,
        "ACCESS-SIGN": sig,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": passphrase,
    }


# ── Market data feed ──────────────────────────────────────────────────────────

class WeexFuturesFeed(BaseMarketDataFeed):
    """
    WEEX Futures WebSocket feed.
    Subscribes to: order book (depth) and trade streams per symbol.
    """

    def __init__(self, symbols: List[str]) -> None:
        super().__init__("WEEX", symbols)
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    # ── Connection ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._running = True
        attempt = 0
        while self._running:
            log.info("[WEEX] Connecting to %s ...", CONFIG.weex.ws_base)
            try:
                async with websockets.connect(
                    CONFIG.weex.ws_base,
                    ping_interval=None,    # WEEX uses its own heartbeat
                    max_size=10 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    attempt = 0

                    # Subscribe to all symbols
                    await self._subscribe(ws)

                    # Start heartbeat (WEEX requires ping every 25 s)
                    self._heartbeat_task = asyncio.create_task(
                        self._heartbeat(ws)
                    )

                    log.info("[WEEX] Subscribed. Streaming ...")
                    async for raw in ws:
                        t0 = time.perf_counter()
                        await self._handle_message(raw)
                        elapsed_ms = (time.perf_counter() - t0) * 1000
                        if elapsed_ms > 5:
                            log.debug(
                                "[WEEX] Message took %.2f ms", elapsed_ms
                            )
            except ConnectionClosed as exc:
                log.warning("[WEEX] WS closed: %s", exc)
            except Exception as exc:
                log.error("[WEEX] Error: %s", exc, exc_info=True)
            finally:
                self._ws = None
                if self._heartbeat_task:
                    self._heartbeat_task.cancel()
                    self._heartbeat_task = None

            if not self._running:
                break
            delay = self._backoff(attempt)
            log.info("[WEEX] Reconnecting in %.1f s (attempt %d) ...", delay, attempt)
            await asyncio.sleep(delay)
            attempt += 1

    async def disconnect(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    # ── Subscription ──────────────────────────────────────────────────────────

    async def _subscribe(self, ws: websockets.WebSocketClientProtocol) -> None:
        for sym in self.symbols:
            # Depth (order book) – WEEX uses "books5" for top-5 or "books" for full
            await ws.send(orjson.dumps({
                "op": "subscribe",
                "args": [
                    {"channel": "books", "instId": sym},
                    {"channel": "trades", "instId": sym},
                ]
            }).decode())
            log.debug("[WEEX] Subscribed to books+trades for %s", sym)

    async def _heartbeat(self, ws: websockets.WebSocketClientProtocol) -> None:
        while True:
            try:
                await asyncio.sleep(20)
                await ws.send('{"op":"ping"}')
                log.debug("[WEEX] Sent ping")
            except Exception:
                break

    # ── Message dispatch ──────────────────────────────────────────────────────

    async def _handle_message(self, raw: bytes | str) -> None:
        if raw == '{"op":"pong"}' or raw == b'{"op":"pong"}':
            return
        msg = orjson.loads(raw)

        # WEEX pushes: {"arg": {"channel": "books", "instId": "BTCUSDT"}, "data": [...]}
        arg = msg.get("arg", {})
        channel = arg.get("channel", "")
        inst_id: str = arg.get("instId", "").upper()
        data = msg.get("data", [])

        if not data or inst_id not in self.order_books:
            return

        recv_ts = time.time()

        if channel == "books":
            await self._handle_books(inst_id, data[0], recv_ts, msg.get("action", "update"))
        elif channel == "trades":
            for tick in data:
                await self._handle_trade(inst_id, tick)

    async def _handle_books(
        self, symbol: str, data: dict, ts: float, action: str
    ) -> None:
        ob = self.order_books[symbol]
        if action == "snapshot":
            ob.apply_snapshot(data.get("bids", []), data.get("asks", []), ts=ts)
        else:
            ob.apply_delta(
                bids=data.get("bids", []),
                asks=data.get("asks", []),
                ts=ts,
                sequence=int(data.get("seqId", 0)),
            )
        await self._emit_order_book(ob)

    async def _handle_trade(self, symbol: str, tick: dict) -> None:
        price = float(tick["px"])
        qty = float(tick["sz"])
        side = tick.get("side", "buy").upper()
        await self._emit_tick(symbol, price, qty, side)


# ── Execution client ──────────────────────────────────────────────────────────

class WeexExecutionClient(BaseExecutionClient):
    """
    REST-based order execution for WEEX Futures.

    All order submissions are fire-and-await: we block until the exchange
    acknowledges the order (or raises an exception after timeout).
    """

    _ORDER_PATH = "/api/mix/v1/order/placeOrder"
    _CANCEL_PATH = "/api/mix/v1/order/cancel-order"
    _POSITION_PATH = "/api/mix/v1/position/singlePosition"
    _ACCOUNT_PATH = "/api/mix/v1/account/account"
    _LEVERAGE_PATH = "/api/mix/v1/account/setLeverage"

    def __init__(self) -> None:
        super().__init__("WEEX")
        self._session: Optional[aiohttp.ClientSession] = None
        self._cfg = CONFIG.weex

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=20,
                ttl_dns_cache=300,
                use_dns_cache=True,
            )
            timeout = aiohttp.ClientTimeout(total=5.0)
            self._session = aiohttp.ClientSession(
                connector=connector, timeout=timeout
            )
        return self._session

    async def _post(self, path: str, body: dict) -> dict:
        body_str = orjson.dumps(body).decode()
        hdrs = _headers(
            self._cfg.api_key,
            self._cfg.api_secret,
            self._cfg.passphrase,
            "POST",
            path,
            body_str,
        )
        url = self._cfg.rest_base + path
        t0 = time.perf_counter()
        session = await self._get_session()
        async with session.post(url, data=body_str, headers=hdrs) as resp:
            result = await resp.json(content_type=None)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        log.debug("[WEEX] POST %s → %.1f ms | code=%s", path, elapsed_ms, result.get("code"))
        if str(result.get("code")) != "00000":
            raise RuntimeError(f"WEEX API error: {result}")
        return result

    async def _get(self, path: str, params: dict) -> dict:
        hdrs = _headers(
            self._cfg.api_key,
            self._cfg.api_secret,
            self._cfg.passphrase,
            "GET",
            path + "?" + urlencode(params) if params else path,
        )
        url = self._cfg.rest_base + path
        t0 = time.perf_counter()
        session = await self._get_session()
        async with session.get(url, params=params, headers=hdrs) as resp:
            result = await resp.json(content_type=None)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        log.debug("[WEEX] GET %s → %.1f ms", path, elapsed_ms)
        return result

    # ── BaseExecutionClient implementation ────────────────────────────────────

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        reduce_only: bool = False,
    ) -> OrderResult:
        t0 = time.perf_counter()
        body = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "size": str(quantity),
            "side": side.lower(),           # "buy" / "sell"
            "orderType": "market",
            "reduceOnly": reduce_only,
        }
        resp = await self._post(self._ORDER_PATH, body)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        order_id = resp.get("data", {}).get("orderId", "")
        log.info(
            "[WEEX] Market %s %s qty=%s → orderId=%s (%.1f ms)",
            side,
            symbol,
            quantity,
            order_id,
            elapsed_ms,
        )
        return OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=side,
            qty=quantity,
            price=0.0,      # market order – price unknown until fill
            status="PLACED",
            raw=resp,
        )

    async def place_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        reduce_only: bool = False,
        post_only: bool = False,
    ) -> OrderResult:
        body = {
            "symbol": symbol,
            "marginCoin": "USDT",
            "price": str(price),
            "size": str(quantity),
            "side": side.lower(),
            "orderType": "limit",
            "timeInForceValue": "post_only" if post_only else "gtc",
            "reduceOnly": reduce_only,
        }
        resp = await self._post(self._ORDER_PATH, body)
        order_id = resp.get("data", {}).get("orderId", "")
        log.info(
            "[WEEX] Limit %s %s qty=%s @ %s → orderId=%s",
            side,
            symbol,
            quantity,
            price,
            order_id,
        )
        return OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=side,
            qty=quantity,
            price=price,
            status="PLACED",
            raw=resp,
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            await self._post(
                self._CANCEL_PATH,
                {"symbol": symbol, "marginCoin": "USDT", "orderId": order_id},
            )
            log.info("[WEEX] Cancelled order %s for %s", order_id, symbol)
            return True
        except Exception as exc:
            log.warning("[WEEX] Cancel failed for %s: %s", order_id, exc)
            return False

    async def get_open_position(self, symbol: str) -> Optional[dict]:
        resp = await self._get(
            self._POSITION_PATH,
            {"symbol": symbol, "marginCoin": "USDT"},
        )
        data = resp.get("data")
        if not data or float(data.get("total", 0)) == 0:
            return None
        return {
            "side": data.get("holdSide"),
            "size": float(data.get("total", 0)),
            "entry_price": float(data.get("averageOpenPrice", 0)),
            "unrealised_pnl": float(data.get("unrealizedPL", 0)),
            "liq_price": float(data.get("liquidationPrice", 0)),
        }

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        for hold_side in ("long", "short"):
            await self._post(
                self._LEVERAGE_PATH,
                {
                    "symbol": symbol,
                    "marginCoin": "USDT",
                    "leverage": str(leverage),
                    "holdSide": hold_side,
                },
            )
        log.info("[WEEX] Leverage set to %d× for %s", leverage, symbol)

    async def get_account_balance(self) -> float:
        resp = await self._get(
            self._ACCOUNT_PATH,
            {"symbol": "BTCUSDT", "marginCoin": "USDT"},
        )
        data = resp.get("data", {})
        balance = float(data.get("available", 0))
        log.debug("[WEEX] Available balance: %.2f USDT", balance)
        return balance

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
