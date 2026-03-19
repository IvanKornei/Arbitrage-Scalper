"""
tools/generate_poly_creds.py – Generate Polymarket CLOB API credentials.

Run once after setting POLY_PRIVATE_KEY in .env:
    python tools/generate_poly_creds.py

Output: POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE
Copy these values into your .env file.

Requires: pip install py-clob-client
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

CHAIN_ID  = 137   # Polygon mainnet
CLOB_HOST = "https://clob.polymarket.com"


def main() -> None:
    private_key = os.getenv("POLY_PRIVATE_KEY", "")
    if not private_key or private_key.startswith("0x_your"):
        print("ERROR: Set POLY_PRIVATE_KEY in .env first", file=sys.stderr)
        sys.exit(1)

    funder = os.getenv("POLY_FUNDER_ADDRESS", "")

    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        print("ERROR: pip install py-clob-client", file=sys.stderr)
        sys.exit(1)

    if funder:
        print(f"Using proxy wallet (funder): {funder}")
        client = ClobClient(
            host=CLOB_HOST,
            key=private_key,
            chain_id=CHAIN_ID,
            signature_type=2,  # POLY_GNOSIS_SAFE
            funder=funder,
        )
    else:
        print("No POLY_FUNDER_ADDRESS found, using EOA signing (signature_type=0)")
        client = ClobClient(host=CLOB_HOST, key=private_key, chain_id=CHAIN_ID)

    creds  = client.create_or_derive_api_creds()

    print("\n── Polymarket CLOB Credentials ─────────────────────────────")
    print(f"POLY_API_KEY={creds.api_key}")
    print(f"POLY_API_SECRET={creds.api_secret}")
    print(f"POLY_API_PASSPHRASE={creds.api_passphrase}")
    print("\nCopy these lines into your .env file.")


if __name__ == "__main__":
    main()
