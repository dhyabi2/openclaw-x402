"""
OpenClaw x402 shared configuration.

All contract addresses are for Base mainnet (eip155:8453).
Prices are in USDC atomic units (6 decimals): 1 USDC = 1,000,000.
"""

import os

# --- x402 Constants ---
X402_NETWORK = "eip155:8453"  # Base mainnet (CAIP-2)
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # Native USDC on Base
WRTC_BASE = "0x5683C10596AaA09AD7F4eF13CAB94b9b74A669c6"  # wRTC on Base
AERODROME_POOL = "0x4C2A0b915279f0C22EA766D58F9B815Ded2d2A3F"  # wRTC/WETH

# --- Facilitator ---
FACILITATOR_URL = "https://x402-facilitator.cdp.coinbase.com"

# --- CDP Credentials ---
CDP_API_KEY_NAME = os.environ.get("CDP_API_KEY_NAME", "")
CDP_API_KEY_PRIVATE_KEY = os.environ.get("CDP_API_KEY_PRIVATE_KEY", "")

# --- Nano (XNO) rail ---
# A nano_ address the operator controls. When set, routes that opt in with a
# `nano_price` also carry a nano:mainnet/XNO accept and settle from a verified
# XNO send (see nano.py). Leave empty to keep the rail entirely off.
NANO_TREASURY = os.environ.get("NANO_TREASURY", "").strip()
# Require the send to be cemented (final, unrollbackable) before serving work.
NANO_REQUIRE_CEMENTED = os.environ.get("NANO_REQUIRE_CEMENTED", "").lower() in ("1", "true", "yes")


# --- Swap Info ---
SWAP_INFO = {
    "wrtc_contract": WRTC_BASE,
    "usdc_contract": USDC_BASE,
    "aerodrome_pool": AERODROME_POOL,
    "swap_url": f"https://aerodrome.finance/swap?from={USDC_BASE}&to={WRTC_BASE}",
    "network": "Base (eip155:8453)",
    "reference_price_usd": 0.10,
}


def is_free(price_str):
    """Check if a price is $0 (free mode)."""
    return price_str in ("0", "")


def has_cdp_credentials():
    """Check if CDP API credentials are configured."""
    return bool(CDP_API_KEY_NAME and CDP_API_KEY_PRIVATE_KEY)
