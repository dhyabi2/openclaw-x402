"""
Nano (XNO) settlement leg for openclaw-x402.

A feeless rail that peels beside the USDC/Base and RTC/RustChain entries a
merchant already carries. A Nano payment is the payer's own send; the merchant's
job is only to verify it against a public node. There is nothing to relay, no gas
to pay, no nonce, and no facilitator - so "cold wallet" is not a state a Nano
merchant has to engineer around, and this leg needs no signing, no wallet action
and no new dependency (stdlib only).

How a request settles on this rail
----------------------------------
1. The middleware 402 challenge (via :func:`build_challenge`) carries a
   `nano:mainnet` / `XNO` accept with a `payTo` the operator configured.
2. The payer sends that raw amount to `payTo` and returns the **send block hash**
   in an `X-NANO-PAYMENT` header.
3. :func:`verify_send` reads that block from a public Nano RPC and marks it
   `settled` only when every field is proven from the chain, never from a client
   claim:
     * the block IS a confirmed state send,
     * its destination (link_as_account) is the configured `payTo`,
     * its amount is >= the quoted amount (the block carries the amount actually
       credited; there is no fee field to deduct),
     * a receive crediting that exact amount exists on `payTo` (the merchant
       reads its OWN account, so this needs no extra trust),
     * and - when the operator requires finality - the send is cemented.

Honest limits, same as the pinned x402-nano-verify suite this ports:
  * A send whose block cannot be read is `settled: null` - uncertainty, not
    payment and not absence. The caller must fail closed on anything that is not
    exactly `settled is True`.
  * The verifier depends on a public node; an independent second read of the
    same block is cheap and is the honest cross-check. Endpoints are
    configurable via `NANO_RPC_URLS` so the operator can point at its own node.
  * A send is not an opened account, and a receive is not work delivered.
  * **Replay and binding are the caller's obligation** (the middleware in this
    repo implements both): a send may be served only once (the block hash is
    recorded atomically as spent), and it must match the exact tagged amount of
    a live challenge the merchant issued for that route - Nano has no memo, so
    the unique per-challenge amount is what binds a send to the one job it pays
    for and stops a third-party send to the treasury being claimed.
"""

from decimal import Decimal
import os
import re

RAW_PER_XNO = Decimal(10) ** 30  # 1 XNO == 10**30 raw; 0.00001 XNO == 10**25 raw

# A nano block hash is exactly 64 lowercase hex characters. Anything else is a
# malformed header and is rejected before any RPC cost is spent on it.
_BLOCK_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

# Operational RPC endpoints, configurable so an operator can point the verifier
# at its own node (or a pair) rather than trusting a fixed public third party.
# Override with NANO_RPC_URLS (comma-separated). The default list is a usable
# fallback only; operators who care about finality or censorship resistance
# should set it to the node(s) they run themselves.
DEFAULT_RPC_ENDPOINTS = [
    "https://rainstorm.city/api",
    "https://rpc.nano.to",
]


def rpc_endpoints():
    """The configured Nano RPC endpoints (env NANO_RPC_URLS, else the default)."""
    env = os.environ.get("NANO_RPC_URLS", "").strip()
    if env:
        return [u.strip() for u in env.split(",") if u.strip()]
    return list(DEFAULT_RPC_ENDPOINTS)


def valid_block_hash(s) -> bool:
    """True only for a well-formed 64-hex Nano block hash."""
    return bool(s) and bool(_BLOCK_HASH_RE.match(s))

# Swappable transport so tests exercise the verification logic without a live
# node. Override with `set_rpc(fn)` or `patch("openclaw_x402.nano.rpc_call")`.
_rpc_fn = None

# Small verified-send cache: a block hash proven once need not be re-proven at
# 100 RPC calls per request on the next hit. Bounded; cleared on of a test rpc.
_verified_cache = {}

# Cache of (send_hash) -> (receive_hash, amount_raw|None) for linked-receive
# lookups, so a busy treasury is not re-scanned for the same send every request.
_linked_cache = {}


def clear_cache() -> None:
    _verified_cache.clear()
    _linked_cache.clear()


def set_rpc(fn) -> None:
    """Install a custom RPC transport (used by tests to avoid the live network).

    `fn(action: str, **params) -> dict` must return the node's JSON-RPC answer
    or raise on an unreachable node.
    """
    global _rpc_fn
    _rpc_fn = fn


def rpc_call(action: str, **params) -> dict:
    """The default transport: one JSON-RPC call to a public Nano node."""
    if _rpc_fn is not None:
        return _rpc_fn(action, **params)
    import json
    import urllib.request

    body = json.dumps({"action": action, **params}).encode()
    last = None
    for url in rpc_endpoints():
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json",
                         "User-Agent": "openclaw-x402/0.1"},
            )
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except Exception as e:
            last = e
            continue
    raise last if last else RuntimeError("no nano rpc endpoint answered")


def raw_to_xno(raw) -> str:
    """Render a raw amount as a decimal string of XNO (e.g. 0.00001)."""
    return format((Decimal(int(raw)) / RAW_PER_XNO).normalize(), "f")


def build_challenge(resource: str, pay_to: str, amount_raw, description=None,
                    x402_version: int = 2) -> dict:
    """The x402 body a feeless-rail seller returns with HTTP 402."""
    entry = {
        "scheme": "exact",
        "network": "nano:mainnet",
        "asset": "XNO",
        "amount": str(amount_raw),
        "payTo": pay_to,
    }
    resource_obj = {"url": resource, "accepts": [entry]}
    if description:
        resource_obj["description"] = description
    return {"x402Version": x402_version, "kind": "resource-server",
            "resources": [resource_obj], "error": "Payment Required"}


def find_linked_receive(pay_to: str, send_hash: str, scan: int = 100):
    """Scan the merchant's own recent history for the receive crediting this send.

    Returns (receive_hash, amount_raw) or (None, None).
    """
    # Cache a proven (send_hash, receive_hash, amount) so a busy treasury is not
    # re-scanned (and re-fetched at up to `scan` block_info calls) for the same
    # send on every request.
    hit = _linked_cache.get(send_hash)
    if hit is not None:
        return hit
    r = rpc_call("account_history", account=pay_to, count=str(scan), raw=True)
    rows = r.get("history", [])
    if not rows:
        r = rpc_call("account_history", account=pay_to, count=str(scan))
        rows = r.get("history", [])
    for b in rows:
        h = b.get("hash")
        if not h:
            continue
        try:
            bi = rpc_call("block_info", hash=h, json_block="true")
        except Exception:
            continue
        if bi.get("subtype") != "receive":
            continue
        link = bi.get("contents", {}).get("link")
        if link == send_hash:
            result = (h, str(bi.get("amount") or b.get("amount")))
            _linked_cache[send_hash] = result
            return result
    _linked_cache[send_hash] = (None, None)
    return None, None


def verify_send(send_hash: str, pay_to: str, amount_raw, require_cemented: bool = False,
                check_receive: bool = True, exact_amount=None) -> dict:
    """Settle only from a send validated against the operator's own challenge.

    `receipt["settled"]` is True only when every field is proven; False on a
    provable rejection (wrong destination, underpaid, not a send); None when the
    send cannot be read or credited yet (uncertainty - never treat as payment).

    When `exact_amount` is given, the send's raw amount must equal it exactly
    (the challenge binding): a send for the plain price, or for a different
    tagged challenge, is refused. This is how a send is bound to the one
    challenge that issued it, since Nano has no memo field.
    """
    receipt = {
        "settled": False,
        "network": "nano:mainnet",
        "asset": "XNO",
        "proof": {"send_block": send_hash},
        "fee": "0",
        "fee_note": "no fee exists on this rail; payer debit == payee credit",
        "reasons": [],
    }
    try:
        info = rpc_call("block_info", hash=send_hash, json_block="true")
    except Exception as e:
        receipt["reasons"].append("rpc_error: %s" % e)
        receipt["settled"] = None  # uncertainty, not absence
        return receipt

    if "contents" not in info:
        receipt["reasons"].append("no_such_block")
        return receipt

    c = info["contents"]
    if c.get("type") != "state":
        receipt["reasons"].append("not_a_state_block")
        return receipt
    if info.get("subtype") != "send":
        receipt["reasons"].append("not_a_send")
        return receipt

    dest = c.get("link_as_account")
    receipt["proof"]["to"] = dest
    # The payer is the block's sender account (the account that signed the
    # send), never the recipient/treasury. Record it for correct accounting.
    receipt["proof"]["from"] = c.get("account") or ""
    if dest != pay_to:
        receipt["reasons"].append("destination_mismatch")
        return receipt

    amount = int(info.get("amount"))
    receipt["amount_raw"] = str(amount)
    receipt["amount_xno"] = raw_to_xno(amount)
    receipt["proof"]["confirmed"] = bool(info.get("confirmed"))

    if exact_amount is not None and amount != int(exact_amount):
        # Challenge binding: the send must pay the exact tagged amount this
        # route issued. A send for the plain price, a survivor of another
        # challenge, or a third-party send to the treasury cannot be claimed.
        receipt["reasons"].append("amount_mismatch_exact")
        return receipt

    if amount < int(amount_raw):
        receipt["reasons"].append("underpaid")
        return receipt

    # Finality: the sender account's confirmation height vs this block's height.
    try:
        ai = rpc_call("account_info", account=c["account"])
        cemented = int(info.get("height") or 0) <= int(ai.get("confirmation_height") or 0)
        receipt["proof"]["cemented"] = cemented
    except Exception:
        receipt["proof"]["cemented"] = None

    if check_receive:
        rh, ramt = find_linked_receive(pay_to, send_hash)
        if rh:
            receipt["proof"]["receive_block"] = rh
            receipt["received_raw"] = ramt
            if ramt is not None and int(ramt) != amount:
                receipt["reasons"].append("received_amount_differs")
                return receipt
        else:
            receipt["reasons"].append("no_linked_receive_found")
            receipt["settled"] = None  # sent, not yet credited: uncertainty
            return receipt

    if not info.get("confirmed"):
        receipt["reasons"].append("send_not_confirmed")
        return receipt
    if require_cemented and not receipt["proof"].get("cemented"):
        receipt["reasons"].append("send_not_cemented")
        return receipt

    receipt["settled"] = True
    return receipt
