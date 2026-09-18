#!/usr/bin/env python3
"""Isolated real-book conversion regressions; no paid calls or order writes.

Inventory contains only order/file metadata. Reports contain counts and hashes,
not paragraphs, credentials, IPs or session tokens. Never publishes test EPUBs.
"""
import argparse
from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import posixpath
import re
import signal
import subprocess
import sys
import time
import traceback
from urllib.parse import unquote, urlsplit
import zipfile


class PhaseDeadline(BaseException):
    """Do not let pipeline fallback handlers swallow a test deadline."""


BOOK_DEADLINE_SECONDS = 900  # Allows bounded large-book conversion and audits.


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()


def exception_metadata(exc):
    """Retain code locations only; exception messages may contain book text."""
    return {'exception': type(exc).__name__, 'frames': [
        {'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
        for frame in traceback.extract_tb(exc.__traceback__)]}


def epubcheck(path, jar, work):
    if not Path(jar).is_file(): return {'status': 'not_run', 'reason': 'missing_jar'}
    report = work / (path.stem + '-check.json')
    try:
        process = subprocess.run(['java', '-Xmx256m', '-jar', jar, str(path), '--json', str(report)],
                                 capture_output=True, text=True, timeout=60)
        data = json.loads(report.read_text())
        messages = data.get('messages', [])
        severity = Counter(item.get('severity') for item in messages)
        return {'status': 'passed' if not severity['FATAL'] and not severity['ERROR'] else 'failed',
                'returncode': process.returncode, 'severity': dict(severity),
                'message_ids': dict(Counter(item.get('ID') or item.get('id') for item in messages))}
    except Exception as exc:
        return {'status': 'not_run', 'reason': type(exc).__name__}


def epub_structure(path):
    from bs4 import BeautifulSoup
    from app.domain.epub_navigation_audit import audit_epub_navigation
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        media = {name: hashlib.sha256(archive.read(name)).hexdigest() for name in names
                 if Path(name).suffix.lower() in {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp'}}
        documents, missing, duplicate_ids, vertical_rules, text_chars = 0, 0, 0, 0, 0
        ids_by_file, soups = {}, {}
        for name in names:
            if Path(name).suffix.lower() in {'.xhtml', '.html', '.htm'}:
                soup = BeautifulSoup(archive.read(name), 'html.parser')
                soups[name] = soup
                ids = [tag.get('id') for tag in soup.find_all(id=True)]
                duplicate_ids += len(ids) - len(set(ids))
                ids_by_file[name] = set(ids)
                documents += 1
                text_chars += len(soup.get_text('', strip=True))
            if Path(name).suffix.lower() in {'.css', '.xhtml', '.html'}:
                vertical_rules += len(re.findall(rb'writing-mode\s*:\s*vertical-[lr]l', archive.read(name), re.I))
        for name, soup in soups.items():
            for tag in soup.find_all(['a', 'img', 'image']):
                value = tag.get('href') if tag.name == 'a' else (tag.get('src') or tag.get('xlink:href') or tag.get('href'))
                if not value: continue
                link = urlsplit(value)
                if link.scheme or link.netloc: continue
                target = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(link.path))) if link.path else name
                if target not in names or (link.fragment and unquote(link.fragment) not in ids_by_file.get(target, set())):
                    missing += 1
        try: navigation = audit_epub_navigation(archive, sample_limit=0)
        except Exception as exc: navigation = {'scan_error': type(exc).__name__}
        navigation.pop('navigation_samples', None)
    return {'documents': documents, 'text_chars': text_chars, 'media_files': len(media),
            'media_hashes': sorted(set(media.values())), 'broken_internal_references': missing,
            'duplicate_ids': duplicate_ids, 'vertical_css_rules': vertical_rules, **navigation}


def manifest_checks(path, job):
    from bs4 import BeautifulSoup, Tag
    from app.domain.manifest_service import build_manifest
    from app.engine.cleaners.semantics_translator import SemanticsTranslator
    from app.engine.chunk_extractor import visible_text_outside_media
    from app.engine.unpacker import EpubUnpacker
    started = time.monotonic()
    manifest = build_manifest(str(path), 'isolated-corpus')
    if manifest.get('error'): return {'status': 'failed', 'reason': 'manifest_load_failed'}
    book = EpubUnpacker(path).load_book()
    if book is None: return {'status': 'failed', 'reason': 'reducer_content_load_failed'}
    # The real reducer receives ebooklib item.get_content(), not ZIP bytes.
    # Malformed input is normalized by ebooklib, so ZIP-only XPath checks
    # would incorrectly report broken locators that the reducer can resolve.
    content_by_file = {item.get_name(): item.get_content() for item in book.get_items()
                       if item.get_name()}
    translator = SemanticsTranslator(target_lang=job.get('target_lang') or 'zh-CN')
    specs = [(ch, c) for ch in manifest['chapters'] for c in ch.get('chunks', [])]
    ids = [c['chunk_id'] for _, c in specs]
    matched = body = media_text = japanese = japanese_rejected = eligible = 0
    for chapter in manifest['chapters']:
        content = content_by_file.get(chapter['file_path'])
        if content is None: continue
        soup = BeautifulSoup(content, 'html.parser')
        # Validate every locator against one DOM index. Repeatedly scanning
        # all same-tag siblings here adds quadratic test-only overhead.
        nodes = {}
        stack = [(soup, '')]
        while stack:
            parent, parent_path = stack.pop()
            counters = Counter()
            for child in parent.children:
                if not isinstance(child, Tag): continue
                counters[child.name] += 1
                child_path = f'{parent_path}/{child.name}[{counters[child.name]}]'
                nodes[child_path] = child
                stack.append((child, child_path))
        for spec in chapter.get('chunks', []):
            matched += spec['locator'] in nodes
            if chapter['chapter_kind'] != 'body': continue
            body += 1
            # Reuse the indexed node: reparsing every large chunk is test-only
            # overhead and can falsely make a healthy manifest time out.
            block = nodes.get(spec['locator'])
            if block is None: continue
            text = visible_text_outside_media(block)
            should = translator._should_translate(text)
            eligible += should
            media_text += bool(block.find(['img', 'svg', 'image']) and text.strip())
            if re.search(r'[\u3041-\u3096\u30a1-\u30fa]', text):
                japanese += 1
                japanese_rejected += not should
    return {'status': 'passed' if matched == len(specs) and len(ids) == len(set(ids)) else 'failed',
            'locator_validation_input': 'ebooklib_content_used_by_reducer',
            'chapters': len(manifest['chapters']), 'chunks': len(specs), 'body_chunks': body,
            'chapter_kind_counts': dict(Counter(ch['chapter_kind'] for ch in manifest['chapters'])),
            'locators_matched': matched, 'duplicate_chunk_ids': len(ids) - len(set(ids)),
            'media_with_external_text': media_text, 'eligible_translation_chunks': eligible,
            'japanese_script_chunks': japanese, 'japanese_chunks_rejected_by_eligibility': japanese_rejected,
            'manifest_stats': manifest.get('stats', {}),
            'elapsed_seconds': round(time.monotonic() - started, 3)}


def pdf_checks(path):
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    counts, images = [], 0
    for page in reader.pages:
        counts.append(len((page.extract_text() or '').strip()))
        resources = page.get('/Resources')
        if resources:
            resources = resources.get_object()
            for ref in (resources.get('/XObject') or {}).get_object().values() if resources.get('/XObject') else []:
                images += ref.get_object().get('/Subtype') == '/Image'
    return {'pages': len(counts), 'text_chars': sum(counts), 'pages_without_text': counts.count(0),
            'image_objects': images, 'layout_visual_review': 'not_performed_by_this_runner'}


def one(job, args):
    started = time.monotonic()
    source = Path(job['input_path'])
    work = Path(args.work) / job['source_sha256'][:12]
    work.mkdir(parents=True, exist_ok=True)
    report = {'filename': job['source_filename'], 'source_sha256': job['source_sha256'],
              'order_ids': job['order_ids'], 'original_statuses': job['statuses'],
              'requested_translation': bool(job['enable_translation']), 'checks': {}, 'findings': [],
              'translation_quality': 'not_testable_without_a_translated_artifact',
              'test_mode': 'isolated_conversion_without_translation'}
    if digest(source) != job['source_sha256']: raise RuntimeError('source_changed_since_inventory')
    os.environ.update({'OPENAI_API_KEY': 'dummy', 'DATABASE_URL': 'sqlite:///' + str(work / 'jobs.sqlite3'),
        'CELERY_BROKER_URL': '', 'REDIS_URL': '', 'EPUB_BOOK_PROFILER_ENABLED': '0',
        'EPUB_LLM_RATE_LIMITER_ENABLED': '0', 'EPUB_LLM_GLOBAL_HEALTH_ENABLED': '0',
        'EPUB_TRANSLATION_CHECKPOINT_DB': str(work / 'translation_cache.db'),
        'EPUBCHECK_JAR': args.jar, 'OPENAI_MODEL': 'deepseek-flash', 'EPUB_DEFAULT_TRANSLATION_MODEL': 'deepseek-flash'})
    os.chdir(work)
    from app.converter import EpubConverter
    from app.models import OutputMode
    from app.engine.unpacker import EpubUnpacker
    original_load = EpubUnpacker.load_book
    def observed_load(unpacker):
        book = original_load(unpacker)
        if book is None and unpacker._last_error is not None:
            report.setdefault('engine_diagnostics', []).append(exception_metadata(unpacker._last_error))
        return book
    EpubUnpacker.load_book = observed_load
    def check(name, operation, seconds=45):
        phase_started = time.monotonic()
        report['active_phase'] = name
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        previous = signal.getsignal(signal.SIGALRM)
        def expired(_signum, _frame): raise PhaseDeadline('isolated phase deadline')
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            report['checks'][name] = operation()
        except (PhaseDeadline, Exception) as exc:
            report['checks'][name] = {'status': 'timed_out' if isinstance(exc, PhaseDeadline) else 'scan_error',
                                      'exception': type(exc).__name__}
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        report.setdefault('phase_seconds', {})[name] = round(time.monotonic() - phase_started, 3)
        report.pop('active_phase', None)
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if source.suffix.lower() == '.epub':
        check('source_epubcheck', lambda: epubcheck(source, args.jar, work), 65)
        check('source_structure', lambda: epub_structure(source))
    elif source.suffix.lower() == '.pdf':
        check('source_pdf', lambda: pdf_checks(source))
    output = work / 'converted.epub'
    def convert():
        result = EpubConverter().convert_file_to_horizontal(source, output,
            OutputMode(job['output_mode']), enable_translation=False, device=job.get('device') or 'generic',
            traditional_variant=job.get('traditional_variant') or 'auto',
            progress_callback=lambda _: None, stage_callback=lambda *args: None)
        return {'status': 'passed' if result.validation_passed else 'validation_failed',
                'validation_passed': result.validation_passed,
                'pipeline_metrics': result.metrics_summary,
                'elapsed_seconds': round(time.monotonic() - started, 3)}
    large_book = report['checks'].get('source_structure', {}).get('text_chars', 0) > 3000000
    check('conversion', convert, 180 if large_book else 90)
    if report['checks']['conversion'].get('status') == 'scan_error':
        report['checks']['conversion']['status'] = 'failed'
    if output.is_file():
        check('output_epubcheck', lambda: epubcheck(output, args.jar, work), 65)
        check('output_structure', lambda: epub_structure(output))
        audit_seconds = 180 if report['checks'].get('source_structure', {}).get('text_chars', 0) > 3000000 else 60
        check('output_manifest', lambda: manifest_checks(output, job), audit_seconds)
        report['test_output_sha256'] = digest(output)
        before = report['checks'].get('source_structure', {})
        after = report['checks']['output_structure']
        if before.get('navigation_broken_targets', 0) < after.get('navigation_broken_targets', 0):
            report['findings'].append('conversion_introduced_broken_navigation')
        if before.get('broken_internal_references', 0) < after.get('broken_internal_references', 0):
            report['findings'].append('conversion_increased_broken_internal_references')
        if after.get('vertical_css_rules'): report['findings'].append('vertical_css_remains_after_conversion')
        if 'source_pdf' in report['checks']:
            if report['checks']['source_pdf'].get('image_objects') and not after.get('media_files'):
                report['findings'].append('pdf_plain_text_adapter_omits_images')
            if report['checks']['source_pdf'].get('text_chars') == 0:
                report['findings'].append('pdf_no_text_placeholder_risk')
        report['media_binary_hashes_preserved'] = before.get('media_hashes') == after.get('media_hashes') if before else None
    if source.suffix.lower() == '.epub':
        audit_seconds = 180 if report['checks'].get('source_structure', {}).get('text_chars', 0) > 3000000 else 60
        check('source_manifest', lambda: manifest_checks(source, job), audit_seconds)
    src = report['checks'].get('source_manifest', {})
    if job['enable_translation'] and src.get('japanese_chunks_rejected_by_eligibility'):
        report['findings'].append('japanese_translation_eligibility_gap')
    if src.get('chunks') and src.get('body_chunks') == 0:
        report['findings'].append('nonempty_epub_has_zero_body_chunks')
    for name in ('source_structure', 'output_structure'):
        report['checks'].get(name, {}).pop('media_hashes', None)
    report['source_unchanged'] = digest(source) == job['source_sha256']
    if not report['source_unchanged']: report['findings'].append('source_changed_during_regression')
    failed_checks = [name for name, result in report['checks'].items()
                     if result.get('status') in {'failed', 'scan_error', 'timed_out', 'not_run', 'validation_failed'}]
    report['failed_or_incomplete_checks'] = failed_checks
    report['regression_status'] = 'needs_attention' if failed_checks or report['findings'] else 'passed_conversion_checks'
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', required=True)
    parser.add_argument('--work', required=True)
    parser.add_argument('--report', required=True)
    parser.add_argument('--jar', required=True)
    parser.add_argument('--backend', required=True)
    parser.add_argument('--one')
    parser.add_argument('--skip-source', action='append', default=[])
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.backend).resolve()))
    if args.one:
        job = json.loads(Path(args.one).read_text())
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()): result = one(job, args)
        except Exception as exc:
            result = {'filename': job['source_filename'], 'source_sha256': job['source_sha256'],
                      'order_ids': job['order_ids'], 'status': 'runner_error', 'exception': type(exc).__name__}
        Path(args.report).write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return
    inventory = json.loads(Path(args.inventory).read_text())
    groups = {}
    excluded = []
    for job in inventory['jobs']:
        if job['source_filename'].lower().endswith('.pdf'):
            excluded.append({'source_sha256': job['source_sha256'], 'filename': job['source_filename'],
                             'reason': 'PDF is not a supported public format'})
            continue
        groups.setdefault(job['source_sha256'], []).append(job)
    results = []
    report = {'start_utc': inventory['start_utc'], 'end_utc': inventory['end_utc'],
              'mode': 'offline; no paid model requests; no production order writes',
              'skipped_previously_tested_sources': args.skip_source, 'books': results}
    report['excluded_unsupported_sources'] = excluded
    work = Path(args.work).resolve(); work.mkdir(parents=True, exist_ok=True)
    for source_sha, jobs in groups.items():
        if source_sha in args.skip_source: continue
        job = {**jobs[0], 'order_ids': [j['id'] for j in jobs], 'statuses': [j['status'] for j in jobs]}
        if not source_sha:
            results.append({'filename': job['source_filename'], 'status': 'missing_source'}); continue
        job_file, result_file = work / (source_sha[:12] + '-job.json'), work / (source_sha[:12] + '-report.json')
        job_file.write_text(json.dumps(job, ensure_ascii=False))
        command = [sys.executable, str(Path(__file__).resolve()), '--inventory', args.inventory,
                   '--work', str(work), '--report', str(result_file), '--jar', args.jar,
                   '--backend', args.backend, '--one', str(job_file)]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=BOOK_DEADLINE_SECONDS)
            result = json.loads(result_file.read_text())
        except Exception as exc:
            result = json.loads(result_file.read_text()) if result_file.is_file() else {
                'filename': job['source_filename'], 'source_sha256': source_sha, 'order_ids': job['order_ids']}
            result.update(status='runner_error', exception=type(exc).__name__)
        results.append(result)
        report['summary'] = dict(Counter(book.get('regression_status') or book.get('status', 'unknown') for book in results))
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({'completed': len(results), 'total': len(groups) - len(args.skip_source),
                          'filename': job['source_filename'], 'checks': result.get('checks', {}).get('conversion'),
                          'findings': result.get('findings', []), 'exception': result.get('exception')}, ensure_ascii=False), flush=True)
    print('REGRESSION_REPORT=' + json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__': main()
