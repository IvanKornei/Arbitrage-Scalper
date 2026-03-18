"""
core/risk_manager.py – Position sizing and real-time stop management.

Responsibilities:
  1. compute_position_size()  – Kelly-lite risk-based sizing.
  2. initial_stops()          – Compute initial SL and optional TP.
  3. update_stops()           – Breakeven and trailing stop logic.
  4. should_emergency_exit()  – Close if the latency window has closed.

Stop progression state machine:
  OPEN  ──(profit >= BE_TRIGGER)──►  BREAKEVEN  ──(new high/low)──►  TRAILING
  Any state  ──(spread collapses or time expired)──►  EMERGENCY EXIT
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from config import CONFIG
from models.order_book import OrderBook
from models.signal import Direction, Signal
from models.trade import Trade, TradeState
from utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class StopUpdate:
    """Return value of update_stops() – describes what action to take."""
    new_stop_loss: Optional[float]
    new_state: TradeState
    reason: str
    should_close: bool = False


class RiskManager:
    """
    Pure-logic class: no I/O, no asyncio.
    All methods are synchronous for minimum latency on the hot path.
    """

    def __init__(self) -> None:
        self._cfg = CONFIG.risk

    # ── Position sizing ───────────────────────────────────────────────────────

    def compute_position_size(
        self,
        entry_price: float,
        stop_loss: float,
        account_balance: Optional[float] = None,
    ) -> float:
        """
        Risk-based position size (base asset qty).

        Formula:
            risk_usdt   = balance × risk_pct / 100
            stop_dist   = abs(entry - stop_loss) / entry   (as a fraction)
            notional    = risk_usdt / stop_dist
            qty         = notional / entry_price           (before leverage)

        Leverage amplifies the notional we control, so the margin required is
            margin = qty × entry_price / leverage

        We size so the *stop distance in USDT* equals risk_usdt regardless of
        leverage – leverage just reduces required margin, not the risk amount.
        """
        balance = account_balance or self._cfg.account_balance
        risk_usdt = balance * self._cfg.risk_per_trade_pct / 100.0

        stop_dist_pct = abs(entry_price - stop_loss) / entry_price
        if stop_dist_pct == 0:
            log.warning("[Risk] Stop distance is zero; defaulting to minimum size")
            return 0.001

        # Notional value of the trade (full face value)
        notional_usdt = risk_usdt / stop_dist_pct

        # Convert to base asset quantity
        qty = notional_usdt / entry_price

        log.debug(
            "[Risk] Size calc: balance=%.2f risk_usdt=%.4f stop_dist=%.4f%% "
            "notional=%.2f qty=%.6f",
            balance,
            risk_usdt,
            stop_dist_pct * 100,
            notional_usdt,
            qty,
        )
        return round(qty, 6)

    # ── Initial stops ─────────────────────────────────────────────────────────

    def initial_stops(
        self,
        direction: Direction,
        entry_price: float,
        trailing_pct: Optional[float] = None,
    ) -> tuple[float, float]:
        """
        Returns (stop_loss, trailing_stop_initial).

        Stop loss is placed at TRAILING_STOP_PCT behind the entry, acting
        as the initial hard stop before breakeven is triggered.
        """
        pct = (trailing_pct or self._cfg.trailing_stop_pct) / 100.0
        if direction == Direction.LONG:
            stop_loss = entry_price * (1.0 - pct)
            trailing = stop_loss           # same level at start
        else:
            stop_loss = entry_price * (1.0 + pct)
            trailing = stop_loss

        log.debug(
            "[Risk] Initial stops for %s entry=%.6f SL=%.6f trailing=%.6f",
            direction.value,
            entry_price,
            stop_loss,
            trailing,
        )
        return stop_loss, trailing

    # ── Stop update (called on every price tick) ──────────────────────────────

    def update_stops(self, trade: Trade, current_price: float) -> StopUpdate:
        """
        Advance the stop state machine.  Returns a StopUpdate describing
        what changed.  The caller (ExecutionEngine) decides whether to amend
        an order on the exchange.

        State transitions:
          OPEN       → check if profit ≥ BREAKEVEN_TRIGGER → BREAKEVEN
          BREAKEVEN  → check if new extreme → TRAILING
          TRAILING   → keep ratcheting the stop up/down
        """
        if trade.state in (TradeState.CLOSED, TradeState.ERROR):
            return StopUpdate(None, trade.state, "Trade already closed")

        unrealised_pct = trade.unrealised_pct(current_price)
        direction = trade.direction

        # ── Update highest/lowest seen price ──────────────────────────────────
        if direction == Direction.LONG:
            if trade.highest_price is None or current_price > trade.highest_price:
                trade.highest_price = current_price
        else:
            if trade.lowest_price is None or current_price < trade.lowest_price:
                trade.lowest_price = current_price

        # ── OPEN → BREAKEVEN ──────────────────────────────────────────────────
        if trade.state == TradeState.OPEN:
            if unrealised_pct >= self._cfg.breakeven_trigger_pct:
                new_sl = trade.entry_price
                log.info(
                    "[Risk] %s BREAKEVEN triggered at %.4f%% profit | "
                    "SL moved to entry %.6f",
                    trade.symbol,
                    unrealised_pct,
                    new_sl,
                )
                return StopUpdate(
                    new_stop_loss=new_sl,
                    new_state=TradeState.BREAKEVEN,
                    reason=f"Breakeven triggered ({unrealised_pct:.4f}%)",
                )

        # ── BREAKEVEN → TRAILING (and ongoing TRAILING updates) ───────────────
        if trade.state in (TradeState.BREAKEVEN, TradeState.TRAILING):
            trail_pct = self._cfg.trailing_stop_pct / 100.0

            if direction == Direction.LONG:
                peak = trade.highest_price or current_price
                new_trail = peak * (1.0 - trail_pct)
                # Only ratchet UP, never down
                if trade.trailing_stop is None or new_trail > trade.trailing_stop:
                    log.debug(
                        "[Risk] %s LONG trailing stop ratcheted: %.6f → %.6f (peak=%.6f)",
                        trade.symbol,
                        trade.trailing_stop or 0,
                        new_trail,
                        peak,
                    )
                    return StopUpdate(
                        new_stop_loss=new_trail,
                        new_state=TradeState.TRAILING,
                        reason=f"Trailing stop updated (peak={peak:.6f})",
                    )
            else:  # SHORT
                trough = trade.lowest_price or current_price
                new_trail = trough * (1.0 + trail_pct)
                # Only ratchet DOWN, never up
                if trade.trailing_stop is None or new_trail < trade.trailing_stop:
                    log.debug(
                        "[Risk] %s SHORT trailing stop ratcheted: %.6f → %.6f (trough=%.6f)",
                        trade.symbol,
                        trade.trailing_stop or 0,
                        new_trail,
                        trough,
                    )
                    return StopUpdate(
                        new_stop_loss=new_trail,
                        new_state=TradeState.TRAILING,
                        reason=f"Trailing stop updated (trough={trough:.6f})",
                    )

        return StopUpdate(
            new_stop_loss=None,
            new_state=trade.state,
            reason="No stop change",
        )

    # ── Stop hit detection ────────────────────────────────────────────────────

    def is_stop_hit(self, trade: Trade, current_price: float) -> bool:
        """Return True if current price has crossed the active stop level."""
        sl = trade.trailing_stop or trade.stop_loss
        if sl is None:
            return False
        if trade.direction == Direction.LONG:
            hit = current_price <= sl
        else:
            hit = current_price >= sl
        if hit:
            log.info(
                "[Risk] %s stop hit: price=%.6f SL=%.6f state=%s",
                trade.symbol,
                current_price,
                sl,
                trade.state.value,
            )
        return hit

    # ── Emergency exit conditions ─────────────────────────────────────────────

    def should_emergency_exit(
        self,
        trade: Trade,
        binance_ob: OrderBook,
        weex_ob: OrderBook,
    ) -> tuple[bool, str]:
        """
        Return (True, reason) if the trade should be force-closed.

        Triggers:
          1. Max trade duration exceeded.
          2. Latency window closed: Binance and WEEX prices have converged
             (spread < EMERGENCY_EXIT_SPREAD_PCT) and we're not in profit.
        """
        # 1. Time-based hard stop
        if trade.duration_sec() >= self._cfg.max_trade_duration_sec:
            return True, f"Max duration {self._cfg.max_trade_duration_sec}s exceeded"

        # 2. Spread collapsed (latency window closed)
        b_mid = binance_ob.mid_price
        w_mid = weex_ob.mid_price
        if b_mid and w_mid:
            spread_pct = abs(b_mid - w_mid) / w_mid * 100.0
            if spread_pct < self._cfg.emergency_exit_spread_pct:
                # Only emergency exit if we haven't reached breakeven yet
                if trade.state == TradeState.OPEN:
                    return (
                        True,
                        f"Latency window closed (spread={spread_pct:.4f}%)",
                    )

        return False, ""

    # ── Max positions guard ───────────────────────────────────────────────────

    def can_open_position(self, current_open_count: int) -> bool:
        allowed = current_open_count < self._cfg.max_open_positions
        if not allowed:
            log.debug(
                "[Risk] Max positions reached (%d/%d)",
                current_open_count,
                self._cfg.max_open_positions,
            )
        return allowed
