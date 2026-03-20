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
from typing import List, Optional, Tuple

from mirofish.predictor import MiroFishPredictor
from polymarket.client import PolyMarket, PolymarketClient
from polymarket.kelly import KellySizer
from polymarket.market_scanner import MarketScanner, ScanConfig
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

    # Kelly sizing
    kelly_fraction: float     = 0.25
    max_bet_fraction: float   = 0.05          # max 5% bankroll per bet
    min_bet_usdc: float       = 1.0           # minimum bet size
    max_bet_usdc: float       = float("inf")  # hard cap per bet in USDC

    # Scan timing
    scan_interval_sec: float  = 1800.0        # scan every 30 minutes
    scan_pages: int           = 100           # pages × 100 = markets fetched
    price_refresh: bool       = True          # refresh live price before decision

    # Execution control
    max_bets_per_cycle: int   = 20            # max bets placed per cycle (top-N by edge)

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
        self._kelly   = KellySizer(
            fraction_kelly=config.kelly_fraction,
            max_fraction_of_bankroll=config.max_bet_fraction,
            min_bet_usdc=config.min_bet_usdc,
            max_bet_usdc=config.max_bet_usdc,
        )
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
        for c in candidates:
            if placed >= bet_limit:
                break
            if self._stop_event.is_set():
                break
            if self._orders.has_position(c.market.condition_id):
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

        log.info("[Agent] Phase 2 complete – %d bets placed", placed)

    async def _evaluate_market(
        self,
        predictor: MiroFishPredictor,
        market: PolyMarket,
        bankroll: float,
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

        # Kelly sizing (only to determine amount; bet not placed yet)
        bet_size = self._kelly.size(bankroll, best_our_prob, best_mkt_price)
        if bet_size <= 0:
            log.info("[Agent] Kelly returned 0 size – skip")
            return None

        return _Candidate(
            market    = market,
            direction = best_direction,
            edge      = best_edge,
            our_prob  = best_our_prob,
            mkt_price = best_mkt_price,
            bet_size  = bet_size,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _build_context(market: PolyMarket) -> str:
        """Build seed text for MiroFish from market metadata."""
        parts = [
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
