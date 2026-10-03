"""PDF product API/SQLite contracts. Stub parsing/payment, no real transports.

Real PDF parsing + public API delivery is a separate opt-in historical gate.
Run via the isolated release guard, never with production configuration.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app import main, job_runner
from app.domain import pdf_product as product
from app.infra.rate_limiter import RateLimiter
from app.models import JobStatus
from app.storage_db import Base, PersistentJobStore
from test_epub_fixture import minimal_epub_bytes


class PdfApiTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="pdf-api-"))).resolve()
        self.uploads, self.outputs = self.root / "uploads", self.root / "outputs"
        self.uploads.mkdir()
        self.outputs.mkdir()
        self.engine = create_engine(f"sqlite:///{self.root / 'jobs.db'}", connect_args={"check_same_thread": False})
        self.stack.callback(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.store = PersistentJobStore(self.engine)
        self.limiter = RateLimiter(str(self.root / "admission.db"))
        self.stack.enter_context(patch.dict(os.environ, {
            "PDF_TEXT_CONVERSION_ENABLED": "1", "SKIP_PAYMENT_CHECK": "0",
            "ADMIN_SECRET": "", "PDF_PREPARATION_PER_IP_DAILY": "100",
            "PDF_PREPARATION_TOTAL_DAILY": "100", "RECONCILE_TIMEOUT_HOURS": "2",
            "ALIPAY_APP_ID": "offline-test-app", "OWNER_PAYMENT_EMAIL_ENABLED": "0",
        }))
        for key, value in (("job_store", self.store), ("UPLOAD_DIR", self.uploads),
                           ("OUTPUT_DIR", self.outputs), ("rate_limiter", self.limiter),
                           ("CONVERSION_PRICE_CNY", "0.99")):
            self.stack.enter_context(patch.object(main, key, value))
        self.stack.enter_context(patch.object(main, "_use_celery", return_value=True))
        self.stack.enter_context(patch.object(main, "get_current_user_optional", return_value=None))
        self.enqueue = self.stack.enter_context(patch.object(main, "_enqueue_conversion"))
        self.pay = self.stack.enter_context(patch.object(main, "create_alipay_page_pay",
                                                       return_value="https://offline.invalid/payment"))
        self.stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("No network")))
        self.client = TestClient(main.app)
        self.client.headers["X-Client-Session"] = "pdf-api-fixture"

    def create(self, **kwargs):
        reply = self.client.post("/api/v2/pdf-jobs", files={
            "file": ("中文原书.pdf", b"%PDF-1.4\nSYNTHETIC-ONLY", "application/pdf")}, **kwargs)
        return reply

    def created(self):
        response = self.create()
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.client.headers["X-Job-Token"] = body["access_token"]
        return self.store.get(body["job_id"])

    def prepared(self, job=None, warnings=(), memory=True):
        job = job or self.created()
        aid = job.translation_stats["attempt_id"]
        self.assertTrue(self.store.begin_execution(job.id, aid, "fixture-owner"))
        def convert(source, output, **kwargs):
            output.write_bytes(minimal_epub_bytes())
            return dict(
                source_sha256=hashlib.sha256(Path(source).read_bytes()).hexdigest(),
                output_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                page_count=1, normalized_characters=80, zero_width_spaces_preserved=0,
                image_assets=0, image_placements=0, toc_entries=1, paragraph_count=1,
                warnings=list(warnings), memory_limited=memory, validation_passed=True,
                epubcheck_warnings=0, requires_review=bool(warnings), eligible_for_payment=not bool(warnings),
            )
        versions = {key: "0" * 64 if key.endswith("_sha256") else "fixture-v1"
                    for key in product._VERSION_KEYS}
        with patch.object(product, "_versions", return_value=versions), patch.object(
                product.pdf_conversion, "convert_text_pdf", side_effect=convert):
            plan = product.prepare_pdf_artifact(job, self.outputs)
        saved = self.store.finish_pdf_preparation(job.id, aid, "fixture-owner", plan)
        self.assertIsNotNone(saved)
        return saved

    def confirm(self, job, **changes):
        plan = job.translation_stats["pdf_conversion"]
        payload = {"plan_id": plan["plan_id"], "accepted_warnings": list(plan["report"]["warnings"])}
        payload.update(changes)
        return self.client.post(f"/api/v2/jobs/{job.id}/confirm-conversion", json=payload)

    def test_closed_capability_rejects_upload_without_files_or_payment(self):
        with patch.dict(os.environ, {"PDF_TEXT_CONVERSION_ENABLED": "0"}):
            self.assertFalse(self.client.get('/api/v2/capabilities').json()['pdf_text_conversion']['enabled'])
            self.assertEqual(self.create().status_code, 503)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.pay.assert_not_called()

    def test_capability_requires_durable_dispatch_but_test_server_is_explicit(self):
        with patch.object(main, "_use_celery", return_value=False):
            self.assertEqual(self.create().status_code, 503)
            with patch.dict(os.environ, {"SKIP_PAYMENT_CHECK": "1"}):
                self.assertEqual(self.create().status_code, 200)
        self.pay.assert_not_called()

    def test_create_persists_unpaid_prepare_and_refresh_hides_private_plan(self):
        job = self.created()
        self.assertEqual(job.output_mode.value, "original")
        self.assertIsNone(job.output_path)
        self.assertEqual(len(self.store.list_dispatches(job.id)), 1)
        reopened = PersistentJobStore(self.engine)
        with patch.object(main, "job_store", reopened):
            view = self.client.get(f"/api/v2/jobs/{job.id}")
            self.assertEqual(view.json()['pdf_conversion']['phase'], 'preparing')
            self.assertIsNone(view.json()['download_url'])
            self.assertNotIn('pdf_conversion', view.json()['translation_stats'])
            self.assertNotIn(str(self.root), view.text)
            legacy = self.client.get(f"/api/v1/jobs/{job.id}")
            self.assertNotIn('pdf_conversion', legacy.json()['translation_stats'])
            self.assertEqual(self.client.get(f"/api/v2/jobs/{job.id}/download").status_code, 400)
            self.assertIn(job.id, [row['job_id'] for row in self.client.get('/api/v2/jobs').json()['items']])
        self.pay.assert_not_called()

    def test_dedicated_route_rejects_extra_product_flags_wrong_type_and_duplicate_files(self):
        for fields in ({'enable_translation': 'true'}, {'output_mode': 'simplified'}, {'enable_precision_polish': 'true'}):
            self.assertEqual(self.create(data=fields).status_code, 400)
        for filename, data in (('bad.epub', b'%PDF-1.4'), ('bad.pdf', b'not-pdf')):
            self.assertEqual(self.client.post('/api/v2/pdf-jobs', files={'file': (filename, data)}).status_code, 400)
        self.assertEqual(self.client.post('/api/v2/pdf-jobs', files=[
            ('file', ('one.pdf', b'%PDF-1.4')), ('file', ('two.pdf', b'%PDF-1.4'))]).status_code, 400)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.pay.assert_not_called()

    def test_original_mode_not_opened_on_ordinary_or_batch_routes(self):
        for route in ('/api/v1/jobs', '/api/v2/jobs'):
            self.assertEqual(self.client.post(route, data={'output_mode': 'original'}, files={
                'file': ('fixture.epub', minimal_epub_bytes())}).status_code, 400)
        self.assertEqual(self.client.post('/api/v2/batches', data={'output_mode': 'original'}, files=[
            ('files', ('a.epub', minimal_epub_bytes())), ('files', ('b.epub', minimal_epub_bytes()))]).status_code, 400)
        self.pay.assert_not_called()

    def test_oversize_and_admission_failure_create_no_order(self):
        with patch.object(main, 'MAX_FILE_SIZE_BYTES', 10):
            self.assertEqual(self.create().status_code, 413)
        with patch.object(self.limiter, 'reserve_pdf_preparation', return_value=False):
            self.assertEqual(self.create().status_code, 429)
        with patch.object(self.limiter, 'reserve_pdf_preparation', side_effect=RuntimeError('unavailable')):
            self.assertEqual(self.create().status_code, 503)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.pay.assert_not_called()

    def test_confirm_exact_risks_frozen_amount_and_new_attempt(self):
        job = self.prepared(warnings=['empty_password_encryption'])
        old_attempt = job.translation_stats['attempt_id']
        self.assertEqual(self.confirm(job, accepted_warnings=[]).status_code, 409)
        self.assertEqual(self.confirm(job, plan_id='f' * 64).status_code, 409)
        self.pay.assert_not_called()
        with patch.object(main, 'CONVERSION_PRICE_CNY', '9.99'):
            response = self.confirm(job)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['amount'], '0.99')
        self.assertEqual(response.json()['status'], 'pending_payment')
        self.assertEqual(self.pay.call_args.kwargs['total_amount'], '0.99')
        saved = self.store.get(job.id)
        self.assertNotEqual(saved.translation_stats['attempt_id'], old_attempt)
        self.assertEqual(len(self.store.list_dispatches(job.id)), 1)  # Prep only, not delivery.
        self.assertEqual(self.confirm(job).status_code, 409)
        self.assertEqual(self.pay.call_count, 1)
        self.assertIsNone(saved.output_path)
        self.assertIn('created_at', saved.translation_stats['payment_checkout'])

    def test_memory_limit_never_acknowledged_even_on_test_server(self):
        job = self.prepared(warnings=['memory_limit_unavailable'], memory=False)
        with patch.dict(os.environ, {'SKIP_PAYMENT_CHECK': '1'}):
            self.assertEqual(self.confirm(job).status_code, 409)
        self.assertFalse(self.client.get(f'/api/v2/jobs/{job.id}').json()['pdf_conversion']['can_confirm'])
        self.pay.assert_not_called()

    def test_missing_or_changed_artifact_and_source_block_before_checkout(self):
        for change in ('artifact', 'source'):
            with self.subTest(change=change):
                job = self.prepared()
                plan = job.translation_stats['pdf_conversion']
                path = (self.outputs / ('.pdf-prepared-' + plan['artifact_id']) / 'book.epub'
                        if change == 'artifact' else Path(job.input_path))
                path.write_bytes(b'changed')
                self.assertEqual(self.confirm(job).status_code, 409)
        self.pay.assert_not_called()

    def test_confirm_requires_owner_and_checkout_failure_rolls_back_without_new_order(self):
        job = self.prepared()
        other = TestClient(main.app)
        plan = job.translation_stats['pdf_conversion']
        self.assertEqual(other.post(f'/api/v2/jobs/{job.id}/confirm-conversion', json={
            'plan_id': plan['plan_id'], 'accepted_warnings': []}).status_code, 403)
        with patch.object(main, 'create_alipay_page_pay', side_effect=RuntimeError('transport failed')):
            self.assertEqual(self.confirm(job).status_code, 503)
        restored = self.store.get(job.id)
        self.assertEqual(restored.status, JobStatus.awaiting_confirmation)
        self.assertEqual(restored.translation_stats['pdf_conversion']['phase'], 'prepared')
        self.assertNotIn('payment_checkout', restored.translation_stats)
        self.assertEqual(self.confirm(restored).status_code, 200)

    def test_disabled_new_intake_does_not_break_confirmation_of_saved_plan(self):
        job = self.prepared()
        with patch.dict(os.environ, {'PDF_TEXT_CONVERSION_ENABLED': '0'}):
            self.assertEqual(self.confirm(job).status_code, 200)

    def test_signing_has_no_persisted_confirming_phase_and_failure_survives_restart(self):
        job = self.prepared()
        before = job.translation_stats['attempt_id']
        def unavailable(**kwargs):
            reloaded = PersistentJobStore(self.engine).get(job.id)
            self.assertEqual(reloaded.status, JobStatus.awaiting_confirmation)
            self.assertEqual(reloaded.translation_stats['attempt_id'], before)
            self.assertNotIn('payment_checkout', reloaded.translation_stats)
            raise RuntimeError('signer unavailable')
        with patch.object(main, 'create_alipay_page_pay', side_effect=unavailable):
            self.assertEqual(self.confirm(job).status_code, 503)
        with patch.object(main, 'job_store', PersistentJobStore(self.engine)):
            self.assertEqual(self.confirm(job).status_code, 200)

    def test_source_or_artifact_changed_during_signing_never_issues_checkout(self):
        for changed in ('source', 'artifact'):
            with self.subTest(changed=changed):
                job = self.prepared()
                plan = job.translation_stats['pdf_conversion']
                def signing(**kwargs):
                    path = (Path(job.input_path) if changed == 'source' else
                            self.outputs / ('.pdf-prepared-' + plan['artifact_id']) / 'book.epub')
                    path.unlink()
                    return 'https://offline.invalid/payment'
                with patch.object(main, 'create_alipay_page_pay', side_effect=signing):
                    reply = self.confirm(job)
                self.assertEqual(reply.status_code, 409)
                self.assertNotIn('pay_url', reply.json())
                saved = self.store.get(job.id)
                self.assertEqual(saved.status, JobStatus.awaiting_confirmation)
                self.assertNotIn('payment_checkout', saved.translation_stats)

    def test_commit_acknowledgement_loss_keeps_resumable_original_checkout(self):
        job = self.prepared()
        original = self.store.confirm_pdf_conversion
        def committed_then_lost(*args, **kwargs):
            self.assertIsNotNone(original(*args, **kwargs))
            raise RuntimeError('commit acknowledgement lost')
        with patch.object(self.store, 'confirm_pdf_conversion', side_effect=committed_then_lost):
            self.assertEqual(self.confirm(job).status_code, 503)
        saved = PersistentJobStore(self.engine).get(job.id)
        self.assertEqual(saved.status, JobStatus.pending_payment)
        self.assertEqual(saved.translation_stats['pdf_conversion']['phase'], 'confirmed')
        with patch('app.infra.alipay.query_checkout_trade', return_value={
                'out_trade_no': job.id, 'trade_status': 'NOT_CREATED'}):
            self.assertEqual(self.client.post(f'/api/v2/jobs/{job.id}/continue-payment').status_code, 200)

    def test_postcommit_artifact_loss_returns_no_link_and_continue_stays_blocked(self):
        job = self.prepared()
        original = self.store.confirm_pdf_conversion
        def committed_then_missing(*args, **kwargs):
            saved = original(*args, **kwargs)
            plan = saved.translation_stats['pdf_conversion']
            (self.outputs / ('.pdf-prepared-' + plan['artifact_id']) / 'book.epub').unlink()
            return saved
        with patch.object(self.store, 'confirm_pdf_conversion', side_effect=committed_then_missing):
            response = self.confirm(job)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('pay_url', response.json())
        with patch('app.infra.alipay.query_checkout_trade', return_value={
                'out_trade_no': job.id, 'trade_status': 'NOT_CREATED'}):
            self.assertEqual(self.client.post(f'/api/v2/jobs/{job.id}/continue-payment').status_code, 409)

    def test_cancel_during_signing_does_not_replace_cancellation_or_issue_link(self):
        job = self.prepared()
        def signing(**kwargs):
            self.store.update_status(job.id, JobStatus.cancelled, "User cancelled",
                expected_attempt_id=job.translation_stats['attempt_id'],
                expected_statuses={JobStatus.awaiting_confirmation})
            return 'https://offline.invalid/payment'
        with patch.object(main, 'create_alipay_page_pay', side_effect=signing):
            response = self.confirm(job)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('pay_url', response.json())
        self.assertEqual(self.store.get(job.id).status, JobStatus.cancelled)

    def test_continue_payment_uses_first_checkout_clock_and_rechecks_artifact(self):
        job = self.prepared()
        self.assertEqual(self.confirm(job).status_code, 200)
        saved = self.store.get(job.id)
        original_clock = saved.translation_stats['payment_checkout']['created_at']
        with patch('app.infra.alipay.query_checkout_trade', return_value={
            'out_trade_no': job.id, 'trade_status': 'NOT_CREATED'}):
            response = self.client.post(f'/api/v2/jobs/{job.id}/continue-payment')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.store.get(job.id).translation_stats['payment_checkout']['created_at'], original_clock)
            plan = saved.translation_stats['pdf_conversion']
            (self.outputs / ('.pdf-prepared-' + plan['artifact_id']) / 'book.epub').unlink()
            calls = self.pay.call_count
            self.assertEqual(self.client.post(f'/api/v2/jobs/{job.id}/continue-payment').status_code, 409)
            self.assertEqual(self.pay.call_count, calls)

    def test_verified_callback_is_idempotent_and_download_survives_refresh_and_upgrade(self):
        job = self.prepared()
        self.assertEqual(self.confirm(job).status_code, 200)
        prepared = self.store.get(job.id).translation_stats['pdf_conversion']
        receipt = {'out_trade_no': job.id, 'trade_status': 'TRADE_SUCCESS',
                   'total_amount': '0.99', 'app_id': 'offline-test-app'}
        with patch.object(main, 'verify_alipay_notification', return_value=True), patch.object(
                main, '_record_verified_payment'):
            for _ in range(3):
                self.assertEqual(self.client.post('/api/v2/webhooks/alipay', data=receipt).text, 'success')
        paid = self.store.get(job.id)
        self.assertEqual(paid.status, JobStatus.pending)
        self.assertEqual(paid.payment_resolution['source'], 'verified_webhook')
        self.assertEqual(len(self.store.list_dispatches(job.id)), 2)
        reopened = PersistentJobStore(self.engine)
        with patch.object(job_runner, 'job_store', reopened), patch.object(
                job_runner, 'OUTPUT_DIR', self.outputs), patch.dict(os.environ, {
                    'PDF_TEXT_CONVERSION_ENABLED': '0', 'REDIS_URL': '', 'CELERY_BROKER_URL': ''}), patch.object(
                job_runner, 'notify_job_completed'), patch.object(
                product.pdf_conversion, 'convert_text_pdf', side_effect=AssertionError('Paid delivery must not reparse')), patch.object(
                job_runner.converter, 'convert_file_to_horizontal', side_effect=AssertionError('No CJK conversion')):
            job_runner.run_job(job.id, expected_attempt_id=paid.translation_stats['attempt_id'])
        done = reopened.get(job.id)
        self.assertEqual(done.status, JobStatus.success, done.message)
        self.assertEqual(hashlib.sha256(Path(done.output_path).read_bytes()).hexdigest(), prepared['artifact_sha256'])
        # Delivery is independent of later source/preparation retention.
        Path(done.input_path).unlink()
        (self.outputs / ('.pdf-prepared-' + prepared['artifact_id']) / 'book.epub').unlink()
        with patch.object(main, 'job_store', reopened):
            view = self.client.get(f'/api/v2/jobs/{job.id}').json()
            self.assertEqual(view['status'], 'completed')
            for route in ('v1', 'v2'):
                response = self.client.get(f'/api/{route}/jobs/{job.id}/download')
                self.assertEqual(response.status_code, 200, response.text[:100])
                self.assertEqual(hashlib.sha256(response.content).hexdigest(), prepared['artifact_sha256'])
            self.assertEqual(self.client.get(f'/api/v2/jobs/{job.id}/preview').status_code, 200)
            Path(done.output_path).write_bytes(minimal_epub_bytes() + b'UNCONFIRMED-CHANGE')
            self.assertEqual(self.client.get(f'/api/v2/jobs/{job.id}/download').status_code, 409)
            with patch.object(main, 'build_book_preview') as preview:
                self.assertEqual(self.client.get(f'/api/v2/jobs/{job.id}/preview').status_code, 409)
                preview.assert_not_called()

    def test_invalid_callback_does_not_release_delivery(self):
        job = self.prepared()
        self.assertEqual(self.confirm(job).status_code, 200)
        receipt = {'out_trade_no': job.id, 'trade_status': 'TRADE_SUCCESS',
                   'total_amount': '0.99', 'app_id': 'offline-test-app'}
        with patch.object(main, 'verify_alipay_notification', return_value=False):
            self.assertEqual(self.client.post('/api/v2/webhooks/alipay', data=receipt).text, 'fail')
        with patch.object(main, 'verify_alipay_notification', return_value=True):
            self.assertEqual(self.client.post('/api/v2/webhooks/alipay', data={**receipt, 'total_amount': '0.01'}).text, 'fail')
        self.assertEqual(self.store.get(job.id).status, JobStatus.pending_payment)
        self.assertEqual(len(self.store.list_dispatches(job.id)), 1)

    def test_explicit_server_test_confirmation_queues_without_payment_transport(self):
        with patch.dict(os.environ, {'SKIP_PAYMENT_CHECK': '1'}):
            job = self.prepared()
            response = self.confirm(job)
        self.assertEqual(response.status_code, 200, response.text)
        saved = self.store.get(job.id)
        self.assertEqual(saved.status, JobStatus.pending)
        self.assertEqual(saved.payment_entitlement['source'], 'server_test_bypass')
        self.assertEqual(len(self.store.list_dispatches(job.id)), 2)
        self.pay.assert_not_called()


class PdfAdmissionTests(unittest.TestCase):
    def test_atomic_per_ip_limit_across_independent_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'admission.db')
            first, second = RateLimiter(path), RateLimiter(path)
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda n: (first if n % 2 else second).reserve_pdf_preparation(
                    'same-principal', per_ip_limit=3, total_limit=100), range(20)))
            self.assertEqual(sum(results), 3)
            self.assertFalse(RateLimiter(path).reserve_pdf_preparation('same-principal'))

    def test_global_limit_does_not_reset_or_consume_ordinary_quota(self):
        with tempfile.TemporaryDirectory() as directory:
            limiter = RateLimiter(str(Path(directory) / 'admission.db'))
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda n: limiter.reserve_pdf_preparation(
                    str(n), per_ip_limit=3, total_limit=5), range(20)))
            self.assertEqual(sum(results), 5)
            self.assertEqual(limiter.get_count('0'), 0)
            with self.assertRaises(ValueError):
                limiter.reserve_pdf_preparation('new', per_ip_limit=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
