"""R6 verified-close boundary: real local RSA and SDK, no gateway/network."""
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


class AlipayCloseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.platform_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(alipay, '_alipay_client', None))
        self.stack.enter_context(patch.object(alipay, '_alipay_public_key', None))
        self.stack.enter_context(patch.dict(os.environ, {
            'ALIPAY_APP_ID': 'offline-close-app',
            'ALIPAY_PRIVATE_KEY': self.app_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()).decode('ascii'),
            'ALIPAY_PUBLIC_KEY': self.platform_key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode('ascii'),
            'ALIPAY_SERVER_URL': 'https://offline.invalid/gateway.do',
        }, clear=True))
        self.network = [self.stack.enter_context(patch(name, side_effect=AssertionError('No live payment/network')))
                        for name in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo')]
        self.assertTrue(alipay.init_alipay())

    def tearDown(self):
        for guard in self.network:
            guard.assert_not_called()

    @staticmethod
    def trade(**overrides):
        return {'code': '10000', 'msg': 'Success', 'out_trade_no': 'offline-order',
                'trade_no': 'offline-gateway-trade', **overrides}

    def wire(self, business, key=None):
        raw = json.dumps(business, ensure_ascii=False, separators=(',', ':'))
        signature = (key or self.platform_key).sign(raw.encode(), padding.PKCS1v15(), hashes.SHA256())
        return ('{"alipay_trade_close_response":' + raw + ',"sign":"' +
                base64.b64encode(signature).decode() + '"}').encode()

    @staticmethod
    def transport(wire):
        return patch('alipay.aop.api.DefaultAlipayClient.do_post', return_value=wire)

    def test_success_uses_actual_close_request_and_signature_verification(self):
        with self.transport(self.wire(self.trade(extra_private='must not leave boundary'))) as transport:
            result = alipay.close_verified_trade('offline-order')
        self.assertEqual(result, {'out_trade_no': 'offline-order', 'trade_no': 'offline-gateway-trade'})
        transport.assert_called_once()
        query = parse_qs(transport.call_args.args[1])
        self.assertEqual(query['method'], ['alipay.trade.close'])
        self.assertEqual(query['app_id'], ['offline-close-app'])
        self.assertEqual(query['sign_type'], ['RSA2'])
        self.assertEqual(json.loads(transport.call_args.args[3]['biz_content']), {'out_trade_no': 'offline-order'})
        self.assertTrue(query.get('sign') or transport.call_args.args[3].get('sign'))

    def test_wrong_rsa_signer_is_not_closure_evidence(self):
        with self.transport(self.wire(self.trade(), self.wrong_key)):
            self.assertIsNone(alipay.close_verified_trade('offline-order'))

    def test_response_tampering_after_signature_rejected(self):
        response = self.wire(self.trade()).replace(b'offline-gateway-trade', b'tampered-gateway-trade')
        with self.transport(response):
            self.assertIsNone(alipay.close_verified_trade('offline-order'))

    def test_valid_signature_for_another_order_rejected(self):
        with self.transport(self.wire(self.trade(out_trade_no='another-order'))):
            self.assertIsNone(alipay.close_verified_trade('offline-order'))

    def test_signed_nonexistent_paid_and_other_business_errors_stay_unknown(self):
        for sub_code in ('ACQ.TRADE_NOT_EXIST', 'ACQ.TRADE_STATUS_ERROR', 'ACQ.SYSTEM_ERROR'):
            with self.subTest(sub_code=sub_code), self.transport(self.wire(self.trade(code='40004', sub_code=sub_code))):
                self.assertIsNone(alipay.close_verified_trade('offline-order'))

    def test_malformed_success_fields_are_not_evidence(self):
        for fields in ({'code': 10000}, {'code': None}, {'out_trade_no': None}, {'trade_no': None},
                       {'trade_no': ''}, {'trade_no': 123}, {'trade_no': ' whitespace '}):
            with self.subTest(fields=fields), self.transport(self.wire(self.trade(**fields))):
                self.assertIsNone(alipay.close_verified_trade('offline-order'))
        for field in ('out_trade_no', 'trade_no', 'code'):
            data = self.trade(); del data[field]
            with self.subTest(missing=field), self.transport(self.wire(data)):
                self.assertIsNone(alipay.close_verified_trade('offline-order'))

    def test_unsigned_malformed_or_empty_transport_results_rejected(self):
        for response in (b'', b'not-json', b'{}', b'[]',
                         json.dumps({'alipay_trade_close_response': self.trade()}).encode()):
            with self.subTest(response=response), self.transport(response):
                self.assertIsNone(alipay.close_verified_trade('offline-order'))

    def test_network_failure_is_unknown_and_never_logs_private_details(self):
        with patch('alipay.aop.api.DefaultAlipayClient.do_post', side_effect=TimeoutError('secret-password full-provider-body')) as transport, \
             self.assertLogs(alipay.logger, level='WARNING') as logs:
            self.assertIsNone(alipay.close_verified_trade('offline-order'))
        transport.assert_called_once()
        self.assertNotIn('secret-password', '\n'.join(logs.output))
        self.assertNotIn('full-provider-body', '\n'.join(logs.output))

    def test_signature_exception_body_is_not_recovered_or_logged(self):
        body = self.wire(self.trade(trade_no='sensitive-private-reference'), self.wrong_key)
        with self.transport(body), self.assertLogs(alipay.logger, level='WARNING') as logs:
            self.assertIsNone(alipay.close_verified_trade('offline-order'))
        self.assertNotIn('sensitive-private-reference', '\n'.join(logs.output))
        self.assertNotIn('alipay_trade_close_response', '\n'.join(logs.output))

    def test_invalid_requested_order_never_calls_gateway(self):
        with self.transport(self.wire(self.trade())) as transport:
            for value in ('', ' ', ' padded ', None, 123):
                with self.subTest(value=value):
                    self.assertIsNone(alipay.close_verified_trade(value))
            transport.assert_not_called()

    def test_uninitialized_client_is_unknown_without_transport(self):
        with patch.object(alipay, '_alipay_client', None), self.transport(self.wire(self.trade())) as transport:
            self.assertIsNone(alipay.close_verified_trade('offline-order'))
            transport.assert_not_called()

    def test_verified_return_shape_compatibility_only_after_execute_success(self):
        # Signature-boundary tests above use the real SDK. This case covers
        # wrapped/inner shapes from successfully verified SDK implementations.
        data = self.trade()
        for payload in (data, {'alipay_trade_close_response': data}):
            with patch.object(alipay._alipay_client, 'execute', return_value=json.dumps(payload)):
                self.assertEqual(alipay.close_verified_trade('offline-order')['trade_no'], data['trade_no'])
        for payload in (None, '', '[]', 'null', '{"alipay_trade_close_response":[]}'):
            with patch.object(alipay._alipay_client, 'execute', return_value=payload):
                self.assertIsNone(alipay.close_verified_trade('offline-order'))


if __name__ == '__main__':
    unittest.main()
