"""
core/execution_engine.py – Manages the full order lifecycle on WEEX.

Flow:
  1. Receives a Signal from SignalGenerator.
  2. Asks RiskManager for position size and initial stops.
  3. Sends a market order to WEEX and records the Trade.
  4. Starts a per-trade monitoring loop that:
       a. Polls WEEX for the current mark price via WS order book.
       b. Calls RiskManager.update_stops() on each tick.
       c. Amends the SL order on WEEX when the stop ratchets.
       d. Calls RiskManager.should_emergency_exit() for time/spread triggers.
  5. Exits the trade (market order, reduce-only) when any exit condition fires.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional

from config import CONFIG
from core.data_fetcher import DataFetcher
from core.risk_manager import RiskManager
from exchanges.weex_futures import WeexExecutionClient
from models.order_book import OrderBook
from models.signal import Direction, Signal
from models.trade import Trade, TradeState
from utils.logger import get_logger

log = get_logger(__name__)


class ExecutionEngine:
    """
    Receives Signal objects and manages the full position lifecycle.

    One asyncio.Task is created per open trade; tasks are cleaned up on exit.
    """

    def __init__(self, fetcher: DataFetcher, risk: RiskManager) -> None:
        self._fetcher = fetcher
        self._risk = risk
        self._client = WeexExecutionClient()
        self._cfg = CONFIG.risk

        # Active trades keyed by symbol (one position per symbol max)
        self._trades: Dict[str, Trade] = {}
        self._monitor_tasks: Dict[str, asyncio.Task] = {}

        self._account_balance: float = self._cfg.account_balance

    # ── Public entry point ────────────────────────────────────────────────────

    async def on_signal(self, signal: Signal) -> None:
        """
        Called by SignalGenerator for every valid signal.
        Enters a trade unless one is already open for this symbol.
        """
        symbol = signal.symbol
        t0 = time.perf_counter()

        # Guard: position already open
        if symbol in self._trades:
            log.debug(
                "[Exec] %s – signal ignored (position already open)", symbol
            )
            return

        # Guard: max positions
        if not self._risk.can_open_position(len(self._trades)):
            return

        log.info("[Exec] Processing signal: %s", signal)

        weex_ob: Optional[OrderBook] = self._fetcher.state[symbol].weex_ob
        if weex_ob is None:
            log.warning("[Exec] %s – no WEEX order book yet", symbol)
            return

        # Entry price = WEEX best ask (LONG) or best bid (SHORT)
        if signal.direction == Direction.LONG:
            entry_price = weex_ob.best_ask
        else:
            entry_price = weex_ob.best_bid

        if entry_price is None:
            log.warning("[Exec] %s – WEEX book empty", symbol)
            return

        # Compute initial stops
        sl, trailing = self._risk.initial_stops(signal.direction, entry_price)

        # Compute position size
        qty = self._risk.compute_position_size(
            entry_price, sl, self._account_balance
        )
        if qty <= 0:
            log.error("[Exec] %s – invalid position size %.6f", symbol, qty)
            return

        # ── Set leverage (idempotent) ─────────────────────────────────────────
        try:
            await self._client.set_leverage(symbol, self._cfg.leverage)
        except Exception as exc:
            log.warning("[Exec] Set leverage failed (non-fatal): %s", exc)

        # ── Send market entry order ───────────────────────────────────────────
        order_side = "buy" if signal.direction == Direction.LONG else "sell"
        try:
            order_result = await self._client.place_market_order(
                symbol=symbol,
                side=order_side,
                quantity=qty,
            )
        except Exception as exc:
            log.error("[Exec] %s – market order failed: %s", symbol, exc, exc_info=True)
            return

        elapsed_entry_ms = (time.perf_counter() - t0) * 1000
        log.info(
            "[Exec] ✦ ENTRY %s %s qty=%.6f entry≈%.6f SL=%.6f | %.1f ms to order",
            signal.direction.value,
            symbol,
            qty,
            entry_price,
            sl,
            elapsed_entry_ms,
        )

        # ── Create Trade record ───────────────────────────────────────────────
        trade = Trade(
            symbol=symbol,
            direction=signal.direction,
            entry_price=entry_price,
            quantity=qty,
            leverage=self._cfg.leverage,
            entry_order_id=order_result.order_id,
            state=TradeState.OPEN,
            stop_loss=sl,
            trailing_stop=trailing,
            highest_price=entry_price if signal.direction == Direction.LONG else None,
            lowest_price=entry_price if signal.direction == Direction.SHORT else None,
        )
        self._trades[symbol] = trade
        log.info("[Exec] Trade created: %s", trade)

        # ── Start monitoring task ─────────────────────────────────────────────
        task = asyncio.create_task(
            self._monitor_trade(trade),
            name=f"monitor-{symbol}-{trade.id}",
        )
        self._monitor_tasks[symbol] = task

    # ── Per-trade monitoring loop ─────────────────────────────────────────────

    async def _monitor_trade(self, trade: Trade) -> None:
        symbol = trade.symbol
        log.debug("[Exec] Monitor started for %s", trade)

        try:
            while trade.state not in (TradeState.CLOSED, TradeState.ERROR):
                await asyncio.sleep(self._cfg.monitor_interval_sec)

                # Get current prices from shared state
                state = self._fetcher.state.get(symbol)
                if state is None:
                    continue

                weex_ob = state.weex_ob
                binance_ob = state.binance_ob
                if weex_ob is None or weex_ob.mid_price is None:
                    continue

                current_price = weex_ob.mid_price

                # ── Check emergency exit ─────────────────────────────────────
                if binance_ob is not None:
                    emergency, reason = self._risk.should_emergency_exit(
                        trade, binance_ob, weex_ob
                    )
                    if emergency:
                        log.warning(
                            "[Exec] %s EMERGENCY EXIT: %s", symbol, reason
                        )
                        await self._exit_trade(trade, reason)
                        return

                # ── Check stop hit ────────────────────────────────────────────
                if self._risk.is_stop_hit(trade, current_price):
                    await self._exit_trade(
                        trade, f"Stop hit at {current_price:.6f}"
                    )
                    return

                # ── Update stops ─────────────────────────────────────────────
                stop_update = self._risk.update_stops(trade, current_price)
                if stop_update.new_stop_loss is not None:
                    prev_sl = trade.stop_loss or trade.trailing_stop
                    trade.stop_loss = stop_update.new_stop_loss
                    trade.trailing_stop = stop_update.new_stop_loss
                    trade.state = stop_update.new_state
                    log.info(
                        "[Exec] %s stop updated %.6f → %.6f (%s)",
                        symbol,
                        prev_sl or 0,
                        stop_update.new_stop_loss,
                        stop_update.reason,
                    )

        except asyncio.CancelledError:
            log.debug("[Exec] Monitor task cancelled for %s", symbol)
        except Exception as exc:
            log.error(
                "[Exec] Monitor error for %s: %s", symbol, exc, exc_info=True
            )
            trade.state = TradeState.ERROR
        finally:
            self._trades.pop(symbol, None)
            self._monitor_tasks.pop(symbol, None)

    # ── Exit trade ────────────────────────────────────────────────────────────

    async def _exit_trade(self, trade: Trade, reason: str) -> None:
        symbol = trade.symbol
        exit_side = "sell" if trade.direction == Direction.LONG else "buy"
        t0 = time.perf_counter()

        try:
            result = await self._client.place_market_order(
                symbol=symbol,
                side=exit_side,
                quantity=trade.quantity,
                reduce_only=True,
            )
            trade.exit_order_id = result.order_id
        except Exception as exc:
            log.error("[Exec] EXIT order failed for %s: %s", symbol, exc, exc_info=True)
            trade.state = TradeState.ERROR
            return

        elapsed_ms = (time.perf_counter() - t0) * 1000
        trade.closed_at = time.time()
        trade.state = TradeState.CLOSED
        trade.exit_reason = reason

        # Estimate P&L (rough; real fill price may differ slightly)
        weex_ob = self._fetcher.state[symbol].weex_ob
        if weex_ob and weex_ob.mid_price:
            trade.exit_price = weex_ob.mid_price
            trade.realised_pnl = trade.unrealised_pnl(weex_ob.mid_price)

        log.info(
            "[Exec] ✦ EXIT %s %s reason='%s' "
            "dur=%.1fs pnl≈%.4f USDT | %.1f ms",
            symbol,
            trade.direction.value,
            reason,
            trade.duration_sec(),
            trade.realised_pnl or 0.0,
            elapsed_ms,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def close_all(self) -> None:
        """Emergency close all open positions (e.g., on shutdown)."""
        tasks = list(self._trades.items())
        for symbol, trade in tasks:
            await self._exit_trade(trade, "Bot shutdown")

        # Cancel monitor tasks
        for task in self._monitor_tasks.values():
            task.cancel()
        await asyncio.gather(*self._monitor_tasks.values(), return_exceptions=True)
        self._monitor_tasks.clear()
        self._trades.clear()

        await self._client.close()
        log.info("[Exec] All positions closed and engine stopped.")

    @property
    def open_trade_count(self) -> int:
        return len(self._trades)

    @property
    def open_trades(self) -> List[Trade]:
        return list(self._trades.values())
