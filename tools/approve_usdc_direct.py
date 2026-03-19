"""
tools/approve_usdc_direct.py – Directly approve USDC allowances for Polymarket
by executing transactions through the Gnosis Safe contract.

Why this is needed:
  update_balance_allowance() tells Polymarket's relayer to set allowances, but
  for Gnosis Safe wallets the relayer returns OK without actually executing the
  on-chain transaction. We must do it ourselves via Safe.execTransaction().

What this does:
  1. Calls USDC.approve(CTFExchange, max_uint256) as the Safe
  2. Calls USDC.approve(NegRiskAdapter, max_uint256) as the Safe

Requirements:
  pip install web3

The EOA must have a small amount of MATIC for gas (~0.01 MATIC = ~$0.005).

Usage:
    python tools/approve_usdc_direct.py
"""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
    from eth_account import Account
    from eth_account.messages import encode_defunct
except ImportError:
    sys.exit("Run: pip install web3")

from dotenv import dotenv_values
env = dotenv_values()

PRIVATE_KEY = env.get("POLY_PRIVATE_KEY", "")
FUNDER      = env.get("POLY_FUNDER_ADDRESS", "")

if not PRIVATE_KEY or not FUNDER:
    sys.exit("FATAL: POLY_PRIVATE_KEY and POLY_FUNDER_ADDRESS required in .env")

EOA = Account.from_key(PRIVATE_KEY).address
SAFE_ADDR = Web3.to_checksum_address(FUNDER)

# Polygon contracts
USDC_ADDR        = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
CTF_EXCHANGE     = Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")
NEG_RISK_ADAPTER = Web3.to_checksum_address("0xC5d563A36AE78145C45a50134d48A1215220f80a")
NEG_RISK_EXCHANGE= Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")
MAX_APPROVAL     = (1 << 256) - 1  # uint256 max

POLYGON_RPCS = [
    "https://1rpc.io/matic",
    "https://polygon-rpc.com",
    "https://polygon.llamarpc.com",
    "https://polygon-bor-rpc.publicnode.com",
]

# ── ABIs ────────────────────────────────────────────────────────────────────

ERC20_ABI = [
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
     "name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],
     "name":"allowance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"account","type":"address"}],
     "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
]

SAFE_ABI = [
    # execTransaction
    {"inputs":[
        {"name":"to","type":"address"},
        {"name":"value","type":"uint256"},
        {"name":"data","type":"bytes"},
        {"name":"operation","type":"uint8"},
        {"name":"safeTxGas","type":"uint256"},
        {"name":"baseGas","type":"uint256"},
        {"name":"gasPrice","type":"uint256"},
        {"name":"gasToken","type":"address"},
        {"name":"refundReceiver","type":"address"},
        {"name":"signatures","type":"bytes"},
    ],
     "name":"execTransaction","outputs":[{"name":"success","type":"bool"}],
     "stateMutability":"payable","type":"function"},
    # getTransactionHash
    {"inputs":[
        {"name":"to","type":"address"},
        {"name":"value","type":"uint256"},
        {"name":"data","type":"bytes"},
        {"name":"operation","type":"uint8"},
        {"name":"safeTxGas","type":"uint256"},
        {"name":"baseGas","type":"uint256"},
        {"name":"gasPrice","type":"uint256"},
        {"name":"gasToken","type":"address"},
        {"name":"refundReceiver","type":"address"},
        {"name":"_nonce","type":"uint256"},
    ],
     "name":"getTransactionHash","outputs":[{"name":"","type":"bytes32"}],
     "stateMutability":"view","type":"function"},
    # nonce
    {"inputs":[],"name":"nonce","outputs":[{"name":"","type":"uint256"}],
     "stateMutability":"view","type":"function"},
    # getOwners
    {"inputs":[],"name":"getOwners","outputs":[{"name":"","type":"address[]"}],
     "stateMutability":"view","type":"function"},
]

# ── Connect ──────────────────────────────────────────────────────────────────

print(f"EOA:         {EOA}")
print(f"Safe wallet: {SAFE_ADDR}")

w3 = None
for rpc in POLYGON_RPCS:
    try:
        candidate = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
        candidate.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        if candidate.is_connected():
            print(f"Connected:   {rpc}  (block {candidate.eth.block_number})")
            w3 = candidate
            break
    except Exception:
        pass

if not w3:
    sys.exit("ERROR: Cannot reach any Polygon RPC")

# ── Pre-flight checks ────────────────────────────────────────────────────────

matic_balance = w3.eth.get_balance(Web3.to_checksum_address(EOA))
print(f"\nEOA MATIC:   {w3.from_wei(matic_balance, 'ether'):.6f} MATIC")
if matic_balance < w3.to_wei(0.005, "ether"):
    print("WARNING: Very low MATIC. You may need at least 0.01 MATIC for gas.")

usdc = w3.eth.contract(address=USDC_ADDR, abi=ERC20_ABI)
safe = w3.eth.contract(address=SAFE_ADDR, abi=SAFE_ABI)

safe_usdc_bal = usdc.functions.balanceOf(SAFE_ADDR).call()
print(f"Safe USDC:   {safe_usdc_bal / 1e6:.2f} USDC")

owners = safe.functions.getOwners().call()
print(f"Safe owners: {owners}")
if Web3.to_checksum_address(EOA) not in [Web3.to_checksum_address(o) for o in owners]:
    sys.exit(f"ERROR: EOA {EOA} is not an owner of Safe {SAFE_ADDR}")

print()
# Show current allowances
for label, spender in [("CTF Exchange", CTF_EXCHANGE),
                        ("NegRisk Adapter", NEG_RISK_ADAPTER),
                        ("NegRisk Exchange", NEG_RISK_EXCHANGE)]:
    a = usdc.functions.allowance(SAFE_ADDR, spender).call()
    status = "✅ approved" if a > 0 else "❌ needs approval"
    print(f"  {label}: allowance={a}  {status}")

# ── Execute Safe transaction ─────────────────────────────────────────────────

def exec_safe_approve(spender_addr: str, spender_label: str) -> bool:
    """Approve spender to spend Safe's USDC by executing a Safe transaction."""
    # Encode USDC.approve(spender, max_uint256)
    approve_data = usdc.encode_abi("approve", args=[spender_addr, MAX_APPROVAL])

    safe_nonce = safe.functions.nonce().call()
    approve_bytes = bytes.fromhex(approve_data[2:])

    tx_hash = safe.functions.getTransactionHash(
        USDC_ADDR,      # to: USDC contract
        0,              # value: 0 ETH
        approve_bytes,  # data: approve() calldata
        0,              # operation: CALL
        0, 0, 0,
        "0x0000000000000000000000000000000000000000",
        "0x0000000000000000000000000000000000000000",
        safe_nonce,
    ).call()

    # Sign the hash (EOA signs the Safe tx hash directly, v += 4 for Safe compat)
    account = Account.from_key(PRIVATE_KEY)
    sig = account.signHash(tx_hash)
    # Safe expects r+s+v where v is 27 or 28
    r = sig.r.to_bytes(32, "big")
    s = sig.s.to_bytes(32, "big")
    v = bytes([sig.v])
    signatures = r + s + v

    print(f"\nApproving {spender_label}...")
    try:
        tx = safe.functions.execTransaction(
            USDC_ADDR,
            0,
            approve_bytes,
            0, 0, 0, 0,
            "0x0000000000000000000000000000000000000000",
            "0x0000000000000000000000000000000000000000",
            signatures,
        ).build_transaction({
            "from": Web3.to_checksum_address(EOA),
            "nonce": w3.eth.get_transaction_count(Web3.to_checksum_address(EOA)),
            "gas": 150_000,
            "maxFeePerGas": w3.to_wei(50, "gwei"),
            "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
            "chainId": 137,
        })
        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash_sent = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        print(f"  TX sent: 0x{tx_hash_sent.hex()}")
        print(f"  Waiting for confirmation...")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash_sent, timeout=60)
        if receipt.status == 1:
            allowance = usdc.functions.allowance(SAFE_ADDR, spender_addr).call()
            print(f"  ✅ SUCCESS! New allowance: {allowance}")
            return True
        else:
            print(f"  ❌ Transaction REVERTED")
            return False
    except Exception as e:
        print(f"  ERROR: {e}")
        return False

print("\n" + "="*60)
print("Setting USDC approvals for all Polymarket contracts...")

exec_safe_approve(CTF_EXCHANGE,      "CTF Exchange")
exec_safe_approve(NEG_RISK_ADAPTER,  "NegRisk Adapter")
exec_safe_approve(NEG_RISK_EXCHANGE, "NegRisk Exchange")

print("\n" + "="*60)
print("Final allowances:")
for label, spender in [("CTF Exchange", CTF_EXCHANGE),
                        ("NegRisk Adapter", NEG_RISK_ADAPTER),
                        ("NegRisk Exchange", NEG_RISK_EXCHANGE)]:
    a = usdc.functions.allowance(SAFE_ADDR, spender).call()
    status = "✅" if a > 0 else "❌ STILL ZERO"
    print(f"  {label}: {status} (allowance={a})")

print("\nDone. Now run: python polymarket_main.py")
