"""PDF regression: synthetic documents + fake providers, no network/model/payment."""
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, NumberObject, DecodedStreamObject, ArrayObject, TextStringObject

from app.domain.pdf import config, local, pipeline, providers, state, text_quality


def fixture(path, kinds=('text', 'scan'), encrypted=False):
    writer = PdfWriter()
    for kind in kinds:
        page = writer.add_blank_page(width=400, height=600)
        if kind == 'text':
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(b'BT /F1 12 Tf 30 550 Td (A faithful translation preserves the exact original meaning.) Tj 0 -20 Td (This paragraph is used only for offline PDF regression testing.) Tj ET')
            page[NameObject('/Contents')] = writer._add_object(stream)
        elif kind == 'scan':
            # A raster-only page. It must not be accepted as empty extracted text.
            image = DecodedStreamObject()
            image.set_data(bytes([220, 225, 240]) * 100)
            image.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'), NameObject('/Width'): NumberObject(10), NameObject('/Height'): NumberObject(10), NameObject('/ColorSpace'): NameObject('/DeviceRGB'), NameObject('/BitsPerComponent'): NumberObject(8)})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/XObject'): DictionaryObject({NameObject('/Im0'): writer._add_object(image)})})
            stream = DecodedStreamObject();stream.set_data(b'q 400 0 0 600 0 0 cm /Im0 Do Q')
            page[NameObject('/Contents')] = writer._add_object(stream)
    if encrypted:
        writer.encrypt('not-a-real-password')
    writer.write(path)
    return path


def response(content=None, reason='stop'):
    return NS(choices=[NS(finish_reason=reason, message=NS(content=json.dumps(content or {'blocks': [{'text': 'Original source text'}], 'blank': False, 'uncertain': False})))])


def routing_fixture(path):
    """Real PDF objects: heading, code, small logo, hidden layer, columns, blank."""
    writer = PdfWriter()
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    font_ref = writer._add_object(font)
    image = DecodedStreamObject()
    image.set_data(bytes([100, 150, 210]) * 100)
    image.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'), NameObject('/Width'): NumberObject(10), NameObject('/Height'): NumberObject(10), NameObject('/ColorSpace'): NameObject('/DeviceRGB'), NameObject('/BitsPerComponent'): NumberObject(8)})
    image_ref = writer._add_object(image)
    streams = [
        b'BT /F1 20 Tf 30 550 Td (Chapter 1) Tj ET',
        rb'BT /F1 12 Tf 30 550 Td (def total\(a, b\):) Tj 0 -20 Td (    return a + b # 123.45) Tj ET',
        b'BT /F1 12 Tf 30 550 Td (A readable paragraph with a small decorative logo.) Tj ET q 30 0 0 30 350 560 cm /Im0 Do Q',
        b'q 400 0 0 600 0 0 cm /Im0 Do Q BT /F1 12 Tf 3 Tr 30 550 Td (Old hidden text that might contain recognition errors.) Tj ET',
        b'BT /F1 12 Tf 30 550 Td (Left first) Tj 0 -30 Td (Left second) Tj 0 -30 Td (Left third) Tj ET BT /F1 12 Tf 250 550 Td (Right first) Tj 0 -30 Td (Right second) Tj 0 -30 Td (Right third) Tj ET',
        b'',
    ]
    for content in streams:
        page = writer.add_blank_page(width=400, height=600)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font_ref}), NameObject('/XObject'): DictionaryObject({NameObject('/Im0'): image_ref})})
        stream = DecodedStreamObject(); stream.set_data(content)
        page[NameObject('/Contents')] = writer._add_object(stream)
    writer.write(path)
    return path


class PdfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'PDF_OCR_CONCURRENCY':'1','PDF_WORK_DIR': str(self.root / 'private'), 'PDF_TRANSLATION_ENABLED': '1', 'PDF_TRANSLATION_PRICE_PER_PAGE_CNY': '.20', 'PDF_DEEPSEEK_API_KEY': 'fake-key-never-send', 'MINERU_API_KEY': '', 'MINERU_LOCAL_PRIORITY_PAGES': '1000', 'PDF_OCR_MAX_REQUESTS_PER_JOB': '600'}, clear=False)
        self.env.start()
        self.source = fixture(self.root / 'test.pdf')
    def tearDown(self):
        self.env.stop();self.tmp.cleanup()

    def test_real_pdfium_classification_and_render(self):
        info = local.inspect_pdf(self.source)
        self.assertEqual((info['kind'], info['page_count'], info['ocr_pages']), ('mixed', 2, 1))
        image = local.render_page(self.source, 1, self.root / 'page.jpg')
        from PIL import Image
        with Image.open(image) as value:
            self.assertLessEqual(value.width * value.height, 4000000)
        self.assertEqual(image.stat().st_mode & 0o777, 0o600)

    def test_real_page_routes_short_code_logo_hidden_columns_and_blank(self):
        routing_fixture(self.source)
        info = local.inspect_pdf(self.source)
        self.assertEqual([p['route'] for p in info['pages']], ['local', 'local', 'local', 'ocr', 'ocr', 'local'])
        self.assertEqual(info['ocr_pages'], 2)
        self.assertIn('hidden_text_layer', info['pages'][3]['assessment']['reasons'])
        self.assertIn('ambiguous_reading_order', info['pages'][4]['assessment']['reasons'])
        self.assertEqual(info['pages'][5]['reason'], 'blank_page')
        self.assertIn('return a + b', info['pages'][1]['text'])

    def test_quality_checks_are_language_aware_and_conservative(self):
        for text in ['第一章', '第 12 页', 'def f(x):\n    return x * 0.99', '123.45', 'Chapitre un']:
            self.assertEqual(text_quality.assess(text)['route'], 'local')
        for text in ['Text \ufffd broken', 'Missing \ue000 glyph', 'not\x00valid', '++ == **']:
            self.assertEqual(text_quality.assess(text)['route'], 'ocr')
        self.assertEqual(text_quality.assess('Some readable text', complex_vectors=True)['route'], 'ocr')
        self.assertEqual(text_quality.assess('Some readable text', uncertain_geometry=True)['route'], 'ocr')
        self.assertEqual(text_quality.assess('', has_objects=True)['route'], 'ocr')
        self.assertEqual(text_quality.assess('', has_objects=False)['route'], 'local')

    def test_annotation_only_page_is_not_discarded_as_blank(self):
        writer = PdfWriter()
        page = writer.add_blank_page(width=400, height=600)
        annotation = DictionaryObject({NameObject('/Type'): NameObject('/Annot'), NameObject('/Subtype'): NameObject('/FreeText'),
                                      NameObject('/Rect'): ArrayObject([NumberObject(n) for n in (30, 400, 300, 550)]),
                                      NameObject('/Contents'): TextStringObject('Content in annotation only'),
                                      NameObject('/DA'): TextStringObject('/Helv 12 Tf 0 g')})
        page[NameObject('/Annots')] = ArrayObject([writer._add_object(annotation)])
        writer.write(self.source)
        info = local.inspect_pdf(self.source)
        self.assertEqual(info['pages'][0]['route'], 'ocr')
        self.assertNotEqual(info['pages'][0]['reason'], 'blank_page')

    def test_unified_extraction_retains_reference_and_only_calls_flash_for_uncertain_pages(self):
        routing_fixture(self.source)
        pf = pipeline.preflight(self.source, 'layer-reference')
        # Public preflight must never carry the source text or private assessment.
        self.assertNotIn('Old hidden', json.dumps(pf))
        with patch.object(providers.Flash, 'call', return_value=response()) as call:
            output, summary = pipeline.prepare(self.source, 'layer-reference', pf['pdf'])
        self.assertEqual(call.call_count, 2)
        self.assertEqual(summary['provider_pages'], {'local': 4, 'mineru': 0, 'deepseek-flash': 2})
        self.assertEqual(summary['embedded_reference_pages'], 5)
        self.assertEqual(summary['text_difference_pages'], 2)
        directory = state.job_dir('layer-reference')
        hidden = state.read(directory / 'page-4.json')
        self.assertIn('Old hidden', hidden['embedded_reference']['text'])
        self.assertEqual(hidden['embedded_reference']['comparison']['status'], 'text_difference')
        self.assertFalse(hidden['embedded_reference']['comparison']['semantic_accuracy_verified'])
        self.assertEqual((directory / 'page-4.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(state.read(directory / 'page-2.json')['embedded_reference']['comparison']['status'], 'embedded_text_reused')
        # Original reference is private metadata, not duplicate untranslated content in EPUB.
        with zipfile.ZipFile(output) as archive:
            self.assertNotIn(b'Old hidden', archive.read('OEBPS/page-0004.xhtml'))
        output.unlink()
        hidden.pop('embedded_reference'); state.save(directory / 'page-4.json', hidden)
        with patch.object(providers.Flash, 'call', side_effect=AssertionError('must reuse cached OCR')):
            pipeline.prepare(self.source, 'layer-reference', pf['pdf'])
        self.assertIn('Old hidden', state.read(directory / 'page-4.json')['embedded_reference']['text'])

    def test_mineru_never_overwrites_reliable_native_text(self):
        pf = pipeline.preflight(self.source, 'mineru-native')
        os.environ['MINERU_API_KEY'] = 'fake-mineru'
        remote = {str(i): {'provider': 'mineru', 'blocks': [{'type': 'text', 'text': 'OCR candidate text'}]} for i in (1, 2)}
        with patch.object(providers.MinerU, 'extract', return_value=remote), patch.object(providers.Flash, 'call', side_effect=AssertionError('unexpected call')):
            _, summary = pipeline.prepare(self.source, 'mineru-native', pf['pdf'])
        native = state.read(state.job_dir('mineru-native') / 'page-1.json')
        self.assertEqual(native['provider'], 'local')
        self.assertIn('faithful translation', native['blocks'][0]['text'])
        self.assertEqual(summary['provider_pages'], {'local': 1, 'mineru': 1, 'deepseek-flash': 0})

    def test_reference_overlap_is_informational_and_preserves_numbers(self):
        page = {'text': '原文价格 123.45 元', 'route': 'ocr'}
        same = text_quality.reference(page, {'provider': 'mineru', 'blocks': [{'text': '原文价格 123.45 元'}]})
        changed = text_quality.reference(page, {'provider': 'mineru', 'blocks': [{'text': '原文价格 999.99 元'}]})
        self.assertEqual(same['comparison']['status'], 'text_overlap')
        self.assertEqual(changed['comparison']['status'], 'text_difference')
        self.assertEqual(changed['text'], page['text'])

    def test_encrypted_and_corrupt_input_fail_without_ocr(self):
        fixture(self.source, encrypted=True)
        with self.assertRaises(config.PdfError): local.inspect_pdf(self.source)
        self.source.write_bytes(b'%PDF-broken')
        with self.assertRaises(config.PdfError): local.inspect_pdf(self.source)

    def test_preflight_has_no_cloud_requests_and_freezes_price(self):
        with patch.object(providers.Flash, 'call', side_effect=AssertionError('paid call')), patch.object(providers.MinerU, 'api', side_effect=AssertionError('remote')):
            result = pipeline.preflight(self.source, 'preflight')
        self.assertFalse(result['pdf']['preview_verified_by_ocr'])
        self.assertEqual(result['pricing']['price_cny'], '3.99')
        self.assertEqual(result['pdf']['preview_pages'], [1, 2])
        self.assertNotIn('fake-key', json.dumps(result))
        os.environ['PDF_TRANSLATION_PRICE_PER_PAGE_CNY'] = '10'
        self.assertEqual(result['pricing']['price_cny'], '3.99')

    def test_confirmed_pdf_retail_prices(self):
        with patch.dict(os.environ, {'PDF_TRANSLATION_PRICE_PER_PAGE_CNY': '0.20', 'PDF_TRANSLATION_MIN_PRICE_CNY': '3.99'}):
            for pages, amount in [(1, '3.99'), (10, '3.99'), (20, '4.00'), (50, '10.00'), (100, '20.00'), (200, '40.00')]:
                self.assertEqual(config.quote(pages)['price_cny'], amount)
            self.assertEqual(config.quote(100, 'high')['price_cny'], '30.00')
            self.assertEqual(config.quote(100, 'literary')['price_cny'], '40.00')
            self.assertEqual(config.quote(100, 'standard', 'deepseek-pro')['price_cny'], '68.00')

    def test_feature_requires_configuration_and_exact_official_key_reuse(self):
        self.assertTrue(config.capabilities()['enabled'])
        os.environ['PDF_TRANSLATION_ENABLED'] = '0'
        self.assertFalse(config.capabilities()['enabled'])
        os.environ['PDF_TRANSLATION_ENABLED'] = '1';os.environ['PDF_TRANSLATION_PRICE_PER_PAGE_CNY'] = '0'
        self.assertFalse(config.capabilities()['enabled'])
        with patch.dict(os.environ, {'PDF_DEEPSEEK_API_KEY': '', 'DEEPSEEK_API_KEY': '', 'OPENAI_API_KEY': 'other-provider', 'OPENAI_BASE_URL': 'https://elsewhere.invalid'}):
            with self.assertRaises(config.PdfError): config.flash_credentials()

    def test_text_route_no_ocr_and_valid_epub_page_links(self):
        fixture(self.source, ('text', 'text'))
        pf = pipeline.preflight(self.source, 'text')
        with patch.object(providers.Flash, 'call', side_effect=AssertionError('remote')):
            output, summary = pipeline.prepare(self.source, 'text', pf['pdf'])
        self.assertEqual(summary['provider_pages']['local'], 2)
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(output) as z:
            self.assertEqual(z.namelist()[0], 'mimetype')
            for name in z.namelist():
                if name.endswith(('.xhtml', '.opf', '.xml')): ET.fromstring(z.read(name))
            self.assertIn(b'page-list', z.read('OEBPS/nav.xhtml'))
            self.assertIn(b'pdf-page-2', z.read('OEBPS/page-0002.xhtml'))
            self.assertNotIn('OEBPS/images/page-2.jpg', z.namelist())
        # A cached complete source EPUB does not trigger extraction again.
        with patch.object(pipeline, 'render_page', side_effect=AssertionError('rerender')):
            self.assertEqual(pipeline.prepare(self.source, 'text', pf['pdf'])[0], output)

    def test_mixed_flash_resume_and_source_identity(self):
        pf = pipeline.preflight(self.source, 'mixed')
        with patch.object(providers.Flash, 'call', return_value=response()) as call:
            output, summary = pipeline.prepare(self.source, 'mixed', pf['pdf'])
            output.unlink()  # Rebuild artifact from page cache after disk/output loss.
            pipeline.prepare(self.source, 'mixed', pf['pdf'])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(summary['provider_pages'], {'local': 1, 'mineru': 0, 'deepseek-flash': 1})
        self.source.write_bytes(self.source.read_bytes() + b'changed')
        with self.assertRaises(config.PdfError): pipeline.prepare(self.source, 'mixed', pf['pdf'])

    def test_mineru_limits_original_source_not_split(self):
        pf = pipeline.preflight(self.source, 'large')
        directory = state.job_dir('large')
        info = state.read(directory / 'source.json'); info['page_count'] = 201
        state.save(directory / 'source.json', info)
        os.environ['MINERU_API_KEY'] = 'fake-mineru'
        with patch.object(providers.MinerU, 'extract', side_effect=AssertionError('must not split')), patch.object(providers.Flash, 'call', return_value=response()):
            pipeline.prepare(self.source, 'large', pf['pdf'])
        self.assertEqual(state.read(directory / 'route.json')['provider'], 'deepseek-flash')

    def test_quota_reservation_concurrency_rolling_and_idempotency(self):
        q = state.Quota('test-quota', self.root / 'quota.db')
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda i: q.reserve(str(i), 200, now=100000), range(8)))
        self.assertEqual(sum(values), 5)
        self.assertFalse(q.reserve('new', 1, now=100001))
        self.assertTrue(q.reserve('later', 200, now=186401))
        accepted = str(values.index(True))
        self.assertTrue(q.reserve(accepted, 200, now=100001))
        self.assertNotIn('test-quota', (self.root / 'quota.db').read_bytes().decode('latin1'))

    def test_quota_circuit_and_external_budget(self):
        q = state.Quota('a', self.root / 'q.db')
        self.assertFalse(q.reserve('a', 201, limit=200))
        q.open_circuit();self.assertFalse(q.reserve('b', 1))

    def test_signed_urls_reject_ssrf_redirect_hosts_and_credentials(self):
        for url in ['http://mineru.oss-cn-shanghai.aliyuncs.com/x', 'https://127.0.0.1/x', 'https://mineru.oss-cn-shanghai.aliyuncs.com.evil.test/x', 'https://user@mineru.oss-cn-shanghai.aliyuncs.com/x', 'https://mineru.oss-cn-shanghai.aliyuncs.com:444/x']:
            with self.assertRaises(config.PdfError): providers.signed_url(url, 'upload')
        self.assertEqual(providers.signed_url('https://mineru.oss-cn-shanghai.aliyuncs.com/x?token=opaque', 'upload'), 'https://mineru.oss-cn-shanghai.aliyuncs.com/x?token=opaque')

    def test_mineru_missing_page_and_untrusted_html(self):
        result = providers.mineru_pages([{'page_idx': 1, 'type': 'text', 'text': '<script>no execution</script>'}], 3)
        self.assertEqual(list(result), ['2'])
        with self.assertRaises(config.PdfError): providers.mineru_pages([{'page_idx': -1}], 3)

    def test_mineru_unknown_content_never_silently_dropped(self):
        result = providers.mineru_pages([{'page_idx': 0, 'type': 'text', 'text': 'known'}, {'page_idx': 0, 'type': 'new_schema', 'text': 'unknown'}], 1)
        self.assertTrue(result['1']['needs_flash'])

    def test_key_rotation_does_not_reset_priority_quota(self):
        q = state.Quota('old-key', self.root / 'shared.db')
        self.assertTrue(q.reserve('old', 1000))
        rotated = state.Quota('new-key', self.root / 'shared.db')
        self.assertFalse(rotated.reserve('new', 1))

    def test_flash_uncertain_and_lost_response_are_not_retried(self):
        directory = state.job_dir('uncertain');image = local.render_page(self.source, 1, directory / 'img.jpg')
        flash = providers.Flash(directory)
        with patch.object(flash, 'call', return_value=response({'uncertain': True, 'blocks': []})) as call:
            with self.assertRaises(config.PdfError): flash.extract(image, 1)
            with self.assertRaises(config.PdfError): flash.extract(image, 1)
        self.assertEqual(call.call_count, 1)
        with patch.object(flash, 'call', side_effect=RuntimeError('fake-key-never-send')):
            with self.assertRaises(config.PdfError) as ctx: flash.extract(image, 2)
        self.assertNotIn('fake-key', str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)

    def test_flash_truncation_splits_bounded_regions_and_caches(self):
        directory = state.job_dir('split');image = local.render_page(self.source, 1, directory / 'img.jpg')
        flash = providers.Flash(directory)
        with patch.object(flash, 'call', side_effect=[response(reason='length'), response(), response()]) as call:
            result = flash.extract(image, 1)
            self.assertEqual(len(result['blocks']), 2)
            self.assertEqual(flash.extract(image, 1), result)
        self.assertEqual(call.call_count, 3)

    def test_flash_budget_survives_retry(self):
        directory = state.job_dir('budget');image = local.render_page(self.source, 1, directory / 'img.jpg')
        flash = providers.Flash(directory);os.environ['PDF_OCR_MAX_REQUESTS_PER_JOB'] = '1'
        with patch.object(flash, 'call', return_value=response()) as call:
            flash.extract(image, 1)
            with self.assertRaises(config.PdfError): flash.extract(image, 2)
        self.assertEqual(call.call_count, 1)

    def test_cancel_before_any_ocr_and_same_job_lock(self):
        pf = pipeline.preflight(self.source, 'cancel')
        with patch.object(providers.Flash, 'call', side_effect=AssertionError('remote')):
            with self.assertRaises(RuntimeError): pipeline.prepare(self.source, 'cancel', pf['pdf'], cancel=lambda: (_ for _ in ()).throw(RuntimeError('cancelled')))
        with state.job_lock('cancel'):
            with self.assertRaises(config.PdfError):
                with state.job_lock('cancel'): pass


if __name__ == '__main__': unittest.main()
