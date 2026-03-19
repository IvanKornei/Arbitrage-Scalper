"""
polymarket/market_scanner.py – Filters and ranks Polymarket markets for trading.

Criteria applied (all configurable via PolyConfig):
  1. Active & not closed
  2. Has both YES and NO token IDs
  3. Price neither 0 nor 1 (resolved markets excluded)
  4. Volume >= min_volume_24h (ensures liquidity)
  5. Liquidity >= min_liquidity
  6. Not in excluded categories
  7. End date within max_days_to_end (optional cap)
  8. Ranked primarily by proximity of end date (soonest first),
     then by uncertainty and volume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Set

from polymarket.client import PolyMarket, PolymarketClient
from utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class ScanConfig:
    min_volume_24h: float = 0.0             # USD – minimum 24h volume (0 = disabled)
    min_liquidity: float = 0.0             # USD – minimum liquidity (0 = disabled)
    max_markets: int = 20                   # markets to return per scan
    excluded_categories: Set[str] = field(
        default_factory=lambda: set()
    )
    price_deadzone_low: float = 0.01        # skip if YES price < 1%
    price_deadzone_high: float = 0.99       # skip if YES price > 99%
    max_days_to_end: Optional[int] = None   # None = no cap


class MarketScanner:
    """Scans Polymarket for tradeable opportunities and returns ranked list."""

    def __init__(self, client: PolymarketClient, config: ScanConfig) -> None:
        self._client = client
        self._cfg = config

    async def scan(self, pages: int = 30) -> List[PolyMarket]:
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
        now = datetime.now(timezone.utc)
        out: List[PolyMarket] = []

        c_inactive = c_no_tokens = c_price = c_volume = c_liquidity = c_category = c_days = 0

        for m in markets:
            # Must be active and not closed
            if not m.active or m.closed:
                c_inactive += 1; continue
            # Must have token IDs for trading
            if not m.yes_token_id or not m.no_token_id:
                c_no_tokens += 1; continue
            # Price must be in tradeable range
            p = m.yes_price
            if p < cfg.price_deadzone_low or p > cfg.price_deadzone_high:
                c_price += 1; continue
            # Volume & liquidity thresholds
            if m.volume_24h < cfg.min_volume_24h:
                c_volume += 1; continue
            if m.liquidity < cfg.min_liquidity:
                c_liquidity += 1; continue
            # Category filter
            cat = m.category.lower()
            if cat in {c.lower() for c in cfg.excluded_categories}:
                c_category += 1; continue
            # End-date cap: skip markets too far in the future
            if cfg.max_days_to_end is not None and m.end_date_iso:
                end_dt = _parse_end_date(m.end_date_iso)
                if end_dt is not None:
                    days_left = (end_dt - now).total_seconds() / 86_400
                    if days_left > cfg.max_days_to_end:
                        c_days += 1; continue
            out.append(m)

        log.info(
            "[Scanner] Filter breakdown — inactive:%d no_tokens:%d price:%d "
            "volume<%g:%d liquidity<%g:%d category:%d days>%s:%d → passed:%d",
            c_inactive, c_no_tokens, c_price,
            cfg.min_volume_24h, c_volume,
            cfg.min_liquidity, c_liquidity,
            c_category,
            cfg.max_days_to_end, c_days,
            len(out),
        )
        return out

    @staticmethod
    def _rank(markets: List[PolyMarket]) -> List[PolyMarket]:
        """
        Rank by composite score (descending):
          1. Time proximity  – markets ending sooner score higher  (weight 0.5)
          2. Uncertainty     – YES price closest to 0.5            (weight 0.3)
          3. Volume          – higher 24 h volume                  (weight 0.2)

        Markets with no parseable end date are pushed to the bottom.
        """
        if not markets:
            return []

        now = datetime.now(timezone.utc)
        max_vol = max(m.volume_24h for m in markets) or 1.0

        # Collect days-to-end for normalisation
        days_list = []
        for m in markets:
            if m.end_date_iso:
                dt = _parse_end_date(m.end_date_iso)
                if dt is not None:
                    days_list.append(max((dt - now).total_seconds() / 86_400, 0.0))
        max_days = max(days_list) if days_list else 1.0

        def score(m: PolyMarket) -> float:
            # Time score: 1.0 for soonest, 0.0 for furthest; no date → 0
            time_score = 0.0
            if m.end_date_iso:
                dt = _parse_end_date(m.end_date_iso)
                if dt is not None:
                    days_left  = max((dt - now).total_seconds() / 86_400, 0.0)
                    time_score = 1.0 - (days_left / max_days) if max_days > 0 else 1.0

            uncertainty = 1.0 - abs(m.yes_price - 0.5) * 2
            norm_vol    = m.volume_24h / max_vol
            return time_score * 0.5 + uncertainty * 0.3 + norm_vol * 0.2

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_end_date(iso: str) -> Optional[datetime]:
    """Parse an ISO 8601 end-date string into a timezone-aware datetime.
    Returns None if the string is empty or unparseable.
    """
    if not iso:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(iso, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None
