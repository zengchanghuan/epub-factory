"""Paid-job PDF preparation with durable page results; no OCR in preflight."""
import html
import os
import re
import time
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from .config import PdfError, PageReviewRequired, ensure_enabled, positive_int, quote
from .local import digest, inspect_pdf, render_page
from .providers import Flash, MinerU, MinerURejected
from .state import Quota, job_dir, job_lock, read, save
from .text_quality import reference, REFERENCE_VERSION
from .content import refresh_result, REFLOW_VERSION
from .math_render import MathAssets
from .concurrency import ordered_pages

_preflight_slots = threading.BoundedSemaphore(2)
ARTIFACT_VERSION = 2


def preflight(source, job_id, quality='standard', model='deepseek-flash', glossary=None, strategy='auto', *, service='translate'):
    if not _preflight_slots.acquire(blocking=False):
        raise PdfError('PDF 预检繁忙，请稍后重新上传')
    try:
        return _preflight(source, job_id, quality, model, glossary, strategy, service=service)
    finally:
        _preflight_slots.release()


def _preflight(source, job_id, quality='standard', model='deepseek-flash', glossary=None, strategy='auto', *, service='translate'):
    ensure_enabled(service)
    directory = job_dir(job_id)
    info = inspect_pdf(source)
    scans = info['ocr_pages']
    if scans > positive_int('PDF_OCR_MAX_REQUESTS_PER_JOB', 600, 5000):
        raise PdfError('PDF 待识别页数超过单任务 OCR 预算上限，请减少页数后上传')
    info['service_mode'] = service
    info['include_source_pages'] = False
    save(directory / 'source.json', info)
    price = quote(info['page_count'], quality, model, service=service)
    samples = sorted({1, max(1, info['page_count'] // 2), info['page_count']})
    for page in samples:
        render_page(source, page, directory / f'preview-{page}.jpg')
    return {
        'schema_version': 1, 'version': 1, 'confirmed': False, 'status': 'awaiting_confirmation',
        'resolved_strategy': strategy if strategy != 'auto' else 'neutral_faithful', 'glossary': glossary or {}, 'characters': [],
        'chapters': [], 'profile': {'genre': 'PDF 本地预检，尚未进行内容分析', 'confidence': 0},
        'confirmation_reasons': [
            '输出可重排 EPUB，不附原页图片。' + ('保留原语言，不翻译。' if service == 'ocr' else '识别后翻译为所选语言。'),
            '按 PDF 总页数计费；可靠文字层直接复用，扫描及疑难页面使用 OCR。',
            '以下仅为原页预览，尚未经过 OCR 验证；付款后开始处理，确认后不追加收费。'],
        'pdf': {'version': 1, 'page_count': info['page_count'], 'ocr_pages': scans,
                'service_mode': service, 'include_source_pages': False,
                'kind': info['kind'], 'preview_pages': samples, 'pricing': price,
                'source_sha256': info['sha256'], 'preview_verified_by_ocr': False},
        'pricing': price,
    }


def verify_source(source, job_id, public_pdf):
    info = read(job_dir(job_id) / 'source.json')
    if not info or info['sha256'] != public_pdf.get('source_sha256') or digest(source) != info['sha256']:
        raise PdfError('PDF 原文件或预检信息发生变化，请重新上传并确认报价')
    if (info.get('service_mode', 'translate') != public_pdf.get('service_mode', 'translate') or
            info.get('include_source_pages', True) != public_pdf.get('include_source_pages', True)):
        raise PdfError('PDF 服务选项与预检不一致，请重新上传并确认报价')
    return info


def prepare(source, job_id, public_pdf, progress=lambda _: None, cancel=lambda: None):
    directory = job_dir(job_id)
    with job_lock(job_id):
        workers = positive_int('PDF_OCR_CONCURRENCY', 2, 4)
        cancel()
        info = verify_source(source, job_id, public_pdf)
        artifact = directory / 'source.epub'
        manifest = read(directory / 'artifact.json', {})
        include_source = public_pdf.get('include_source_pages', True)
        if (artifact.exists() and manifest.get('version') == ARTIFACT_VERSION
                and manifest.get('include_source_pages', True) == include_source
                and manifest.get('sha256') == digest(artifact)):
            progress('PDF 提取检查点已复用')
            return artifact, manifest['summary']
        started = time.monotonic()
        needs = [p for p in info['pages'] if p['route'] == 'ocr']
        route_path = directory / 'route.json'
        route = read(route_path)
        key = os.environ.get('MINERU_API_KEY', '')
        quota = Quota(key) if key else None
        if route is None:
            # Limits apply to the ORIGINAL file, not a split/shrunken derivative.
            eligible = bool(needs and key and info['page_count'] <= 200 and Path(source).stat().st_size <= 200 * 1024 * 1024)
            # Deduct external account usage conservatively; default zero local allocation
            # until operator explicitly sets a known available budget for this deployment.
            try:
                limit = min(1000, max(0, int(os.environ.get('MINERU_LOCAL_PRIORITY_PAGES', '0'))))
            except ValueError:
                raise PdfError('MinerU 本地可用额度配置无效') from None
            chosen = 'mineru' if eligible and quota.reserve(job_id, info['page_count'], limit) else 'deepseek-flash' if needs else 'local'
            route = {'provider': chosen, 'original_pages': info['page_count'], 'version': 1}
            save(route_path, route)
        mineru = {}
        if route['provider'] == 'mineru':
            if not key:
                raise PdfError('原 MinerU 任务需要配置原账号密钥后继续查询')
            try:
                mineru = MinerU(key, directory, quota).extract(source, job_id, info['page_count'], cancel, progress)
            except MinerURejected:
                route['provider'] = 'deepseek-flash'
                route['fallback_reason'] = 'definite_pre_upload_rejection'
                save(route_path, route)
                progress('MinerU 未接收原文件，已切换 Flash OCR')
        flash = Flash(directory)
        completed, pending = [], []
        counts = {'local': 0, 'mineru': 0, 'deepseek-flash': 0}
        def extract_page(page, check):
            check()
            number = page['page']
            checkpoint = directory / f'page-{number}.json'
            result = read(checkpoint)
            image = directory / f'page-{number}.jpg'
            if not image.exists():
                render_page(source, number, image)
            if result is None:
                if page['route'] == 'local':
                    blocks = page.get('blocks')
                    if blocks is None:  # Older preflight; never pay to refresh it implicitly.
                        blocks = [{'type': 'text', 'text': p}
                                  for p in re.split(r'\r?\n\s*\r?\n', page['text']) if p.strip()]
                    result = {'provider': 'local', 'blocks': blocks}
                elif str(number) in mineru and mineru[str(number)]['blocks'] and not mineru[str(number)].get('needs_flash'):
                    result = mineru[str(number)]
                else:
                    # A completed MinerU result may explicitly omit a page. Only then
                    # identify that unresolved page with Flash (never parallel duplicate OCR).
                    try:
                        result = flash.extract(image, number, check)
                    except PageReviewRequired as exc:
                        # Only received, page-local content failures may continue.
                        # Authentication, balance, cancellation and unknown requests
                        # still propagate immediately, before any further paid work.
                        save(directory / f'page-{number}-review.json', {'page': number, 'reason': exc.reason})
                        result = {'provider': 'unresolved', 'blocks': [], 'page': number,
                                  'label': page['label'], 'review_required': True,
                                  'review_reason': exc.reason}
                        return result
            if (result.get('ir_version') != 3 or
                    result.get('embedded_reference', {}).get('version') != REFERENCE_VERSION or
                    result.get('provider') == 'deepseek-flash' and result.get('reflow_version') != REFLOW_VERSION):
                result = refresh_result(result)
                result.update({'page': number, 'label': page['label'], 'ir_version': 3})
                for index, block in enumerate(result['blocks']):
                    block.update({'id': f'p{number}-b{index + 1}', 'source_page': number})
                result['embedded_reference'] = reference(page, result)
                save(checkpoint, result)
            return result

        for result in ordered_pages(info['pages'], extract_page, workers=workers, cancel=cancel):
            number = result['page']
            if result.get('review_required'):
                pending.append({'page': number, 'reason': result['review_reason']})
                progress(f'PDF 第 {number} 页待核验，继续处理其他页；不会重复请求本页')
            else:
                counts[result['provider']] += 1
            completed.append(result)
            progress(f'PDF 提取与原页对照：{len(completed)}/{info["page_count"]} 页')
        cancel()
        temporary = directory / 'source.tmp.epub'
        title = info.get('title') or Path(source).stem.removeprefix(job_id + '-')
        rendering = build_epub(completed, directory, temporary, title, include_source_pages=include_source)
        temporary.replace(artifact)
        os.chmod(artifact, 0o600)
        summary = {'pages_total': len(completed), 'pages_extracted': len(completed) - len(pending),
                   'provider_pages': counts, 'extraction_elapsed_seconds': round(time.monotonic() - started, 2),
                   'original_images_preserved': include_source, 'ocr_quality_verified': False, 'page_concurrency': workers,
                   'service_mode': public_pdf.get('service_mode', 'translate'),
                   'review_required': bool(pending), 'pending_pages': pending, 'rendering': rendering}
        summary['embedded_reference_pages'] = sum(bool(p.get('embedded_reference', {}).get('text', '').strip()) for p in completed)
        summary['text_difference_pages'] = sum(p.get('embedded_reference', {}).get('comparison', {}).get('status') == 'text_difference' for p in completed)
        save(directory / 'artifact.json', {'version': ARTIFACT_VERSION, 'include_source_pages': include_source,
                                         'sha256': digest(artifact), 'summary': summary})
        return artifact, summary


def _escape(value):
    return html.escape(re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value)), quote=True)


def _block_html(block, math=None, page=0):
    kind, text = block.get('type'), _escape(block['text'])
    if kind == 'equation' and math is not None:
        return '<div class="pdf-equation">' + math.equation(block['text'], page) + '</div>'
    if kind in {'code', 'equation'}:
        # Native code tags are protected by the existing translation/STEM pipeline.
        return f'<pre class="pdf-{kind}"><code>{text}</code></pre>'
    if kind == 'table' and block.get('rows'):
        return '<table><tbody>' + ''.join('<tr>' + ''.join('<td><p>' + (math.inline(cell, page) if math else _escape(cell)) + '</p></td>' for cell in row) + '</tr>' for row in block['rows']) + '</tbody></table>'
    if kind == 'heading':
        return '<h3>' + (math.inline(block['text'], page) if math is not None else text) + '</h3>'
    tag = '<p' + (' class="pdf-footnote"' if kind == 'footnote' else '') + '>'
    if math is not None:
        return tag + math.inline(block['text'], page, paragraph_tag=tag) + '</p>'
    text = re.sub(r'(?<!\\)\$([^$\n]+)\$', r'<code class="pdf-math">\1</code>', text)
    return tag + text.replace('\n\n', '</p>' + tag).replace('\n', '<br/>') + '</p>'


def apply_review_gate(result, summary):
    """Partial source may be translated/checkpointed, but never delivered as complete."""
    if not summary.get('review_required'):
        return
    from ...models import ErrorCode
    stats = dict(getattr(result, 'translation_stats', {}) or {})
    stats.update(pdf_extraction=summary, deliverable=False, delivery_gate_failed=True)
    result.translation_stats = stats
    flags = set((stats.get('qa_report') or {}).get('flags') or [])
    only_pdf_review = bool(flags) and flags <= {'pdf_ocr_review_required', 'partial_translation'}
    if result.validation_passed or only_pdf_review:
        result.validation_passed = False
        result.error_code = ErrorCode.PARTIAL_TRANSLATION.value
        numbers = ', '.join(str(p['page']) for p in summary['pending_pages'][:20])
        result.message = (f'PDF 第 {numbers} 页待核验；其他页面已处理并保留检查点。'
                          '完整 EPUB 尚未就绪，请联系客服核验后继续，无需重新上传。')


def build_epub(pages, directory, destination, title, *, language=None, include_source_pages=True):
    """One chapter per physical page; stable anchors and EPUB 3 page-list."""
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    manifest, spine, links = [], [], []
    math = MathAssets(include_source_pages=include_source_pages)
    if language is None:
        sample = ''.join(b['text'] for p in pages for b in p['blocks'])[:50000]
        language = 'zh-Hans' if len(re.findall(r'[\u3400-\u9fff]', sample)) > len(re.findall(r'[a-zA-Z]', sample)) else 'en'
    if not re.fullmatch(r'[a-zA-Z]{2,8}(?:-[a-zA-Z0-9]{1,8})*', language):
        raise ValueError('invalid document language')
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as book:
        book.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        book.writestr('META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        for page in pages:
            number = page['page']
            name = f'page-{number:04d}.xhtml'
            label = _escape(page['label'])
            page_title = (f'原 PDF 第 {number} 页' if include_source_pages else
                          _escape(page.get('title') or next((b['text'] for b in page['blocks'] if b['type'] == 'heading'), f'第 {number} 页')))
            body = ''.join(_block_html(b, math, number) for b in page['blocks'])
            if page.get('review_required'):
                body = ('<p class="pdf-review">本页 OCR 内容待核验。' +
                        ('原页保留如下；' if include_source_pages else '') + '其余页面继续处理。</p>')
            reference = ''
            heading = ''
            if include_source_pages:
                heading = f'<h2>原 PDF 第 {number} 页 · 印刷页码 {label}</h2>'
                reference = f'<figure id="original-page-{number}"><figcaption>原页对照（图片内文字保留原文）</figcaption><img src="images/page-{number}.jpg" alt="原 PDF 第 {number} 页"/></figure>'
            book.writestr('OEBPS/' + name, f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{language}" xml:lang="{language}">
<head><title>{page_title}</title><link rel="stylesheet" href="pdf.css" type="text/css"/></head>
<body><span id="pdf-page-{number}" epub:type="pagebreak" role="doc-pagebreak" aria-label="{number}"/>
{heading}{body}{reference}</body></html>''')
            if include_source_pages:
                book.write(directory / f'page-{number}.jpg', f'OEBPS/images/page-{number}.jpg')
                manifest.append(f'<item id="i{number}" href="images/page-{number}.jpg" media-type="image/jpeg"/>')
            properties = ' properties="mathml"' if '<math ' in body else ''
            manifest.append(f'<item id="p{number}" href="{name}" media-type="application/xhtml+xml"{properties}/>')
            spine.append(f'<itemref idref="p{number}"/>')
            links.append(f'<li><a href="{name}#pdf-page-{number}">{page_title}</a></li>')
        for index, (name, svg) in enumerate(math.assets.items()):
            book.writestr('OEBPS/' + name, svg)
            manifest.append(f'<item id="math{index}" href="{name}" media-type="image/svg+xml"/>')
        book.writestr('OEBPS/pdf.css', 'body{line-height:1.7;margin:1em;}p{margin:.7em 0;}img{max-width:100%;height:auto;}figure{margin:2em 0;page-break-inside:avoid;}figcaption{color:#666;font-size:.85em;}h2{font-size:1.2em;}h3{font-size:1.1em;}pre{white-space:pre-wrap;overflow-wrap:anywhere;}table{border-collapse:collapse;}td{border:1px solid #aaa;padding:.3em;} .pdf-footnote{font-size:.85em;} .pdf-review{border:1px solid #a65;padding:.7em;} .pdf-equation{margin:1em 0;overflow-x:auto;white-space:pre-wrap;} .pdf-equation .pdf-math-image{max-width:none;} .pdf-math-reference{font-size:.75em;} math{font-family:STIX Two Math,Cambria Math,serif;}')
        nav_title = '原 PDF 页码对照' if include_source_pages else '目录'
        description = 'PDF 原文提取与原页对照；内容准确性需另行核验。' if include_source_pages else 'PDF 提取正文；不附带原页图片。'
        book.writestr('OEBPS/nav.xhtml', f'''<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="zh" xml:lang="zh"><head><title>{nav_title}</title></head><body><nav epub:type="toc" id="toc"><h1>{nav_title}</h1><ol>{''.join(links)}</ol></nav><nav epub:type="page-list" id="pages"><h2>页码</h2><ol>{''.join(links)}</ol></nav></body></html>''')
        book.writestr('OEBPS/content.opf', f'''<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">urn:pdf:{_escape(directory.name)}</dc:identifier><dc:title>{_escape(title)}</dc:title><dc:language>{language}</dc:language><dc:description>{description}</dc:description><meta property="dcterms:modified">{now}</meta></metadata><manifest>{''.join(manifest)}<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="css" href="pdf.css" media-type="text/css"/></manifest><spine>{''.join(spine)}</spine></package>''')
    return dict(math.report, language=language, source_pages_included=include_source_pages)
