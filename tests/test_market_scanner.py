"""Unit tests for polymarket/market_scanner.py"""
import pytest
from polymarket.client import PolyMarket
from polymarket.market_scanner import MarketScanner, ScanConfig


def make_market(**kwargs) -> PolyMarket:
    defaults = dict(
        condition_id="0xabc",
        question="Will X happen?",
        description="Test market",
        end_date_iso="2025-12-31",
        yes_token_id="yes_tok",
        no_token_id="no_tok",
        yes_price=0.5,
        no_price=0.5,
        volume_24h=1000.0,
        liquidity=500.0,
        category="crypto",
        active=True,
        closed=False,
        tags=[],
    )
    defaults.update(kwargs)
    return PolyMarket(**defaults)


@pytest.fixture
def cfg():
    return ScanConfig(
        min_volume_24h=500.0,
        min_liquidity=200.0,
        max_markets=10,
        price_deadzone_low=0.02,
        price_deadzone_high=0.98,
    )


@pytest.fixture
def scanner(cfg):
    return MarketScanner(client=None, config=cfg)


# ── _filter() ────────────────────────────────────────────────────────────────

def test_filter_passes_valid_market(scanner):
    m = make_market()
    assert scanner._filter([m]) == [m]

def test_filter_removes_inactive(scanner):
    m = make_market(active=False)
    assert scanner._filter([m]) == []

def test_filter_removes_closed(scanner):
    m = make_market(closed=True)
    assert scanner._filter([m]) == []

def test_filter_removes_missing_yes_token(scanner):
    m = make_market(yes_token_id="")
    assert scanner._filter([m]) == []

def test_filter_removes_missing_no_token(scanner):
    m = make_market(no_token_id="")
    assert scanner._filter([m]) == []

def test_filter_removes_price_below_deadzone(scanner):
    m = make_market(yes_price=0.01)
    assert scanner._filter([m]) == []

def test_filter_removes_price_above_deadzone(scanner):
    m = make_market(yes_price=0.99)
    assert scanner._filter([m]) == []

def test_filter_keeps_price_at_deadzone_boundary(scanner):
    low  = make_market(condition_id="low",  yes_price=0.02)
    high = make_market(condition_id="high", yes_price=0.98)
    result = scanner._filter([low, high])
    assert len(result) == 2

def test_filter_removes_low_volume(scanner):
    m = make_market(volume_24h=100.0)
    assert scanner._filter([m]) == []

def test_filter_removes_low_liquidity(scanner):
    m = make_market(liquidity=50.0)
    assert scanner._filter([m]) == []

def test_filter_removes_excluded_category(cfg):
    cfg.excluded_categories.add("sports")
    scanner = MarketScanner(client=None, config=cfg)
    m = make_market(category="Sports")
    assert scanner._filter([m]) == []

def test_filter_multiple_markets_mixed(scanner):
    good  = make_market(condition_id="good")
    bad1  = make_market(condition_id="inactive", active=False)
    bad2  = make_market(condition_id="lowvol", volume_24h=10.0)
    result = scanner._filter([good, bad1, bad2])
    assert result == [good]


# ── _rank() ───────────────────────────────────────────────────────────────────

def test_rank_empty(scanner):
    assert scanner._rank([]) == []

def test_rank_most_uncertain_first(scanner):
    """Market with YES price = 0.5 (most uncertain) should rank first."""
    certain   = make_market(condition_id="certain",   yes_price=0.9, volume_24h=1000.0)
    uncertain = make_market(condition_id="uncertain", yes_price=0.5, volume_24h=1000.0)
    ranked = scanner._rank([certain, uncertain])
    assert ranked[0].condition_id == "uncertain"

def test_rank_higher_volume_preferred_when_similar_uncertainty(scanner):
    low_vol  = make_market(condition_id="low",  yes_price=0.5, volume_24h=100.0)
    high_vol = make_market(condition_id="high", yes_price=0.5, volume_24h=5000.0)
    ranked = scanner._rank([low_vol, high_vol])
    assert ranked[0].condition_id == "high"

def test_rank_max_markets_respected(scanner):
    markets = [make_market(condition_id=str(i)) for i in range(20)]
    scanner._cfg.max_markets = 5
    result = scanner._rank(markets)[:scanner._cfg.max_markets]
    assert len(result) == 5
