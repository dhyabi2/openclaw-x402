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
import secrets
import time
from decimal import Decimal

from flask import jsonify, request

from .config import (
    X402_NETWORK, USDC_BASE, FACILITATOR_URL, SWAP_INFO,
    is_free, has_cdp_credentials,
    NANO_TREASURY, NANO_REQUIRE_CEMENTED, NANO_CHALLENGE_TTL,
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
            db.execute("""PRAGMA foreign_keys=ON""")
            db.execute("""
                CREATE TABLE IF NOT EXISTS x402_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payer_address TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    amount_usdc TEXT NOT NULL,
                    tx_hash TEXT,
                    network TEXT DEFAULT 'eip155:8453',
                    currency TEXT DEFAULT 'USDC',
                    description TEXT,
                    created_at REAL NOT NULL
                )
            """)
            # Nano spent-hash ledger: a block hash may be consumed exactly once.
            # The UNIQUE constraint is what defeats replay - a second attempt
            # to serve the same send hash fails the INSERT and is refused.
            db.execute("""
                CREATE TABLE IF NOT EXISTS nano_spent (
                    send_hash TEXT PRIMARY KEY,
                    consumed_at REAL NOT NULL,
                    path TEXT NOT NULL
                )
            """)
            # Issued (unconsumed) challenges, each carrying a unique tagged raw
            # amount so a send is bound to the exact challenge it was issued for.
            db.execute("""
                CREATE TABLE IF NOT EXISTS nano_challenges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL,
                    amount_raw TEXT NOT NULL,
                    expires_at REAL NOT NULL
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
                    # Only log a warning when the request actually presented an
                    # X-NANO-PAYMENT header and was refused. A normal 402 with no
                    # header is the expected first round of the handshake, not
                    # noise worth a WARNING on every miss.
                    nano_header_present = bool(
                        request.headers.get("X-NANO-PAYMENT", "").strip()
                    )
                    if reason and nano_header_present:
                        log.warning(
                            "Rejected Nano payment for %s: %s", request.path, reason
                        )

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
        optional cementing) *and* bound to a challenge this route issued: the
        send must be for exactly the tagged amount of a live challenge for this
        path, and the hash must not have been spent before (atomic UNIQUE
        insert). The request is served only when all of that holds; anything
        else - no header, a malformed hash, an unreadable block, an underpaid,
        misaddressed or already-spent send - is refused (fail closed).
        """
        if not NANO_TREASURY:
            return False, "nano rail not configured"
        from .nano import verify_send, valid_block_hash

        block_hash = request.headers.get("X-NANO-PAYMENT", "").strip()
        if not block_hash:
            return False, "no X-NANO-PAYMENT block hash"
        if not valid_block_hash(block_hash):
            # Malformed header: refuse before spending any RPC on it. This also
            # keeps the independent-read amplification cost away from junk input.
            return False, "malformed block hash (not 64 hex)"

        # price is an XNO decimal string; convert to raw for the challenge.
        base_amount_raw = str(int(Decimal(str(nano_price)) * (Decimal(10) ** 30)))

        # Bound the send to the challenge this route issued. The 402 carried a
        # unique tagged amount; the send MUST pay exactly that. This is what
        # keeps a third-party send to the treasury unclaimable and stops a
        # payment issued for one route settling another.
        if self.db_func:
            expected = self._active_challenge_amount(path)
            if expected is None:
                return False, "no live challenge issued for this route"
        else:
            # No durable store => cannot bind or consume safely. Fail closed so
            # a send can never be replayed on this rail.
            return False, "nano rail requires a payment store (db_func) to bind and consume sends"

        try:
            receipt = verify_send(
                block_hash, NANO_TREASURY, base_amount_raw,
                require_cemented=NANO_REQUIRE_CEMENTED,
                exact_amount=expected,
            )
        except Exception as e:  # node unreachable -> uncertainty -> fail closed
            return False, "nano verify error: %s" % e

        if receipt.get("settled") is not True:
            return False, "; ".join(receipt.get("reasons", [])) or "nano payment not settled"

        # Atomically consume the send hash. If it was already spent (or the
        # store is unavailable), refuse - a single send may not pay for more
        # than one served request, and a hash read from the treasury's own
        # public history may not be replayed by anyone.
        if not self._consume_send(block_hash, path):
            return False, "send hash already spent"

        # Accounting: the payer is the SENDER of the block, never the treasury.
        sender = receipt.get("proof", {}).get("from") or receipt.get("proof", {}).get("to", "")
        self._log_payment(
            sender,
            path,
            receipt.get("amount_xno", nano_price),
            block_hash,
            description + " (nano)",
            currency="XNO",
        )
        return True, None

    def _issue_challenge(self, path, base_amount_raw):
        """Issue a Nano challenge bound to this route and return its tagged amount.

        Nano has no memo field, so the send is bound to the challenge by giving
        each challenge a *unique* raw amount: the quoted price plus a random
        dust tag in the low-order raw. Only a send of exactly that amount
        satisfies the challenge, so a third-party send to the treasury at the
        plain price can never be claimed, and a payer cannot reuse a payment
        issued for a different route. The challenge expires so stale rows do not
        accumulate. Returns (challenge_id, tagged_amount_raw).
        """
        now = time.time()
        # A random dust tag in [0, 10**18) raw keeps the exact amount unique
        # while staying astronomically below the price (a nano_price is normally
        # >= 0.00001 XNO = 10**25 raw), so the tag never confuses the price.
        dust = secrets.randbelow(10 ** 18)
        tagged = int(base_amount_raw) + dust
        db = self.db_func() if self.db_func else None
        challenge_id = None
        if db is not None:
            try:
                cur = db.execute(
                    "INSERT INTO nano_challenges (path, amount_raw, expires_at) "
                    "VALUES (?, ?, ?)",
                    (path, str(tagged), now + NANO_CHALLENGE_TTL),
                )
                db.commit()
                challenge_id = cur.lastrowid
            except Exception as e:
                log.warning("Failed to record nano challenge: %s", e)
        return challenge_id, tagged

    def _consume_send(self, send_hash, path):
        """Atomically mark a send hash as spent. Returns True if it was free.

        The PRIMARY KEY on nano_spent.send_hash means two concurrent requests
        bearing the same send hash race the INSERT; exactly one wins, the other
        gets IntegrityError and is refused. This is what stops a single send
        from paying for unlimited requests, and what stops anyone replaying a
        hash they read from the treasury's public history.
        """
        if not self.db_func:
            # No DB configured: fail safe by refusing (a middleware with no
            # durable store cannot prove a hash was not already spent).
            return False
        try:
            db = self.db_func()
            db.execute(
                "INSERT INTO nano_spent (send_hash, consumed_at, path) VALUES (?, ?, ?)",
                (send_hash, time.time(), path),
            )
            db.commit()
            return True
        except Exception:
            # UNIQUE violation (or any store error) => already spent / unsafe.
            return False

    def _active_challenge_amount(self, path):
        """Return the tagged raw amount of a live unconsumed challenge for path.

        A challenge is 'live' when it has not expired. To keep the binding
        unambiguous we accept a send only when it matches the *most recent* live
        challenge for this route; older overlapping challenges are treated as
        superseded so an old quote cannot be replayed after a newer one was
        issued. Returns an int or None.
        """
        if not self.db_func:
            return None
        try:
            db = self.db_func()
            now = time.time()
            row = db.execute(
                "SELECT amount_raw FROM nano_challenges "
                "WHERE path = ? AND expires_at > ? "
                "ORDER BY id DESC LIMIT 1",
                (path, now),
            ).fetchone()
            # Expire stale rows for this path so the table does not grow forever.
            db.execute(
                "DELETE FROM nano_challenges WHERE path = ? AND expires_at <= ?",
                (path, now),
            )
            db.commit()
            return int(row[0]) if row else None
        except Exception as e:
            log.warning("Failed to read nano challenge: %s", e)
            return None

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
            base_raw = int(Decimal(str(nano_price)) * (Decimal(10) ** 30))
            # Issue a challenge the payer must satisfy: a unique tagged raw
            # amount stored server-side. Even without a DB we bind by amount and
            # fail safe (no consumption) rather than serve unproven work.
            _, tagged = self._issue_challenge(request.path, base_raw)
            x402["nano"] = build_challenge(
                request.url, NANO_TREASURY,
                str(tagged),
                description=description + " (XNO)",
            )["resources"][0]
            if self.db_func:
                # Surface the challenge id so the payer can prove which challenge
                # the send satisfies; bind is enforced in _verify_nano_payment.
                x402["nano"]["challenge"] = request.path
        return jsonify({
            "error": "Payment Required",
            "x402": x402,
        }), 402


    def _log_payment(self, payer, endpoint, amount, tx_hash, description, currency="USDC"):
        """Log a payment to the database."""
        if not self.db_func:
            return
        try:
            db = self.db_func()
            db.execute(
                "INSERT INTO x402_payments (payer_address, endpoint, amount_usdc, tx_hash, currency, description, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (payer, endpoint, amount, tx_hash, currency, description, time.time()),
            )
            db.commit()
        except Exception as e:
            log.warning("Failed to log x402 payment: %s", e)
