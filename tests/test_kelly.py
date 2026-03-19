"""Unit tests for polymarket/kelly.py"""
import pytest
from polymarket.kelly import KellySizer


@pytest.fixture
def sizer():
    return KellySizer(fraction_kelly=0.25, max_fraction_of_bankroll=0.05, min_bet_usdc=1.0)


# ── edge() ────────────────────────────────────────────────────────────────────

def test_edge_positive(sizer):
    assert sizer.edge(0.7, 0.5) == pytest.approx(0.2)

def test_edge_negative(sizer):
    assert sizer.edge(0.3, 0.5) == pytest.approx(-0.2)

def test_edge_zero(sizer):
    assert sizer.edge(0.5, 0.5) == pytest.approx(0.0)


# ── size() — zero cases ────────────────────────────────────────────────────────

def test_size_no_edge_returns_zero(sizer):
    """When our probability equals market price, Kelly = 0."""
    assert sizer.size(1000.0, 0.5, 0.5) == 0.0

def test_size_negative_edge_returns_zero(sizer):
    assert sizer.size(1000.0, 0.3, 0.6) == 0.0

def test_size_invalid_market_price_zero(sizer):
    assert sizer.size(1000.0, 0.9, 0.0) == 0.0

def test_size_invalid_market_price_one(sizer):
    assert sizer.size(1000.0, 0.9, 1.0) == 0.0

def test_size_below_min_bet_returns_zero(sizer):
    """Tiny bankroll → Kelly bet < min → returns 0 (not partial fill)."""
    assert sizer.size(1.0, 0.55, 0.5) == 0.0


# ── size() — happy path ────────────────────────────────────────────────────────

def test_size_positive_edge(sizer):
    bet = sizer.size(1000.0, 0.7, 0.5)
    assert bet >= sizer.min_bet_usdc
    assert bet > 0.0

def test_size_capped_by_max_fraction(sizer):
    """Even with massive edge, bet must not exceed max_fraction * bankroll."""
    bet = sizer.size(1000.0, 0.99, 0.01)
    assert bet <= 1000.0 * sizer.max_fraction_of_bankroll + 1e-9

def test_size_scales_with_bankroll(sizer):
    small = sizer.size(100.0, 0.7, 0.5)
    large = sizer.size(1000.0, 0.7, 0.5)
    assert large == pytest.approx(small * 10, rel=1e-6)

def test_size_larger_edge_larger_bet(sizer):
    small_edge = sizer.size(1000.0, 0.55, 0.5)
    large_edge = sizer.size(1000.0, 0.80, 0.5)
    assert large_edge > small_edge

def test_size_respects_min_bet_usdc(sizer):
    """When Kelly-sized bet is above min, it should exceed min_bet_usdc."""
    bet = sizer.size(1000.0, 0.7, 0.5)
    assert bet >= sizer.min_bet_usdc


# ── Kelly formula correctness ─────────────────────────────────────────────────

def test_kelly_formula_manual():
    """
    Verify Kelly formula against manual calculation.

    market_price = 0.4 → b = 0.6/0.4 = 1.5
    our_prob = 0.7
    Kelly f* = (1.5 * 0.7 - 0.3) / 1.5 = (1.05 - 0.3) / 1.5 = 0.5
    Quarter-Kelly = 0.125, capped at 0.05
    Bet = 1000 * 0.05 = 50.0
    """
    sizer = KellySizer(fraction_kelly=0.25, max_fraction_of_bankroll=0.05, min_bet_usdc=1.0)
    bet = sizer.size(1000.0, 0.7, 0.4)
    assert bet == pytest.approx(50.0, rel=1e-6)
