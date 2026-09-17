"""Preview/feedback auth, sandboxing, persistence and real-book read-only checks."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

_runtime = tempfile.TemporaryDirectory()
os.environ['DATABASE_URL'] = 'sqlite:///' + _runtime.name + '/jobs.db'
os.environ['OPENAI_API_KEY'] = 'offline-only'
os.environ['ALIPAY_APP_ID'] = ''
from fastapi.testclient import TestClient
from app import main
from app.models import Job, JobStatus, OutputMode
from app.domain.book_preview_service import build_book_preview
from app.domain.feedback_service import FeedbackLimiter


class PreviewFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.tmp.name) / 'fixture.epub'
        with zipfile.ZipFile(self.output, 'w') as z:
            z.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="EPUB/book.opf"/></rootfiles></container>')
            z.writestr('EPUB/book.opf', '<package><manifest><item id="one" href="one.xhtml" media-type="application/xhtml+xml"/><item id="two" href="two.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="one"/><itemref idref="two"/></spine></package>')
            z.writestr('EPUB/one.xhtml', '<html><body onload="steal()"><h1>第一章</h1><script>steal()</script><iframe src="https://evil.invalid"></iframe><img src="https://evil.invalid/a.png"><img src="../image.png" onerror="steal()"><a href="javascript:steal()">链接</a><p style="background:url(https://evil.invalid)">正文</p><svg><script>bad()</script></svg></body></html>')
            z.writestr('EPUB/two.xhtml', '<html><body><h1>第二章</h1></body></html>')
            z.writestr('image.png', b'\x89PNG\r\n\x1a\nfixture')
        self.id = Path(self.tmp.name).name
        main.job_store.add(Job(id=self.id, trace_id='offline', source_filename='fixture.epub', input_path=str(self.output),
                               output_path=str(self.output), output_mode=OutputMode.simplified, status=JobStatus.success,
                               access_token='test-only-token'))
        self.client = TestClient(main.app)
        self.headers = {'X-Job-Token': 'test-only-token'}
        main.feedback_limiter = FeedbackLimiter()

    def tearDown(self):
        self.client.close(); self.tmp.cleanup()

    def test_preview_is_authenticated_and_read_only(self):
        before = hashlib.sha256(self.output.read_bytes()).hexdigest()
        path = f'/api/v2/jobs/{self.id}/preview'
        self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.get(path, headers={'X-Job-Token': 'wrong'}).status_code, 403)
        result = self.client.get(path, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers['cache-control'], 'no-store')
        html = result.json()['html']
        for forbidden in ['<script', '<iframe', 'onload', 'onerror', 'javascript:', 'evil.invalid', '<svg']:
            self.assertNotIn(forbidden, html)
        self.assertIn('Content-Security-Policy', html)
        self.assertIn('data:image/png;base64,', html)
        self.assertEqual(result.json()['images_omitted'], 2)
        self.assertEqual(result.json()['total_chapters'], 2)
        self.assertIn('第二章', self.client.get(path+'?chapter=1', headers=self.headers).json()['html'])
        self.assertEqual(self.client.get(path+'?chapter=-1', headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get(path+'?chapter=2', headers=self.headers).status_code, 404)
        self.assertEqual(hashlib.sha256(self.output.read_bytes()).hexdigest(), before)

    def test_feedback_cannot_target_another_users_book(self):
        payload = {'job_id': self.id, 'type': 'translation', 'message': '测试内容，仅写测试目录'}
        self.assertEqual(self.client.post('/api/v2/feedback', json=payload).status_code, 403)
        with patch.object(main, 'BASE_DIR', Path(self.tmp.name)):
            response = self.client.post('/api/v2/feedback', json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            saved = json.loads((Path(self.tmp.name)/'feedback.jsonl').read_text())
            self.assertEqual(saved['job_id'], self.id)
        with patch.object(main, 'persist_feedback', side_effect=OSError('test storage failure')):
            self.assertEqual(self.client.post('/api/v2/feedback', json=payload, headers=self.headers).status_code, 503)

    def test_encoded_markup_stays_text_and_svg_raster_cover_is_safe(self):
        from app.domain.book_preview_service import sanitize_chapter
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'safe.epub'
            with zipfile.ZipFile(path, 'w') as z:
                z.writestr('chapter.xhtml', '<html><body>&lt;script&gt;text, not code&lt;/script&gt; &amp; words<svg><image xlink:href="cover.png" onload="evil()"/></svg></body></html>')
                z.writestr('cover.png', b'\x89PNG\r\n\x1a\nfixture')
            with zipfile.ZipFile(path) as z:
                html, omitted = sanitize_chapter(z, 'chapter.xhtml', '封面')
            self.assertIn('&lt;script&gt;text, not code&lt;/script&gt;', html)
            self.assertNotIn('<script', html)
            self.assertNotIn('onload', html)
            self.assertIn('data:image/png;base64,', html)
            self.assertEqual(omitted, 0)

    def test_feedback_validation_and_existing_general_suggestion(self):
        base = {'job_id': 'general_suggestion', 'type': 'suggestion', 'message': '测试建议'}
        for payload in [[], {**base, 'message': 'x'*2001}, {**base, 'type': 'unknown'}, {**base, 'message': {}}]:
            self.assertEqual(self.client.post('/api/v2/feedback', json=payload).status_code, 400)
        self.assertEqual(self.client.post('/api/v2/feedback', content='x'*16385).status_code, 413)
        with patch.object(main, 'BASE_DIR', Path(self.tmp.name)):
            for _ in range(10): self.assertEqual(self.client.post('/api/v2/feedback', json=base).status_code, 200)
            self.assertEqual(self.client.post('/api/v2/feedback', json=base).status_code, 429)

    @unittest.skipUnless(os.environ.get('EPUB_REGRESSION_TRANSLATED_BOOK'), 'real translated book not provided')
    def test_real_selected_book_preview_without_modification(self):
        path = Path(os.environ['EPUB_REGRESSION_TRANSLATED_BOOK'])
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        first = build_book_preview(path)
        self.assertGreater(first['total_chapters'], 20)
        for chapter in range(first['total_chapters']):
            preview = build_book_preview(path, chapter)
            self.assertIn('Content-Security-Policy', preview['html'])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)


if __name__ == '__main__': unittest.main()
