"""
polymarket/client.py – Async Polymarket CLOB API client.

Authentication (per official docs):
  signature_type=2 (POLY_PROXY) – proxy/browser wallet, funds in proxy wallet.
    Requires: private_key (EOA signer) + funder (proxy wallet address).
  signature_type=0 (EOA) – direct wallet, funds in EOA address.
    Requires: private_key only (funder == EOA).

Order placement:
  Uses create_market_order() + MarketOrderArgs(amount=usdc) for dollar-amount
  buys. "amount" in MarketOrderArgs is USDC to spend (not shares).
  Uses py_clob_client in executor to avoid blocking the event loop.

Read-only endpoints use plain async aiohttp (no signing needed).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

from utils.logger import get_logger

log = get_logger(__name__)

CLOB_HOST  = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
CHAIN_ID   = 137  # Polygon mainnet


@dataclass
class PolyMarket:
    """Represents a single Polymarket binary market."""
    condition_id: str
    question: str
    description: str
    end_date_iso: str
    yes_token_id: str
    no_token_id: str
    yes_price: float    # best ask for YES (0-1)
    no_price: float     # best ask for NO  (0-1)
    volume_24h: float   # USD
    liquidity: float    # USD
    category: str = ""
    active: bool = True
    closed: bool = False
    tags: List[str] = field(default_factory=list)

    @property
    def implied_yes_prob(self) -> float:
        return self.yes_price

    @property
    def implied_no_prob(self) -> float:
        return 1.0 - self.yes_price


@dataclass
class OrderResult:
    order_id: str
    status: str          # "live" | "matched" | "delayed"
    size_matched: float
    price: float
    side: str            # "BUY" | "SELL"
    token_id: str


class PolymarketClient:
    """
    Async Polymarket client.

    Initialization:
        With proxy wallet (signature_type=2):
            PolymarketClient(private_key=..., api_key=..., ..., funder="0x<proxy>")

        With EOA only (signature_type=0):
            PolymarketClient(private_key=..., api_key=..., ...)
            funder defaults to "" → EOA is used as funder

    The CLOB client (sync py_clob_client) is created once on __aenter__.
    """

    def __init__(
        self,
        private_key: str = "",
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        clob_host: str = CLOB_HOST,
        chain_id: int = CHAIN_ID,
        funder: str = "",
    ) -> None:
        self._private_key    = private_key
        self._api_key        = api_key
        self._api_secret     = api_secret
        self._api_passphrase = api_passphrase
        self._clob_host      = clob_host
        self._chain_id       = chain_id
        self._funder         = funder.strip()
        self._session: Optional[aiohttp.ClientSession] = None
        self._clob_client: Any = None

    # ── Session lifecycle ──────────────────────────────────────────────────────

    async def __aenter__(self) -> "PolymarketClient":
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        )
        self._clob_client = await self._run_sync(self._build_clob_client)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        assert self._session is not None, "Use as async context manager"
        return self._session

    # ── CLOB client factory ────────────────────────────────────────────────────

    def _build_clob_client(self) -> Any:
        """Build sync py_clob_client.ClobClient (called once in executor)."""
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        creds = ApiCreds(
            api_key=self._api_key,
            api_secret=self._api_secret,
            api_passphrase=self._api_passphrase,
        )

        if self._funder:
            # Proxy-wallet mode: EOA signs on behalf of the Gnosis Safe proxy wallet
            # signature_type=2 = POLY_GNOSIS_SAFE – verified via getSafeAddress(EOA)
            # on the CTF Exchange contract (must match POLY_FUNDER_ADDRESS).
            log.info(
                "[Poly] Using proxy wallet (signature_type=2 POLY_GNOSIS_SAFE). "
                "EOA signer, funder=%s", self._funder,
            )
            client = ClobClient(
                host=self._clob_host,
                key=self._private_key,
                chain_id=self._chain_id,
                creds=creds,
                signature_type=2,
                funder=self._funder,
            )
        else:
            # EOA mode: private key is both signer and funder
            log.info("[Poly] Using EOA signing (signature_type=0). No proxy wallet.")
            client = ClobClient(
                host=self._clob_host,
                key=self._private_key,
                chain_id=self._chain_id,
                creds=creds,
                signature_type=0,
            )

        return client

    async def _run_sync(self, fn, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    # ── Market data ────────────────────────────────────────────────────────────

    async def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
    ) -> List[PolyMarket]:
        """Fetch markets from the Gamma API."""
        params: Dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "closed": "false",
        }
        if active:
            params["active"] = "true"

        try:
            async with self.session.get(f"{GAMMA_HOST}/markets", params=params) as resp:
                if resp.status != 200:
                    log.warning("[Poly] get_markets HTTP %d", resp.status)
                    return []
                raw = await resp.json(content_type=None)
                if isinstance(raw, dict):
                    raw = raw.get("markets", [])
        except Exception as exc:
            log.error("[Poly] get_markets error: %s", exc)
            return []

        markets = []
        for m in raw:
            parsed = self._parse_market(m)
            if parsed:
                markets.append(parsed)
        return markets

    async def get_market(self, condition_id: str) -> Optional[PolyMarket]:
        """Fetch a single market by condition ID."""
        try:
            async with self.session.get(
                f"{GAMMA_HOST}/markets", params={"condition_id": condition_id}
            ) as resp:
                data = await resp.json(content_type=None)
                if isinstance(data, list) and data:
                    return self._parse_market(data[0])
                if isinstance(data, dict):
                    return self._parse_market(data)
        except Exception as exc:
            log.error("[Poly] get_market(%s) error: %s", condition_id, exc)
        return None

    async def get_orderbook(self, token_id: str) -> Dict[str, List[Dict[str, float]]]:
        """Return {'bids': [...], 'asks': [...]}."""
        try:
            async with self.session.get(
                f"{self._clob_host}/book", params={"token_id": token_id}
            ) as resp:
                if resp.status != 200:
                    return {"bids": [], "asks": []}
                return await resp.json(content_type=None) or {"bids": [], "asks": []}
        except Exception as exc:
            log.error("[Poly] get_orderbook error: %s", exc)
            return {"bids": [], "asks": []}

    async def get_best_ask(self, token_id: str) -> float:
        """Return the best ask price using the /price endpoint.

        Uses /price?token_id=...&side=buy instead of /book because the
        /book endpoint is known to return stale ghost data (ask: 0.99)
        for active markets (Polymarket py-clob-client issue #180).
        """
        try:
            async with self.session.get(
                f"{self._clob_host}/price",
                params={"token_id": token_id, "side": "buy"},
            ) as resp:
                if resp.status != 200:
                    return 0.0
                data = await resp.json(content_type=None)
                price = data.get("price", 0)
                return float(price) if price else 0.0
        except Exception as exc:
            log.error("[Poly] get_best_ask error: %s", exc)
            return 0.0

    async def get_balance_usdc(self) -> float:
        """Return USDC balance (collateral) available for trading."""
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = await self._run_sync(self._clob_client.get_balance_allowance, params)
            raw = result.get("balance", "0") if isinstance(result, dict) else result
            return float(raw) / 1_000_000
        except Exception as exc:
            log.error("[Poly] get_balance error: %s", exc)
            return 0.0

    # ── Order placement ────────────────────────────────────────────────────────

    async def buy_yes(
        self,
        token_id: str,
        size_usdc: float,
        price: float = 0.0,
        market_order: bool = True,
    ) -> Optional[OrderResult]:
        """Buy YES shares spending `size_usdc` USDC."""
        return await self._place_market_order(token_id, "BUY", size_usdc)

    async def buy_no(
        self,
        token_id: str,
        size_usdc: float,
        price: float = 0.0,
        market_order: bool = True,
    ) -> Optional[OrderResult]:
        """Buy NO shares spending `size_usdc` USDC."""
        return await self._place_market_order(token_id, "BUY", size_usdc)

    async def _place_market_order(
        self,
        token_id: str,
        side: str,
        amount_usdc: float,
    ) -> Optional[OrderResult]:
        """
        Place a FOK market order.

        Uses MarketOrderArgs where amount = USDC to spend (for BUY orders).
        py_clob_client internally converts USDC → shares using the current price.
        """
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            side_const = BUY if side.upper() == "BUY" else SELL

            args = MarketOrderArgs(
                token_id=token_id,
                amount=round(amount_usdc, 2),
                side=side_const,
            )
            signed = await self._run_sync(self._clob_client.create_market_order, args)
            resp   = await self._run_sync(self._clob_client.post_order, signed, OrderType.FOK)

            log.info(
                "[Poly] Order placed: side=%s token=%s amount_usdc=%.2f → %s",
                side, token_id[:12], amount_usdc, resp,
            )
            return OrderResult(
                order_id    = resp.get("orderID", ""),
                status      = resp.get("status", "unknown"),
                size_matched= float(resp.get("sizeMatched", 0)),
                price       = 0.0,
                side        = side,
                token_id    = token_id,
            )
        except Exception as exc:
            log.error("[Poly] Order error: %s", exc)
            return None

    # ── Market parser ──────────────────────────────────────────────────────────

    @staticmethod
    def _parse_market(m: Dict[str, Any]) -> Optional[PolyMarket]:
        import json as _json

        def _j(v):
            if isinstance(v, str):
                try:
                    return _json.loads(v)
                except Exception:
                    return []
            return v or []

        try:
            condition_id = m.get("conditionId") or m.get("condition_id", "")
            if not condition_id:
                return None

            token_ids = _j(m.get("clobTokenIds") or m.get("tokens", "[]"))
            outcomes  = _j(m.get("outcomes", '["Yes","No"]'))
            prices    = _j(m.get("outcomePrices", '["0.5","0.5"]'))

            yes_idx = next((i for i, o in enumerate(outcomes) if str(o).lower() == "yes"), 0)
            no_idx  = next((i for i, o in enumerate(outcomes) if str(o).lower() == "no"),  1)

            return PolyMarket(
                condition_id = condition_id,
                question     = m.get("question", ""),
                description  = m.get("description", ""),
                end_date_iso = m.get("endDateIso") or m.get("endDate") or "",
                yes_token_id = str(token_ids[yes_idx]) if yes_idx < len(token_ids) else "",
                no_token_id  = str(token_ids[no_idx])  if no_idx  < len(token_ids) else "",
                yes_price    = float(prices[yes_idx])   if yes_idx < len(prices)    else 0.5,
                no_price     = float(prices[no_idx])    if no_idx  < len(prices)    else 0.5,
                volume_24h   = float(m.get("volume24hr", 0) or 0),
                liquidity    = float(m.get("liquidity", 0) or 0),
                category     = m.get("category", "") or "",
                active       = bool(m.get("active", True)),
                closed       = bool(m.get("closed", False)),
                tags         = [t.get("label", "") for t in m.get("tags", [])],
            )
        except Exception as exc:
            log.debug("[Poly] parse_market error: %s", exc)
            return None
