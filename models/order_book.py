"""
models/order_book.py – Lightweight, lock-free order book snapshot.

Uses sorted lists maintained by bisect for O(log n) updates.
Timestamps are always UNIX epoch in seconds (float) for sub-ms precision.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# price → quantity  (quantity=0 means level deleted)
Level = Tuple[float, float]


@dataclass
class OrderBook:
    symbol: str
    exchange: str

    # bids: descending price order  [best_bid, ...]
    bids: List[Level] = field(default_factory=list)
    # asks: ascending price order   [best_ask, ...]
    asks: List[Level] = field(default_factory=list)

    # Internal dict for O(1) updates keyed by price string (avoids float key issues)
    _bid_map: Dict[str, float] = field(default_factory=dict, repr=False)
    _ask_map: Dict[str, float] = field(default_factory=dict, repr=False)

    last_update_ts: float = field(default_factory=time.time)
    sequence: int = 0

    # ── Snapshot initialisation ───────────────────────────────────────────────

    def apply_snapshot(
        self,
        bids: List[List[str]],
        asks: List[List[str]],
        ts: Optional[float] = None,
    ) -> None:
        """Replace book with a full snapshot (list of [price_str, qty_str])."""
        self._bid_map = {p: float(q) for p, q in bids if float(q) > 0}
        self._ask_map = {p: float(q) for p, q in asks if float(q) > 0}
        self._rebuild_sorted()
        self.last_update_ts = ts or time.time()

    # ── Incremental update ────────────────────────────────────────────────────

    def apply_delta(
        self,
        bids: List[List[str]],
        asks: List[List[str]],
        ts: Optional[float] = None,
        sequence: int = 0,
    ) -> None:
        """Apply a diff update; qty=0 removes the level."""
        changed = False
        for p, q in bids:
            qty = float(q)
            if qty == 0.0:
                self._bid_map.pop(p, None)
            else:
                self._bid_map[p] = qty
            changed = True
        for p, q in asks:
            qty = float(q)
            if qty == 0.0:
                self._ask_map.pop(p, None)
            else:
                self._ask_map[p] = qty
            changed = True
        if changed:
            self._rebuild_sorted()
        self.last_update_ts = ts or time.time()
        self.sequence = sequence

    def _rebuild_sorted(self) -> None:
        self.bids = sorted(
            ((float(p), q) for p, q in self._bid_map.items()),
            reverse=True,
        )
        self.asks = sorted(
            (float(p), q) for p, q in self._ask_map.items()
        )

    # ── Accessors ─────────────────────────────────────────────────────────────

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def mid_price(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        return (bb + ba) / 2.0 if bb and ba else None

    @property
    def spread(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        return ba - bb if bb and ba else None

    def depth(self, levels: int = 5) -> Tuple[List[Level], List[Level]]:
        """Return top-N levels on each side."""
        return self.bids[:levels], self.asks[:levels]

    def age_ms(self) -> float:
        """Milliseconds since last update."""
        return (time.time() - self.last_update_ts) * 1000.0

    def __repr__(self) -> str:
        return (
            f"OrderBook({self.exchange}:{self.symbol} "
            f"bid={self.best_bid} ask={self.best_ask} "
            f"age={self.age_ms():.1f}ms)"
        )
