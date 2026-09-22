"""Offline payment-boundary regressions using real locally generated RSA signatures.

Only SDK HTTP transport is replaced. Request signing, response extraction and the
pinned SDK's RSA signature verification all run normally; no gateway/model call.
"""
import base64
import json
import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.infra import alipay


def private_pem(key, fmt=serialization.PrivateFormat.TraditionalOpenSSL):
    return key.private_bytes(serialization.Encoding.PEM, fmt,
                             serialization.NoEncryption()).decode('ascii')


def public_pem(key, fmt=serialization.PublicFormat.SubjectPublicKeyInfo):
    return key.public_key().public_bytes(serialization.Encoding.PEM, fmt).decode('ascii')


def bare_der(key, public=True):
    if public:
        data = key.public_key().public_bytes(serialization.Encoding.DER,
                                            serialization.PublicFormat.SubjectPublicKeyInfo)
    else:
        data = key.private_bytes(serialization.Encoding.DER,
                                 serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption())
    return base64.b64encode(data).decode('ascii')


class AlipayVerificationTests(unittest.TestCase):
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
            'ALIPAY_APP_ID': 'offline-test-app',
            'ALIPAY_PRIVATE_KEY': private_pem(self.app_key),
            'ALIPAY_PUBLIC_KEY': public_pem(self.platform_key),
            'ALIPAY_SERVER_URL': 'https://offline.invalid/gateway.do',
        }, clear=True))
        self.stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')))
        self.assertTrue(alipay.init_alipay())

    def signed_wire(self, business, method='query', key=None):
        raw = json.dumps(business, ensure_ascii=False, separators=(',', ':'))
        signature = (key or self.platform_key).sign(raw.encode('utf-8'), padding.PKCS1v15(), hashes.SHA256())
        return ('{"alipay_trade_' + method + '_response":' + raw + ',"sign":"' +
                base64.b64encode(signature).decode('ascii') + '"}').encode('utf-8')

    def sdk_response(self, wire):
        return patch('alipay.aop.api.DefaultAlipayClient.do_post', return_value=wire)

    def trade(self, **values):
        return {'code': '10000', 'msg': 'Success', 'out_trade_no': 'repair_offline',
                'trade_status': 'TRADE_SUCCESS', 'total_amount': '0.99',
                'trade_no': 'offline-trade', **values}

    def test_precreate_uses_real_sdk_verified_unwrapped_response(self):
        business = {'code': '10000', 'msg': 'Success', 'out_trade_no': 'repair_offline',
                    'qr_code': 'https://offline.invalid/qr'}
        with self.sdk_response(self.signed_wire(business, 'precreate')) as transport:
            self.assertEqual(alipay.create_alipay_precreate('repair_offline', '0.99', '格式修复'),
                             'https://offline.invalid/qr')
        self.assertEqual(transport.call_count, 1)

    def test_verified_query_uses_real_sdk_signature_and_unwrapped_result(self):
        with self.sdk_response(self.signed_wire(self.trade())):
            result = alipay.query_verified_trade('repair_offline')
        self.assertEqual(result, {'out_trade_no': 'repair_offline', 'trade_status': 'TRADE_SUCCESS',
                                  'total_amount': '0.99', 'trade_no': 'offline-trade'})

    def test_legacy_status_helper_uses_verified_sdk_unwrapped_result(self):
        with self.sdk_response(self.signed_wire(self.trade())):
            self.assertEqual(alipay.query_alipay_trade('repair_offline'), 'TRADE_SUCCESS')

    def test_query_rejects_valid_signature_for_different_order(self):
        with self.sdk_response(self.signed_wire(self.trade(out_trade_no='other'))):
            self.assertIsNone(alipay.query_verified_trade('repair_offline'))
            self.assertIsNone(alipay.query_alipay_trade('repair_offline'))

    def test_query_rejects_wrong_signing_key(self):
        with self.sdk_response(self.signed_wire(self.trade(), key=self.wrong_key)):
            self.assertIsNone(alipay.query_verified_trade('repair_offline'))
            self.assertIsNone(alipay.query_alipay_trade('repair_offline'))

    def test_query_rejects_wrong_configured_platform_key(self):
        os.environ['ALIPAY_PUBLIC_KEY'] = public_pem(self.wrong_key)
        self.assertTrue(alipay.init_alipay())
        with self.sdk_response(self.signed_wire(self.trade())):
            self.assertIsNone(alipay.query_verified_trade('repair_offline'))

    def test_query_rejects_amount_tampering_after_signature(self):
        tampered = self.signed_wire(self.trade()).replace(b'"0.99"', b'"5.99"')
        with self.sdk_response(tampered):
            self.assertIsNone(alipay.query_verified_trade('repair_offline'))
            self.assertIsNone(alipay.query_alipay_trade('repair_offline'))

    def test_query_rejects_unsigned_or_malformed_success_response(self):
        for wire in (json.dumps({'alipay_trade_query_response': self.trade()}).encode(), b'{}', b'not-json'):
            with self.subTest(wire=wire), self.sdk_response(wire):
                self.assertIsNone(alipay.query_verified_trade('repair_offline'))

    def test_query_signature_exception_payload_is_never_recovered_or_logged(self):
        forged = json.dumps({'alipay_trade_query_response': self.trade()})
        with patch.object(alipay._alipay_client, 'execute', side_effect=RuntimeError(
                'response sign verify failed ' + forged)), self.assertLogs(alipay.logger, 'WARNING') as logs:
            self.assertIsNone(alipay.query_alipay_trade('repair_offline'))
        self.assertNotIn('offline-trade', '\n'.join(logs.output))
        self.assertNotIn('total_amount', '\n'.join(logs.output))

    def test_query_signed_business_error_stays_unknown(self):
        with self.sdk_response(self.signed_wire({'code': '40004', 'sub_code': 'ACQ.TRADE_NOT_EXIST'})):
            self.assertIsNone(alipay.query_verified_trade('repair_offline'))

    def test_precreate_rejects_wrong_signature(self):
        business = {'code': '10000', 'qr_code': 'https://offline.invalid/forged'}
        with self.sdk_response(self.signed_wire(business, 'precreate', self.wrong_key)):
            with self.assertRaises(Exception):
                alipay.create_alipay_precreate('repair_offline', '0.99', 'repair')

    def test_precreate_rejects_missing_qr_or_mismatched_order_or_business_error(self):
        for business in ({'code': '10000'}, {'code': '10000', 'qr_code': ''},
                         {'code': '10000', 'qr_code': 'qr', 'out_trade_no': 'other'},
                         {'code': '40004', 'sub_msg': 'Payment unavailable'}):
            with self.subTest(business=business), self.sdk_response(self.signed_wire(business, 'precreate')):
                with self.assertRaises(ValueError):
                    alipay.create_alipay_precreate('repair_offline', '0.99', 'repair')

    def test_successful_sdk_return_shape_compatibility(self):
        # This only tests shape compatibility, in addition to real RSA SDK tests above.
        business = {'code': '10000', 'qr_code': 'https://offline.invalid/qr'}
        for result in (business, {'alipay_trade_precreate_response': business}):
            with patch.object(alipay._alipay_client, 'execute', return_value=json.dumps(result)):
                self.assertEqual(alipay.create_alipay_precreate('repair_offline', '0.99', 'repair'), business['qr_code'])
        for result in (self.trade(), {'alipay_trade_query_response': self.trade()}):
            with patch.object(alipay._alipay_client, 'execute', return_value=json.dumps(result)):
                self.assertEqual(alipay.query_verified_trade('repair_offline')['total_amount'], '0.99')

    def test_bad_sdk_result_types_do_not_become_payment_evidence(self):
        for result in (None, '', '[]', 'null', '{"alipay_trade_query_response":[]}'):
            with self.subTest(result=result), patch.object(alipay._alipay_client, 'execute', return_value=result):
                self.assertIsNone(alipay.query_verified_trade('repair_offline'))

    def test_public_key_formats_are_canonicalized_for_sdk(self):
        expected = public_pem(self.platform_key)
        variants = (expected, expected.replace('\n', '\\n'), bare_der(self.platform_key),
                    public_pem(self.platform_key, serialization.PublicFormat.PKCS1))
        for raw in variants:
            with self.subTest(format=raw[:26]):
                self.assertEqual(alipay.normalize_alipay_public_key(raw), expected)
                os.environ['ALIPAY_PUBLIC_KEY'] = raw
                self.assertTrue(alipay.init_alipay())
                with self.sdk_response(self.signed_wire(self.trade())):
                    self.assertIsNotNone(alipay.query_verified_trade('repair_offline'))

    def test_app_private_pkcs8_and_bare_formats_work_with_actual_sdk_signer(self):
        variants = (private_pem(self.app_key), private_pem(self.app_key, serialization.PrivateFormat.PKCS8),
                    bare_der(self.app_key, public=False))
        for raw in variants:
            with self.subTest(format=raw[:26]):
                os.environ['ALIPAY_PRIVATE_KEY'] = raw
                self.assertTrue(alipay.init_alipay())
                with self.sdk_response(self.signed_wire(self.trade())):
                    self.assertIsNotNone(alipay.query_verified_trade('repair_offline'))

    def test_private_key_in_public_field_fails_closed_without_key_in_logs(self):
        variants = (private_pem(self.platform_key), bare_der(self.platform_key, public=False),
                    private_pem(self.platform_key, serialization.PrivateFormat.PKCS8))
        for raw in variants:
            os.environ['ALIPAY_PUBLIC_KEY'] = raw
            with self.assertLogs(alipay.logger, 'ERROR') as logs:
                self.assertFalse(alipay.init_alipay())
            text = '\n'.join(logs.output)
            self.assertIn('支付宝平台 RSA 公钥', text)
            self.assertNotIn(raw, text)
            self.assertIsNone(alipay._alipay_client)
            self.assertIsNone(alipay._alipay_public_key)

    def test_app_public_key_cannot_impersonate_platform_public_key(self):
        os.environ['ALIPAY_PUBLIC_KEY'] = public_pem(self.app_key)
        with self.assertLogs(alipay.logger, 'ERROR') as logs:
            self.assertFalse(alipay.init_alipay())
        self.assertIn('填成了应用公钥', '\n'.join(logs.output))
        self.assertIsNone(alipay._alipay_client)

    def test_malformed_config_is_disabled_without_crashing_app(self):
        for field, raw in (('ALIPAY_PUBLIC_KEY', 'not-base64'), ('ALIPAY_PUBLIC_KEY', '你好'),
                           ('ALIPAY_PRIVATE_KEY', public_pem(self.app_key))):
            with self.subTest(field=field), patch.dict(os.environ, {field: raw}):
                with self.assertLogs(alipay.logger, 'ERROR'):
                    self.assertFalse(alipay.init_alipay())
                self.assertIsNone(alipay._alipay_client)

    def test_undersized_rsa_config_is_disabled(self):
        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        for field, value in (('ALIPAY_PUBLIC_KEY', public_pem(weak)),
                             ('ALIPAY_PRIVATE_KEY', private_pem(weak))):
            with self.subTest(field=field), patch.dict(os.environ, {field: value}):
                with self.assertLogs(alipay.logger, 'ERROR') as logs:
                    self.assertFalse(alipay.init_alipay())
                self.assertIn('2048', '\n'.join(logs.output))
                self.assertIsNone(alipay._alipay_client)

    def test_missing_configuration_clears_previously_initialized_client(self):
        os.environ.pop('ALIPAY_PUBLIC_KEY')
        with self.assertLogs(alipay.logger, 'WARNING'):
            self.assertFalse(alipay.init_alipay())
        self.assertIsNone(alipay._alipay_client)
        self.assertFalse(alipay.verify_alipay_notification({}))
        self.assertIsNone(alipay.query_verified_trade('repair_offline'))
        with self.assertRaises(ValueError):
            alipay.create_alipay_page_pay('repair_offline', '0.99', 'repair', 'https://offline.invalid')

    def notification(self, key=None):
        params = {'app_id': 'offline-test-app', 'out_trade_no': 'repair_offline',
                  'total_amount': '0.99', 'trade_status': 'TRADE_SUCCESS', 'subject': '格式修复',
                  'empty_optional': '', 'sign_type': 'RSA2'}
        message = '&'.join(f'{name}={params[name]}' for name in sorted(params)
                           if name != 'sign_type' and params[name])
        signature = (key or self.platform_key).sign(message.encode('utf-8'), padding.PKCS1v15(), hashes.SHA256())
        params['sign'] = base64.b64encode(signature).decode('ascii')
        return params

    def test_webhook_valid_real_rsa2_signature_without_mutating_params(self):
        params = self.notification()
        original = params.copy()
        self.assertTrue(alipay.verify_alipay_notification(params))
        self.assertEqual(params, original)

    def test_webhook_wrong_key_rejected(self):
        self.assertFalse(alipay.verify_alipay_notification(self.notification(self.wrong_key)))

    def test_webhook_tampered_amount_rejected(self):
        params = self.notification()
        params['total_amount'] = '5.99'
        self.assertFalse(alipay.verify_alipay_notification(params))

    def test_webhook_missing_malformed_signature_and_wrong_sign_type_rejected(self):
        for replacement in ({'sign': None}, {'sign': 'not-a-signature'}, {'sign_type': 'RSA'}):
            params = {**self.notification(), **replacement}
            with self.subTest(replacement=replacement):
                self.assertFalse(alipay.verify_alipay_notification(params))


if __name__ == '__main__':
    unittest.main()
