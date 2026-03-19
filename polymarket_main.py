"""
polymarket_main.py – Entry point for the Polymarket AI trading agent.

Usage:
    # Dry run (default – no real orders)
    python polymarket_main.py

    # Live trading (requires wallet keys in .env)
    POLY_DRY_RUN=false python polymarket_main.py

    # Custom MiroFish URL
    MIROFISH_URL=http://localhost:5001 python polymarket_main.py

Environment variables (see .env.example for full list):
    POLY_DRY_RUN              true|false (default: true)
    POLY_PRIVATE_KEY     Polygon wallet private key (0x...)
    POLY_API_KEY         Polymarket CLOB API key
    POLY_API_SECRET      Polymarket CLOB API secret
    POLY_API_PASSPHRASE  Polymarket CLOB API passphrase
    MIROFISH_URL         MiroFish base URL (default: http://localhost:5001)
    POLY_MIN_EDGE_PCT    Minimum edge % to trigger a bet (default: 5.0)
    POLY_MAX_POSITIONS   Max open positions (default: 10)
    POLY_SCAN_INTERVAL   Seconds between scans (default: 1800)
    POLY_SIM_ROUNDS      MiroFish simulation rounds (default: 10)
    POLY_MIN_VOLUME      Min 24h volume USD to consider a market (default: 500)
    POLY_MIN_LIQUIDITY   Min liquidity USD to consider a market (default: 200)
    POLY_MIN_BET_USDC    Minimum bet size (default: 1.0)
    POLY_MAX_BET_FRACTION Max fraction of bankroll per bet (default: 0.05)
    POLY_KELLY_FRACTION  Kelly fraction multiplier (default: 0.25)
"""

from __future__ import annotations

import asyncio
import os
import signal

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

from dotenv import load_dotenv

load_dotenv()

from agents.polymarket_agent import AgentConfig, PolymarketAgent
from polymarket.client import PolymarketClient
from polymarket.market_scanner import ScanConfig
from utils.logger import get_logger, setup_logging

log = get_logger(__name__)


def _bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


def _float(key: str, default: float) -> float:
    return float(os.getenv(key, default))


def _int(key: str, default: int) -> int:
    return int(os.getenv(key, default))


def _str(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def build_config() -> AgentConfig:
    scan_cfg = ScanConfig(
        min_volume_24h      = _float("POLY_MIN_VOLUME",         0.0),
        min_liquidity       = _float("POLY_MIN_LIQUIDITY",      0.0),
        max_markets         = _int  ("POLY_MAX_SCAN_MARKETS",    20),
        price_deadzone_low  = _float("POLY_PRICE_DEADZONE_LOW",  0.01),
        price_deadzone_high = _float("POLY_PRICE_DEADZONE_HIGH", 0.99),
        max_days_to_end     = None,
    )
    return AgentConfig(
        mirofish_url        = _str  ("MIROFISH_URL",          "http://localhost:5001"),
        mirofish_rounds     = _int  ("POLY_SIM_ROUNDS",       10),
        min_edge_pct        = _float("POLY_MIN_EDGE_PCT",      5.0) / 100.0,
        max_open_positions  = _int  ("POLY_MAX_POSITIONS",     10),
        kelly_fraction      = _float("POLY_KELLY_FRACTION",    0.25),
        max_bet_fraction    = _float("POLY_MAX_BET_FRACTION",  0.05),
        min_bet_usdc        = _float("POLY_MIN_BET_USDC",      1.0),
        scan_interval_sec   = _float("POLY_SCAN_INTERVAL",     1800.0),
        price_refresh       = True,
        scan_config         = scan_cfg,
        dry_run             = _bool ("POLY_DRY_RUN",                True),
    )


def build_client() -> PolymarketClient:
    return PolymarketClient(
        private_key    = _str("POLY_PRIVATE_KEY"),
        api_key        = _str("POLY_API_KEY"),
        api_secret     = _str("POLY_API_SECRET"),
        api_passphrase = _str("POLY_API_PASSPHRASE"),
    )


async def main() -> None:
    setup_logging(level=_str("LOG_LEVEL", "INFO"), log_file="logs/polymarket.log")

    cfg    = build_config()
    client = build_client()

    agent: PolymarketAgent

    loop = asyncio.get_running_loop()

    async with client:
        agent = PolymarketAgent(client, cfg)

        def _handle_signal():
            log.info("[Main] Shutdown signal received")
            agent.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handle_signal)

        await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
