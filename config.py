"""
config.py – Centralised configuration loaded from environment variables.
All modules import from here; never read os.environ directly elsewhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _float(key: str, default: float) -> float:
    return float(os.getenv(key, default))


def _int(key: str, default: int) -> int:
    return int(os.getenv(key, default))


def _str(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _list(key: str, default: str = "") -> List[str]:
    raw = os.getenv(key, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


# ── Exchange credentials ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class BinanceConfig:
    api_key: str = field(default_factory=lambda: _str("BINANCE_API_KEY"))
    api_secret: str = field(default_factory=lambda: _str("BINANCE_API_SECRET"))
    ws_base: str = "wss://fstream.binance.com"
    rest_base: str = "https://fapi.binance.com"


@dataclass(frozen=True)
class WeexConfig:
    api_key: str = field(default_factory=lambda: _str("WEEX_API_KEY"))
    api_secret: str = field(default_factory=lambda: _str("WEEX_API_SECRET"))
    passphrase: str = field(default_factory=lambda: _str("WEEX_PASSPHRASE"))
    ws_base: str = "wss://futures.weex.com/ws"
    rest_base: str = "https://futures.weex.com"


# ── Strategy parameters ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SignalConfig:
    # Price latency
    latency_threshold_pct: float = field(
        default_factory=lambda: _float("LATENCY_THRESHOLD_PCT", 0.08)
    )
    # Volume spike
    volume_spike_multiplier: float = field(
        default_factory=lambda: _float("VOLUME_SPIKE_MULTIPLIER", 3.0)
    )
    volume_lookback_bars: int = field(
        default_factory=lambda: _int("VOLUME_LOOKBACK_BARS", 20)
    )
    # Micro-structure / bot detection
    tick_repetition_window: float = field(
        default_factory=lambda: _float("TICK_REPETITION_WINDOW", 1.0)
    )
    tick_repetition_min_count: int = field(
        default_factory=lambda: _int("TICK_REPETITION_MIN_COUNT", 5)
    )
    # Signal cooldown
    cooldown_sec: float = field(
        default_factory=lambda: _float("SIGNAL_COOLDOWN_SEC", 2.0)
    )
    # Order book staleness thresholds
    binance_book_max_stale_ms: float = field(
        default_factory=lambda: _float("BINANCE_BOOK_MAX_STALE_MS", 500.0)
    )
    weex_book_max_stale_ms: float = field(
        default_factory=lambda: _float("WEEX_BOOK_MAX_STALE_MS", 1000.0)
    )


@dataclass(frozen=True)
class RiskConfig:
    account_balance: float = field(
        default_factory=lambda: _float("ACCOUNT_BALANCE_USDT", 1000.0)
    )
    risk_per_trade_pct: float = field(
        default_factory=lambda: _float("RISK_PER_TRADE_PCT", 0.5)
    )
    max_open_positions: int = field(
        default_factory=lambda: _int("MAX_OPEN_POSITIONS", 3)
    )
    leverage: int = field(default_factory=lambda: _int("LEVERAGE", 10))
    breakeven_trigger_pct: float = field(
        default_factory=lambda: _float("BREAKEVEN_TRIGGER_PCT", 0.15)
    )
    trailing_stop_pct: float = field(
        default_factory=lambda: _float("TRAILING_STOP_PCT", 0.12)
    )
    emergency_exit_spread_pct: float = field(
        default_factory=lambda: _float("EMERGENCY_EXIT_SPREAD_PCT", 0.02)
    )
    max_trade_duration_sec: int = field(
        default_factory=lambda: _int("MAX_TRADE_DURATION_SEC", 30)
    )
    monitor_interval_sec: float = field(
        default_factory=lambda: _float("MONITOR_INTERVAL_SEC", 0.1)
    )


# ── Diagnostics ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DiagConfig:
    enabled: bool = field(default_factory=lambda: _str("DIAG_ENABLED", "true").lower() == "true")
    log_file: str = field(default_factory=lambda: _str("DIAG_LOG_FILE", "logs/diagnostics.jsonl"))
    # Minimum seconds between diagnostic writes per symbol (throttle)
    interval_sec: float = field(default_factory=lambda: _float("DIAG_INTERVAL_SEC", 0.5))


# ── Top-level config singleton ─────────────────────────────────────────────────

@dataclass(frozen=True)
class BotConfig:
    trading_pairs: List[str] = field(
        default_factory=lambda: _list("TRADING_PAIRS", "BTCUSDT,ETHUSDT,SOLUSDT")
    )
    log_level: str = field(default_factory=lambda: _str("LOG_LEVEL", "INFO"))
    log_file: str = field(default_factory=lambda: _str("LOG_FILE", "logs/scalper.log"))

    binance: BinanceConfig = field(default_factory=BinanceConfig)
    weex: WeexConfig = field(default_factory=WeexConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    diag: DiagConfig = field(default_factory=DiagConfig)


# Singleton – import this everywhere
CONFIG = BotConfig()
