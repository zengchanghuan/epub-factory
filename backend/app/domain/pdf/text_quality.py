"""Conservative, offline text-layer assessment; never a claim of OCR accuracy."""
from collections import Counter
from difflib import SequenceMatcher
import hashlib
import re
import unicodedata

REFERENCE_VERSION = 2


def assess(text, *, image_ratio=0, multi_column=False, invisible=False,
           complex_vectors=False, uncertain_geometry=False, has_objects=True, math_layout=False):
    characters = [c for c in text if not c.isspace()]
    invalid = sum(c == '\ufffd' or unicodedata.category(c) in {'Co', 'Cs', 'Cn', 'Cc'}
                  for c in characters)
    reasons = []
    if not characters:
        if has_objects:
            reasons.append('no_usable_text')
    elif invalid / len(characters) >= .01:
        reasons.append('invalid_text_encoding')
    elif not any(c.isalnum() for c in characters):
        reasons.append('symbols_without_readable_text')
    if any(unicodedata.category(c) == 'Co' for c in characters):
        reasons.append('private_use_glyph')
    if invisible:
        reasons.append('hidden_text_layer')
    if image_ratio > .08:
        reasons.append('substantial_image_content')
    if multi_column:
        reasons.append('ambiguous_reading_order')
    if complex_vectors:
        reasons.append('complex_vector_layout')
    if uncertain_geometry:
        reasons.append('uncertain_page_geometry')
    if math_layout or re.search(r'[∫∑∏√∂∇]|[⁰¹²³⁴⁵⁶⁷⁸⁹]', text):
        reasons.append('math_layout_requires_ocr')
    # A short visible heading/folio does not itself justify a model call.
    kind = ('mixed' if characters else 'scan') if image_ratio > .08 else 'text'
    if not has_objects and not characters:
        kind = 'blank'
    return {'version': 4, 'route': 'ocr' if reasons else 'local',
            'source_kind': kind, 'reasons': reasons or ['blank_page' if kind == 'blank' else 'embedded_text'],
            'text_chars': len(characters), 'invalid_chars': invalid,
            'image_area_ratio': round(min(1, max(0, image_ratio)), 4),
            'reliable_for_direct_use': not reasons}


def tokens(text):
    # CJK characters separately; words/numbers elsewhere. Counter comparison ignores
    # layout order intentionally: this is a discrepancy signal, not semantic QA.
    return Counter(re.findall(r'[\u3400-\u9fff]|[^\W_]+', unicodedata.normalize('NFKC', text).casefold()))


def reference(page, result):
    text = page.get('text', '')
    layer = {'version': REFERENCE_VERSION, 'text': text, 'sha256': hashlib.sha256(text.encode()).hexdigest(),
             'assessment': page.get('assessment', {'route': page['route'], 'reasons': [page.get('reason', 'legacy')]})}
    if result['provider'] == 'local':
        comparison = {'status': 'embedded_text_reused'}
    elif not text.strip():
        comparison = {'status': 'no_embedded_reference'}
    else:
        original = tokens(text)
        output = '\n'.join(b['text'] for b in result['blocks'])
        extracted = tokens(output)
        total = sum(original.values())
        coverage = sum((original & extracted).values()) / total if total else 0
        alignment = differences(text, output)
        comparison = {'status': 'text_difference' if alignment['different'] else 'text_overlap',
                      'embedded_token_coverage': round(coverage, 4),
                      'semantic_accuracy_verified': False, 'alignment': alignment}
    layer['comparison'] = comparison
    return layer


def differences(original, extracted):
    """Private review hints, never an instruction to overwrite either source.

    Whitespace alone is ignored; case, punctuation and mathematical characters
    remain significant. Bound expensive alignment and snippet storage per page.
    """
    left, right = ''.join(original.split()), ''.join(extracted.split())
    result = {'different': left != right, 'normalization': 'whitespace_only',
              'original_chars': len(left), 'extracted_chars': len(right),
              'inserted_chars': 0, 'deleted_chars': 0, 'replaced_chars': 0, 'changes': []}
    if left == right:
        return result
    if max(len(left), len(right)) > 6000:
        result['alignment_skipped'] = 'page_text_limit'
        return result
    for kind, a, b, c, d in SequenceMatcher(None, left, right, autojunk=False).get_opcodes():
        if kind == 'equal':
            continue
        if kind == 'insert': result['inserted_chars'] += d-c
        elif kind == 'delete': result['deleted_chars'] += b-a
        else: result['replaced_chars'] += max(b-a, d-c)
        if len(result['changes']) < 30:
            result['changes'].append({'kind': kind, 'original_range': [a,b], 'extracted_range': [c,d],
                                      'original': left[a:b][:160], 'extracted': right[c:d][:160],
                                      'context_before': left[max(0,a-12):a], 'context_after': left[b:b+12]})
        else: result['changes_truncated'] = True
    return result
