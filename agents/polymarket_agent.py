"""
agents/polymarket_agent.py – Main AI trading agent for Polymarket.

Two-phase loop (every scan_interval_sec):

  Phase 1 – Analysis (MiroFish processes all candidates):
    1. Scan Polymarket for up to POLY_SCAN_PAGES × 100 markets
    2. Filter & rank by liquidity (top POLY_MAX_SCAN_MARKETS passed to MiroFish)
    3. For each candidate:
         a. Refresh live price
         b. Build context (question + description + market data)
         c. Ask MiroFish for YES probability
         d. Compute edge = our_prob - market_price
         e. If edge > threshold: record as a candidate bet

  Phase 2 – Execution (place only the best bets):
    4. Sort all candidates by edge (descending)
    5. Place the top POLY_MAX_BETS_PER_CYCLE bets (default 20), capped at
       max_open_positions remaining slots.

This ensures we never bet on the first market MiroFish evaluates if a
better opportunity appears later in the list.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from mirofish.predictor import MiroFishPredictor
from polymarket.calibration import CalibrationLog
from polymarket.client import PolyMarket, PolymarketClient
from polymarket.kelly import KellySizer
from polymarket.market_scanner import MarketScanner, ScanConfig, _parse_end_date
from polymarket.order_manager import OrderManager
from utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class AgentConfig:
    # MiroFish
    mirofish_url: str         = "http://localhost:5001"
    mirofish_rounds: int      = 10            # simulation rounds per market

    # Edge thresholds
    min_edge_pct: float       = 0.05          # 5% minimum edge to bet
    max_open_positions: int   = 10

    # Bet size – fixed, non-configurable
    # Each bet is always exactly $1.00 USDC. This cannot be changed via config.

    # Scan timing
    scan_interval_sec: float  = 1800.0        # scan every 30 minutes
    scan_pages: int           = 100           # pages × 100 = markets fetched
    price_refresh: bool       = True          # refresh live price before decision

    # Execution control
    max_bets_per_cycle: int    = 20           # max bets placed per cycle (top-N by edge)
    max_bets_per_event: int    = 1            # max bets sharing the same event tag
    max_bets_per_category: int = 3            # max bets in the same category per cycle

    # Fast-lane: near-expiry markets (skip Gemini, bet on leading side)
    fast_lane_minutes: int     = 5            # minutes-to-end threshold for fast lane
    fast_lane_min_prob: float  = 0.80         # minimum market confidence to enter fast lane
    fast_lane_min_edge: float  = 0.02         # lower edge threshold used in fast lane

    # Market filter
    scan_config: ScanConfig   = field(default_factory=ScanConfig)

    # Safety
    dry_run: bool             = True          # start in dry-run mode by default


@dataclass
class _Candidate:
    """Holds MiroFish evaluation result before bet placement."""
    market: PolyMarket
    direction: str      # "YES" | "NO"
    edge: float
    our_prob: float
    mkt_price: float
    bet_size: float


class PolymarketAgent:
    """
    Autonomous AI trading agent:
      MiroFish predictions → collect all results → Kelly sizing → top-N bets.
    """

    def __init__(
        self,
        poly_client: PolymarketClient,
        config: AgentConfig,
    ) -> None:
        self._poly    = poly_client
        self._cfg     = config
        self._scanner = MarketScanner(poly_client, config.scan_config)
        self._orders  = OrderManager(poly_client, dry_run=config.dry_run)
        self._kelly   = KellySizer()
        self._cal     = CalibrationLog()
        self._stop_event = asyncio.Event()
        self._cycle      = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        log.info("=" * 70)
        log.info("  Polymarket AI Agent | dry_run=%s | min_edge=%.0f%%",
                 self._cfg.dry_run, self._cfg.min_edge_pct * 100)
        log.info("  MiroFish: %s | rounds=%d",
                 self._cfg.mirofish_url, self._cfg.mirofish_rounds)
        log.info("  Scan: %d pages | top-%d markets analysed | top-%d bets placed",
                 self._cfg.scan_pages,
                 self._cfg.scan_config.max_markets,
                 self._cfg.max_bets_per_cycle)
        log.info("=" * 70)

        await self._check_mirofish()

        while not self._stop_event.is_set():
            self._cycle += 1
            log.info("[Agent] ── Cycle %d ──────────────────────────────────", self._cycle)
            t0 = time.monotonic()
            try:
                await self._scan_and_trade()
            except Exception as exc:
                log.error("[Agent] Cycle %d error: %s", self._cycle, exc)

            log.info("[Agent] Cycle %d done in %.1fs", self._cycle, time.monotonic() - t0)
            log.info("%s", self._orders.summary())

            cal = self._cal.summary()
            if cal.get("resolved", 0) > 0:
                log.info(
                    "[Calibration] Brier=%.4f | Market Brier=%.4f | %s (%d resolved)",
                    cal["brier_score"], cal["market_brier"],
                    cal["verdict"], cal["resolved"],
                )

            await self._sleep_interruptible(self._cfg.scan_interval_sec)

        log.info("[Agent] Stopped.")

    # ── Core logic ────────────────────────────────────────────────────────────

    async def _scan_and_trade(self) -> None:
        # 1. Get account balance
        bankroll = await self._poly.get_balance_usdc()
        if bankroll <= 0:
            log.warning("[Agent] Zero USDC balance – skipping cycle (dry_run=%s)",
                        self._cfg.dry_run)
            if not self._cfg.dry_run:
                return
            bankroll = 100.0  # synthetic bankroll for dry run testing

        log.info("[Agent] Bankroll: $%.2f USDC | open positions: %d",
                 bankroll, self._orders.open_count)

        # 2. Check position cap
        slots_available = self._cfg.max_open_positions - self._orders.open_count
        if slots_available <= 0:
            log.info("[Agent] Max positions reached (%d) – skipping scan",
                     self._cfg.max_open_positions)
            return

        # 3. Scan markets (up to scan_pages × 100, filtered & ranked)
        markets = await self._scanner.scan(pages=self._cfg.scan_pages)
        if not markets:
            log.warning("[Agent] No tradeable markets found")
            return

        # ── PHASE 1: MiroFish analysis ─────────────────────────────────────
        log.info("[Agent] Phase 1: analysing %d markets with MiroFish…", len(markets))
        candidates: List[_Candidate] = []

        async with MiroFishPredictor(
            base_url=self._cfg.mirofish_url,
            simulation_rounds=self._cfg.mirofish_rounds,
            cleanup_after=True,
        ) as predictor:
            for market in markets:
                if self._stop_event.is_set():
                    break
                if self._orders.has_position(market.condition_id):
                    continue

                candidate = await self._evaluate_market(predictor, market, bankroll)
                if candidate is not None:
                    candidates.append(candidate)

        if not candidates:
            log.info("[Agent] Phase 1 complete – no edge found in any market")
            return

        # ── PHASE 2: Place top-N bets by edge ─────────────────────────────
        candidates.sort(key=lambda c: c.edge, reverse=True)
        bet_limit = min(self._cfg.max_bets_per_cycle, slots_available)

        log.info(
            "[Agent] Phase 2: %d candidates with edge, placing top-%d bets",
            len(candidates), bet_limit,
        )

        placed = 0
        # Track bets per event-tag and per category to avoid over-concentration
        tag_counts: dict = {}   # event_key → int
        cat_counts: dict = {}   # category  → int

        for c in candidates:
            if placed >= bet_limit:
                break
            if self._stop_event.is_set():
                break
            if self._orders.has_position(c.market.condition_id):
                continue

            # ── Per-event deduplication ────────────────────────────────────
            # Event key: use tags if present, else the first segment of the
            # question before ":" (e.g. "Mavericks vs. Bucks" or "Max Christie")
            event_keys = (
                c.market.tags
                if c.market.tags
                else [c.market.question.split(":")[0].strip()[:40]]
            )
            if any(tag_counts.get(k, 0) >= self._cfg.max_bets_per_event
                   for k in event_keys):
                log.info(
                    "[Agent] Skip (event cap %d): %s",
                    self._cfg.max_bets_per_event, c.market.question[:60],
                )
                continue

            # ── Per-category cap ───────────────────────────────────────────
            cat = c.market.category.lower() or "unknown"
            if cat_counts.get(cat, 0) >= self._cfg.max_bets_per_category:
                log.info(
                    "[Agent] Skip (category cap %d for '%s'): %s",
                    self._cfg.max_bets_per_category, cat, c.market.question[:60],
                )
                continue

            log.info(
                "[Agent] SIGNAL %s | edge=%.2f%% | bet=$%.2f | dir=%s",
                c.market.question[:50],
                c.edge * 100, c.bet_size, c.direction,
            )

            result = await self._orders.place_bet(
                market       = c.market,
                direction    = c.direction,
                size_usdc    = c.bet_size,
                price        = c.mkt_price,
                market_order = True,
            )
            if result is not None:
                placed += 1
                for k in event_keys:
                    tag_counts[k] = tag_counts.get(k, 0) + 1
                cat_counts[cat] = cat_counts.get(cat, 0) + 1

        log.info("[Agent] Phase 2 complete – %d bets placed", placed)

    async def _evaluate_market(
        self,
        predictor: MiroFishPredictor,
        market: PolyMarket,
        bankroll: float = 100.0,
    ) -> Optional[_Candidate]:
        """
        Run MiroFish on one market.
        Returns a _Candidate if edge exceeds threshold, else None.
        Does NOT place any orders.
        """
        log.info("[Agent] Evaluating: %s", market.question[:70])

        # Refresh live price
        if self._cfg.price_refresh:
            market = await self._scanner.refresh_price(market)

        market_yes_price = market.yes_price
        if market_yes_price <= 0:
            log.debug("[Agent] No YES ask price for %s – skip", market.condition_id)
            return None

        # Re-validate price after live refresh (stale price may have passed filter)
        sc = self._scanner._cfg
        if market_yes_price < sc.price_deadzone_low or market_yes_price > sc.price_deadzone_high:
            log.info(
                "[Agent] Skip after refresh – price %.4f out of [%.2f, %.2f]: %s",
                market_yes_price, sc.price_deadzone_low, sc.price_deadzone_high,
                market.question[:60],
            )
            return None

        # ── Fast lane: near-expiry markets ────────────────────────────────
        # If the market expires within fast_lane_minutes, skip Gemini entirely
        # and bet on the leading side if it shows sufficient confidence.
        if market.end_date_iso and self._cfg.fast_lane_minutes > 0:
            end_dt = _parse_end_date(market.end_date_iso)
            if end_dt is not None:
                mins_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 60
                if 0 < mins_left <= self._cfg.fast_lane_minutes:
                    return self._fast_lane_candidate(market, mins_left, bankroll)

        # Build seed context for MiroFish
        context = self._build_context(market)

        # Get MiroFish prediction
        our_yes_prob = await predictor.predict(
            question        = market.question,
            context         = context,
            resolution_date = market.end_date_iso,
            market_id       = market.condition_id,
        )

        our_no_prob     = 1.0 - our_yes_prob
        market_no_price = 1.0 - market_yes_price

        if abs(our_yes_prob - 0.5) < 0.01:
            log.warning(
                "[Agent] MiroFish returned ~50%% (uncertain/fallback?) for: %s",
                market.question[:70],
            )

        # Calculate edges for both directions
        yes_edge = self._kelly.edge(our_yes_prob, market_yes_price)
        no_edge  = self._kelly.edge(our_no_prob,  market_no_price)

        log.info(
            "[Agent] %s | market=%.3f | mirofish=%.3f | "
            "yes_edge=%+.3f no_edge=%+.3f",
            market.question[:50],
            market_yes_price, our_yes_prob,
            yes_edge, no_edge,
        )

        best_edge      = max(yes_edge, no_edge)
        best_direction = "YES" if yes_edge >= no_edge else "NO"
        best_our_prob  = our_yes_prob if best_direction == "YES" else our_no_prob
        best_mkt_price = market_yes_price if best_direction == "YES" else market_no_price

        if best_edge < self._cfg.min_edge_pct:
            log.info("[Agent] Edge %.2f%% < threshold %.2f%% – skip",
                     best_edge * 100, self._cfg.min_edge_pct * 100)
            return None

        bet_size = self._kelly.size(bankroll, best_our_prob, best_mkt_price)
        if bet_size <= 0:
            log.info("[Agent] Kelly returns 0 for %s – skip", market.question[:50])
            return None
        log.info("[Agent] Kelly size: $%.2f (bankroll=$%.2f edge=%.2f%%)",
                 bet_size, bankroll, best_edge * 100)

        # Log prediction for calibration tracking
        self._cal.log(
            market_id    = market.condition_id,
            question     = market.question,
            our_prob     = best_our_prob,
            market_price = best_mkt_price,
            direction    = best_direction,
            edge         = best_edge,
            bet_placed   = True,
            bet_size     = bet_size,
        )

        return _Candidate(
            market    = market,
            direction = best_direction,
            edge      = best_edge,
            our_prob  = best_our_prob,
            mkt_price = best_mkt_price,
            bet_size  = bet_size,
        )

    def _fast_lane_candidate(
        self, market: PolyMarket, mins_left: float, bankroll: float = 100.0
    ) -> Optional[_Candidate]:
        """
        Fast-lane path for near-expiry markets.

        Skips Gemini entirely and bets on whichever side the market already
        shows high confidence in (>= fast_lane_min_prob). Treats the market
        price itself as the probability estimate — a small fixed bonus (0.02)
        creates a positive edge so the candidate passes the filter.

        Rationale: with < fast_lane_minutes remaining, prices are usually
        anchored close to resolution. Quick capital turnover is the goal.
        """
        yes_p = market.yes_price
        no_p  = 1.0 - yes_p
        threshold = self._cfg.fast_lane_min_prob

        if yes_p >= threshold:
            direction = "YES"
            mkt_price = yes_p
        elif no_p >= threshold:
            direction = "NO"
            mkt_price = no_p
        else:
            return None  # neither side confident enough

        # Tiny fixed bonus to create positive edge (market price is our base)
        our_prob = min(mkt_price + 0.02, 0.99)
        edge     = our_prob - mkt_price  # = 0.02

        if edge < self._cfg.fast_lane_min_edge:
            return None

        log.info(
            "[Agent] FAST LANE (%.1f min left) %s | dir=%s | mkt=%.3f",
            mins_left, market.question[:55], direction, mkt_price,
        )
        bet_size = self._kelly.size(bankroll, our_prob, mkt_price)
        if bet_size <= 0:
            bet_size = self._kelly.min_bet_usdc  # fast lane: always place minimum

        return _Candidate(
            market    = market,
            direction = direction,
            edge      = edge,
            our_prob  = our_prob,
            mkt_price = mkt_price,
            bet_size  = bet_size,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _build_context(market: PolyMarket) -> str:
        """Build seed text for MiroFish from market metadata."""
        today = datetime.now(timezone.utc).strftime("%B %d, %Y")
        parts = [
            f"Today's date: {today}",
            f"Market question: {market.question}",
            "",
        ]
        if market.description:
            parts += [f"Description:\n{market.description}", ""]
        if market.end_date_iso:
            parts += [f"Resolution date: {market.end_date_iso}", ""]
        if market.category:
            parts += [f"Category: {market.category}", ""]
        if market.tags:
            parts += [f"Tags: {', '.join(market.tags)}", ""]
        parts += [
            f"Current market price (YES): {market.yes_price:.4f}  "
            f"(implied probability {market.yes_price * 100:.1f}%)",
            f"24h volume: ${market.volume_24h:,.0f} USD",
            f"Liquidity: ${market.liquidity:,.0f} USD",
        ]
        return "\n".join(parts)

    async def _sleep_interruptible(self, seconds: float) -> None:
        """Sleep for `seconds`, waking early if stop requested."""
        try:
            await asyncio.wait_for(
                self._stop_event.wait(), timeout=seconds
            )
        except asyncio.TimeoutError:
            pass

    async def _check_mirofish(self) -> None:
        """Verify MiroFish is reachable before starting the trading loop."""
        async with MiroFishPredictor(base_url=self._cfg.mirofish_url) as p:
            ok = await p.is_available()
        if ok:
            log.info("[Agent] MiroFish reachable at %s", self._cfg.mirofish_url)
        else:
            raise RuntimeError(
                f"MiroFish is not reachable at {self._cfg.mirofish_url}. "
                "Start it with: docker compose up -d"
            )
