"""
core/signal_generator.py – Evaluates the 3-confluence entry conditions.

Condition 1 – Price Latency  (oracle vs lagging spread)
  • Binance mid-price has moved sharply.
  • The spread between Binance and WEEX exceeds LATENCY_THRESHOLD_PCT.
  • Direction is determined by the sign of the spread.

Condition 2 – Volume Injection  (order-flow validation)
  • The aggressor volume in the last N Binance ticks is X× the rolling
    average – confirming that real orders (not noise) drove the move.
  • Volume is directional: only buy volume counts for a LONG signal,
    only sell volume for a SHORT signal.

Condition 3 – Micro-structure / Bot Detection  (tick repetition)
  • Within the last TICK_REPETITION_WINDOW seconds on Binance, the same
    price level has been repeatedly hit (≥ TICK_REPETITION_MIN_COUNT).
  • This fingerprints aggressive market-making or momentum bots that
    are pushing the price in a tight cluster, validating the move.

All three conditions must be True simultaneously.  A confidence score
[0.0 – 1.0] is computed as the geometric mean of the three sub-scores.
"""

from __future__ import annotations

import time
from collections import deque, Counter
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np

from config import CONFIG
from core.data_fetcher import DataFetcher, MarketState
from models.order_book import OrderBook
from models.signal import Direction, NoSignal, Signal, SignalType
from utils.diagnostics import DiagLogger
from utils.logger import get_logger

log = get_logger(__name__)


class SymbolMetrics:
    """
    Per-symbol rolling statistics consumed by SignalGenerator.
    All updates are O(1) amortised using deques.
    """

    def __init__(self, lookback: int = 20) -> None:
        self.lookback = lookback
        # Rolling volume bars (each bar = qty of one aggTrade message)
        self._vol_window: Deque[float] = deque(maxlen=lookback)
        # Recent ticks: (price_rounded_to_tick, side, ts)
        self._recent_ticks: Deque[Tuple[float, str, float]] = deque(maxlen=500)

    # ── Volume ────────────────────────────────────────────────────────────────

    def add_tick(self, price: float, qty: float, side: str, ts: float) -> None:
        self._vol_window.append(qty)
        # Round price to 6 sig figs to normalise minor float drift
        rounded = round(price, 6)
        self._recent_ticks.append((rounded, side, ts))

    @property
    def rolling_avg_volume(self) -> float:
        if not self._vol_window:
            return 0.0
        return float(np.mean(self._vol_window))

    def directional_volume(self, direction: Direction, lookback_n: int = 5) -> float:
        """Sum of buy (for LONG) or sell (for SHORT) volumes in last N ticks."""
        target_side = "BUY" if direction == Direction.LONG else "SELL"
        total = 0.0
        for qty in list(self._vol_window)[-lookback_n:]:
            # We track all ticks in vol_window; directional filtering is approximate
            # (we don't store side in vol_window).  Use recent_ticks for precision.
            pass
        # Precise directional volume from recent_ticks
        total = sum(
            1.0  # qty is not stored per-tick here; count as presence
            for _, s, _ in list(self._recent_ticks)[-lookback_n * 3:]
            if s.upper() == target_side
        )
        return total

    def directional_volume_qty(self, direction: Direction, lookback_n: int = 5) -> float:
        """Actual qty sum for the target side from recent ticks."""
        target_side = "BUY" if direction == Direction.LONG else "SELL"
        recent = list(self._recent_ticks)
        # Zip with vol_window is tricky; we store qty separately below
        return sum(
            q for p, s, t in recent[-lookback_n:]
            if s.upper() == target_side
            for q in [1.0]  # placeholder – replaced by _vol_with_side below
        )

    # ── Tick repetition ───────────────────────────────────────────────────────

    def tick_repetition(self, window_sec: float, direction: Direction) -> int:
        """
        Count how many times the dominant price level (top-1 by frequency)
        appears within the last `window_sec` seconds on the target side.
        Returns 0 if no dominant cluster is found.
        """
        now = time.time()
        target_side = "BUY" if direction == Direction.LONG else "SELL"
        recent = [
            price
            for price, side, ts in self._recent_ticks
            if ts >= now - window_sec and side.upper() == target_side
        ]
        if not recent:
            return 0
        counter = Counter(recent)
        return counter.most_common(1)[0][1]


class SymbolMetricsV2:
    """
    Improved per-symbol metrics that stores (price, qty, side, ts) per tick.
    """

    def __init__(self, lookback: int = 20) -> None:
        self.lookback = lookback
        # (price, qty, side, ts)
        self._ticks: Deque[Tuple[float, float, str, float]] = deque(maxlen=500)

    def add_tick(self, price: float, qty: float, side: str, ts: float) -> None:
        self._ticks.append((price, qty, side, ts))

    @property
    def rolling_avg_volume(self) -> float:
        if not self._ticks:
            return 0.0
        recent = list(self._ticks)[-self.lookback:]
        return float(np.mean([q for _, q, _, _ in recent])) if recent else 0.0

    def directional_volume(self, direction: Direction, last_n: int = 5) -> float:
        """Sum of qty for the target side in the last N ticks."""
        target = "BUY" if direction == Direction.LONG else "SELL"
        recent = list(self._ticks)[-last_n:]
        return sum(q for _, q, s, _ in recent if s.upper() == target)

    def tick_repetition(self, window_sec: float, direction: Direction) -> int:
        """
        Within window_sec, find the most repeated price (to 2 decimal places)
        on the target side – proxy for an aggressive bot hitting a level.
        """
        now = time.time()
        target = "BUY" if direction == Direction.LONG else "SELL"
        prices = [
            round(p, 2)
            for p, _, s, ts in self._ticks
            if ts >= now - window_sec and s.upper() == target
        ]
        if not prices:
            return 0
        return Counter(prices).most_common(1)[0][1]


class SignalGenerator:
    """
    Subscribes to DataFetcher callbacks and emits Signal objects
    when all three confluences are satisfied.

    Usage:
        gen = SignalGenerator(data_fetcher)
        gen.on_signal(my_async_handler)
        # DataFetcher.run() drives everything
    """

    def __init__(self, fetcher: DataFetcher) -> None:
        self._fetcher = fetcher
        self._cfg = CONFIG.signal
        self._metrics: Dict[str, SymbolMetricsV2] = {
            sym: SymbolMetricsV2(self._cfg.volume_lookback_bars)
            for sym in fetcher.symbols
        }
        self._signal_callbacks = []
        self._cooldown: Dict[str, float] = {}   # symbol → last signal ts
        self._COOLDOWN_SEC = 2.0                # minimum gap between signals per symbol
        self._diag = DiagLogger()

        # Wire DataFetcher
        fetcher.on_market_update(self._on_market_update)
        # Wire tick updates from binance feed directly
        fetcher.binance.on_trade_tick(self._on_binance_tick)

    def on_signal(self, cb) -> None:
        self._signal_callbacks.append(cb)

    # ── Tick intake ───────────────────────────────────────────────────────────

    async def _on_binance_tick(
        self, symbol: str, price: float, qty: float, side: str
    ) -> None:
        if symbol in self._metrics:
            self._metrics[symbol].add_tick(price, qty, side, time.time())

    # ── Main evaluation ───────────────────────────────────────────────────────

    async def _on_market_update(
        self, binance_ob: OrderBook, weex_ob: OrderBook
    ) -> None:
        symbol = binance_ob.symbol
        t0 = time.perf_counter()

        # Cooldown guard
        if time.time() - self._cooldown.get(symbol, 0) < self._COOLDOWN_SEC:
            return

        result = self._evaluate(symbol, binance_ob, weex_ob)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        log.debug("[Signal] %s evaluation in %.3f ms", symbol, elapsed_ms)

        if isinstance(result, Signal):
            self._cooldown[symbol] = time.time()
            log.info("[Signal] ✦ %s", result)
            for cb in self._signal_callbacks:
                await cb(result)
        else:
            log.debug("[Signal] %s – %s", symbol, result.reason)

    def _evaluate(
        self, symbol: str, binance_ob: OrderBook, weex_ob: OrderBook
    ) -> Signal | NoSignal:
        metrics = self._metrics[symbol]

        # ── Guard: stale books ────────────────────────────────────────────────
        if binance_ob.age_ms() > 500:
            return NoSignal(symbol, f"Binance book stale ({binance_ob.age_ms():.0f}ms)")
        if weex_ob.age_ms() > 1000:
            return NoSignal(symbol, f"WEEX book stale ({weex_ob.age_ms():.0f}ms)")

        binance_mid = binance_ob.mid_price
        weex_mid = weex_ob.mid_price
        if binance_mid is None or weex_mid is None:
            return NoSignal(symbol, "Missing mid price")

        # ── Condition 1: Price Latency ─────────────────────────────────────────
        spread_pct = (binance_mid - weex_mid) / weex_mid * 100.0
        abs_spread = abs(spread_pct)
        c1_pass = abs_spread >= self._cfg.latency_threshold_pct

        # Direction is determined by the spread sign regardless of c1
        direction = Direction.LONG if spread_pct >= 0 else Direction.SHORT
        latency_score = min(abs_spread / (self._cfg.latency_threshold_pct * 3), 1.0) if c1_pass else 0.0

        # ── Condition 2: Volume Injection ─────────────────────────────────────
        avg_vol = metrics.rolling_avg_volume
        if avg_vol == 0:
            # No volume history yet – log and bail early (nothing useful to record)
            return NoSignal(symbol, "Insufficient volume history")

        dir_vol = metrics.directional_volume(direction, last_n=5)
        volume_ratio = dir_vol / avg_vol
        c2_pass = volume_ratio >= self._cfg.volume_spike_multiplier
        vol_score = min(volume_ratio / (self._cfg.volume_spike_multiplier * 2), 1.0) if c2_pass else 0.0

        # ── Condition 3: Micro-structure / Bot Detection ───────────────────────
        tick_count = metrics.tick_repetition(self._cfg.tick_repetition_window, direction)
        c3_pass = tick_count >= self._cfg.tick_repetition_min_count
        tick_score = min(tick_count / (self._cfg.tick_repetition_min_count * 2), 1.0) if c3_pass else 0.0

        # ── Confidence (only meaningful when all pass) ─────────────────────────
        all_pass = c1_pass and c2_pass and c3_pass
        confidence = float(np.cbrt(latency_score * vol_score * tick_score)) if all_pass else None

        # ── Always log raw metrics for diagnostics ────────────────────────────
        self._diag.record(
            symbol=symbol,
            direction=direction.value,
            spread_pct=spread_pct,
            c1_pass=c1_pass,
            volume_ratio=volume_ratio,
            c2_pass=c2_pass,
            tick_count=tick_count,
            c3_pass=c3_pass,
            confidence=confidence,
            binance_mid=binance_mid,
            weex_mid=weex_mid,
        )

        # ── Structured INFO log whenever a condition is partially met ──────────
        if c1_pass or c2_pass or c3_pass:
            log.info(
                "[Diag] %s | spread=%.4f%%(%s) vol=%.2fx(%s) ticks=%d(%s) | all=%s conf=%s",
                symbol,
                abs_spread, "✓" if c1_pass else "✗",
                volume_ratio, "✓" if c2_pass else "✗",
                tick_count, "✓" if c3_pass else "✗",
                "✓" if all_pass else "✗",
                f"{confidence:.3f}" if confidence is not None else "—",
            )

        if not all_pass:
            reasons = []
            if not c1_pass:
                reasons.append(f"spread {abs_spread:.4f}%<{self._cfg.latency_threshold_pct}%")
            if not c2_pass:
                reasons.append(f"vol {volume_ratio:.2f}x<{self._cfg.volume_spike_multiplier}x")
            if not c3_pass:
                reasons.append(f"ticks {tick_count}<{self._cfg.tick_repetition_min_count}")
            return NoSignal(symbol, "; ".join(reasons))

        return Signal(
            symbol=symbol,
            direction=direction,
            signal_type=SignalType.LATENCY_ARB,
            binance_price=binance_mid,
            weex_price=weex_mid,
            spread_pct=spread_pct,
            volume_ratio=volume_ratio,
            tick_count=tick_count,
            confidence=confidence,
        )
