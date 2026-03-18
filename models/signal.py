"""
models/signal.py – Immutable signal emitted by SignalGenerator.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalType(str, Enum):
    LATENCY_ARB = "LATENCY_ARB"
    NO_SIGNAL = "NO_SIGNAL"


@dataclass(frozen=True)
class Signal:
    symbol: str
    direction: Direction
    signal_type: SignalType

    # Raw measurements that caused the signal
    binance_price: float
    weex_price: float
    spread_pct: float          # (binance - weex) / weex * 100
    volume_ratio: float        # current_volume / rolling_avg_volume
    tick_count: int            # repeated ticks detected in window

    # Confidence [0.0 – 1.0] derived from how far each condition exceeds threshold
    confidence: float

    created_at: float = time.time()

    @property
    def age_ms(self) -> float:
        return (time.time() - self.created_at) * 1000.0

    def __str__(self) -> str:
        return (
            f"[{self.signal_type.value}] {self.symbol} {self.direction.value} "
            f"spread={self.spread_pct:+.4f}% "
            f"vol_ratio={self.volume_ratio:.2f}× "
            f"ticks={self.tick_count} "
            f"conf={self.confidence:.2f}"
        )


@dataclass(frozen=True)
class NoSignal:
    symbol: str
    reason: str
    created_at: float = time.time()
