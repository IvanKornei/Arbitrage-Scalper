"""
web/server.py – aiohttp WebSocket + REST API server.

Bridges the browser dashboard to the running bot.
All real-time data is pushed via WebSocket; control actions use REST.

Endpoints:
  GET  /                    → serve dashboard HTML
  GET  /static/<file>       → static assets
  GET  /api/status          → bot status + config snapshot
  POST /api/start           → start the bot
  POST /api/stop            → stop the bot
  POST /api/config          → update signal/risk params (hot-reload)
  POST /api/emergency       → close all positions immediately
  POST /api/dry-run         → toggle dry-run mode
  WS   /ws                  → real-time push stream (JSON frames)

WebSocket message types (server → client):
  { type: "status",    data: { running, dry_run, uptime_sec } }
  { type: "market",    data: { symbol, binance_mid, weex_mid, spread_pct, b_age_ms, w_age_ms } }
  { type: "signal",    data: Signal.__dict__ }
  { type: "trade",     data: Trade.__dict__ }
  { type: "trade_closed", data: Trade.__dict__ }
  { type: "stats",     data: { open_count, total_pnl, trade_count } }
  { type: "log",       data: { level, msg, ts_ms } }
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional, Set

from aiohttp import WSMsgType, web

from utils.logger import get_logger

log = get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


# ── WebSocket broadcast hub ───────────────────────────────────────────────────

class Hub:
    """Fan-out broadcaster to all connected WebSocket clients."""

    def __init__(self) -> None:
        self._clients: Set[web.WebSocketResponse] = set()

    def add(self, ws: web.WebSocketResponse) -> None:
        self._clients.add(ws)
        log.debug("[Hub] Client connected (total=%d)", len(self._clients))

    def remove(self, ws: web.WebSocketResponse) -> None:
        self._clients.discard(ws)
        log.debug("[Hub] Client disconnected (total=%d)", len(self._clients))

    async def broadcast(self, msg_type: str, data: dict) -> None:
        if not self._clients:
            return
        frame = json.dumps({"type": msg_type, "data": data, "ts": time.time()})
        dead = set()
        for ws in self._clients:
            try:
                await ws.send_str(frame)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self._clients.discard(ws)


# ── Bot controller ────────────────────────────────────────────────────────────

class BotController:
    """
    Wraps BotRunner lifecycle and exposes push hooks for the Hub.
    Imported lazily so the web server starts even before the bot modules load.
    """

    def __init__(self, hub: Hub) -> None:
        self._hub = hub
        self._runner_task: Optional[asyncio.Task] = None
        self._runner = None
        self.dry_run: bool = os.getenv("DRY_RUN", "false").lower() in ("true", "1")
        self.started_at: Optional[float] = None
        self._total_pnl: float = 0.0
        self._trade_count: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> dict:
        if self._runner_task and not self._runner_task.done():
            return {"ok": False, "msg": "Already running"}

        # Import here to avoid circular imports at module load
        from main import BotRunner
        os.environ["DRY_RUN"] = "true" if self.dry_run else "false"

        self._runner = BotRunner()
        self._runner._hub = self._hub          # inject hub for signal/trade events
        self._runner._controller = self        # back-reference

        self._runner_task = asyncio.create_task(
            self._run_with_hooks(), name="bot-runner"
        )
        self.started_at = time.time()
        log.info("[WebServer] Bot started (dry_run=%s)", self.dry_run)
        await self._hub.broadcast("status", self._status_dict())
        return {"ok": True, "msg": "Bot started"}

    async def stop(self) -> dict:
        if not self._runner_task or self._runner_task.done():
            return {"ok": False, "msg": "Not running"}
        if self._runner:
            self._runner._request_shutdown()
        await asyncio.sleep(0.5)
        if not self._runner_task.done():
            self._runner_task.cancel()
        self.started_at = None
        log.info("[WebServer] Bot stopped")
        await self._hub.broadcast("status", self._status_dict())
        return {"ok": True, "msg": "Bot stopped"}

    async def emergency_close(self) -> dict:
        if self._runner and hasattr(self._runner, "_engine") and self._runner._engine:
            await self._runner._engine.close_all()
            await self._hub.broadcast("status", self._status_dict())
            return {"ok": True, "msg": "All positions closed"}
        return {"ok": False, "msg": "No active engine"}

    async def toggle_dry_run(self, enabled: bool) -> dict:
        self.dry_run = enabled
        os.environ["DRY_RUN"] = "true" if enabled else "false"
        await self._hub.broadcast("status", self._status_dict())
        return {"ok": True, "dry_run": self.dry_run}

    def is_running(self) -> bool:
        return bool(self._runner_task and not self._runner_task.done())

    def _status_dict(self) -> dict:
        engine = None
        if self._runner and hasattr(self._runner, "_engine"):
            engine = self._runner._engine
        return {
            "running": self.is_running(),
            "dry_run": self.dry_run,
            "uptime_sec": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "open_positions": engine.open_trade_count if engine else 0,
            "total_pnl": round(self._total_pnl, 4),
            "trade_count": self._trade_count,
        }

    async def _run_with_hooks(self) -> None:
        """Run BotRunner and catch exceptions."""
        try:
            await self._runner.run()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.error("[WebServer] BotRunner crashed: %s", exc, exc_info=True)
            await self._hub.broadcast("log", {
                "level": "ERROR",
                "msg": f"Bot crashed: {exc}",
                "ts_ms": time.time() * 1000,
            })
        finally:
            self.started_at = None
            await self._hub.broadcast("status", self._status_dict())

    # ── Config hot-reload ─────────────────────────────────────────────────────

    async def update_config(self, patch: dict) -> dict:
        """Update .env and attempt live config reload."""
        env_path = Path(".env")
        if not env_path.exists():
            env_path = Path(".env.example")

        # Read current .env
        lines = env_path.read_text().splitlines() if env_path.exists() else []
        updated_keys = set()
        new_lines = []
        for line in lines:
            if "=" in line and not line.startswith("#"):
                key = line.split("=")[0].strip()
                if key in patch:
                    new_lines.append(f"{key}={patch[key]}")
                    updated_keys.add(key)
                    continue
            new_lines.append(line)
        # Append new keys not yet in file
        for key, val in patch.items():
            if key not in updated_keys:
                new_lines.append(f"{key}={val}")

        Path(".env").write_text("\n".join(new_lines) + "\n")
        log.info("[WebServer] Config updated: %s", list(patch.keys()))
        return {"ok": True, "updated": list(patch.keys())}


# ── HTTP handlers ─────────────────────────────────────────────────────────────

async def handle_index(request: web.Request) -> web.Response:
    index = STATIC_DIR / "index.html"
    return web.FileResponse(index)


async def handle_status(request: web.Request) -> web.Response:
    ctrl: BotController = request.app["ctrl"]
    # Also include current market state snapshot
    state_snapshot = {}
    if ctrl._runner and hasattr(ctrl._runner, "_fetcher") and ctrl._runner._fetcher:
        for sym, ms in ctrl._runner._fetcher.state.items():
            if ms.is_ready():
                b_mid = ms.binance_ob.mid_price or 0
                w_mid = ms.weex_ob.mid_price or 0
                spread = (b_mid - w_mid) / w_mid * 100 if w_mid else 0
                state_snapshot[sym] = {
                    "binance_mid": round(b_mid, 6),
                    "weex_mid": round(w_mid, 6),
                    "spread_pct": round(spread, 4),
                    "b_age_ms": round(ms.binance_ob.age_ms(), 1),
                    "w_age_ms": round(ms.weex_ob.age_ms(), 1),
                }
    # Open trades
    trades = []
    if ctrl._runner and hasattr(ctrl._runner, "_engine") and ctrl._runner._engine:
        for t in ctrl._runner._engine.open_trades:
            trades.append(_trade_dict(t, state_snapshot.get(t.symbol, {})))

    return web.json_response({
        **ctrl._status_dict(),
        "market": state_snapshot,
        "trades": trades,
    })


async def handle_start(request: web.Request) -> web.Response:
    ctrl: BotController = request.app["ctrl"]
    result = await ctrl.start()
    return web.json_response(result)


async def handle_stop(request: web.Request) -> web.Response:
    ctrl: BotController = request.app["ctrl"]
    result = await ctrl.stop()
    return web.json_response(result)


async def handle_emergency(request: web.Request) -> web.Response:
    ctrl: BotController = request.app["ctrl"]
    result = await ctrl.emergency_close()
    return web.json_response(result)


async def handle_dry_run(request: web.Request) -> web.Response:
    ctrl: BotController = request.app["ctrl"]
    body = await request.json()
    result = await ctrl.toggle_dry_run(bool(body.get("enabled", True)))
    return web.json_response(result)


async def handle_config(request: web.Request) -> web.Response:
    ctrl: BotController = request.app["ctrl"]
    body = await request.json()
    result = await ctrl.update_config(body)
    return web.json_response(result)


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    hub: Hub = request.app["hub"]
    ctrl: BotController = request.app["ctrl"]

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    hub.add(ws)

    # Send initial state immediately on connect
    await ws.send_str(json.dumps({
        "type": "status",
        "data": ctrl._status_dict(),
        "ts": time.time(),
    }))

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # Client can send { "type": "ping" }
                data = json.loads(msg.data)
                if data.get("type") == "ping":
                    await ws.send_str(json.dumps({"type": "pong", "ts": time.time()}))
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        hub.remove(ws)

    return ws


# ── Periodic push task ────────────────────────────────────────────────────────

async def _push_loop(app: web.Application) -> None:
    """Push market state to all WS clients every 500 ms."""
    hub: Hub = app["hub"]
    ctrl: BotController = app["ctrl"]

    while True:
        await asyncio.sleep(0.5)
        if not hub._clients:
            continue

        # Market data
        if ctrl._runner and hasattr(ctrl._runner, "_fetcher") and ctrl._runner._fetcher:
            for sym, ms in ctrl._runner._fetcher.state.items():
                if not ms.is_ready():
                    continue
                b_mid = ms.binance_ob.mid_price or 0
                w_mid = ms.weex_ob.mid_price or 0
                spread = (b_mid - w_mid) / w_mid * 100 if w_mid else 0
                await hub.broadcast("market", {
                    "symbol": sym,
                    "binance_mid": round(b_mid, 6),
                    "weex_mid": round(w_mid, 6),
                    "spread_pct": round(spread, 4),
                    "b_age_ms": round(ms.binance_ob.age_ms(), 1),
                    "w_age_ms": round(ms.weex_ob.age_ms(), 1),
                })

        # Status heartbeat every 5 s
        if int(time.time()) % 5 == 0:
            await hub.broadcast("status", ctrl._status_dict())

        # Open trades P&L update
        if ctrl._runner and hasattr(ctrl._runner, "_engine") and ctrl._runner._engine:
            for t in ctrl._runner._engine.open_trades:
                ms = ctrl._runner._fetcher.state.get(t.symbol) if ctrl._runner._fetcher else None
                price = ms.weex_ob.mid_price if ms and ms.weex_ob else t.entry_price
                await hub.broadcast("trade", _trade_dict(t, {"weex_mid": price}))


def _trade_dict(trade, market: dict) -> dict:
    price = market.get("weex_mid") or trade.entry_price
    return {
        "id": trade.id,
        "symbol": trade.symbol,
        "direction": trade.direction.value,
        "entry_price": trade.entry_price,
        "quantity": trade.quantity,
        "stop_loss": trade.trailing_stop or trade.stop_loss,
        "state": trade.state.value,
        "duration_sec": round(trade.duration_sec(), 1),
        "unrealised_pct": round(trade.unrealised_pct(price), 4) if price else 0,
        "unrealised_pnl": round(trade.unrealised_pnl(price), 4) if price else 0,
    }


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> web.Application:
    app = web.Application()
    hub = Hub()
    ctrl = BotController(hub)

    app["hub"] = hub
    app["ctrl"] = ctrl

    # Routes
    app.router.add_get("/", handle_index)
    app.router.add_static("/static", STATIC_DIR)
    app.router.add_get("/api/status", handle_status)
    app.router.add_post("/api/start", handle_start)
    app.router.add_post("/api/stop", handle_stop)
    app.router.add_post("/api/emergency", handle_emergency)
    app.router.add_post("/api/dry-run", handle_dry_run)
    app.router.add_post("/api/config", handle_config)
    app.router.add_get("/ws", handle_ws)

    # Background push task
    async def start_push(app):
        app["push_task"] = asyncio.create_task(_push_loop(app))

    async def stop_push(app):
        app["push_task"].cancel()

    app.on_startup.append(start_push)
    app.on_cleanup.append(stop_push)

    return app


def run_server(host: str = "0.0.0.0", port: int = 8080) -> None:
    app = create_app()
    log.info("[WebServer] Dashboard at http://%s:%d", host, port)
    web.run_app(app, host=host, port=port, print=None)
