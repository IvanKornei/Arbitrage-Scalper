"""
tools/setup_allowances.py – One-time setup: approve USDC allowances for Polymarket.

Must be run ONCE before the bot can place real orders.
This sets on-chain approvals so the CTF Exchange contract can
debit USDC from your proxy wallet when orders are filled.

Usage:
    python tools/setup_allowances.py
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

if not PRIVATE_KEY:
    sys.exit("FATAL: POLY_PRIVATE_KEY missing from .env")

print("=" * 60)
print("Setting up Polymarket allowances...")
print(f"  EOA funder: {FUNDER or '(EOA mode)'}")

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)

if FUNDER:
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=PRIVATE_KEY,
        chain_id=137,
        creds=creds,
        signature_type=1,  # POLY_PROXY (1=proxy wallet, 2=Gnosis Safe)
        funder=FUNDER,
    )
else:
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=PRIVATE_KEY,
        chain_id=137,
        creds=creds,
        signature_type=0,
    )

# Step 1: Update COLLATERAL (USDC) allowance
print("\nSTEP 1: Update USDC (COLLATERAL) allowance")
try:
    resp = client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    print(f"  OK: {resp}")
except Exception as e:
    print(f"  FAILED: {e}")

# Step 2: Update CONDITIONAL token allowance
print("\nSTEP 2: Update CONDITIONAL token allowance")
try:
    resp = client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL))
    print(f"  OK: {resp}")
except Exception as e:
    print(f"  FAILED: {e}")

# Step 3: Check resulting balances
print("\nSTEP 3: Check balance after update")
try:
    result = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    print(f"  COLLATERAL: {result}")
except Exception as e:
    print(f"  FAILED: {e}")

print("\n" + "=" * 60)
print("Done. Now run: python polymarket_main.py")
