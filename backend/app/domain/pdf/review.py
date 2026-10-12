"""Explicit local-operator approval for a retained OCR response missing a flag.

No provider calls. Content declared uncertain/malformed needs correction, not this
format-only approval. The digest binds approval to the exact inspected response.
"""
import hashlib
import json
from datetime import datetime, timezone
from .config import PdfError, PageReviewRequired
from .content import flash_payload
from .state import job_dir, job_lock, read, save


def inspect_response(job_id, page):
    directory = job_dir(job_id)
    info = read(directory / 'source.json') or {}
    if type(page) is not int or not 1 <= page <= info.get('page_count', 0):
        raise PdfError('待核验页码无效')
    state = read(directory / f'flash-{page}-full.json') or {}
    if state.get('state') != 'invalid' or not isinstance(state.get('response'), str):
        raise PdfError('该页没有可核验的完整原响应')
    raw = state['response']
    try:
        flash_payload(raw, page)
    except PageReviewRequired as exc:
        reason = exc.reason
    else:
        reason = 'manual_review_required'
    return {'page': page, 'reason': reason, 'response_sha256': hashlib.sha256(raw.encode()).hexdigest(),
            'source_sha256': info.get('sha256'), 'response_chars': len(raw)}


def approve_missing_flag(job_id, page, expected_sha256, reviewer, note):
    if not reviewer.strip() or not note.strip() or len(reviewer) > 100 or len(note) > 1000:
        raise PdfError('必须填写核验人和核验说明')
    with job_lock(job_id):
        metadata = inspect_response(job_id, page)
        if metadata['response_sha256'] != expected_sha256:
            raise PdfError('响应指纹不匹配，请重新核验，未修改检查点')
        if metadata['reason'] != 'missing_confidence_field':
            raise PdfError('仅允许审批缺字段响应；内容不确定或损坏必须另行核验修正')
        directory = job_dir(job_id)
        path = directory / f'flash-{page}-full.json'
        state = read(path)
        payload = json.loads(state['response'])
        payload['uncertain'] = False
        result = flash_payload(json.dumps(payload), page)
        review = {**metadata, 'reviewer': reviewer, 'note': note,
                  'approved_at': datetime.now(timezone.utc).isoformat()}
        # Invalidate derived artifacts BEFORE publishing the approval. If interrupted,
        # recovery rebuilds from unchanged page caches without duplicate OCR.
        (directory / 'artifact.json').unlink(missing_ok=True)
        (directory / f'page-{page}-review.json').unlink(missing_ok=True)
        save(path, {**state, 'state': 'done', 'result': result, 'review': review})
        return review
