"""
polymarket/market_scanner.py – Filters and ranks Polymarket markets for trading.

Criteria applied (all configurable via PolyConfig):
  1. Active & not closed
  2. Has both YES and NO token IDs
  3. Price neither 0 nor 1 (resolved markets excluded)
  4. Volume >= min_volume_24h (ensures liquidity)
  5. Liquidity >= min_liquidity
  6. Not in excluded categories
  7. Ranked by absolute edge potential (price closest to 0.5 = most uncertain = best for AI)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Set

from polymarket.client import PolyMarket, PolymarketClient
from utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class ScanConfig:
    min_volume_24h: float = 500.0           # USD – filter out low-activity markets
    min_liquidity: float = 200.0            # USD – filter out illiquid markets
    max_markets: int = 20                   # markets to return per scan
    excluded_categories: Set[str] = field(
        default_factory=lambda: set()       # e.g. {"sports"} to exclude
    )
    price_deadzone_low: float = 0.02        # skip if YES price < 2% (near-resolved)
    price_deadzone_high: float = 0.98       # skip if YES price > 98%


class MarketScanner:
    """Scans Polymarket for tradeable opportunities and returns ranked list."""

    def __init__(self, client: PolymarketClient, config: ScanConfig) -> None:
        self._client = client
        self._cfg = config

    async def scan(self, pages: int = 3) -> List[PolyMarket]:
        """
        Fetch and filter markets.

        Args:
            pages: Number of 100-market pages to fetch.
        Returns:
            Filtered and ranked list of PolyMarket objects.
        """
        all_markets: List[PolyMarket] = []
        for page in range(pages):
            batch = await self._client.get_markets(
                limit=100, offset=page * 100, active=True
            )
            all_markets.extend(batch)
            log.debug("[Scanner] Page %d: fetched %d markets", page + 1, len(batch))
            if len(batch) < 100:
                break   # no more pages

        log.info("[Scanner] Total fetched: %d markets", len(all_markets))
        filtered = self._filter(all_markets)
        ranked   = self._rank(filtered)
        result   = ranked[:self._cfg.max_markets]
        log.info(
            "[Scanner] After filter+rank: %d markets selected", len(result)
        )
        return result

    def _filter(self, markets: List[PolyMarket]) -> List[PolyMarket]:
        cfg = self._cfg
        out: List[PolyMarket] = []
        for m in markets:
            # Must be active and not closed
            if not m.active or m.closed:
                continue
            # Must have token IDs for trading
            if not m.yes_token_id or not m.no_token_id:
                continue
            # Price must be in tradeable range
            p = m.yes_price
            if p < cfg.price_deadzone_low or p > cfg.price_deadzone_high:
                continue
            # Volume & liquidity thresholds
            if m.volume_24h < cfg.min_volume_24h:
                continue
            if m.liquidity < cfg.min_liquidity:
                continue
            # Category filter
            cat = m.category.lower()
            if cat in {c.lower() for c in cfg.excluded_categories}:
                continue
            out.append(m)
        return out

    @staticmethod
    def _rank(markets: List[PolyMarket]) -> List[PolyMarket]:
        """
        Rank by:
          1. Uncertainty (YES price closest to 0.5 → most uncertain)
          2. Volume (higher volume = better liquidity for execution)
        Combined score: uncertainty_score * 0.6 + norm_volume * 0.4
        """
        if not markets:
            return []

        max_vol = max(m.volume_24h for m in markets) or 1.0

        def score(m: PolyMarket) -> float:
            uncertainty = 1.0 - abs(m.yes_price - 0.5) * 2  # 1 at 0.5, 0 at extremes
            norm_vol    = m.volume_24h / max_vol
            return uncertainty * 0.6 + norm_vol * 0.4

        return sorted(markets, key=score, reverse=True)

    async def refresh_price(self, market: PolyMarket) -> PolyMarket:
        """Fetch fresh order-book price for a market and return updated market."""
        yes_ask = await self._client.get_best_ask(market.yes_token_id)
        no_ask  = await self._client.get_best_ask(market.no_token_id)
        # Build a new dataclass with updated prices
        return PolyMarket(
            condition_id  = market.condition_id,
            question      = market.question,
            description   = market.description,
            end_date_iso  = market.end_date_iso,
            yes_token_id  = market.yes_token_id,
            no_token_id   = market.no_token_id,
            yes_price     = yes_ask if yes_ask > 0 else market.yes_price,
            no_price      = no_ask  if no_ask  > 0 else market.no_price,
            volume_24h    = market.volume_24h,
            liquidity     = market.liquidity,
            category      = market.category,
            active        = market.active,
            closed        = market.closed,
            tags          = market.tags,
        )
