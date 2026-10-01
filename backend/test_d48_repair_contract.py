"""R10 API/execution contracts: offline, real processes, real EPUB repair.

Run through the isolated regression harness; no live gateway/model is used.
"""
from contextlib import ExitStack
import hashlib
import multiprocessing
import os
from pathlib import Path
import queue
import tempfile
import threading
import unittest
import uuid
from unittest.mock import Mock, patch

from app.domain.repair_executor import RepairExecutor
from app.domain.repair_repository import RepairRepository
from app.domain.repair_runner import run_repair
from test_epub_fixture import repairable_epub_bytes


def load_main():
    # Never load a developer's payment/model credentials, including on spawn.
    with patch('dotenv.load_dotenv', return_value=False):
        from app import main
    return main


def api_client(main):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.middleware('http')(main.admin_private_cache)
    for suffix, endpoint, method in [('/{job_id}/pay', main.repair_pay, 'POST'),
            ('/{job_id}/status', main.repair_status, 'GET'),
            ('/{job_id}/recover', main.repair_recover_payment, 'POST'),
            ('/{job_id}/download', main.repair_download, 'GET')]:
        app.add_api_route('/api/v2/repair' + suffix, endpoint, methods=[method])
    app.add_api_route('/webhook', main.alipay_webhook, methods=['POST'])
    return TestClient(app)


def callback_process(root, job_id, ready, start, release, results):
    main = load_main()
    executor = RepairExecutor()
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(main, '_REPAIR_UPLOAD_DIR', Path(root)))
            stack.enter_context(patch.object(main, '_get_repair_executor', return_value=executor))
            stack.enter_context(patch.object(main, 'verify_alipay_notification', return_value=True))
            stack.enter_context(patch.object(main, 'record_event', return_value=True))
            stack.enter_context(patch.dict(os.environ, {'ALIPAY_APP_ID': '', 'ALIPAY_SELLER_ID': '',
                'OWNER_PAYMENT_EMAIL_ENABLED': '0', 'SKIP_PAYMENT_CHECK': '0'}))
            def runner(repository, key, owner):
                results.put(('entered', owner))
                if not release.wait(20):
                    raise AssertionError('Callback process was not released')
                run_repair(repository, key, owner)
            stack.enter_context(patch.object(main, '_run_repair_owned', side_effect=runner))
            client = stack.enter_context(api_client(main))
            old = client.get(f'/api/v2/repair/{job_id}/status').json()['status']
            ready.put(old)
            if not start.wait(20):
                raise AssertionError('No concurrent start')
            response = client.post('/webhook', data={'out_trade_no': 'repair_' + job_id,
                'total_amount': '2.99', 'trade_status': 'TRADE_SUCCESS'})
            results.put(('callback', response.text))
            executor.shutdown(wait=True)
            status = client.get(f'/api/v2/repair/{job_id}/status')
            results.put(('fresh', status.json()['status'], status.headers['cache-control']))
    finally:
        executor.shutdown(wait=True)


def interrupted_process(root, job_id, entered):
    repository = RepairRepository(root)
    executor = RepairExecutor()
    def interrupted_engine(source, destination):
        Path(destination).write_bytes(b'incomplete before process death')
        entered.set()
        threading.Event().wait(60)
    with patch('app.engine.epub_repairer.repair', side_effect=interrupted_engine):
        executor.run_inline(repository, job_id, run_repair)


class RepairContractTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.repo = RepairRepository(self.root)
        self.main = load_main()
        self.real_get_executor = self.main._get_repair_executor
        self.executor = RepairExecutor()
        self.stack.callback(self.executor.shutdown)
        self.stack.enter_context(patch.object(self.main, '_REPAIR_UPLOAD_DIR', self.root))
        self.stack.enter_context(patch.object(self.main, '_get_repair_executor', return_value=self.executor))
        self.stack.enter_context(patch.dict(os.environ, {'SKIP_PAYMENT_CHECK': '0',
            'OWNER_PAYMENT_EMAIL_ENABLED': '0', 'ALIPAY_DISABLE_PRECREATE': '0'}))
        self.stack.enter_context(patch.object(self.main, 'record_event', return_value=True))
        self.stack.enter_context(patch.object(self.main, '_queue_repair_completion_email'))
        self.clock = self.stack.enter_context(patch.object(self.main, '_repair_now', return_value=1_000_000))
        self.client = self.stack.enter_context(api_client(self.main))

    def job(self, **extra):
        key = uuid.uuid4().hex
        with self.repo.transaction(key, create=True) as value:
            value.update(status='paid', filename='original.epub', expected_amount='2.99',
                         payment_confirmed_at=123.5, out_trade_no='repair_' + key)
            value.update(extra)
        (self.root / key / 'original.epub').write_bytes(repairable_epub_bytes())
        return key

    def url(self, key, operation):
        return f'/api/v2/repair/{key}/{operation}'

    def run_job(self, key):
        self.assertTrue(self.executor.run_inline(self.repo, key, run_repair))
        return self.repo.get(key)

    def test_real_repair_publishes_only_bound_artifact_and_refresh_download(self):
        key = self.job()
        original = (self.root / key / 'original.epub').read_bytes()
        job = self.run_job(key)
        self.assertEqual(job['status'], 'repaired')
        self.assertEqual(job['execution_attempts'], 1)
        artifact = self.root / key / job['artifact_file']
        self.assertTrue(artifact.name.startswith('.repair-'))
        self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), job['artifact_sha256'])
        self.assertEqual((self.root / key / 'original.epub').read_bytes(), original)
        status = self.client.get(self.url(key, 'status'))
        self.assertEqual(status.json()['status'], 'repaired')
        self.assertEqual(status.headers['cache-control'], 'no-store')
        download = self.client.get(self.url(key, 'download'))
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.headers['cache-control'], 'no-store')
        self.assertEqual(download.content, artifact.read_bytes())
        self.assertIn('original_fixed.epub', download.headers['content-disposition'])
        self.assertFalse(self.executor.submit(self.repo, key, Mock()))

    def test_unknown_ids_http_reads_leave_no_permanent_lock_or_job_files(self):
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*'))
        for _ in range(100):
            key = uuid.uuid4().hex
            for operation in ('status', 'download'):
                response = self.client.get(self.url(key, operation))
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.headers['cache-control'], 'no-store')
        after = sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*'))
        self.assertEqual(after, before)

    def test_paid_stray_fixed_file_is_neither_reused_nor_downloaded_even_dev(self):
        key = self.job()
        stray = self.root / key / 'original_fixed.epub'
        stray.write_bytes(b'old incomplete output')
        with patch.dict(os.environ, {'SKIP_PAYMENT_CHECK': '1'}):
            self.assertEqual(self.client.get(self.url(key, 'download')).status_code, 402)
        job = self.run_job(key)
        self.assertNotEqual(job['artifact_file'], stray.name)
        self.assertEqual(stray.read_bytes(), b'old incomplete output')
        self.assertNotEqual(self.client.get(self.url(key, 'download')).content, stray.read_bytes())

    def test_legacy_committed_filename_works_but_glob_without_pointer_does_not(self):
        key = self.job(status='repaired', download_filename='legacy_fixed.epub')
        path = self.root / key / 'legacy_fixed.epub'
        path.write_bytes(repairable_epub_bytes())
        self.assertEqual(self.client.get(self.url(key, 'download')).content, path.read_bytes())
        with self.repo.transaction(key) as job:
            del job['download_filename']
        self.assertEqual(self.client.get(self.url(key, 'download')).status_code, 404)

    def test_missing_recorded_source_cannot_pick_up_unrelated_epub(self):
        key = self.job()
        (self.root / key / 'original.epub').unlink()
        (self.root / key / 'stray.epub').write_bytes(repairable_epub_bytes())
        job = self.run_job(key)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['payment_confirmed_at'], 123.5)
        self.assertNotIn('artifact_file', job)

    def test_partial_engine_failure_keeps_payment_and_cannot_publish(self):
        key = self.job()
        def broken(source, destination):
            Path(destination).write_bytes(b'partial')
            raise RuntimeError('controlled engine failure')
        with patch('app.engine.epub_repairer.repair', side_effect=broken):
            job = self.run_job(key)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['expected_amount'], '2.99')
        self.assertEqual(job['payment_confirmed_at'], 123.5)
        self.assertFalse(list((self.root / key).glob('.repair-*.epub')))
        self.assertEqual(self.client.get(self.url(key, 'download')).status_code, 402)

    def test_invalid_complete_output_is_not_delivery(self):
        key = self.job()
        with patch('app.engine.epub_repairer.repair', side_effect=lambda src, dest: Path(dest).write_bytes(b'bad zip')):
            self.assertEqual(self.run_job(key)['status'], 'failed')

    def test_late_owner_cannot_replace_new_owner_artifact_or_metadata(self):
        from app.engine.epub_repairer import repair
        key = self.job()
        winner = self.root / key / 'winner_fixed.epub'
        winner.write_bytes(b'current owner artifact')
        def superseded(source, destination):
            repair(source, destination)
            with self.repo.transaction(key) as current:
                current.update(status='repaired', execution_owner='b' * 32,
                               artifact_file=winner.name, download_filename=winner.name)
        with patch('app.engine.epub_repairer.repair', side_effect=superseded):
            job = self.run_job(key)
        self.assertEqual(job['execution_owner'], 'b' * 32)
        self.assertEqual(job['artifact_file'], winner.name)
        self.assertEqual(winner.read_bytes(), b'current owner artifact')
        self.assertFalse(list((self.root / key).glob('.repair-*.epub')))

    def test_metadata_outcome_unknown_keeps_possibly_committed_artifact(self):
        key = self.job()
        original_write = self.repo._atomic_json
        def fail_after_write(directory_fd, filename, payload):
            original_write(directory_fd, filename, payload)
            if b'"repaired"' in payload:
                raise OSError('simulated post-commit acknowledgement loss')
        with patch.object(self.repo, '_atomic_json', side_effect=fail_after_write):
            job = self.run_job(key)
        self.assertEqual(job['status'], 'repaired')
        self.assertTrue((self.root / key / job['artifact_file']).is_file())
        self.assertEqual(self.client.get(self.url(key, 'download')).status_code, 200)

    def test_publication_before_metadata_commit_failure_is_not_downloadable(self):
        key = self.job()
        original_write = self.repo._atomic_json
        def fail_before_write(directory_fd, filename, payload):
            if b'"repaired"' in payload:
                raise OSError('simulated metadata failure after artifact rename')
            original_write(directory_fd, filename, payload)
        with patch.object(self.repo, '_atomic_json', side_effect=fail_before_write):
            job = self.run_job(key)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['payment_confirmed_at'], 123.5)
        self.assertNotIn('artifact_file', job)
        self.assertEqual(len(list((self.root / key).glob('.repair-*.epub'))), 1)
        self.assertEqual(self.client.get(self.url(key, 'download')).status_code, 402)

    def test_interruption_budget_retains_payment_for_manual_attention(self):
        key = self.job(execution_attempts=3)
        with patch('app.engine.epub_repairer.repair') as engine:
            job = self.run_job(key)
        engine.assert_not_called()
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['execution_state'], 'attention_required')
        self.assertEqual(job['payment_confirmed_at'], 123.5)

    def test_invalid_owner_cannot_mutate_metadata_or_escape_job_directory(self):
        key = self.job()
        before = self.repo.get(key)
        with self.assertRaises(ValueError):
            run_repair(self.repo, key, '../../outside')
        self.assertEqual(self.repo.get(key), before)

    def test_tick_skips_corrupt_record_and_counts_admitted_not_busy_jobs(self):
        bad = self.job()
        (self.root / bad / 'order.json').write_text('{broken')
        keys = [self.job() for _ in range(5)]
        metadata = [self.root / key / 'order.json' for key in [bad, *keys]]
        with patch.object(Path, 'glob', return_value=iter(metadata)), patch.object(
                self.main, '_repair_gateway_available', return_value=False), patch.object(
                self.main, '_ensure_repair_running', side_effect=[False, False, False, True, True]) as submit:
            self.main._repair_payment_tick()
        self.assertEqual([call.args[0] for call in submit.call_args_list], keys)

    def test_distinct_orders_share_gateway_budget_across_repository_instances(self):
        keys = [self.job(status='pending_payment', checkout_started_at=999_900) for _ in range(2)]
        with patch('app.infra.alipay.query_verified_trade', return_value=None) as query:
            first = self.client.post(self.url(keys[0], 'recover')).json()
            second = self.client.post(self.url(keys[1], 'recover')).json()
        self.assertEqual(first['payment_check'], 'unavailable')
        self.assertEqual(second['payment_check'], 'throttled')
        query.assert_called_once()

    def test_payment_during_slow_gateway_response_never_reverts_to_pending(self):
        key = self.job(status='pending_payment', quoted_amount='2.99')
        def verified_during_precreate(**kwargs):
            self.assertTrue(self.main._repair_confirm_payment(key, '2.99', 'verified_webhook'))
            return 'alipay://controlled'
        with patch.object(self.main, '_ensure_repair_running', return_value=False), patch(
                'app.infra.alipay.create_alipay_precreate', side_effect=verified_during_precreate):
            response = self.client.post(self.url(key, 'pay'))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'paid')
        self.assertEqual(self.repo.get(key)['status'], 'paid')

    def test_two_api_processes_with_stale_reads_accept_duplicate_webhook_once(self):
        key = self.job(status='pending_payment')
        context = multiprocessing.get_context('spawn')
        ready, results = context.Queue(), context.Queue()
        start, release = context.Event(), context.Event()
        processes = [context.Process(target=callback_process,
            args=(str(self.root), key, ready, start, release, results)) for _ in range(2)]
        for process in processes:
            process.start()
        messages = []
        try:
            self.assertEqual([ready.get(timeout=20) for _ in processes], ['pending_payment'] * 2)
            start.set()
            while len([item for item in messages if item[0] == 'callback']) < 2:
                messages.append(results.get(timeout=20))
            self.assertEqual([item[1] for item in messages if item[0] == 'callback'], ['success'] * 2)
            release.set()
            while len([item for item in messages if item[0] == 'fresh']) < 2:
                messages.append(results.get(timeout=20))
            for process in processes:
                process.join(20)
                self.assertEqual(process.exitcode, 0)
            while True:
                try:
                    messages.append(results.get_nowait())
                except queue.Empty:
                    break
            self.assertEqual(len([item for item in messages if item[0] == 'entered']), 1)
            self.assertTrue(all(item[2] == 'no-store' for item in messages if item[0] == 'fresh'))
            self.assertTrue(all(item[1] in {'paid', 'repaired'} for item in messages if item[0] == 'fresh'))
            saved = self.repo.get(key)
            self.assertEqual(saved['status'], 'repaired')
            self.assertEqual(saved['execution_attempts'], 1)
            self.assertEqual(saved['confirmation_attempts'], 1)
            self.assertEqual(saved['expected_amount'], '2.99')
            self.assertEqual(self.client.get(self.url(key, 'download')).status_code, 200)
        finally:
            start.set()
            release.set()
            for process in processes:
                if process.is_alive():
                    process.kill()
                process.join(10)
            ready.close()
            results.close()

    def test_sigkill_mid_repair_is_recovered_by_tick_without_serving_partial(self):
        key = self.job()
        context = multiprocessing.get_context('spawn')
        entered = context.Event()
        process = context.Process(target=interrupted_process, args=(str(self.root), key, entered))
        process.start()
        try:
            self.assertTrue(entered.wait(20))
            self.assertEqual(self.repo.get(key)['status'], 'paid')
            self.assertEqual(self.client.get(self.url(key, 'download')).status_code, 402)
            process.kill()
            process.join(10)

            self.assertFalse(process.is_alive())
            with patch.object(self.main, '_repair_gateway_available', return_value=False):
                self.main._repair_payment_tick()
            self.executor.shutdown(wait=True)
            job = self.repo.get(key)
            self.assertEqual(job['status'], 'repaired')
            self.assertEqual(job['execution_attempts'], 2)
            self.assertEqual(job['expected_amount'], '2.99')
            response = self.client.get(self.url(key, 'download'))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(hashlib.sha256(response.content).hexdigest(), job['artifact_sha256'])
        finally:
            if process.is_alive():
                process.kill()
            process.join(10)

    def test_failed_native_thread_start_retains_paid_then_api_recreates_executor(self):
        key = self.job()
        with patch.object(self.main, '_get_repair_executor', self.real_get_executor), patch.object(
                self.main, '_repair_executor', self.executor), patch.dict(os.environ, {'REPAIR_CONCURRENCY': '1'}):
            with patch('threading.Thread.start', side_effect=RuntimeError('controlled native failure')):
                self.assertFalse(self.main._ensure_repair_running(key))
            self.assertTrue(self.executor.closed)
            self.assertEqual(self.repo.get(key)['status'], 'paid')
            self.assertNotIn('execution_attempts', self.repo.get(key))
            replacement = self.main._get_repair_executor()
            self.stack.callback(replacement.shutdown)
            self.assertIsNot(replacement, self.executor)
            self.main._do_repair_async(key)
            self.assertEqual(self.repo.get(key)['status'], 'repaired')
            self.assertEqual(self.repo.get(key)['execution_attempts'], 1)
        self.assertEqual(self.client.get(self.url(key, 'download')).status_code, 200)


if __name__ == '__main__':
    unittest.main(verbosity=2)
