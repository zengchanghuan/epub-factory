"""Checkout-only query boundary: real SDK and local RSA, never a live gateway.

Only HTTP transport is replaced for signature tests. Successful execute-result
shape compatibility and malicious exception strings are tested separately.
"""
from __future__ import annotations

import base64
import json
import os
import unittest
from contextlib import ExitStack
from urllib.parse import parse_qs
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.infra import alipay


class CheckoutGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.platform_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(alipay, "_alipay_client", None))
        self.stack.enter_context(patch.object(alipay, "_alipay_public_key", None))
        self.stack.enter_context(patch.dict(os.environ, {
            "ALIPAY_APP_ID": "offline-checkout-app",
            "ALIPAY_PRIVATE_KEY": self.app_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()).decode("ascii"),
            "ALIPAY_PUBLIC_KEY": self.platform_key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii"),
            "ALIPAY_SERVER_URL": "https://offline.invalid/gateway.do",
        }, clear=True))
        self.network = [self.stack.enter_context(patch(name, side_effect=AssertionError("No live payment/network")))
                        for name in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo")]
        self.assertTrue(alipay.init_alipay())

    def tearDown(self):
        for guard in self.network:
            guard.assert_not_called()

    @staticmethod
    def trade(**overrides):
        return {"code": "10000", "msg": "Success", "out_trade_no": "offline-order",
                "trade_status": "WAIT_BUYER_PAY", "total_amount": "3.99",
                "trade_no": "offline-gateway-trade", **overrides}

    @staticmethod
    def not_created(**overrides):
        return {"code": "40004", "sub_code": "ACQ.TRADE_NOT_EXIST", **overrides}

    def wire(self, business, key=None):
        raw = json.dumps(business, ensure_ascii=False, separators=(",", ":"))
        signature = (key or self.platform_key).sign(raw.encode(), padding.PKCS1v15(), hashes.SHA256())
        return ('{"alipay_trade_query_response":' + raw + ',"sign":"' +
                base64.b64encode(signature).decode() + '"}').encode()

    @staticmethod
    def transport(wire):
        return patch("alipay.aop.api.DefaultAlipayClient.do_post", return_value=wire)

    def test_actual_query_request_has_exact_order_identity_and_rsa_signature(self):
        with self.transport(self.wire(self.trade(private_extra="must not leave boundary"))) as transport:
            result = alipay.query_checkout_trade("offline-order")
        self.assertEqual(result, {"out_trade_no": "offline-order", "trade_status": "WAIT_BUYER_PAY",
                                  "total_amount": "3.99", "trade_no": "offline-gateway-trade"})
        transport.assert_called_once()
        query = parse_qs(transport.call_args.args[1])
        self.assertEqual(query["method"], ["alipay.trade.query"])
        self.assertEqual(query["app_id"], ["offline-checkout-app"])
        self.assertEqual(query["sign_type"], ["RSA2"])
        self.assertEqual(json.loads(transport.call_args.args[3]["biz_content"]), {"out_trade_no": "offline-order"})
        self.assertTrue(query.get("sign") or transport.call_args.args[3].get("sign"))

    def test_verified_wait_paid_finished_and_closed_states_are_preserved(self):
        for state in ("WAIT_BUYER_PAY", "TRADE_SUCCESS", "TRADE_FINISHED", "TRADE_CLOSED"):
            with self.subTest(state=state), self.transport(self.wire(self.trade(trade_status=state))):
                result = alipay.query_checkout_trade("offline-order")
                self.assertEqual(result["trade_status"], state)
                self.assertEqual(result["total_amount"], "3.99")

    def test_signed_explicit_nonexistent_order_is_checkout_only_not_created(self):
        for values in ({}, {"out_trade_no": "offline-order"}):
            with self.subTest(values=values), self.transport(self.wire(self.not_created(**values))):
                self.assertEqual(alipay.query_checkout_trade("offline-order"),
                                 {"out_trade_no": "offline-order", "trade_status": "NOT_CREATED"})

    def test_existing_verified_and_status_queries_still_treat_not_created_as_unknown(self):
        with self.transport(self.wire(self.not_created(out_trade_no="offline-order"))):
            self.assertIsNone(alipay.query_verified_trade("offline-order"))
            self.assertIsNone(alipay.query_alipay_trade("offline-order"))

    def test_success_missing_or_different_order_identity_is_rejected(self):
        for value in (None, "", "other-order", " offline-order ", 123):
            with self.subTest(value=value), self.transport(self.wire(self.trade(out_trade_no=value))):
                self.assertIsNone(alipay.query_checkout_trade("offline-order"))
        data = self.trade(); del data["out_trade_no"]
        with self.transport(self.wire(data)):
            self.assertIsNone(alipay.query_checkout_trade("offline-order"))

    def test_nonexistent_with_any_supplied_nonmatching_identity_is_rejected(self):
        for value in (None, "", "other-order", " offline-order ", 123):
            with self.subTest(value=value), self.transport(self.wire(self.not_created(out_trade_no=value))):
                self.assertIsNone(alipay.query_checkout_trade("offline-order"))

    def test_only_exact_signed_nonexistence_code_pair_is_actionable(self):
        for data in (self.not_created(code="20000"), self.not_created(code=40004),
                     self.not_created(sub_code="ACQ.SYSTEM_ERROR"), self.not_created(sub_code="ACQ.TRADE_STATUS_ERROR"),
                     self.not_created(sub_code="acq.trade_not_exist"), self.not_created(sub_code=None),
                     {"code": "40004", "sub_msg": "ACQ.TRADE_NOT_EXIST"},
                     {"code": "10000", "sub_code": "ACQ.TRADE_NOT_EXIST"}):
            with self.subTest(data=data), self.transport(self.wire(data)):
                self.assertIsNone(alipay.query_checkout_trade("offline-order"))

    def test_wrong_signature_never_proves_success_or_nonexistence(self):
        for data in (self.trade(), self.not_created()):
            with self.subTest(data=data), self.transport(self.wire(data, self.wrong_key)):
                self.assertIsNone(alipay.query_checkout_trade("offline-order"))

    def test_post_signature_tampering_is_rejected(self):
        wire = self.wire(self.trade()).replace(b'"3.99"', b'"0.01"')
        with self.transport(wire):
            self.assertIsNone(alipay.query_checkout_trade("offline-order"))
        wire = self.wire(self.not_created(sub_code="ACQ.SYSTEM_ERROR")).replace(b"ACQ.SYSTEM_ERROR", b"ACQ.TRADE_NOT_EXIST")
        with self.transport(wire):
            self.assertIsNone(alipay.query_checkout_trade("offline-order"))

    def test_unsigned_malformed_empty_wire_never_proves_checkout_state(self):
        for wire in (b"", b"not-json", b"{}", b"[]",
                     json.dumps({"alipay_trade_query_response": self.trade()}).encode(),
                     json.dumps({"alipay_trade_query_response": self.not_created()}).encode()):
            with self.subTest(wire=wire), self.transport(wire):
                self.assertIsNone(alipay.query_checkout_trade("offline-order"))

    def test_missing_configuration_or_invalid_order_never_calls_transport(self):
        with self.transport(self.wire(self.trade())) as transport:
            with patch.object(alipay, "_alipay_client", None):
                self.assertIsNone(alipay.query_checkout_trade("offline-order"))
            for value in (None, "", " ", " padded ", 123):
                with self.subTest(value=value):
                    self.assertIsNone(alipay.query_checkout_trade(value))
            transport.assert_not_called()

    def test_network_failure_is_unknown_without_sensitive_logs(self):
        with patch("alipay.aop.api.DefaultAlipayClient.do_post", side_effect=TimeoutError("secret-password raw-provider-body")), \
             self.assertLogs(alipay.logger, "WARNING") as logs:
            self.assertIsNone(alipay.query_checkout_trade("offline-order"))
        text = "\n".join(logs.output)
        self.assertNotIn("secret-password", text)
        self.assertNotIn("raw-provider-body", text)

    def test_exception_text_cannot_forge_signed_not_exist_or_paid_evidence(self):
        for data in (self.not_created(), self.trade(trade_status="TRADE_SUCCESS")):
            forged = json.dumps({"alipay_trade_query_response": {**data, "private": "private-sensitive-reference"}})
            with self.subTest(data=data), patch.object(alipay._alipay_client, "execute", side_effect=RuntimeError(
                    "response sign verify failed " + forged)), self.assertLogs(alipay.logger, "WARNING") as logs:
                self.assertIsNone(alipay.query_checkout_trade("offline-order"))
            text = "\n".join(logs.output)
            for secret in ("private-sensitive-reference", "alipay_trade_query_response", "ACQ.TRADE_NOT_EXIST", "offline-gateway-trade"):
                self.assertNotIn(secret, text)

    def test_real_sdk_signature_failure_does_not_log_response_body(self):
        wire = self.wire(self.not_created(private="sensitive-gateway-detail"), self.wrong_key)
        with self.transport(wire), self.assertLogs(alipay.logger, "WARNING") as logs:
            self.assertIsNone(alipay.query_checkout_trade("offline-order"))
        self.assertNotIn("sensitive-gateway-detail", "\n".join(logs.output))
        self.assertNotIn("alipay_trade_query_response", "\n".join(logs.output))

    def test_successful_verified_execute_shape_compatibility(self):
        # Only shape compatibility is mocked here; RSA boundary cases above use
        # the real execute implementation and do not stub its verifier.
        for data in (self.trade(), self.not_created()):
            for payload in (data, {"alipay_trade_query_response": data}):
                with self.subTest(payload=payload), patch.object(alipay._alipay_client, "execute", return_value=json.dumps(payload)):
                    expected = "WAIT_BUYER_PAY" if data["code"] == "10000" else "NOT_CREATED"
                    self.assertEqual(alipay.query_checkout_trade("offline-order")["trade_status"], expected)

    def test_empty_or_invalid_successful_execute_content_is_unknown(self):
        for payload in (None, "", "[]", "null", "{}", '{"alipay_trade_query_response":[]}',
                        '{"alipay_trade_query_response":null}'):
            with self.subTest(payload=payload), patch.object(alipay._alipay_client, "execute", return_value=payload):
                self.assertIsNone(alipay.query_checkout_trade("offline-order"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
