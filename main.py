"""
main.py – Application entry point.

Wires together all components and runs the asyncio event loop.

Architecture diagram:
  ┌─────────────────────────────────────────────────────────────────────┐
  │                          DataFetcher                                │
  │  ┌──────────────────────┐       ┌─────────────────────────────┐    │
  │  │  BinanceFuturesFeed  │       │     WeexFuturesFeed          │    │
  │  │  (WS price leader)   │       │  (WS lagging exchange)       │    │
  │  └──────────┬───────────┘       └──────────────┬──────────────┘    │
  │             │ OrderBook + Ticks                 │ OrderBook + Ticks  │
  └─────────────┼─────────────────────────────────-┼───────────────────┘
                │                                  │
                └──────────────┬───────────────────┘
                               │ MarketState (per symbol)
                    ┌──────────▼──────────┐
                    │   SignalGenerator    │
                    │  ① Price Latency    │
                    │  ② Volume Spike     │
                    │  ③ Tick Repetition  │
                    └──────────┬──────────┘
                               │ Signal
                    ┌──────────▼──────────┐
                    │   ExecutionEngine    │◄──── RiskManager
                    │  (WEEX REST orders)  │      (sizing + stops)
                    └─────────────────────┘

Usage:
    # 1. Copy and fill in .env
    cp .env.example .env

    # 2. Install dependencies
    pip install -r requirements.txt

    # 3. Run
    python main.py

    # Optional: paper-trade mode (no orders sent)
    DRY_RUN=true python main.py

    # Web dashboard (default port 8080)
    WEB=true python main.py
    WEB=true WEB_PORT=8080 python main.py

    # Bot-only (no web UI)
    python main.py
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from typing import Optional

# ── uvloop for 30–50 % lower event-loop latency on Linux ─────────────────────
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    _UVLOOP = True
except ImportError:
    _UVLOOP = False

from config import CONFIG
from core.data_fetcher import DataFetcher
from core.execution_engine import ExecutionEngine
from core.risk_manager import RiskManager
from core.signal_generator import SignalGenerator
from utils.logger import get_logger, setup_logging

log = get_logger(__name__)

DRY_RUN: bool = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes")
WEB:     bool = os.getenv("WEB", "false").lower() in ("true", "1", "yes")
WEB_HOST: str = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT: int = int(os.getenv("WEB_PORT", "8080"))


# ── Graceful shutdown ─────────────────────────────────────────────────────────

class BotRunner:
    """
    Top-level orchestrator.  Handles startup, shutdown, and SIGINT/SIGTERM.
    """

    def __init__(self) -> None:
        self._fetcher: Optional[DataFetcher] = None
        self._engine: Optional[ExecutionEngine] = None
        self._shutdown_event = asyncio.Event()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()

        # Register signal handlers for clean shutdown
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._request_shutdown)

        log.info("=" * 70)
        log.info("  Arbitrage Scalper  |  uvloop=%s  |  dry_run=%s", _UVLOOP, DRY_RUN)
        log.info("  Pairs : %s", CONFIG.trading_pairs)
        log.info(
            "  Risk  : %.2f%% per trade | max %d positions | %d× leverage",
            CONFIG.risk.risk_per_trade_pct,
            CONFIG.risk.max_open_positions,
            CONFIG.risk.leverage,
        )
        log.info(
            "  Signal: spread≥%.3f%% | vol≥%.1f× | ticks≥%d",
            CONFIG.signal.latency_threshold_pct,
            CONFIG.signal.volume_spike_multiplier,
            CONFIG.signal.tick_repetition_min_count,
        )
        log.info("=" * 70)

        # ── Build component graph ─────────────────────────────────────────────
        self._fetcher = DataFetcher(CONFIG.trading_pairs)
        risk = RiskManager()
        self._engine = ExecutionEngine(self._fetcher, risk)
        signal_gen = SignalGenerator(self._fetcher)

        # Wire signal → execution (or dry-run logger)
        dry = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes")
        if dry:
            signal_gen.on_signal(self._dry_run_handler)
            log.warning("[DRY RUN] Signal received → logged only, no orders sent")
        else:
            signal_gen.on_signal(self._engine.on_signal)

        # Push signals to web hub if available
        if hasattr(self, "_hub") and self._hub:
            async def _hub_signal(sig):
                await self._hub.broadcast("signal", {
                    "symbol":      sig.symbol,
                    "direction":   sig.direction.value,
                    "spread_pct":  sig.spread_pct,
                    "volume_ratio":sig.volume_ratio,
                    "tick_count":  sig.tick_count,
                    "confidence":  sig.confidence,
                })
            signal_gen.on_signal(_hub_signal)

        # ── Launch all tasks ──────────────────────────────────────────────────
        feed_task = asyncio.create_task(self._fetcher.run(), name="data-feeds")
        stats_task = asyncio.create_task(self._stats_loop(), name="stats-printer")

        log.info("[Main] All tasks started. Waiting for shutdown signal ...")
        await self._shutdown_event.wait()

        # ── Graceful shutdown ─────────────────────────────────────────────────
        log.info("[Main] Shutdown requested – closing positions and feeds ...")
        feed_task.cancel()
        stats_task.cancel()

        if not DRY_RUN and self._engine:
            await self._engine.close_all()
        await self._fetcher.stop()

        await asyncio.gather(feed_task, stats_task, return_exceptions=True)
        log.info("[Main] Shutdown complete.")

    def _request_shutdown(self) -> None:
        log.info("[Main] Shutdown signal received.")
        self._shutdown_event.set()

    # ── Dry run ───────────────────────────────────────────────────────────────

    @staticmethod
    async def _dry_run_handler(sig) -> None:
        log.info(
            "[DRY RUN] Signal: %s | conf=%.2f | spread=%+.4f%% | vol=%.2f× | ticks=%d",
            sig,
            sig.confidence,
            sig.spread_pct,
            sig.volume_ratio,
            sig.tick_count,
        )

    # ── Periodic stats ────────────────────────────────────────────────────────

    async def _stats_loop(self) -> None:
        """Print a heartbeat stats table every 30 seconds."""
        while True:
            await asyncio.sleep(30)
            if self._fetcher is None:
                continue
            lines = ["── Market State ──────────────────────────────────────"]
            for sym, ms in self._fetcher.state.items():
                if not ms.is_ready():
                    lines.append(f"  {sym}: waiting for data ...")
                    continue
                b_mid = ms.binance_ob.mid_price or 0
                w_mid = ms.weex_ob.mid_price or 0
                spread = (b_mid - w_mid) / w_mid * 100 if w_mid else 0
                b_age = ms.binance_ob.age_ms() if ms.binance_ob else 0
                w_age = ms.weex_ob.age_ms() if ms.weex_ob else 0
                lines.append(
                    f"  {sym}: B={b_mid:.4f} W={w_mid:.4f} "
                    f"spread={spread:+.4f}% "
                    f"B_age={b_age:.0f}ms W_age={w_age:.0f}ms"
                )
            if self._engine:
                lines.append(
                    f"── Open positions: {self._engine.open_trade_count} ──"
                )
                for trade in self._engine.open_trades:
                    lines.append(f"  {trade}")
            log.info("\n".join(lines))


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging(
        level=CONFIG.log_level,
        log_file=CONFIG.log_file,
    )

    if WEB:
        # Run web dashboard + bot together
        from web.server import create_app
        from aiohttp import web as aio_web

        async def run_with_web() -> None:
            app = create_app()
            runner_http = aio_web.AppRunner(app)
            await runner_http.setup()
            site = aio_web.TCPSite(runner_http, WEB_HOST, WEB_PORT)
            await site.start()
            log.info("[Main] Dashboard → http://%s:%d", WEB_HOST, WEB_PORT)
            # Keep alive until Ctrl+C
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass
            finally:
                await runner_http.cleanup()

        try:
            asyncio.run(run_with_web())
        except KeyboardInterrupt:
            pass
    else:
        # Headless bot only
        runner = BotRunner()
        try:
            asyncio.run(runner.run())
        except KeyboardInterrupt:
            pass
        finally:
            log.info("[Main] Process exited.")


if __name__ == "__main__":
    main()
