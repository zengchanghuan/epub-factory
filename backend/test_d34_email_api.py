"""Offline authorization and lifecycle tests for optional result emails."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import re
import socket
import tempfile
from urllib.parse import parse_qs, urlsplit
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from app import main as main_module
from app.models import Job, JobStatus, OutputMode
from app.storage_db import Base, PersistentJobStore
from app.main import _authorize_job_access, _authorize_batch_access
from app.domain.completion_email_router import make_completion_email_router
from app.domain import completion_email_service as service
from app.domain.completion_email_worker import CompletionEmailWorker


class EmailApiTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        future = datetime.now(timezone.utc) + timedelta(days=7)
        self.jobs = {key: Job(id=key, source_filename='private.epub', input_path='/private/source',
            trace_id='email-test', output_mode=OutputMode.simplified, status=JobStatus.running,
            access_token='owner-token', token_expires_at=future, batch_id='batch-id') for key in ('job-a', 'job-b')}
        self.store = SimpleNamespace(get=self.jobs.get)
        self.repair_id = 'a' * 32
        app = FastAPI()
        app.include_router(make_completion_email_router(self.store, _authorize_job_access,
            lambda key: list(self.jobs.values()) if key == 'batch-id' else [],
            _authorize_batch_access, lambda key: {'status': 'paid'} if key == self.repair_id else None))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.saved = self.stack.enter_context(patch.object(service, 'set_email_subscriptions'))
        self.read = self.stack.enter_context(patch.object(service, 'get_email_subscription', return_value={
            'email': 'reader@example.com', 'enabled': True, 'status': 'subscribed', 'available': True}))
        self.cap = self.stack.enter_context(patch.object(service, 'email_capabilities', return_value={'available': True}))
        self.headers = {'X-Job-Token': 'owner-token'}

    def test_job_owner_required_for_read_and_write(self):
        for headers in ({}, {'X-Job-Token': 'wrong'}):
            self.assertEqual(self.client.get('/api/v2/jobs/job-a/notification-email', headers=headers).status_code, 403)
            self.assertEqual(self.client.put('/api/v2/jobs/job-a/notification-email', json={'email': 'reader@example.com'}, headers=headers).status_code, 403)
        self.saved.assert_not_called(); self.read.assert_not_called()
        result = self.client.put('/api/v2/jobs/job-a/notification-email', json={'email': 'reader@example.com'}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.saved.assert_called_once_with(['job-a'], 'reader@example.com')

    def test_expired_token_cannot_read_address_or_subscribe(self):
        self.jobs['job-a'].token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertEqual(self.client.get('/api/v2/jobs/job-a/notification-email', headers=self.headers).status_code, 403)
        self.read.assert_not_called()

    def test_batch_is_authorized_and_saved_atomically(self):
        url = '/api/v2/batches/batch-id/notification-email'
        self.assertEqual(self.client.put(url, json={'email': 'reader@example.com'}).status_code, 403)
        self.saved.assert_not_called()
        result = self.client.put(url, json={'email': 'reader@example.com'}, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        self.saved.assert_called_once_with(['job-a', 'job-b'], 'reader@example.com')
        self.assertEqual(result.json()['subscribed_count'], 2)
        self.assertTrue(result.json()['per_file'])

    def test_repair_uses_valid_existing_task_capability(self):
        for key in ('wrong', 'b' * 32):
            self.assertEqual(self.client.put(f'/api/v2/repair/{key}/notification-email', json={'email': 'reader@example.com'}).status_code, 404)
        self.saved.assert_not_called()
        result = self.client.put(f'/api/v2/repair/{self.repair_id}/notification-email', json={'email': 'reader@example.com'})
        self.assertEqual(result.status_code, 200)
        self.saved.assert_called_once_with(['repair:' + self.repair_id], 'reader@example.com')

    def test_cross_origin_write_is_rejected(self):
        result = self.client.put('/api/v2/jobs/job-a/notification-email', json={'email': 'reader@example.com'},
            headers={**self.headers, 'Origin': 'https://unrelated.example'})
        self.assertEqual(result.status_code, 403)
        self.saved.assert_not_called()

    def test_invalid_payload_is_rejected_before_subscription(self):
        for email in (None, 123, 'x' * 255):
            self.assertEqual(self.client.put('/api/v2/jobs/job-a/notification-email', json={'email': email}, headers=self.headers).status_code, 422)
        self.saved.assert_not_called()

    def test_provider_unavailable_and_limits_do_not_fake_success(self):
        for error, status in ((service.EmailUnavailableError(), 503), (service.EmailRateLimitError(), 429), (ValueError('private-input'), 422)):
            self.saved.side_effect = error
            result = self.client.put('/api/v2/jobs/job-a/notification-email', json={'email': 'reader@example.com'}, headers=self.headers)
            self.assertEqual(result.status_code, status)
            self.assertNotIn('private-input', result.text)

    def test_cancellation_uses_same_authorization(self):
        result = self.client.put('/api/v2/jobs/job-a/notification-email', json={'email': ''}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.saved.assert_called_once_with(['job-a'], '')


class EmailDeliveryIntegrationTests(unittest.TestCase):
    def test_persisted_opt_in_to_email_link_to_fresh_download_without_browser(self):
        """Real routes/store/service; only SMTP and network are replaced."""
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                'NOTIFY_EMAIL_ENABLED': '1', 'SMTP_HOST': 'smtp.example.com',
                'SMTP_PORT': '465', 'SMTP_SECURITY': 'ssl', 'SMTP_USER': 'sender@example.com',
                'SMTP_FROM': 'sender@example.com', 'SMTP_PASSWORD': 'offline-only',
                'SITE_BASE_URL': 'https://fixepub.com', 'DOWNLOAD_SIGN_SECRET': 'offline-signing',
            }))
            stack.enter_context(patch.object(socket.socket, 'connect', side_effect=AssertionError('No network allowed')))
            engine = create_engine('sqlite:///' + str(Path(temp) / 'jobs.db'), connect_args={'check_same_thread': False})
            stack.callback(engine.dispose)
            Base.metadata.create_all(engine)
            store = PersistentJobStore(engine=engine)
            output = Path(temp) / 'result.epub'
            from test_epub_fixture import minimal_epub_bytes
            output.write_bytes(minimal_epub_bytes())
            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            job = Job(id='offline-result', source_filename='fixture.epub', input_path='fixture.epub',
                trace_id='offline-mail', output_mode=OutputMode.simplified, status=JobStatus.running,
                created_at=yesterday, access_token='offline-owner',
                token_expires_at=yesterday + timedelta(days=7), output_path=str(output))
            store.add(job)
            stack.enter_context(patch.object(service, 'job_store', store))
            stack.enter_context(patch.object(main_module, 'job_store', store))
            smtp_class = stack.enter_context(patch.object(service.smtplib, 'SMTP_SSL'))
            smtp = smtp_class.return_value.__enter__.return_value
            smtp.send_message.return_value = {}
            app = FastAPI()
            app.include_router(make_completion_email_router(store, _authorize_job_access,
                lambda _: [], _authorize_batch_access, lambda _: None))
            app.add_api_route('/api/v2/jobs/{job_id}', main_module.get_job_v2, methods=['GET'])
            app.add_api_route('/api/v2/jobs/{job_id}/download', main_module.download_result_v2, methods=['GET'])
            client = TestClient(app)
            response = client.put('/api/v2/jobs/offline-result/notification-email',
                json={'email': 'reader@example.com'}, headers={'X-Job-Token': 'offline-owner'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['status'], 'subscribed')
            smtp_class.assert_not_called()
            client.close()  # No open browser is required to complete/send.
            resumed_store = PersistentJobStore(engine=engine)
            stack.enter_context(patch.object(service, 'job_store', resumed_store))
            stack.enter_context(patch.object(main_module, 'job_store', resumed_store))
            resumed_store.update_status(job.id, JobStatus.success, 'completed')
            self.assertEqual(service.dispatch_pending_email_notifications()['sent'], 1)
            service.dispatch_pending_email_notifications()
            smtp.send_message.assert_called_once()
            msg = smtp.send_message.call_args.args[0]
            result_url = re.search(r'https://\S+', msg.get_content()).group(0)
            link = urlsplit(result_url)
            self.assertEqual(parse_qs(link.query), {'job_id': [job.id]})
            token = parse_qs(link.fragment)['access_token'][0]
            self.assertNotIn(token, link.query)
            reopened = TestClient(app)
            stack.callback(reopened.close)
            self.assertEqual(reopened.get('/api/v2/jobs/' + job.id).status_code, 403)
            detail = reopened.get('/api/v2/jobs/' + job.id, headers={'X-Job-Token': token})
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(detail.json()['status'], 'completed')
            download_url = detail.json()['download_url']
            self.assertIn('sig=', download_url)
            result = reopened.get(download_url)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.content, output.read_bytes())


class EmailWorkerTests(unittest.TestCase):
    def test_missing_configuration_does_not_start_sender(self):
        worker = CompletionEmailWorker()
        with patch('app.domain.completion_email_worker.email_capabilities', return_value={'available': False}), \
             patch('app.domain.completion_email_worker.threading.Thread') as thread:
            worker.start()
            thread.assert_not_called()

    def test_dispatcher_does_not_need_a_browser_or_book_queue(self):
        worker = CompletionEmailWorker()
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        worker._stop = stop
        with patch('app.domain.completion_email_worker.dispatch_pending_email_notifications') as dispatch:
            worker._run()
        dispatch.assert_called_once_with(limit=20)
        stop.wait.assert_called_once_with(30)

    def test_dispatch_failure_is_contained_without_sensitive_logs(self):
        worker = CompletionEmailWorker()
        stop = Mock(); stop.is_set.side_effect = [False, True]; worker._stop = stop
        with patch('app.domain.completion_email_worker.dispatch_pending_email_notifications', side_effect=RuntimeError('smtp-private-secret')), \
             self.assertLogs('epub_factory', level='WARNING') as logs:
            worker._run()
        self.assertNotIn('smtp-private-secret', '\n'.join(logs.output))


if __name__ == '__main__':
    unittest.main()
