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

print("\nSTEP 6: Test market order (DRY RUN – not submitting)")
try:
    from py_clob_client.clob_types import MarketOrderArgs
    from py_clob_client.order_builder.constants import BUY

    # Use a well-known token_id for Trump 2024 win market (doesn't matter, just testing signing)
    TEST_TOKEN = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
    args = MarketOrderArgs(token_id=TEST_TOKEN, amount=1.0, side=BUY)
    signed_order = client.create_market_order(args)
    print(f"  OK: Order signed successfully")
    print(f"  maker:         {signed_order.maker}")
    print(f"  signatureType: {signed_order.signatureType}")
    print(f"  sig prefix:    {signed_order.signature[:10]}...")
    print()
    print("  >>> To actually post this order, uncomment the next block <<<")
    # from py_clob_client.clob_types import OrderType
    # resp = client.post_order(signed_order, OrderType.FOK)
    # print(f"  POST result: {resp}")
except Exception as e:
    print(f"  FAILED: {e}")

print("\n" + "=" * 60)
print("Done. Fix any FAILED steps above.")
