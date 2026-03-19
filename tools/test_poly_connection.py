"""
tools/test_poly_connection.py – Step-by-step Polymarket connection test.
Run: python tools/test_poly_connection.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import dotenv_values
env = dotenv_values()

PRIVATE_KEY    = env.get("POLY_PRIVATE_KEY", "")
API_KEY        = env.get("POLY_API_KEY", "")
API_SECRET     = env.get("POLY_API_SECRET", "")
API_PASSPHRASE = env.get("POLY_API_PASSPHRASE", "")
FUNDER         = env.get("POLY_FUNDER_ADDRESS", "")

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID  = 137

print("=" * 60)
print("STEP 1: Check .env values")
print(f"  POLY_PRIVATE_KEY:    {'SET (' + PRIVATE_KEY[:6] + '...)' if PRIVATE_KEY else 'MISSING'}")
print(f"  POLY_API_KEY:        {'SET (' + API_KEY[:8] + '...)' if API_KEY else 'MISSING'}")
print(f"  POLY_API_SECRET:     {'SET' if API_SECRET else 'MISSING'}")
print(f"  POLY_API_PASSPHRASE: {'SET' if API_PASSPHRASE else 'MISSING'}")
print(f"  POLY_FUNDER_ADDRESS: {FUNDER or 'NOT SET (EOA mode)'}")

if not PRIVATE_KEY:
    print("\nFATAL: POLY_PRIVATE_KEY is missing from .env")
    sys.exit(1)

print("\nSTEP 2: Build ClobClient")
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)

    if FUNDER:
        client = ClobClient(
            host=CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
            creds=creds, signature_type=2, funder=FUNDER,
        )
        print(f"  OK: signature_type=2, funder={FUNDER}")
    else:
        client = ClobClient(
            host=CLOB_HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
            creds=creds, signature_type=0,
        )
        print("  OK: signature_type=0 (EOA mode)")
except Exception as e:
    print(f"  FAILED: {e}")
    sys.exit(1)

print("\nSTEP 3: Check derived EOA address")
try:
    from eth_account import Account
    acct = Account.from_key(PRIVATE_KEY)
    print(f"  EOA address: {acct.address}")
except Exception as e:
    print(f"  FAILED: {e}")

print("\nSTEP 4: Test API key (GET /api-keys)")
try:
    result = client.get_api_keys()
    print(f"  OK: {result}")
except Exception as e:
    print(f"  FAILED: {e}")

print("\nSTEP 5: Get USDC balance")
try:
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    result = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    raw_balance = result.get("balance", "0") if isinstance(result, dict) else result
    usdc = float(raw_balance) / 1_000_000
    print(f"  OK: Balance = {usdc:.2f} USDC")
except Exception as e:
    print(f"  FAILED: {e}")

print("\nSTEP 6: Fetch a real active market token_id")
real_token = None
try:
    import requests
    resp = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={"active": "true", "closed": "false", "limit": 5},
        timeout=10,
    )
    markets = resp.json() if isinstance(resp.json(), list) else resp.json().get("markets", [])
    import json as _json
    for m in markets:
        token_ids = m.get("clobTokenIds") or m.get("tokens", "[]")
        if isinstance(token_ids, str):
            token_ids = _json.loads(token_ids)
        if token_ids:
            real_token = str(token_ids[0])
            print(f"  Found market: {m.get('question', '')[:60]}")
            print(f"  token_id:     {real_token}")
            break
    if not real_token:
        print("  WARNING: No active markets found")
except Exception as e:
    print(f"  FAILED to fetch market: {e}")

print("\nSTEP 7: Test order signing with real token (DRY RUN – not submitting)")
if real_token:
    try:
        from py_clob_client.clob_types import MarketOrderArgs
        from py_clob_client.order_builder.constants import BUY

        args = MarketOrderArgs(token_id=real_token, amount=1.0, side=BUY)
        signed_order = client.create_market_order(args)
        print(f"  OK: Order signed successfully!")
        attrs = vars(signed_order) if hasattr(signed_order, '__dict__') else {}
        for k, v in attrs.items():
            val = str(v)
            print(f"  {k}: {val[:60]}{'...' if len(val) > 60 else ''}")
        print()
        print("  AUTH IS WORKING. Bot can place real orders.")
    except Exception as e:
        print(f"  FAILED: {e}")
else:
    print("  SKIPPED: no token_id available")

print("\n" + "=" * 60)
print("Done. Fix any FAILED steps above.")
