"""
polymarket/client.py – Async Polymarket CLOB API client.

Authentication:
  Polymarket uses EIP-712 signed API keys.  To generate your API key:
    from py_clob_client.client import ClobClient
    client = ClobClient(host, key=private_key, chain_id=137)
    client.create_or_derive_api_creds()

  Set the resulting values in .env:
    POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE

Order signing uses py_clob_client's built-in signer; we call it via
asyncio.run_in_executor to avoid blocking the event loop.

Read-only endpoints (market listing, order books) use plain async HTTP.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

from utils.logger import get_logger

log = get_logger(__name__)

CLOB_HOST   = "https://clob.polymarket.com"
GAMMA_HOST  = "https://gamma-api.polymarket.com"  # market metadata
CHAIN_ID    = 137  # Polygon mainnet


@dataclass
class PolyMarket:
    """Represents a single Polymarket binary market."""
    condition_id: str
    question: str
    description: str
    end_date_iso: str
    yes_token_id: str
    no_token_id: str
    yes_price: float    # current best ask for YES (0-1)
    no_price: float     # current best ask for NO (0-1)
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
    - Read ops: direct aiohttp (fast, no signing needed)
    - Write ops: py_clob_client in executor (handles EIP-712 signing)
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
        self._funder         = funder   # proxy wallet address (auto-resolved if empty)
        self._session: Optional[aiohttp.ClientSession] = None
        self._clob_client: Any = None   # py_clob_client.ClobClient (lazy)

    # ── Session lifecycle ─────────────────────────────────────────────────────

    async def __aenter__(self) -> "PolymarketClient":
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        )
        if not self._funder and self._private_key:
            self._funder = await self._resolve_funder()
            if self._funder:
                log.info("[Poly] Proxy wallet (funder): %s", self._funder)
            else:
                log.warning("[Poly] Could not resolve proxy wallet – orders may fail")
        return self

    async def _resolve_funder(self) -> str:
        """Derive proxy wallet address: try Gamma API profile, then CLOB /proxy-wallets."""
        try:
            from eth_account import Account
            eoa = Account.from_key(self._private_key).address

            # ── 1. Gamma API /profiles (unauthenticated, same host already in use) ──
            url = f"{GAMMA_HOST}/profiles?address={eoa}"
            try:
                async with self._session.get(url) as resp:
                    data = await resp.json(content_type=None)
                    profiles = data if isinstance(data, list) else data.get("data", [])
                    for p in profiles:
                        addr = p.get("proxyWallet") or p.get("proxy_wallet") or p.get("proxyAddress")
                        if addr and addr != "0x0000000000000000000000000000000000000000":
                            return addr
            except Exception:
                pass

            # ── 2. CLOB /proxy-wallets with L2 auth (authenticated) ─────────────
            from py_clob_client.signer import Signer
            from py_clob_client.headers.headers import create_level_2_headers
            from py_clob_client.clob_types import ApiCreds, RequestArgs
            from py_clob_client.http_helpers.helpers import get as clob_get

            signer = Signer(self._private_key, chain_id=self._chain_id)
            creds = ApiCreds(
                api_key=self._api_key,
                api_secret=self._api_secret,
                api_passphrase=self._api_passphrase,
            )
            req_args = RequestArgs(method="GET", request_path="/proxy-wallets")
            headers = create_level_2_headers(signer, creds, req_args)
            data = await self._run_sync(
                clob_get, f"{self._clob_host}/proxy-wallets?signer={eoa}", headers=headers
            )
            wallets = data if isinstance(data, list) else data.get("proxy_wallets", [])
            for w in wallets:
                addr = w.get("proxyAddress") or w.get("address") if isinstance(w, dict) else str(w)
                if addr and addr != "0x0000000000000000000000000000000000000000":
                    return addr

        except Exception as exc:
            log.warning("[Poly] proxy wallet auto-resolve error: %s", exc)

        log.warning(
            "[Poly] Could not resolve proxy wallet address.\n"
            "       Add to .env:  POLY_FUNDER_ADDRESS=<your_proxy_wallet>\n"
            "       Find it at:   polymarket.com → profile → deposit address"
        )
        return ""

    async def __aexit__(self, *_: Any) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        assert self._session is not None, "Use as async context manager"
        return self._session

    # ── CLOB client (sync, used via executor) ─────────────────────────────────

    def _get_clob_client(self) -> Any:
        """Lazily create the sync py_clob_client.ClobClient."""
        if self._clob_client is not None:
            return self._clob_client
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
        except ImportError as e:
            raise ImportError(
                "py_clob_client is required for order placement. "
                "pip install py-clob-client"
            ) from e

        creds = ApiCreds(
            api_key=self._api_key,
            api_secret=self._api_secret,
            api_passphrase=self._api_passphrase,
        )
        kwargs: Dict[str, Any] = dict(
            host=self._clob_host,
            key=self._private_key,
            chain_id=self._chain_id,
            creds=creds,
            signature_type=2,   # POLY_PROXY – matches Polymarket web-app proxy wallet
        )
        if self._funder:
            kwargs["funder"] = self._funder
        self._clob_client = ClobClient(**kwargs)
        return self._clob_client

    async def _run_sync(self, fn, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
    ) -> List[PolyMarket]:
        """Fetch markets from the Gamma API with pagination."""
        params: Dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "closed": "false",
        }
        if active:
            params["active"] = "true"

        url = f"{GAMMA_HOST}/markets"
        try:
            async with self.session.get(url, params=params) as resp:
                if resp.status != 200:
                    log.warning("[Poly] get_markets HTTP %d", resp.status)
                    return []
                raw_list = await resp.json(content_type=None)
                if isinstance(raw_list, dict):
                    raw_list = raw_list.get("markets", [])
        except Exception as exc:
            log.error("[Poly] get_markets error: %s", exc)
            return []

        markets: List[PolyMarket] = []
        for m in raw_list:
            parsed = self._parse_market(m)
            if parsed:
                markets.append(parsed)
        return markets

    async def get_market(self, condition_id: str) -> Optional[PolyMarket]:
        """Fetch a single market by condition ID."""
        url = f"{GAMMA_HOST}/markets"
        params = {"condition_id": condition_id}
        try:
            async with self.session.get(url, params=params) as resp:
                data = await resp.json(content_type=None)
                if isinstance(data, list) and data:
                    return self._parse_market(data[0])
                if isinstance(data, dict):
                    return self._parse_market(data)
        except Exception as exc:
            log.error("[Poly] get_market(%s) error: %s", condition_id, exc)
        return None

    async def get_orderbook(
        self, token_id: str
    ) -> Dict[str, List[Dict[str, float]]]:
        """Return {'bids': [...], 'asks': [...]} with price/size dicts."""
        url = f"{self._clob_host}/book"
        params = {"token_id": token_id}
        try:
            async with self.session.get(url, params=params) as resp:
                if resp.status != 200:
                    return {"bids": [], "asks": []}
                data = await resp.json(content_type=None)
                return data or {"bids": [], "asks": []}
        except Exception as exc:
            log.error("[Poly] get_orderbook error: %s", exc)
            return {"bids": [], "asks": []}

    async def get_best_ask(self, token_id: str) -> float:
        """Return the best ask price for a token (0 if no liquidity)."""
        book = await self.get_orderbook(token_id)
        asks = book.get("asks", [])
        if not asks:
            return 0.0
        # asks are sorted ascending; first is best
        return float(asks[0].get("price", 0))

    async def get_balance_usdc(self) -> float:
        """Return USDC balance available for trading."""
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            clob = self._get_clob_client()
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = await self._run_sync(clob.get_balance_allowance, params)
            if isinstance(result, dict):
                raw = result.get("balance", "0")
            elif isinstance(result, (int, float)):
                raw = result
            else:
                raw = result  # string – raw micro-USDC value
            return float(raw) / 1_000_000
        except Exception as exc:
            log.error("[Poly] get_balance error: %s", exc)
            return 0.0

    # ── Order placement ───────────────────────────────────────────────────────

    async def buy_yes(
        self,
        token_id: str,
        size_usdc: float,
        price: float,
        market_order: bool = True,
    ) -> Optional[OrderResult]:
        """
        Place a BUY order for YES shares.

        Args:
            token_id:     YES token ID from the market.
            size_usdc:    USDC to spend.
            price:        Limit price (0-1). Ignored if market_order=True.
            market_order: Use FOK market order (True) or limit GTC (False).
        """
        return await self._place_order(
            token_id=token_id,
            side="BUY",
            size_usdc=size_usdc,
            price=price,
            market_order=market_order,
        )

    async def buy_no(
        self,
        token_id: str,
        size_usdc: float,
        price: float,
        market_order: bool = True,
    ) -> Optional[OrderResult]:
        """Place a BUY order for NO shares (equivalent to betting NO)."""
        return await self._place_order(
            token_id=token_id,
            side="BUY",
            size_usdc=size_usdc,
            price=price,
            market_order=market_order,
        )

    async def _place_order(
        self,
        token_id: str,
        side: str,
        size_usdc: float,
        price: float,
        market_order: bool,
    ) -> Optional[OrderResult]:
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            clob = self._get_clob_client()
            order_type = OrderType.FOK if market_order else OrderType.GTC

            args = OrderArgs(
                token_id=token_id,
                price=round(price, 4),
                size=round(size_usdc, 2),
                side=side,
            )
            signed_order = await self._run_sync(
                clob.create_order, args
            )
            resp = await self._run_sync(
                clob.post_order, signed_order, order_type
            )
            log.info(
                "[Poly] Order placed: side=%s token=%s size=%.2f price=%.4f → %s",
                side, token_id[:12], size_usdc, price, resp,
            )
            return OrderResult(
                order_id=resp.get("orderID", ""),
                status=resp.get("status", "unknown"),
                size_matched=float(resp.get("sizeMatched", 0)),
                price=price,
                side=side,
                token_id=token_id,
            )
        except Exception as exc:
            log.error("[Poly] Order error: %s", exc)
            return None

    # ── Helper: parse Gamma API market ────────────────────────────────────────

    @staticmethod
    def _parse_market(m: Dict[str, Any]) -> Optional[PolyMarket]:
        """Parse a raw Gamma API market dict into a PolyMarket dataclass."""
        import json as _json

        def _parse_json_field(v):
            """Gamma API returns some list fields as JSON strings."""
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

            # token IDs: 'clobTokenIds' or 'tokens' – both are JSON-encoded strings
            token_ids = _parse_json_field(m.get("clobTokenIds") or m.get("tokens", "[]"))
            outcomes  = _parse_json_field(m.get("outcomes", '["Yes","No"]'))
            prices    = _parse_json_field(m.get("outcomePrices", '["0.5","0.5"]'))

            yes_idx = next((i for i, o in enumerate(outcomes) if str(o).lower() == "yes"), 0)
            no_idx  = next((i for i, o in enumerate(outcomes) if str(o).lower() == "no"),  1)

            yes_token_id = str(token_ids[yes_idx]) if yes_idx < len(token_ids) else ""
            no_token_id  = str(token_ids[no_idx])  if no_idx  < len(token_ids) else ""
            yes_price    = float(prices[yes_idx])   if yes_idx < len(prices)    else 0.5
            no_price     = float(prices[no_idx])    if no_idx  < len(prices)    else 0.5

            return PolyMarket(
                condition_id  = condition_id,
                question      = m.get("question", ""),
                description   = m.get("description", ""),
                end_date_iso  = m.get("endDateIso") or m.get("endDate") or m.get("end_date_iso", ""),
                yes_token_id  = yes_token_id,
                no_token_id   = no_token_id,
                yes_price     = yes_price,
                no_price      = no_price,
                volume_24h    = float(m.get("volume24hr", 0) or 0),
                liquidity     = float(m.get("liquidity", 0) or 0),
                category      = m.get("category", "") or "",
                active        = bool(m.get("active", True)),
                closed        = bool(m.get("closed", False)),
                tags          = [t.get("label", "") for t in m.get("tags", [])],
            )
        except Exception as exc:
            log.debug("[Poly] parse_market error: %s", exc)
            return None
