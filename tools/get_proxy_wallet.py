"""
tools/get_proxy_wallet.py – Print the Polymarket proxy wallet address for your EOA.

Usage:
    python tools/get_proxy_wallet.py

Then copy the result into .env as:
    POLY_FUNDER_ADDRESS=0x...
"""

from __future__ import annotations
import os, sys, json
import requests
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")
if not PRIVATE_KEY:
    sys.exit("POLY_PRIVATE_KEY not set in .env")

try:
    from eth_account import Account
except ImportError:
    sys.exit("eth_account not installed: pip install eth-account")

eoa = Account.from_key(PRIVATE_KEY).address
print(f"EOA address: {eoa}")

# ── Try 1: Gamma API /profiles ─────────────────────────────────────────────────
url = f"https://gamma-api.polymarket.com/profiles?address={eoa}"
print(f"\nQuerying {url} ...")
try:
    r = requests.get(url, timeout=15)
    data = r.json()
    profiles = data if isinstance(data, list) else data.get("data", [])
    for p in profiles:
        proxy = p.get("proxyWallet") or p.get("proxy_wallet") or p.get("proxyAddress")
        if proxy:
            print(f"\n✅  Proxy wallet: {proxy}")
            print(f"\nAdd to .env:\n  POLY_FUNDER_ADDRESS={proxy}")
            sys.exit(0)
    print("Gamma API returned no proxy wallet field:", json.dumps(data)[:300])
except Exception as e:
    print(f"Gamma API error: {e}")

# ── Try 2: CLOB /proxy-wallets with L2 auth ────────────────────────────────────
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    from py_clob_client.headers.headers import create_level_2_headers
    from py_clob_client.clob_types import RequestArgs
    from py_clob_client.signer import Signer
    from py_clob_client.http_helpers.helpers import get as clob_get

    creds = ApiCreds(
        api_key=os.getenv("POLY_API_KEY", ""),
        api_secret=os.getenv("POLY_API_SECRET", ""),
        api_passphrase=os.getenv("POLY_API_PASSPHRASE", ""),
    )
    signer = Signer(PRIVATE_KEY, chain_id=137)
    req_args = RequestArgs(method="GET", request_path="/proxy-wallets")
    headers = create_level_2_headers(signer, creds, req_args)
    url2 = f"https://clob.polymarket.com/proxy-wallets?signer={eoa}"
    print(f"\nQuerying {url2} ...")
    data2 = clob_get(url2, headers=headers)
    print("CLOB response:", json.dumps(data2)[:300])
    wallets = data2 if isinstance(data2, list) else data2.get("proxy_wallets", [])
    for w in wallets:
        addr = w.get("proxyAddress") or w.get("address") if isinstance(w, dict) else str(w)
        if addr:
            print(f"\n✅  Proxy wallet: {addr}")
            print(f"\nAdd to .env:\n  POLY_FUNDER_ADDRESS={addr}")
            sys.exit(0)
except Exception as e:
    print(f"CLOB API error: {e}")

print("\n❌  Could not auto-detect proxy wallet.")
print("Find it manually: polymarket.com → profile → deposit address (QR code)")
print("Then add to .env:  POLY_FUNDER_ADDRESS=0x...")
