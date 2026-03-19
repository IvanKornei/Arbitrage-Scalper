"""
tools/check_safe_registration.py – Diagnose 'invalid signature' by checking
on-chain Safe registration in the CTF Exchange contract.

The CTF Exchange validates signatureType=2 orders by calling:
    getSafeAddress(order.signer) == order.maker

If the mapping is missing, all orders fail with 'invalid signature'.

Usage:
    python tools/check_safe_registration.py
"""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import dotenv_values
from eth_account import Account

try:
    from web3 import Web3
except ImportError:
    print("web3 not installed. Run: pip install web3")
    import sys; sys.exit(1)

env = dotenv_values()

PRIVATE_KEY = env.get("POLY_PRIVATE_KEY", "")
FUNDER      = env.get("POLY_FUNDER_ADDRESS", "")

if not PRIVATE_KEY:
    sys.exit("FATAL: POLY_PRIVATE_KEY missing from .env")

EOA = Account.from_key(PRIVATE_KEY).address
print(f"EOA address:   {EOA}")
print(f"Proxy wallet:  {FUNDER or '(not set)'}")

# CTF Exchange contract on Polygon mainnet
CTF_EXCHANGE     = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_ADAPTER = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

POLYGON_RPCS = [
    "https://polygon-rpc.com",
    "https://rpc-mainnet.matic.network",
    "https://rpc-mainnet.maticvigil.com",
    "https://matic-mainnet.chainstacklabs.com",
    "https://polygon.llamarpc.com",
    "https://1rpc.io/matic",
    "https://polygon-bor-rpc.publicnode.com",
]

GET_SAFE_ADDRESS_ABI = [{
    "inputs": [{"internalType": "address", "name": "_addr", "type": "address"}],
    "name": "getSafeAddress",
    "outputs": [{"internalType": "address", "name": "", "type": "address"}],
    "stateMutability": "view",
    "type": "function",
}]

w3 = None
for rpc in POLYGON_RPCS:
    print(f"\nTrying RPC: {rpc}")
    try:
        candidate = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
        if candidate.is_connected():
            block = candidate.eth.block_number
            print(f"  Connected. Block: {block}")
            w3 = candidate
            break
        else:
            print("  Not connected.")
    except Exception as e:
        print(f"  Error: {e}")

if w3 is None:
    print("\nERROR: Cannot reach any Polygon RPC. Check your internet connection.")
    sys.exit(1)

print(f"\nChecking CTF Exchange Safe Factory: {CTF_EXCHANGE}")
try:
    contract = w3.eth.contract(address=Web3.to_checksum_address(CTF_EXCHANGE),
                               abi=GET_SAFE_ADDRESS_ABI)
    registered_safe = contract.functions.getSafeAddress(
        Web3.to_checksum_address(EOA)
    ).call()
    print(f"  getSafeAddress({EOA[:10]}...) → {registered_safe}")

    if registered_safe == "0x0000000000000000000000000000000000000000":
        print("\n  ❌ PROBLEM: EOA is NOT registered in the Gnosis Safe factory!")
        print("  Fix: Go to polymarket.com, connect your wallet, and complete onboarding.")
        print("  This creates the on-chain EOA → Safe mapping required for orders.")
    elif FUNDER and registered_safe.lower() == FUNDER.lower():
        print("\n  ✅ CORRECT: EOA is registered and Safe matches POLY_FUNDER_ADDRESS")
        print("  The 'invalid signature' is caused by something else.")
    else:
        print(f"\n  ⚠️  MISMATCH!")
        print(f"  On-chain Safe:      {registered_safe}")
        print(f"  POLY_FUNDER_ADDRESS: {FUNDER}")
        print(f"  Fix: Update POLY_FUNDER_ADDRESS={registered_safe}")
        print(f"  (The .env has the wrong proxy wallet address)")
except Exception as e:
    print(f"  ERROR calling contract: {e}")

print(f"\nChecking NegRisk Adapter Safe Factory: {NEG_RISK_ADAPTER}")
try:
    contract2 = w3.eth.contract(address=Web3.to_checksum_address(NEG_RISK_ADAPTER),
                                abi=GET_SAFE_ADDRESS_ABI)
    registered_safe2 = contract2.functions.getSafeAddress(
        Web3.to_checksum_address(EOA)
    ).call()
    print(f"  getSafeAddress({EOA[:10]}...) → {registered_safe2}")
except Exception as e:
    print(f"  (NegRisk adapter check failed: {e})")
