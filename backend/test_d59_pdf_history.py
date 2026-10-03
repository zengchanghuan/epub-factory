"""Opt-in Linux: actual selected PDF through durable product API to download.

No PDF engine or EPUB validator is stubbed. Only payment/network transports are
replaced; this never pays, calls a model, emails, or touches a production DB.
The thirteen independent D58 source/artifact audits run on the downloaded file.
Run in the network-disabled Linux evidence container, with a private source-code
runtime copy. Do not put the customer's file in CI or the repository.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_d58_pdf_conversion_history as audit


class PdfProductHistoryTests(audit.PdfConversionHistoryTests):
    @classmethod
    def setUpClass(cls):
        selected = os.environ.get(audit.SOURCE_ENV, '')
        if not selected:
            raise unittest.SkipTest('EPUB_PDF_HISTORY_FILE not provided; product real-file gate not executed')
        audit.require(platform.system() == 'Linux', 'Product real-file payment admission requires Linux hard limits')
        cls.path = Path(selected).resolve(strict=True)
        audit.require(audit.file_digest(cls.path) == audit.SOURCE_SHA256, 'Explicit SHA-pinned real PDF required')
        cls.original = audit.file_identity(cls.path)
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        cls.root = Path(cls.stack.enter_context(tempfile.TemporaryDirectory(prefix='d59-real-product-'))).resolve()
        cls.saved_output = os.environ.get('EPUB_PDF_HISTORY_OUTPUT', '')
        env = {key: os.environ[key] for key in ('PATH', 'EPUBCHECK_JAR', 'JAVA_HOME') if key in os.environ}
        env.update({audit.SOURCE_ENV: str(cls.path), 'HOME': str(cls.root), 'TMPDIR': str(cls.root),
            'TMP': str(cls.root), 'TEMP': str(cls.root), 'PYTHONDONTWRITEBYTECODE': '1',
            'DATABASE_URL': 'sqlite:///' + str(cls.root / 'jobs.db'), 'EPUB_PERSISTENT_STORE': '1',
            'PDF_TEXT_CONVERSION_ENABLED': '1', 'SKIP_PAYMENT_CHECK': '0', 'JOB_DISPATCH_ENABLED': '0',
            'PDF_PREPARATION_PER_IP_DAILY': '3', 'PDF_PREPARATION_TOTAL_DAILY': '100',
            'ALIPAY_APP_ID': 'offline-product-history', 'ALIPAY_PRIVATE_KEY': '', 'ALIPAY_PUBLIC_KEY': '',
            'OPENAI_API_KEY': '', 'DEEPSEEK_API_KEY': '', 'DASHSCOPE_API_KEY': '', 'GEMINI_API_KEY': '',
            'CELERY_BROKER_URL': '', 'REDIS_URL': '', 'SMTP_HOST': '', 'SENTRY_DSN': '',
            'NOTIFY_EMAIL_ENABLED': '0', 'OWNER_PAYMENT_EMAIL_ENABLED': '0'})
        cls.stack.enter_context(patch.dict(os.environ, env, clear=True))
        cls.stack.enter_context(patch.object(tempfile, 'tempdir', str(cls.root)))
        cls.stack.enter_context(patch('dotenv.load_dotenv', return_value=False))
        cls.guards = [cls.stack.enter_context(patch(target, side_effect=AssertionError('D59 external request forbidden')))
                      for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'socket.socket.sendto',
                                     'socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr',
                                     'socket.getnameinfo', 'openai.OpenAI', 'openai.AsyncOpenAI',
                                     'smtplib.SMTP', 'smtplib.SMTP_SSL')]
        cls.addClassCleanup(lambda: [guard.assert_not_called() for guard in cls.guards])
        cls.addClassCleanup(cls.assert_original_unchanged)
        cls.reference = audit.inspect_source(cls.path)
        from fastapi.testclient import TestClient
        from app import main, job_runner
        from app.domain import pdf_product as product
        from app.infra.rate_limiter import RateLimiter
        from app.storage_db import PersistentJobStore
        from app.models import JobStatus
        cls.main, cls.runner, cls.product = main, job_runner, product
        cls.store = main.job_store
        cls.uploads, cls.outputs = cls.root / 'uploads', cls.root / 'outputs'
        cls.uploads.mkdir(); cls.outputs.mkdir()
        cls.stack.enter_context(patch.object(main, 'UPLOAD_DIR', cls.uploads))
        cls.stack.enter_context(patch.object(main, 'OUTPUT_DIR', cls.outputs))
        cls.stack.enter_context(patch.object(job_runner, 'OUTPUT_DIR', cls.outputs))
        cls.stack.enter_context(patch.object(job_runner, 'job_store', cls.store))
        cls.stack.enter_context(patch.object(main, 'rate_limiter', RateLimiter(str(cls.root / 'admission.db'))))
        cls.stack.enter_context(patch.object(main, '_use_celery', return_value=True))
        cls.stack.enter_context(patch.object(main, 'get_current_user_optional', return_value=None))
        cls.stack.enter_context(patch.object(main, 'CONVERSION_PRICE_CNY', '0.99'))
        cls.enqueue = cls.stack.enter_context(patch.object(main, '_enqueue_conversion'))
        cls.pay = cls.stack.enter_context(patch.object(main, 'create_alipay_page_pay', return_value='https://offline.invalid/payment'))
        cls.stack.enter_context(patch.object(job_runner, 'notify_job_completed'))
        cls.stack.enter_context(patch.object(main, '_record_verified_payment'))
        cls.no_cjk = cls.stack.enter_context(patch.object(job_runner.converter, 'convert_file_to_horizontal',
                                                         side_effect=AssertionError('PDF must not use CJK converter')))
        cls.client = TestClient(main.app)
        cls.client.headers['X-Client-Session'] = 'pdf-history-isolated'
        with cls.path.open('rb') as source:
            response = cls.client.post('/api/v2/pdf-jobs', files={'file': ('西南联大逻辑通识课.pdf', source, 'application/pdf')})
        audit.require(response.status_code == 200, 'Real PDF intake failed')
        created = response.json()
        cls.job_id, cls.token = created['job_id'], created['access_token']
        cls.client.headers['X-Job-Token'] = cls.token
        cls.before = cls.store.get(cls.job_id)
        audit.require(cls.before.status == JobStatus.pending and cls.before.output_path is None, 'Preparation not durably queued')
        cls.pay.assert_not_called()
        # A fresh store instance reads the same queued attempt after API restart.
        cls.store = PersistentJobStore(cls.store._engine)
        cls.stack.enter_context(patch.object(main, 'job_store', cls.store))
        cls.stack.enter_context(patch.object(job_runner, 'job_store', cls.store))
        real_parser = product.pdf_conversion.convert_text_pdf
        cls.parser = cls.stack.enter_context(patch.object(product.pdf_conversion, 'convert_text_pdf', wraps=real_parser))
        job_runner.run_job(cls.job_id, expected_attempt_id=cls.before.translation_stats['attempt_id'])
        cls.prepared = cls.store.get(cls.job_id)
        audit.require(cls.prepared.status == JobStatus.awaiting_confirmation, 'Real preparation did not reach confirmation')
        cls.plan = cls.prepared.translation_stats['pdf_conversion']
        audit.require(cls.plan['report']['memory_limited'] is True, 'Actual parser hard memory limit unavailable')
        cls.pay.assert_not_called()
        cls.denied_download = cls.client.get(f'/api/v2/jobs/{cls.job_id}/download').status_code
        cls.missing_ack = cls.client.post(f'/api/v2/jobs/{cls.job_id}/confirm-conversion', json={
            'plan_id': cls.plan['plan_id'], 'accepted_warnings': []}).status_code
        cls.pay.assert_not_called()
        confirmation = {'plan_id': cls.plan['plan_id'], 'accepted_warnings': cls.plan['report']['warnings']}
        # A deployment/price change must not alter a previously frozen quote.
        with patch.object(main, 'CONVERSION_PRICE_CNY', '9.99'), patch.dict(os.environ, {'PDF_TEXT_CONVERSION_ENABLED': '0'}):
            confirmed = cls.client.post(f'/api/v2/jobs/{cls.job_id}/confirm-conversion', json=confirmation)
        audit.require(confirmed.status_code == 200 and confirmed.json()['amount'] == '0.99', 'Frozen confirmation failed')
        cls.duplicate_confirmation = cls.client.post(f'/api/v2/jobs/{cls.job_id}/confirm-conversion', json=confirmation).status_code
        receipt = {'out_trade_no': cls.job_id, 'trade_status': 'TRADE_SUCCESS', 'total_amount': '0.99',
                   'app_id': 'offline-product-history'}
        with patch.object(main, 'verify_alipay_notification', return_value=True):
            for _ in range(3):
                reply = cls.client.post('/api/v2/webhooks/alipay', data=receipt)
                audit.require(reply.text == 'success', 'Controlled payment callback failed')
        cls.paid = cls.store.get(cls.job_id)
        audit.require(cls.paid.status == JobStatus.pending, 'Verified receipt did not queue delivery')
        # A late prepare delivery cannot adopt the new paid attempt.
        job_runner.run_job(cls.job_id, expected_attempt_id=cls.before.translation_stats['attempt_id'])
        audit.require(cls.store.get(cls.job_id).status == JobStatus.pending, 'Old attempt changed paid phase')
        with patch.object(product.pdf_conversion, 'convert_text_pdf', side_effect=AssertionError('Must not reparse after payment')):
            job_runner.run_job(cls.job_id, expected_attempt_id=cls.paid.translation_stats['attempt_id'])
        cls.done = cls.store.get(cls.job_id)
        audit.require(cls.done.status == JobStatus.success, 'Paid delivery failed')
        cls.output = cls.root / 'downloaded.epub'
        downloaded = cls.client.get(f'/api/v2/jobs/{cls.job_id}/download')
        audit.require(downloaded.status_code == 200, 'Authorized completed download unavailable')
        cls.output.write_bytes(downloaded.content)
        cls.output_sha256 = audit.file_digest(cls.output)
        audit.require(cls.output_sha256 == cls.plan['artifact_sha256'], 'Downloaded bytes differ from confirmed artifact')
        cls.summary = {**cls.plan['report'], 'source_sha256': cls.plan['source_sha256'], 'output_sha256': cls.output_sha256}
        cls.snapshot = audit.EpubSnapshot(cls.output)
        if cls.saved_output:
            with Path(cls.saved_output).open('xb') as output, cls.output.open('rb') as source:
                shutil.copyfileobj(source, output)

    def test_product_durable_phases_and_distinct_attempts(self):
        self.assertNotEqual(self.before.translation_stats['attempt_id'], self.paid.translation_stats['attempt_id'])
        self.assertEqual(self.store.get_execution(self.job_id, self.before.translation_stats['attempt_id'])['state'], 'finished')
        self.assertEqual(self.store.get_execution(self.job_id, self.paid.translation_stats['attempt_id'])['state'], 'finished')
        self.assertEqual(len(self.store.list_dispatches(self.job_id)), 2)
        self.assertEqual(self.parser.call_count, 1)
        self.no_cjk.assert_not_called()

    def test_product_unpaid_download_risk_confirmation_and_frozen_quote(self):
        self.assertEqual(self.denied_download, 400)
        self.assertEqual(self.missing_ack, 409)
        self.assertEqual(self.duplicate_confirmation, 409)
        self.assertEqual(self.pay.call_count, 1)
        self.assertEqual(self.pay.call_args.kwargs['total_amount'], '0.99')
        self.assertTrue(self.plan['report']['requires_review'])
        self.assertFalse(self.plan['report']['eligible_for_payment'])
        self.assertEqual(set(self.plan['report']['warnings']), {
            'empty_password_encryption', 'late_text_overlay_preserved', 'pages_without_extractable_text'})

    def test_product_refresh_owner_download_and_private_serialization(self):
        from app.storage_db import PersistentJobStore
        with patch.object(self.main, 'job_store', PersistentJobStore(self.store._engine)):
            reply = self.client.get(f'/api/v2/jobs/{self.job_id}')
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(reply.json()['status'], 'completed')
            self.assertNotIn('pdf_conversion', reply.json()['translation_stats'])
            self.assertNotIn(str(self.root), reply.text)
            self.assertNotIn(self.plan['artifact_id'], reply.text)
            listed = self.client.get('/api/v2/jobs').json()['items']
            row = next(item for item in listed if item['job_id'] == self.job_id)
            self.assertEqual(row['output_mode'], 'original')
            self.assertEqual(row['pdf_conversion']['plan_id'], self.plan['plan_id'])
            self.assertEqual(self.client.get(f'/api/v2/jobs/{self.job_id}/preview').status_code, 200)
            response = self.client.get(f'/api/v1/jobs/{self.job_id}/download')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(hashlib.sha256(response.content).hexdigest(), self.output_sha256)
        from fastapi.testclient import TestClient
        stranger = TestClient(self.main.app)
        self.assertEqual(stranger.get(f'/api/v2/jobs/{self.job_id}/download').status_code, 403)


def main():
    if not os.environ.get(audit.SOURCE_ENV) or platform.system() != 'Linux':
        print('This real product gate requires the explicit PDF sample and isolated Linux runtime.', file=sys.stderr)
        return 2
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestLoader().loadTestsFromTestCase(PdfProductHistoryTests))
    if not result.wasSuccessful() or result.testsRun != 16 or result.skipped:
        return 1
    print(json.dumps({'tests_run': result.testsRun, 'skipped': 0, 'success': True,
        'source_sha256': audit.SOURCE_SHA256, 'output_sha256': PdfProductHistoryTests.output_sha256,
        'pages': audit.PAGE_COUNT, 'normalized_characters': audit.NORMALIZED_CHARACTERS,
        'original_images': audit.IMAGE_OBJECTS, 'image_draws': audit.IMAGE_DRAWS,
        'actual_parser_calls': 1, 'real_gateway_calls': 0, 'model_calls': 0,
        'epubcheck_errors': 0, 'epubcheck_warnings': 0, 'actual_hard_memory_limit': True,
        'scope': 'one_real_pdf_product_api_preparation_confirmation_controlled_payment_download_and_content_audit'}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
