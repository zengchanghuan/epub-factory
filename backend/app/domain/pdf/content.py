"""Provider content contracts. Preserve structure; never fetch embedded URLs."""
import json
import re
from copy import deepcopy
from .config import PdfError, PageReviewRequired

REFLOW_VERSION = 2

def flash_payload(raw, page):
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        raise PageReviewRequired(page, 'invalid_json') from None
    if not isinstance(payload, dict):
        raise PageReviewRequired(page, 'invalid_schema')
    if 'uncertain' not in payload:
        raise PageReviewRequired(page, 'missing_confidence_field')
    if payload['uncertain'] is True:
        raise PageReviewRequired(page, 'uncertain_content')
    if payload['uncertain'] is not False:
        raise PageReviewRequired(page, 'invalid_confidence_field')
    blocks = payload.get('blocks')
    if not isinstance(blocks, list) or len(blocks) > 2000:
        raise PageReviewRequired(page, 'invalid_blocks')
    if ('blank' in payload and type(payload['blank']) is not bool) or (payload.get('blank') is True and blocks):
        raise PageReviewRequired(page, 'invalid_blank_field')
    if not blocks and payload.get('blank') is not True:
        raise PageReviewRequired(page, 'unconfirmed_blank')
    checked = []
    for block in blocks:
        if (not isinstance(block, dict) or not isinstance(block.get('text'), str)
                or not block['text'].strip() or len(block['text']) > 50000):
            raise PageReviewRequired(page, 'invalid_blocks')
        kind = block.get('type', 'text')
        if kind not in {'text', 'code', 'equation', 'heading', 'footnote', 'caption', 'verse', 'list'}:
            raise PageReviewRequired(page, 'unknown_block_type')
        value = {'type': kind, 'text': block['text']}
        for key in ('layout', 'paragraph_id'):
            if key in block:
                if not isinstance(block[key], str) or not 0 < len(block[key]) <= 80:
                    raise PageReviewRequired(page, 'invalid_paragraph_metadata')
                value[key] = block[key]
        checked.append(value)
    return {'blocks': regroup_paragraphs(checked), 'raw_blocks': checked,
            'reflow_version': REFLOW_VERSION, 'provider': 'deepseek-flash'}


def mineru_pages(blocks, page_count):
    if not isinstance(blocks, list) or len(blocks) > 100000:
        raise PdfError('MinerU 内容列表无效')
    result = {}
    continuation_pages = set()
    for block in blocks:
        if not isinstance(block, dict):
            raise PdfError('MinerU 内容块无效')
        index = block.get('page_idx')
        if type(index) is not int or not 0 <= index < page_count:
            raise PdfError('MinerU 结果缺少可靠的原始页码')
        page = result.setdefault(str(index + 1), {'blocks': [], 'provider': 'mineru', 'parser_version': 2})

        def unresolved(reason):
            page['needs_flash'] = True
            if reason not in page.setdefault('review_reasons', []):
                page['review_reasons'].append(reason)

        def append(text, kind='text'):
            if not isinstance(text, str) or not text.strip() or len(text) > 100000:
                unresolved('invalid_' + kind)
                return
            page['blocks'].append({'type': kind, 'text': text})

        kind = block.get('type')
        if kind in {'header', 'footer', 'page_number'}:
            # Retained privately and in the page image, not repeated in body translation.
            value = block.get('text', '')
            if isinstance(value, str) and len(value) <= 10000:
                page.setdefault('marginalia', []).append({'type': kind, 'text': value})
            else:
                unresolved('invalid_marginalia')
        elif kind == 'text':
            value = block.get('text')
            if value == '' and block.get('bbox'):
                # Observed MinerU merges continuation text into a preceding page
                # and leaves an empty located block. Both page assignments need OCR;
                # do not guess a split from an old/possibly wrong embedded text layer.
                continuation_pages.update((index, index + 1))
            append(value, 'heading' if block.get('text_level') else 'text')
        elif kind == 'code':
            value = block.get('code_body')
            if isinstance(value, str):
                match = re.fullmatch(r'```[^\n]*\n(.*?)\n```\s*', value, re.S)
                value = match.group(1) if match else value
            append(value, 'code')
        elif kind == 'equation':
            append(block.get('text'), 'equation')
        elif kind == 'page_footnote':
            append(block.get('text'), 'footnote')
        elif kind == 'table':
            from bs4 import BeautifulSoup
            body = block.get('table_body')
            if not isinstance(body, str) or len(body) > 100000:
                unresolved('invalid_table')
            else:
                soup = BeautifulSoup(body, 'html.parser')
                rows = [[cell.get_text(' ', strip=True) for cell in row.find_all(['th', 'td'], recursive=False)]
                        for row in soup.find_all('tr')]
                if not rows or not any(any(row) for row in rows):
                    unresolved('invalid_table')
                else:
                    page['blocks'].append({'type': 'table', 'rows': rows,
                                           'text': '\n'.join('\t'.join(row) for row in rows)})
        elif kind not in {'image', 'chart'}:
            unresolved('unknown_block_type')
        for key in ('image_caption', 'image_footnote', 'table_caption', 'table_footnote',
                    'chart_caption', 'chart_footnote', 'code_caption'):
            items = block.get(key, [])
            if not isinstance(items, list):
                unresolved('invalid_caption')
            else:
                for item in items:
                    append(item, 'footnote' if 'footnote' in key else 'caption')
    for number in continuation_pages:
        if str(number) in result:
            page = result[str(number)]
            page['needs_flash'] = True
            page.setdefault('review_reasons', []).append('cross_page_continuation')
    return result


def _join_prose(left, right):
    # Keep every source character except boundary whitespace. Never infer whether
    # a printed hyphen is discretionary, and never join across physical pages.
    left, right = left.rstrip(), right.lstrip()
    cjk = lambda c: '\u3400' <= c <= '\u9fff' or c in '，。！？；：“”‘’、（）《》'
    separator = '' if left.endswith(('-', '\u00ad')) or (left and right and cjk(left[-1]) and cjk(right[0])) else ' '
    return left + separator + right


def regroup_paragraphs(blocks):
    """Only explicit prose layout/adjacent paragraph IDs authorize joining.

    Old responses without layout evidence retain their line breaks. Raw source and
    zero-based input block indices are saved for audit whenever text is regrouped.
    """
    output = []
    for index, original in enumerate(blocks):
        block = dict(original)
        if block.get('type') == 'text':
            paragraph = block.get('paragraph_id')
            if block.get('layout') == 'paragraph' or paragraph:
                block['source_text'] = block['text']
                block['source_block_indices'] = [index]
                # Explicit blank lines are still paragraph boundaries.
                paragraphs = re.split(r'\n\s*\n', block['text'])
                rebuilt = []
                for part in paragraphs:
                    lines = part.splitlines()
                    text = lines[0] if lines else ''
                    for line in lines[1:]:
                        text = text + '\n\n' + line if _paragraph_boundary(text, line) else _join_prose(text, line)
                    rebuilt.append(text)
                block['text'] = '\n\n'.join(rebuilt)
                previous = output[-1] if output else {}
                if (paragraph and previous.get('type') == 'text' and previous.get('paragraph_id') == paragraph
                        and not _paragraph_boundary(previous['text'], block['text'])):
                    previous['text'] = _join_prose(previous['text'], block['text'])
                    previous['source_text'] += '\n' + block['source_text']
                    previous['source_block_indices'].append(index)
                    continue
        output.append(block)
    return output


def _paragraph_boundary(left, right):
    # A repeated model paragraph_id is insufficient to erase a Chinese sentence
    # or dialogue boundary. Prefer an extra paragraph over mixing speakers.
    return ('\n\n' in left or '\n\n' in right or
            bool(re.search(r'[。！？：][”’」』）]*\s*$', left)) or
            bool(re.match(r'^(?:[\u3000]{1,}| {2,}|[“「『])', right)))


def refresh_result(result):
    """Reflow cached OCR offline; do not change validity or spend another call."""
    if result.get('provider') != 'deepseek-flash' or result.get('reflow_version') == REFLOW_VERSION:
        return result
    updated = deepcopy(result)
    raw = updated.get('raw_blocks')
    if raw is None:
        # Legacy caches retain source_text, but lose individual block boundaries.
        # Recover only what was recorded; don't invent layout or source words.
        raw = []
        for block in updated['blocks']:
            item = {key: value for key, value in block.items()
                    if key not in {'source_text', 'source_block_indices', 'id', 'source_page'}}
            item['text'] = block.get('source_text', block['text'])
            raw.append(item)
    blocks = ([part for item in raw for part in regroup_paragraphs([item])]
              if updated.get('split_regions') else regroup_paragraphs(raw))
    updated.update(raw_blocks=deepcopy(raw), blocks=blocks, reflow_version=REFLOW_VERSION)
    return updated
