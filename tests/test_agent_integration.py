"""
Integration test: full dry-run agent cycle with all external calls mocked.

Tests that:
  - Agent starts up, passes pre-flight (mocked MiroFish health)
  - Scans markets, filters, evaluates via MiroFish
  - Places a dry-run bet when edge exceeds threshold
  - Skips a market when edge is below threshold
  - Does not crash when zero balance (uses synthetic $100)
  - Stops cleanly on stop() call
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.polymarket_agent import AgentConfig, PolymarketAgent
from polymarket.client import PolyMarket, PolymarketClient
from polymarket.market_scanner import ScanConfig


def make_market(condition_id="0xabc", yes_price=0.4, volume=2000.0) -> PolyMarket:
    return PolyMarket(
        condition_id=condition_id,
        question=f"Will event {condition_id} happen?",
        description="Test market",
        end_date_iso="2025-12-31",
        yes_token_id=f"yes_{condition_id}",
        no_token_id=f"no_{condition_id}",
        yes_price=yes_price,
        no_price=1.0 - yes_price,
        volume_24h=volume,
        liquidity=500.0,
        category="crypto",
        active=True,
        closed=False,
        tags=[],
    )


def build_config(min_edge_pct=0.05, dry_run=True) -> AgentConfig:
    return AgentConfig(
        mirofish_url="http://localhost:5001",
        mirofish_rounds=1,
        min_edge_pct=min_edge_pct,
        max_open_positions=5,
        kelly_fraction=0.25,
        max_bet_fraction=0.05,
        min_bet_usdc=1.0,
        scan_interval_sec=9999,
        price_refresh=False,   # skip live price refresh
        scan_config=ScanConfig(min_volume_24h=100.0, min_liquidity=100.0),
        dry_run=dry_run,
    )


@pytest.fixture
def poly_client():
    client = AsyncMock(spec=PolymarketClient)
    client.get_balance_usdc = AsyncMock(return_value=100.0)
    client.get_markets = AsyncMock(return_value=[])
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


# ── Helper: run one cycle then stop ──────────────────────────────────────────

async def run_one_cycle(agent: PolymarketAgent) -> None:
    """Run agent for exactly one cycle then stop."""
    original_sleep = agent._sleep_interruptible
    call_count = 0

    async def stop_after_first(*_):
        nonlocal call_count
        call_count += 1
        agent.stop()

    agent._sleep_interruptible = stop_after_first
    await agent.run()


# ── Tests ─────────────────────────────────────────────────────────────────────

async def test_agent_places_bet_when_edge_sufficient(poly_client):
    """Agent should place a dry-run bet when MiroFish returns high confidence."""
    market = make_market(condition_id="0x001", yes_price=0.4)
    poly_client.get_markets = AsyncMock(return_value=[market])

    agent = PolymarketAgent(poly_client, build_config(min_edge_pct=0.05))

    with patch("agents.polymarket_agent.MiroFishPredictor") as MockPredictor:
        instance = AsyncMock()
        instance.is_available = AsyncMock(return_value=True)
        # MiroFish returns 0.7 → edge = 0.7 - 0.4 = 0.3 → well above 5%
        instance.predict = AsyncMock(return_value=0.7)
        MockPredictor.return_value.__aenter__ = AsyncMock(return_value=instance)
        MockPredictor.return_value.__aexit__ = AsyncMock(return_value=None)

        await run_one_cycle(agent)

    assert agent._orders.open_count == 1
    pos = agent._orders.open_positions[0]
    assert pos.condition_id == "0x001"
    assert pos.direction in ("YES", "NO")


async def test_agent_skips_bet_when_edge_insufficient(poly_client):
    """Agent should NOT bet when MiroFish edge is below threshold."""
    market = make_market(condition_id="0x002", yes_price=0.5)
    poly_client.get_markets = AsyncMock(return_value=[market])

    agent = PolymarketAgent(poly_client, build_config(min_edge_pct=0.10))

    with patch("agents.polymarket_agent.MiroFishPredictor") as MockPredictor:
        instance = AsyncMock()
        instance.is_available = AsyncMock(return_value=True)
        # MiroFish returns 0.52 → edge = 0.02 → below 10%
        instance.predict = AsyncMock(return_value=0.52)
        MockPredictor.return_value.__aenter__ = AsyncMock(return_value=instance)
        MockPredictor.return_value.__aexit__ = AsyncMock(return_value=None)

        await run_one_cycle(agent)

    assert agent._orders.open_count == 0


async def test_agent_uses_synthetic_bankroll_on_zero_balance(poly_client):
    """Zero balance in dry_run mode uses $100 synthetic bankroll."""
    poly_client.get_balance_usdc = AsyncMock(return_value=0.0)
    market = make_market(condition_id="0x003", yes_price=0.3)
    poly_client.get_markets = AsyncMock(return_value=[market])

    agent = PolymarketAgent(poly_client, build_config(min_edge_pct=0.05))

    with patch("agents.polymarket_agent.MiroFishPredictor") as MockPredictor:
        instance = AsyncMock()
        instance.is_available = AsyncMock(return_value=True)
        instance.predict = AsyncMock(return_value=0.75)
        MockPredictor.return_value.__aenter__ = AsyncMock(return_value=instance)
        MockPredictor.return_value.__aexit__ = AsyncMock(return_value=None)

        await run_one_cycle(agent)

    # Should have placed a bet with synthetic bankroll, not crashed
    assert agent._orders.open_count == 1


async def test_agent_fails_fast_if_mirofish_unavailable(poly_client):
    """RuntimeError should be raised immediately if MiroFish is down."""
    agent = PolymarketAgent(poly_client, build_config())

    with patch("agents.polymarket_agent.MiroFishPredictor") as MockPredictor:
        instance = AsyncMock()
        instance.is_available = AsyncMock(return_value=False)
        MockPredictor.return_value.__aenter__ = AsyncMock(return_value=instance)
        MockPredictor.return_value.__aexit__ = AsyncMock(return_value=None)

        with pytest.raises(RuntimeError, match="MiroFish is not reachable"):
            await agent.run()


async def test_agent_no_markets_does_not_crash(poly_client):
    """When scanner returns no markets, cycle completes without error."""
    poly_client.get_markets = AsyncMock(return_value=[])

    agent = PolymarketAgent(poly_client, build_config())

    with patch("agents.polymarket_agent.MiroFishPredictor") as MockPredictor:
        instance = AsyncMock()
        instance.is_available = AsyncMock(return_value=True)
        MockPredictor.return_value.__aenter__ = AsyncMock(return_value=instance)
        MockPredictor.return_value.__aexit__ = AsyncMock(return_value=None)

        await run_one_cycle(agent)

    assert agent._orders.open_count == 0


async def test_agent_skips_duplicate_position(poly_client):
    """Second call with same market should not open a second position."""
    market = make_market(condition_id="0x004", yes_price=0.4)
    poly_client.get_markets = AsyncMock(return_value=[market])

    agent = PolymarketAgent(poly_client, build_config(min_edge_pct=0.05))

    with patch("agents.polymarket_agent.MiroFishPredictor") as MockPredictor:
        instance = AsyncMock()
        instance.is_available = AsyncMock(return_value=True)
        instance.predict = AsyncMock(return_value=0.70)
        MockPredictor.return_value.__aenter__ = AsyncMock(return_value=instance)
        MockPredictor.return_value.__aexit__ = AsyncMock(return_value=None)

        # Run two cycles
        await run_one_cycle(agent)
        agent._stop_event.clear()
        await run_one_cycle(agent)

    assert agent._orders.open_count == 1  # only one position, not two
