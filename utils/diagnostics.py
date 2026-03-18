"""
utils/diagnostics.py – Throttled JSONL writer for signal evaluation diagnostics.

Every time SignalGenerator evaluates a symbol it writes one JSON line with the
raw values of all three conditions – regardless of whether any condition passed.

This lets us analyse real market data and tune thresholds before tightening
or relaxing the entry criteria.

Output format (one JSON object per line):
{
  "ts": 1710000000.123,          # unix timestamp (float)
  "symbol": "BTCUSDT",
  "direction": "LONG",           # tentative direction based on spread sign, or null
  "spread_pct": 0.091,           # Binance-WEEX spread %
  "c1_pass": true,               # spread >= LATENCY_THRESHOLD_PCT
  "volume_ratio": 2.4,           # dir_vol / rolling_avg
  "c2_pass": false,              # volume_ratio >= VOLUME_SPIKE_MULTIPLIER
  "tick_count": 3,               # tick repetition count
  "c3_pass": false,              # tick_count >= TICK_REPETITION_MIN_COUNT
  "all_pass": false,             # all three conditions
  "confidence": null,            # geometric-mean confidence or null
  "binance_mid": 68500.1,
  "weex_mid": 68444.5
}
"""

from __future__ import annotations

import json
import os
import time
from typing import Dict, Optional

from config import CONFIG
from utils.logger import get_logger

log = get_logger(__name__)


class DiagLogger:
    """
    Thread-safe (single-threaded asyncio) JSONL diagnostics writer.

    Throttles output to at most one record per symbol per DIAG_INTERVAL_SEC
    to avoid filling the disk during high-frequency feeds.
    """

    def __init__(self) -> None:
        self._cfg = CONFIG.diag
        self._last_write: Dict[str, float] = {}
        self._file = None
        self._enabled = self._cfg.enabled

        if self._enabled:
            path = self._cfg.log_file
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # Open in append mode so runs accumulate
            self._file = open(path, "a", buffering=1, encoding="utf-8")  # noqa: SIM115
            log.info("[Diag] Diagnostics logging → %s (interval=%.1fs)", path, self._cfg.interval_sec)

    # ── Public API ────────────────────────────────────────────────────────────

    def record(
        self,
        *,
        symbol: str,
        direction: Optional[str],
        spread_pct: float,
        c1_pass: bool,
        volume_ratio: float,
        c2_pass: bool,
        tick_count: int,
        c3_pass: bool,
        confidence: Optional[float],
        binance_mid: Optional[float],
        weex_mid: Optional[float],
    ) -> None:
        if not self._enabled or self._file is None:
            return

        now = time.time()
        last = self._last_write.get(symbol, 0.0)
        if now - last < self._cfg.interval_sec:
            return

        self._last_write[symbol] = now

        row = {
            "ts": round(now, 3),
            "symbol": symbol,
            "direction": direction,
            "spread_pct": round(spread_pct, 6),
            "c1_pass": c1_pass,
            "volume_ratio": round(volume_ratio, 4),
            "c2_pass": c2_pass,
            "tick_count": tick_count,
            "c3_pass": c3_pass,
            "all_pass": c1_pass and c2_pass and c3_pass,
            "confidence": round(confidence, 4) if confidence is not None else None,
            "binance_mid": binance_mid,
            "weex_mid": weex_mid,
        }
        try:
            self._file.write(json.dumps(row) + "\n")
        except Exception as exc:
            log.warning("[Diag] Write error: %s", exc)

    def close(self) -> None:
        if self._file:
            self._file.flush()
            self._file.close()
            self._file = None
