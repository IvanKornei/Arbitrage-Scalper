"""
polymarket/kelly.py – Kelly Criterion bet sizing for binary prediction markets.

For a binary market:
  - p  = our estimated YES probability
  - q  = market price of YES (implied probability / cost per share)
  - b  = net profit per $1 bet if we win = (1 - q) / q   (odds)

Kelly fraction: f* = (b*p - (1-p)) / b = p - (1-p)/b

We apply a fraction_kelly multiplier (default 0.25 = quarter-Kelly)
for conservative sizing, then cap at max_fraction_of_bankroll.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class KellySizer:
    fraction_kelly: float = 0.25        # how aggressively to follow Kelly
    max_fraction_of_bankroll: float = 0.05  # never bet more than 5% per trade
    min_bet_usdc: float = 1.0           # Polymarket minimum

    def size(
        self,
        bankroll: float,
        our_prob: float,
        market_price: float,
    ) -> float:
        """
        Return recommended bet size in USDC.

        Args:
            bankroll:     Available USDC balance.
            our_prob:     Our estimated YES probability (0-1).
            market_price: Current market YES price (0-1).

        Returns:
            Bet size in USDC (0.0 if no edge).
        """
        if market_price <= 0.0 or market_price >= 1.0:
            return 0.0

        # Net odds per $1 risked
        b = (1.0 - market_price) / market_price
        q = 1.0 - our_prob

        kelly_f = (b * our_prob - q) / b

        if kelly_f <= 0.0:
            return 0.0  # no edge

        fraction = kelly_f * self.fraction_kelly
        fraction = min(fraction, self.max_fraction_of_bankroll)

        bet = bankroll * fraction
        return max(bet, self.min_bet_usdc) if bet >= self.min_bet_usdc else 0.0

    def edge(self, our_prob: float, market_price: float) -> float:
        """Return the EV edge: our_prob - market_price."""
        return our_prob - market_price
