"""
OpenClaw x402 Flask Middleware.

Drop-in x402 payment enforcement for any Flask API.
Supports free mode ($0 pricing), real USDC payments via Coinbase facilitator,
and graceful degradation when x402 libraries are not installed.

Usage:
    from openclaw_x402 import X402Middleware

    x402 = X402Middleware(app, treasury="0xYourAddress")

    @app.route("/api/premium/data")
    @x402.premium(price="10000", description="Premium data export")
    def premium_data():
        return jsonify({"data": "..."})
"""

import functools
import logging
import time
from decimal import Decimal

from flask import jsonify, request

from .config import (
    X402_NETWORK, USDC_BASE, FACILITATOR_URL, SWAP_INFO,
    is_free, has_cdp_credentials,
    NANO_TREASURY, NANO_REQUIRE_CEMENTED,
)

log = logging.getLogger("openclaw_x402")

# Try importing x402 Flask helpers (optional dependency)
try:
    from x402.flask import x402_middleware as _x402_mw
    X402_LIB_AVAILABLE = True
except ImportError:
    X402_LIB_AVAILABLE = False
    log.info("x402 Flask library not installed — running in manual mode")


class X402Middleware:
    """
    x402 payment middleware for Flask.

    Args:
        app: Flask application (or None, call init_app later)
        treasury: Base chain address to receive payments
        db_func: Optional callable returning a DB connection (for payment logging)
    """

    def __init__(self, app=None, treasury="", db_func=None):
        self.treasury = treasury
        self.db_func = db_func
        self._payment_table_created = False
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        """Register x402 routes and middleware on the Flask app."""
        self.app = app
        self._ensure_payment_table()
        self._register_routes(app)
        log.info(
            "OpenClaw x402 initialized: treasury=%s, x402_lib=%s",
            self.treasury[:10] + "..." if self.treasury else "NOT SET",
            X402_LIB_AVAILABLE,
        )

    def _ensure_payment_table(self):
        """Create x402_payments table if DB function is provided."""
        if not self.db_func or self._payment_table_created:
            return
        try:
            db = self.db_func()
            db.execute("""
                CREATE TABLE IF NOT EXISTS x402_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payer_address TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    amount_usdc TEXT NOT NULL,
                    tx_hash TEXT,
                    network TEXT DEFAULT 'eip155:8453',
                    description TEXT,
                    created_at REAL NOT NULL
                )
            """)
            db.commit()
            self._payment_table_created = True
        except Exception as e:
            log.warning("Failed to create x402_payments table: %s", e)

    def _register_routes(self, app):
        """Register x402 status endpoint."""

        @app.route("/api/x402/status")
        def x402_status():
            return jsonify({
                "x402_enabled": True,
                "x402_lib": X402_LIB_AVAILABLE,
                "cdp_configured": has_cdp_credentials(),
                "network": X402_NETWORK,
                "facilitator": FACILITATOR_URL,
                "treasury": self.treasury,
                "swap_info": SWAP_INFO,
            })

    def premium(self, price="0", description="Premium endpoint", nano_price=None):
        """
        Decorator to enforce x402 payment on a route.

        If price is "0", requests pass through freely (proving the flow).
        If price is non-zero:
          - With x402 lib: uses Coinbase facilitator for verification
          - Without x402 lib: returns 402 with manual payment instructions

        When `nano_price` (an XNO amount, e.g. "0.0005") is given AND NANO_TREASURY
        is configured, the route also accepts a Nano (XNO) settlement: the 402
        carries a nano:mainnet accept, and a request with an `X-NANO-PAYMENT`
        header (the send block hash) is served only when the send verifies on
        chain (see openclaw_x402.nano.verify_send). Any other request still
        fails closed with a 402.

        Args:
            price: USDC atomic units (6 decimals). "10000" = $0.01
            description: Human-readable endpoint description
            nano_price: optional XNO amount string to also accept on the Nano rail.
        """
        nano_accept = bool(nano_price and NANO_TREASURY)

        def decorator(f):
            @functools.wraps(f)
            def wrapper(*args, **kwargs):
                # Free mode — pass through
                if is_free(price):
                    return f(*args, **kwargs)

                # Nano rail first: a verified XNO send settles the request
                # directly, with nothing to relay and fail-closed otherwise.
                if nano_accept:
                    sent, reason = self._verify_nano_payment(
                        nano_price, description, request.path
                    )
                    if sent:
                        return f(*args, **kwargs)
                    if reason:
                        log.warning("Rejected Nano payment for %s: %s", request.path, reason)

                # Check for x402 payment header
                payment_header = request.headers.get("X-PAYMENT", "").strip()

                # SECURITY (fail closed): the facilitator verification path is
                # not actually wired here — the imported x402 middleware is never
                # invoked — so an X-PAYMENT header must NEVER be trusted on its
                # own. Previously, with the x402 lib installed, ANY non-empty
                # X-PAYMENT header was logged as "x402-verified" and granted free
                # access. Until real on-chain/facilitator settlement verification
                # is implemented, every unverified request gets a 402.
                if payment_header:
                    log.warning(
                        "Rejected unverified X-PAYMENT header for %s "
                        "(facilitator verification not implemented; failing closed)",
                        request.path,
                    )
                return self._payment_required(price, description, nano_price)

            return wrapper
        return decorator

    def _verify_nano_payment(self, nano_price, description, path):
        """Return (accepted, reason) for an X-NANO-PAYMENT attempt.

        Works only when NANO_TREASURY is set. The header carries the send block
        hash; the send is verified on chain (amount, destination, receipt,
        optional cementing) and the request is served only when settled is True.
        Anything else — no header, an unreadable block, an underpaid or
        misaddressed send — is refused (fail closed).
        """
        if not NANO_TREASURY:
            return False, "nano rail not configured"
        from .nano import verify_send

        block_hash = request.headers.get("X-NANO-PAYMENT", "").strip()
        if not block_hash:
            return False, "no X-NANO-PAYMENT block hash"

        # price is an XNO decimal string; convert to raw for the challenge.
        amount_raw = str(int(Decimal(str(nano_price)) * (Decimal(10) ** 30)))
        try:
            receipt = verify_send(
                block_hash, NANO_TREASURY, amount_raw,
                require_cemented=NANO_REQUIRE_CEMENTED,
            )
        except Exception as e:  # node unreachable -> uncertainty -> fail closed
            return False, "nano verify error: %s" % e

        if receipt.get("settled") is True:
            self._log_payment(
                receipt.get("proof", {}).get("to", ""),
                path,
                receipt.get("amount_xno", nano_price),
                receipt.get("proof", {}).get("receive_block") or block_hash,
                description + " (nano)",
            )
            return True, None
        return False, "; ".join(receipt.get("reasons", [])) or "nano payment not settled"

    def _payment_required(self, price, description, nano_price=None):
        """Return HTTP 402 with x402 payment instructions."""
        x402 = {
            "version": "1",
            "network": X402_NETWORK,
            "asset": USDC_BASE,
            "payTo": self.treasury,
            "maxAmountRequired": price,
            "facilitator": FACILITATOR_URL,
            "resource": request.url,
            "description": description,
        }
        if nano_price and NANO_TREASURY:
            from .nano import build_challenge
            x402["nano"] = build_challenge(
                request.url, NANO_TREASURY,
                str(int(Decimal(str(nano_price)) * (Decimal(10) ** 30))),
                description=description + " (XNO)",
            )["resources"][0]
        return jsonify({
            "error": "Payment Required",
            "x402": x402,
        }), 402


    def _log_payment(self, payer, endpoint, amount, tx_hash, description):
        """Log a payment to the database."""
        if not self.db_func:
            return
        try:
            db = self.db_func()
            db.execute(
                "INSERT INTO x402_payments (payer_address, endpoint, amount_usdc, tx_hash, description, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (payer, endpoint, amount, tx_hash, description, time.time()),
            )
            db.commit()
        except Exception as e:
            log.warning("Failed to log x402 payment: %s", e)
