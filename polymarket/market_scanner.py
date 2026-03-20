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
        for i, m in enumerate(result[:5], 1):
            log.debug(
                "[Scanner] #%d %s | end=%s | yes=%.2f | vol=%.0f",
                i, m.question[:50], m.end_date_iso, m.yes_price, m.volume_24h,
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
        Rank by composite score (descending).

        Weights:
          1. Time proximity  – 0.50  soonest-ending markets first
          2. Liquidity       – 0.30  deeper book = better MiroFish analysis
          3. Uncertainty     – 0.20  YES price closest to 0.5

        Markets with no parseable end date get time_score = 0.
        """
        if not markets:
            return []

        now = datetime.now(timezone.utc)
        max_liq = max(m.liquidity for m in markets) or 1.0

        # Collect days-to-end for normalisation
        days_list = []
        for m in markets:
            if m.end_date_iso:
                dt = _parse_end_date(m.end_date_iso)
                if dt is not None:
                    days_list.append(max((dt - now).total_seconds() / 86_400, 0.0))
        max_days = max(days_list) if days_list else 1.0

        def score(m: PolyMarket) -> float:
            norm_liq = m.liquidity / max_liq

            # Time score: 1.0 for soonest, 0.0 for furthest; no date → 0
            time_score = 0.0
            if m.end_date_iso:
                dt = _parse_end_date(m.end_date_iso)
                if dt is not None:
                    days_left  = max((dt - now).total_seconds() / 86_400, 0.0)
                    time_score = 1.0 - (days_left / max_days) if max_days > 0 else 1.0

            uncertainty = 1.0 - abs(m.yes_price - 0.5) * 2
            return time_score * 0.50 + norm_liq * 0.30 + uncertainty * 0.20

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
    """Parse an end-date value into a timezone-aware datetime.

    Handles:
      - Unix timestamps (integer or float as string, e.g. "1735689600")
      - ISO 8601 strings (various formats returned by Gamma API)

    Returns None if the value is empty or unparseable.
    """
    if not iso:
        return None

    # Unix timestamp (integer or float stored as string)
    try:
        ts = float(iso)
        if ts > 0:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        pass

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

    log.debug("[Scanner] Could not parse end_date: %r", iso)
    return None
