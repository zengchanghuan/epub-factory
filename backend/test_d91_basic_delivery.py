"""Basic conversion delivery, with real packaging and optional real EPUBCheck.

Synthetic regressions derive from empty optional navigation in a failed book.
Private originals can run the SAME paid-delivery scenario using an explicit
server-local JSON manifest [{path, sha256}]. No originals or tokens enter Git.
All orders, payment facts and broker delivery are isolated test fixtures.
"""
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_book(path, *, image_only=False):
    from ebooklib import epub
    book = epub.EpubBook()
    book.set_identifier('basic-delivery-regression')
    book.set_title('繁體軟體與電腦')
    book.set_language('zh')
    body = '<img src="pixel.png" alt=""/>' if image_only else (
        '<h1 id="title">繁體軟體與電腦</h1><p id="p1">學習軟體與滑鼠，閱讀臺灣故事。</p>'
        '<p id="p2">第二段完整保留，<a href="next.xhtml#note">註釋</a>。</p>'
        '<pre>繁體軟體 -- code</pre><img src="pixel.png" alt=""/>')
    chapter = epub.EpubHtml(uid='chapter', file_name='chapter.xhtml', title='繁體軟體與電腦')
    chapter.content = body
    next_chapter = epub.EpubHtml(uid='next', file_name='next.xhtml', title='註釋')
    next_chapter.content = '<p id="note">完整註釋與資源。</p>' if not image_only else '<img src="pixel.png" alt=""/>'
    # A real tiny PNG; no customer resources.
    import base64
    pixel = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
    book.add_item(epub.EpubItem(uid='pixel', file_name='pixel.png', media_type='image/png', content=pixel))
    for item in (chapter, next_chapter, epub.EpubNav(), epub.EpubNcx()):
        book.add_item(item)
    book.toc = (epub.Link('chapter.xhtml#title' if not image_only else 'chapter.xhtml', '繁體軟體與電腦', 'toc-title'),)
    book.spine = ['nav', chapter, next_chapter]
    epub.write_epub(str(path), book)
    # Exact structural failure shape, sanitized: anonymous empty optional nav.
    with zipfile.ZipFile(path) as source:
        members = [(item, source.read(item.filename)) for item in source.infolist()]
    with zipfile.ZipFile(path, 'w') as target:
        for item, data in members:
            if item.filename.endswith('/nav.xhtml'):
                data = data.replace(b'</body>', b'<nav epub:type="landmarks" hidden="hidden"><ol/></nav></body>')
            target.writestr(item, data)


def assert_preserved(test, source, output):
    """Independent resource/order inventories plus expected normalized prose."""
    import ebooklib
    from bs4 import BeautifulSoup
    from app.engine.unpacker import EpubUnpacker
    from app.engine.cleaners.cjk_normalizer import CjkNormalizer
    from app.engine.chunk_extractor import is_source_placeholder_document
    original, result = EpubUnpacker(source).load_book(), EpubUnpacker(output).load_book()
    test.assertIsNotNone(original); test.assertIsNotNone(result)
    def reading(book):
        return [book.get_item_with_id(key).get_name() for key, _linear in book.spine]
    test.assertEqual(reading(original), reading(result))
    def resources(book):
        return {item.get_name(): hashlib.sha256(item.get_content()).hexdigest() for item in book.get_items()
                if item.media_type.startswith(('image/', 'font/', 'audio/', 'video/'))
                or Path(item.get_name()).suffix in {'.ttf', '.otf', '.woff', '.woff2'}}
    test.assertEqual(resources(original), resources(result))
    def text(raw):
        soup = BeautifulSoup(raw, 'html.parser')
        for node in soup(['script', 'style', 'nav']):
            node.decompose()
        return ''.join(c for c in (soup.body or soup).get_text() if c.isalnum())
    actual = {item.get_name(): item for item in result.get_items_of_type(ebooklib.ITEM_DOCUMENT)}
    normalizer = CjkNormalizer('simplified', 'auto', [], False)
    for item in original.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        if 'nav' in getattr(item, 'properties', []) or is_source_placeholder_document(item.get_content()):
            continue
        name = item.get_name()
        test.assertIn(name, actual)
        expected = text(normalizer.process(item.get_content(), ebooklib.ITEM_DOCUMENT))
        # Hash assertions keep private prose out of test logs.
        test.assertEqual(hashlib.sha256(expected.encode()).hexdigest(),
                         hashlib.sha256(text(actual[name].get_content()).encode()).hexdigest(),
                         'Normalized prose mismatch in ' + name)


class IsolatedCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = tempfile.TemporaryDirectory(prefix='basic-delivery-')
        cls.addClassCleanup(cls.sandbox.cleanup)
        cls.root = Path(cls.sandbox.name)
        cls.outer = ExitStack()
        cls.addClassCleanup(cls.outer.close)
        env = {k: v for k, v in os.environ.items() if k in {'PATH', 'JAVA_HOME', 'EPUBCHECK_JAR', 'HOME', 'TMPDIR', 'BASIC_DELIVERY_MANIFEST'}}
        env.update(LANG='C.UTF-8', LC_ALL='C.UTF-8', DATABASE_URL='sqlite:///' + str(cls.root / 'bootstrap.db'),
                   UPLOAD_DIR=str(cls.root / 'uploads'), OUTPUT_DIR=str(cls.root / 'outputs'),
                   EPUB_TRANSLATION_CHECKPOINT_DB=str(cls.root / 'checkpoint.db'),
                   REPAIR_UPLOAD_DIR=str(cls.root / 'repair'), EPUB_PERSISTENT_STORE='1',
                   SKIP_PAYMENT_CHECK='0', JOB_DISPATCH_ENABLED='0',
                   DOWNLOAD_SIGN_SECRET='offline-basic-delivery-signature',
                   JWT_SECRET_KEY='offline-basic-delivery-jwt-secret',
                   NOTIFY_EMAIL_ENABLED='0', OWNER_PAYMENT_EMAIL_ENABLED='0',
                   EPUB_LLM_RATE_LIMITER_ENABLED='0')
        cls.outer.enter_context(patch.dict(os.environ, env, clear=True))
        cls.outer.enter_context(patch('dotenv.load_dotenv', return_value=False))
        from test_d54_infra_history import isolate_legacy_rate_limiter
        isolate_legacy_rate_limiter(cls.outer, cls.root)
        cls.network_guards = [cls.outer.enter_context(patch(target, side_effect=AssertionError('No external calls in basic delivery tests')))
            for target in ('socket.getaddrinfo', 'socket.socket.connect', 'socket.socket.connect_ex',
                           'socket.socket.sendto', 'smtplib.SMTP', 'smtplib.SMTP_SSL', 'openai.OpenAI', 'openai.AsyncOpenAI')]
        from app.engine.compiler import ExtremeCompiler, EPUBCHECK_JAR
        cls.Compiler = ExtremeCompiler
        cls.jar = Path(EPUBCHECK_JAR)

    @classmethod
    def tearDownClass(cls):
        for guard in cls.network_guards:
            guard.assert_not_called()

    def setUp(self):
        self.case = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.case.cleanup)
        self.work = Path(self.case.name)
        self.source = self.work / 'original.epub'
        self.output = self.work / 'converted.epub'
        make_book(self.source)

    def compiler(self, **options):
        return self.Compiler(str(self.source), str(self.output), lexicon_domains=[], enable_proper_noun=False, **options)


class BasicConversionTests(IsolatedCase):
    def test_required_converter_exception_never_packages_or_falls_back(self):
        from app.engine.cleaners.cjk_normalizer import CjkNormalizer
        compiler = self.compiler()
        with patch.object(CjkNormalizer, 'process', side_effect=RuntimeError('injected')), patch.object(compiler, '_run_safe_mode') as fallback:
            self.assertFalse(compiler.run())
        fallback.assert_not_called()
        self.assertFalse(compiler.validation_passed)
        self.assertEqual(compiler.error_code, 'CONVERT_FAILED')
        self.assertFalse(self.output.exists())

    def test_partial_pipeline_mutation_is_discarded_and_options_and_toc_survive(self):
        from app.engine.packager import EpubPackager
        from app.engine.unpacker import EpubUnpacker
        from bs4 import BeautifulSoup
        original = EpubPackager.save
        count = 0
        def fail_once(packager):
            nonlocal count
            count += 1
            if count == 1:
                # Mutation after normal conversion, before packaging fails.
                for item in packager.book.get_items_of_type(9):
                    if item.get_name() == 'chapter.xhtml':
                        item.set_content(b'<p>LOST ORIGINAL</p>')
                raise RuntimeError('injected packaging failure')
            return original(packager)
        compiler = self.compiler(output_mode='simplified', traditional_variant='tw')
        with patch.object(EpubPackager, 'save', fail_once), patch.object(compiler, '_run_epubcheck', return_value=True):
            self.assertTrue(compiler.run())
        self.assertTrue(compiler.validation_passed, compiler.final_message)
        self.assertEqual(compiler.metrics.mode, 'safe')
        book = EpubUnpacker(self.output).load_book()
        text = '\n'.join(BeautifulSoup(i.get_content(), 'html.parser').get_text() for i in book.get_items_of_type(9))
        self.assertIn('学习软体与滑鼠', text)  # Disabled vocabulary must stay disabled.
        self.assertIn('繁体软体与电脑', text)
        self.assertNotIn('LOST ORIGINAL', text)
        self.assertNotIn('軟體與電腦', text)

    def test_silent_removed_paragraph_is_not_deliverable_even_with_valid_epub(self):
        from app.engine.cleaners.typography_enhancer import TypographyEnhancer
        from bs4 import BeautifulSoup
        def lose_paragraph(cleaner, content, item_type):
            if item_type != 9:
                return content
            soup = BeautifulSoup(content, 'html.parser')
            node = soup.find(id='p2')
            if node:
                node.decompose()
            return str(soup).encode()
        compiler = self.compiler()
        with patch.object(TypographyEnhancer, 'process', lose_paragraph), patch.object(compiler, '_run_epubcheck', return_value=True):
            compiler.run()
        self.assertFalse(compiler.validation_passed)
        self.assertEqual(compiler.error_code, 'CONVERT_FAILED')
        self.assertEqual(compiler.get_translation_stats()['content_completeness']['missing_units'], 1)

    def test_image_only_publication_is_not_rejected_as_missing_prose(self):
        make_book(self.source, image_only=True)
        compiler = self.compiler(output_mode='keep')
        with patch.object(compiler, '_run_epubcheck', return_value=True):
            self.assertTrue(compiler.run())
        self.assertTrue(compiler.validation_passed, compiler.final_message)

    def test_textless_document_loss_is_still_detected(self):
        from app.domain.content_completeness import capture_content_baseline, audit_content_completeness
        make_book(self.source, image_only=True)
        baseline = capture_content_baseline(self.source, allow_textless=True)
        with zipfile.ZipFile(self.source) as original, zipfile.ZipFile(self.output, 'w') as damaged:
            for item in original.infolist():
                if not item.filename.endswith('/chapter.xhtml'):
                    damaged.writestr(item, original.read(item.filename))
        report = audit_content_completeness(baseline, self.output)
        self.assertEqual(report['expected_units'], 0)
        self.assertEqual(report['missing_documents'], 1)
        self.assertEqual(report['status'], 'failed')

    def test_content_scans_propagate_cancellation_and_worker_timeout(self):
        from app.cancellation import JobCancelled
        from billiard.exceptions import SoftTimeLimitExceeded
        from app.domain.content_completeness import capture_content_baseline, audit_content_completeness
        baseline = capture_content_baseline(self.source)
        for interrupt in (JobCancelled, SoftTimeLimitExceeded):
            with self.subTest(interrupt=interrupt.__name__):
                with patch('app.domain.content_completeness.capture_content_baseline', side_effect=interrupt()):
                    with self.assertRaises(interrupt):
                        self.compiler().run()
                with patch('app.domain.content_completeness._output_documents', side_effect=interrupt()):
                    with self.assertRaises(interrupt):
                        audit_content_completeness(baseline, self.output)

    def test_selected_vocabulary_cannot_silently_disappear(self):
        from app.engine.cleaners.cjk_normalizer import CjkNormalizer
        for target in ('_load_yaml', '_build_automaton'):
            with self.subTest(target=target), patch('app.engine.cleaners.lexicon_matcher.' + target,
                    return_value={} if target == '_load_yaml' else None):
                with self.assertRaisesRegex(RuntimeError, '词典无法加载'):
                    CjkNormalizer('simplified', lexicon_domains=['general'], enable_proper_noun=False)
                with self.assertRaisesRegex(RuntimeError, '词典无法加载'):
                    CjkNormalizer('simplified', lexicon_domains=[], enable_proper_noun=True)
                CjkNormalizer('simplified', lexicon_domains=[], enable_proper_noun=False)

    def test_script_matrix_and_protected_literals(self):
        from app.engine.cleaners.cjk_normalizer import CjkNormalizer
        for mode, variant, source, expected in (
            ('simplified', 'auto', '學習繁體與電腦', '学习繁体与电脑'),
            ('simplified', 'tw', '學習繁體與電腦', '学习繁体与电脑'),
            ('simplified', 'hk', '學習繁體與電腦', '学习繁体与电脑'),
            ('traditional', 'auto', '学习汉字与电脑', '學習漢字與電腦'),
            ('traditional', 'tw', '学习汉字与电脑', '學習漢字與電腦'),
            ('traditional', 'hk', '学习汉字与电脑', '學習漢字與電腦'),
            ('keep', 'auto', '學習汉字與电脑', '學習汉字與电脑')):
            with self.subTest(mode=mode, variant=variant):
                cleaner = CjkNormalizer(mode, variant, [], False)
                raw = f'<p id="繁體" title="繁體">{source}</p><code>繁體</code><svg><text>繁體</text></svg>'
                result = cleaner.process(raw.encode(), 9).decode()
                self.assertIn('>' + expected + '</p>', result)
                self.assertIn('id="繁體" title="繁體"', result)
                self.assertIn('<code>繁體</code><svg><text>繁體</text></svg>', result)


class PaidBasicDeliveryTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        if not self.jar.is_file():
            if os.environ.get('EPUBCHECK_JAR'):
                self.fail('Explicit EPUBCHECK_JAR is missing; acceptance cannot silently skip')
            self.skipTest('Real EPUBCheck JAR required; isolated unit tests do not replace this acceptance')
        from sqlalchemy import create_engine
        from app.storage_db import Base, PersistentJobStore
        from app import main, job_runner
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        self.main, self.runner = main, job_runner
        self.engine = create_engine('sqlite:///' + str(self.work / 'orders.db'))
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.store = PersistentJobStore(self.engine)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for module, name, value in ((main, 'job_store', self.store), (job_runner, 'job_store', self.store),
                (main, 'UPLOAD_DIR', self.work / 'uploads'), (main, 'OUTPUT_DIR', self.work / 'outputs'),
                (job_runner, 'OUTPUT_DIR', self.work / 'outputs')):
            self.stack.enter_context(patch.object(module, name, value))
        (self.work / 'uploads').mkdir(); (self.work / 'outputs').mkdir()
        self.stack.enter_context(patch('app.infra.execution_lease.tempfile.gettempdir', return_value=str(self.work)))
        self.stack.enter_context(patch.object(main, '_use_celery', return_value=True))
        self.checkout = self.stack.enter_context(patch('app.infra.alipay.create_alipay_precreate', return_value='https://offline.invalid/qr'))
        self.stack.enter_context(patch.object(main, 'verify_alipay_notification', return_value=True))
        self.queue, self.available = [], False
        def publish(job_id, attempt_id):
            if not self.available:
                raise ConnectionError('controlled offline broker')
            self.queue.append((job_id, attempt_id))
        self.stack.enter_context(patch('app.infra.job_dispatch_publisher.publish_conversion', side_effect=publish))
        self.execute = self.stack.enter_context(patch.object(job_runner, '_run_job_locked', wraps=job_runner._run_job_locked))
        from app.admin import router
        from app.admin.auth import password_hash
        self.stack.enter_context(patch.dict(os.environ, {
            'ADMIN_USERNAME': 'offline-auditor', 'ADMIN_PASSWORD_HASH': password_hash('offline-password')}))
        self.trades = {}
        self.stack.enter_context(patch.object(router, 'query_verified_trade', side_effect=lambda key: self.trades.get(key)))
        api = FastAPI()
        api.include_router(router.make_router(self.store, self.work / 'uploads', self.work / 'outputs', self.main._enqueue_conversion))
        for path, endpoint, methods in (
            ('/api/v2/jobs', main.create_job_v2, ['POST']),
            ('/api/v2/jobs/{job_id}', main.get_job_v2, ['GET']),
            ('/api/v2/jobs/{job_id}/download', main.download_result_v2, ['GET']),
            ('/api/v2/batches', main.create_batch_v2, ['POST']),
            ('/api/v2/batches/{batch_id}', main.get_batch_v2, ['GET']),
            ('/api/v2/batches/{batch_id}/download', main.download_batch_v2, ['GET']),
            ('/api/v2/webhooks/alipay', main.alipay_webhook, ['POST'])):
            api.add_api_route(path, endpoint, methods=methods)
        self.client = TestClient(api, base_url='https://testserver')
        self.addCleanup(self.client.close)

    def exercise_delivery(self, source, *, failure=False):
        from app.domain.job_dispatch_service import dispatch_pending
        from app.storage_db import PersistentJobStore
        from app.models import JobStatus
        from app.engine.cleaners.cjk_normalizer import CjkNormalizer
        original_hash = sha(source)
        form = {'output_mode': 'simplified', 'enable_translation': 'false',
                'enable_precision_polish': 'false', 'lexicon_domains_json': '[]', 'enable_proper_noun': 'false'}
        def upload():
            with source.open('rb') as file:
                return self.client.post('/api/v2/jobs', files={'file': ('fixture.epub', file, 'application/epub+zip')}, data=form, headers={'X-Client-Session': 'offline-basic-browser'})
        response = upload()
        self.assertEqual(response.status_code, 200, response.text)
        key = response.json()['job_id']
        owner = {'X-Job-Token': response.json()['access_token']}
        job = self.store.get(key)
        self.assertEqual(job.status, JobStatus.pending_payment)
        self.assertFalse(job.is_test_order)
        self.assertEqual(self.client.get(f'/api/v2/jobs/{key}/download', headers=owner).status_code, 400)
        trade = {'out_trade_no': key, 'trade_status': 'TRADE_SUCCESS', 'total_amount': job.expected_amount, 'trade_no': 'offline-' + key}
        self.trades[key] = trade
        self.assertEqual(self.client.post('/api/v2/webhooks/alipay', data={**trade, 'total_amount': '0.00'}).text, 'fail')
        self.assertEqual(self.store.get(key).status, JobStatus.pending_payment)
        self.assertEqual(self.store.list_dispatches(key), [])
        self.assertEqual(self.client.post('/api/v2/webhooks/alipay', data=trade).text, 'success')
        dispatches = self.store.list_dispatches(key)
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(dispatches[0]['status'], 'pending')
        self.assertFalse(self.queue)
        # Restart store/process state before broker recovery.
        self.store = PersistentJobStore(self.engine)
        self.main.job_store = self.runner.job_store = self.store
        self.available = True
        result = dispatch_pending(self.store, self.main._publish_conversion, job_id=key, now=dispatches[0]['next_attempt_at'] + .1)
        self.assertEqual(result['sent'], 1)
        self.assertEqual(len(self.queue), 1)
        with ExitStack() as faults, redirect_stdout(io.StringIO()):
            if failure:
                faults.enter_context(patch.object(CjkNormalizer, 'process', side_effect=RuntimeError('injected conversion failure')))
            self.runner.run_job(self.queue[0][0], expected_attempt_id=self.queue[0][1])
        delivered = self.store.get(key)
        self.assertEqual(sha(source), original_hash)
        if failure:
            self.assertEqual(delivered.status, JobStatus.failed)
            self.assertIsNone(delivered.output_path)
            self.assertEqual(self.client.get(f'/api/v2/jobs/{key}/download', headers=owner).status_code, 400)
            # Recovery uses fresh verified gateway facts and the existing paid
            # order, without creating another checkout or any model call.
            login = self.client.post('/api/admin/login', json={
                'username': 'offline-auditor', 'password': 'offline-password'})
            self.assertEqual(login.status_code, 200)
            recovered = self.client.post(f'/api/admin/orders/{key}/retry', json={'acknowledge_cost': True},
                                         headers={'X-CSRF-Token': login.json()['csrf']})
            self.assertEqual(recovered.status_code, 200, recovered.text)
            self.assertEqual(len(self.queue), 2)
            with redirect_stdout(io.StringIO()):
                self.runner.run_job(key, expected_attempt_id=self.queue[-1][1])
            delivered = self.store.get(key)
            self.assertEqual(delivered.expected_amount, job.expected_amount)
        self.assertEqual(delivered.status, JobStatus.success, delivered.message)
        self.assertTrue(delivered.translation_stats['epub_validation']['passed'])
        self.assertEqual(delivered.translation_stats['content_completeness']['status'], 'passed')
        assert_preserved(self, source, Path(delivered.output_path))
        public = self.client.get(f'/api/v2/jobs/{key}', headers=owner)
        self.assertEqual(public.status_code, 200, public.text)
        body = self.client.get(public.json()['download_url'], headers=owner)
        self.assertEqual(body.status_code, 200)
        self.assertEqual(hashlib.sha256(body.content).hexdigest(), sha(delivered.output_path))
        self.assertEqual(self.client.get(f'/api/v2/jobs/{key}/download').status_code, 403)
        # Retransmission of payment, delivery and upload must not execute twice.
        self.assertEqual(self.client.post('/api/v2/webhooks/alipay', data=trade).text, 'success')
        self.runner.run_job(key, expected_attempt_id=self.queue[0][1])
        self.assertEqual(self.execute.call_count, 2 if failure else 1)
        duplicate = upload()
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        self.assertEqual(duplicate.json()['detail']['job_id'], key)
        self.assertEqual(duplicate.json()['detail']['access_token'], owner['X-Job-Token'])
        self.assertEqual(len(self.store.list_dispatches(key)), 2 if failure else 1)
        self.assertLessEqual(self.checkout.call_count, 1)
        output = Path(delivered.output_path)
        data = output.read_bytes()
        output.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        self.assertEqual(self.client.get(f'/api/v2/jobs/{key}/download', headers=owner).status_code, 410)

    def test_paid_conversion_broker_recovery_download_and_deduplication(self):
        self.exercise_delivery(self.source)

    def test_failed_conversion_is_not_released_to_paid_customer(self):
        self.exercise_delivery(self.source, failure=True)

    def test_batch_paid_once_delivers_two_valid_artifacts_and_checks_every_member(self):
        from app.models import JobStatus
        from app.domain.job_dispatch_service import dispatch_pending
        body = self.source.read_bytes()
        response = self.client.post('/api/v2/batches', files=[
            ('files', ('one.epub', body, 'application/epub+zip')),
            ('files', ('two.epub', body, 'application/epub+zip'))],
            data={'output_mode': 'simplified'}, headers={'X-Client-Session': 'offline-batch-browser'})
        self.assertEqual(response.status_code, 200, response.text)
        key = response.json()['batch_id']
        owner = {'X-Job-Token': response.json()['access_token']}
        jobs = self.store.list_jobs()
        leader = next(j for j in jobs if j.batch_index == 0)
        trade = {'out_trade_no': 'batch_' + key, 'trade_status': 'TRADE_SUCCESS',
                 'total_amount': leader.expected_amount, 'trade_no': 'offline-batch'}
        for _ in range(2):
            self.assertEqual(self.client.post('/api/v2/webhooks/alipay', data=trade).text, 'success')
        intents = self.store.list_dispatches()
        self.assertEqual(len(intents), 2)
        self.available = True
        for item in intents:
            dispatch_pending(self.store, self.main._publish_conversion, job_id=item['job_id'],
                             now=item['next_attempt_at'] + .1)
        with redirect_stdout(io.StringIO()):
            for job_id, attempt in self.queue:
                self.runner.run_job(job_id, expected_attempt_id=attempt)
        delivered = self.store.list_jobs()
        self.assertTrue(all(j.status == JobStatus.success for j in delivered), [j.message for j in delivered])
        self.assertTrue(all(j.translation_stats['epub_validation']['passed'] for j in delivered))
        url = f'/api/v2/batches/{key}/download'
        result = self.client.get(url, headers=owner)
        self.assertEqual(result.status_code, 200, result.text[:200] if result.status_code != 200 else '')
        with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
            self.assertEqual(len(archive.namelist()), 2)
            self.assertEqual(sorted(hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist()),
                             sorted(sha(j.output_path) for j in delivered))
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.execute.call_count, 2)
        self.assertEqual(self.checkout.call_count, 1)
        self.assertFalse(list((self.work / 'outputs').glob('batch-*.zip')))
        Path(delivered[0].output_path).unlink()
        self.assertEqual(self.client.get(url, headers=owner).status_code, 410)

    def test_private_failed_originals_through_same_delivery_chain(self):
        manifest = os.environ.get('BASIC_DELIVERY_MANIFEST')
        if not manifest:
            self.skipTest('Private real-book gate requires an explicit server-local SHA-pinned manifest')
        cases = json.loads(Path(manifest).read_text())
        self.assertGreaterEqual(len(cases), 2)
        # Each subcase gets independent order state and controlled transport.
        for case in cases:
            with self.subTest(sha256=case['sha256'][:12]):
                source = Path(case['path'])
                self.assertEqual(sha(source), case['sha256'])
                self.queue.clear(); self.available = False; self.execute.reset_mock(); self.checkout.reset_mock()
                self.exercise_delivery(source)


if __name__ == '__main__':
    unittest.main()
