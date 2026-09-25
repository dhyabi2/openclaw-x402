"""Nano (XNO) settle-leg tests for the Flask middleware.

The verification logic is exercised against a MOCKED node (nano.set_rpc) so the
suite is deterministic and offline. Each test controls what block_info /
account_history return and asserts the middleware fails closed except on a fully
verified send.
"""

import unittest
from unittest import mock

from flask import Flask, jsonify

from openclaw_x402 import nano as nano_mod
from openclaw_x402 import middleware as middleware_module

SEND_HASH = "AAAA1111BBBB2222CCCC3333DDDD4444EEEE5555FFFF6666AAAABBBBCCCCDDDD"
PAY_TO = "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7"
AMOUNT_RAW = str(5 * 10 ** 29)  # 0.5 XNO (nano_price in the test app)
RECEIVE_HASH = "5555CCCCDDDD4444EEEE3333FFFF2222GGGG1111HHHH0000IIII9999JJJJ8888"


def make_valid_node(amount_raw=AMOUNT_RAW, pay_to=PAY_TO, cemented=True,
                    send_subtype="send", dest=None, confirmed=True,
                    has_receive=True):
    """Return an rpc transport that answers like a public Nano node for a valid send."""
    send_block = {
        "contents": {
            "type": "state",
            "account": "nano_3sender00000000000000000000000000000000000000000000000000",
            "link_as_account": dest if dest is not None else pay_to,
        },
        "subtype": send_subtype,
        "amount": str(amount_raw),
        "confirmed": confirmed,
        "height": 100,
    }
    receive_block = {
        "subtype": "receive",
        "contents": {"link": SEND_HASH},
        "amount": str(amount_raw),
    }

    def rpc(action, **params):
        if action == "block_info":
            h = params.get("hash")
            if h == SEND_HASH:
                return dict(send_block)
            if h == RECEIVE_HASH:
                return dict(receive_block)
            return {}
        if action == "account_info":
            return {"confirmation_height": 100 if cemented else 99}
        if action == "account_history":
            if has_receive:
                return {"history": [{"hash": RECEIVE_HASH, "amount": str(amount_raw)}]}
            return {"history": []}
        return {}
    return rpc


def make_app(nano_price="0.5", configure_nano=True, usdc_price="1000"):
    app = Flask(__name__)
    x402 = middleware_module.X402Middleware(app, treasury="0xdeadbeef")

    @app.route("/premium")
    @x402.premium(price=usdc_price, description="Premium endpoint",
                  nano_price=nano_price if configure_nano else None)
    def premium_endpoint():
        return jsonify({"ok": True})

    @app.route("/free")
    @x402.premium(price="0", description="Free endpoint",
                  nano_price=nano_price if configure_nano else None)
    def free_endpoint():
        return jsonify({"ok": True})

    return app


class NanoPaymentTests(unittest.TestCase):
    def setUp(self):
        nano_mod.set_rpc(None)
        self.addCleanup(lambda: nano_mod.set_rpc(None))
        self._nt = mock.patch.object(middleware_module, "NANO_TREASURY", PAY_TO)
        self._nt.start()
        self.addCleanup(self._nt.stop)

    def _client(self, configure_nano=True):
        app = make_app(configure_nano=configure_nano)
        return app.test_client()

    def test_valid_nano_send_is_accepted(self):
        nano_mod.set_rpc(make_valid_node())
        c = self._client()
        r = c.get("/premium", headers={"X-NANO-PAYMENT": SEND_HASH})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"ok": True})

    def test_no_header_fails_closed_even_when_rail_configured(self):
        nano_mod.set_rpc(make_valid_node())
        c = self._client()
        r = c.get("/premium")
        self.assertEqual(r.status_code, 402)

    def test_underpaid_send_is_refused(self):
        nano_mod.set_rpc(make_valid_node(amount_raw=str(1 * 10 ** 26)))  # 0.1 < 0.5
        c = self._client()
        r = c.get("/premium", headers={"X-NANO-PAYMENT": SEND_HASH})
        self.assertEqual(r.status_code, 402)

    def test_wrong_destination_is_refused(self):
        nano_mod.set_rpc(make_valid_node(dest="nano_3other000000000000000000000000000000000000000000000000"))
        c = self._client()
        r = c.get("/premium", headers={"X-NANO-PAYMENT": SEND_HASH})
        self.assertEqual(r.status_code, 402)

    def test_unreadable_block_is_refused_not_served(self):
        def broken(action, **params):
            raise RuntimeError("node down")
        nano_mod.set_rpc(broken)
        c = self._client()
        r = c.get("/premium", headers={"X-NANO-PAYMENT": SEND_HASH})
        self.assertEqual(r.status_code, 402)

    def test_no_linked_receive_is_refused_not_served(self):
        # send exists but the merchant's own account shows no crediting receive
        nano_mod.set_rpc(make_valid_node(has_receive=False))
        c = self._client()
        r = c.get("/premium", headers={"X-NANO-PAYMENT": SEND_HASH})
        self.assertEqual(r.status_code, 402)

    def test_valid_send_to_route_that_did_not_opt_in_is_refused(self):
        # nano_price not passed -> rail off for this route -> fail closed
        nano_mod.set_rpc(make_valid_node())
        c = self._client(configure_nano=False)
        r = c.get("/premium", headers={"X-NANO-PAYMENT": SEND_HASH})
        self.assertEqual(r.status_code, 402)

    def test_free_route_stays_accessible(self):
        nano_mod.set_rpc(make_valid_node())
        c = self._client()
        r = c.get("/free", headers={"X-NANO-PAYMENT": SEND_HASH})
        self.assertEqual(r.status_code, 200)

    def test_402_carries_nano_accept_when_configured(self):
        nano_mod.set_rpc(make_valid_node())
        c = self._client()
        r = c.get("/premium")
        body = r.get_json()
        self.assertEqual(r.status_code, 402)
        nano = body["x402"]["nano"]
        self.assertEqual(nano["accepts"][0]["network"], "nano:mainnet")
        self.assertEqual(nano["accepts"][0]["asset"], "XNO")
        self.assertEqual(nano["accepts"][0]["payTo"], PAY_TO)


class NanoRequireCementedTests(unittest.TestCase):
    """When NANO_REQUIRE_CEMENTED is on, an uncemented send must be refused."""

    def setUp(self):
        nano_mod.set_rpc(None)
        self.addCleanup(lambda: nano_mod.set_rpc(None))
        self._nt = mock.patch.object(middleware_module, "NANO_TREASURY", PAY_TO)
        self._nt.start()
        self.addCleanup(self._nt.stop)

    @mock.patch.object(middleware_module, "NANO_REQUIRE_CEMENTED", True)
    def test_uncemented_send_refused_when_finality_required(self):
        nano_mod.set_rpc(make_valid_node(cemented=False))
        c = self._client()
        r = c.get("/premium", headers={"X-NANO-PAYMENT": SEND_HASH})
        self.assertEqual(r.status_code, 402)

    @mock.patch.object(middleware_module, "NANO_REQUIRE_CEMENTED", True)
    def test_cemented_send_accepted_when_finality_required(self):
        nano_mod.set_rpc(make_valid_node(cemented=True))
        c = self._client()
        r = c.get("/premium", headers={"X-NANO-PAYMENT": SEND_HASH})
        self.assertEqual(r.status_code, 200)

    def test_uncemented_send_accepted_when_only_confirmation_required(self):
        # default (no cementing requirement) -> confirmed is enough
        nano_mod.set_rpc(make_valid_node(cemented=False))
        c = self._client()
        r = c.get("/premium", headers={"X-NANO-PAYMENT": SEND_HASH})
        self.assertEqual(r.status_code, 200)

    def _client(self):
        app = make_app()
        return app.test_client()


if __name__ == "__main__":
    unittest.main()
