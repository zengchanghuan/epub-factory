"""Private, immutable precision edit evidence and user-selected EPUB downloads.

Book text never enters job statistics. Access is authorized by the job API.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import zipfile

from lxml import etree


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def collect_edits(document, before, after):
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
    original = list(etree.fromstring(before, parser).iter())
    updated = list(etree.fromstring(after, parser).iter())
    if len(original) != len(updated):
        raise ValueError('Precision review structure changed')
    edits = []
    for index, (left, right) in enumerate(zip(original, updated)):
        for field in ('text', 'tail'):
            old, new = getattr(left, field) or '', getattr(right, field) or ''
            if old != new:
                edits.append({'document': document, 'node': index, 'field': field, 'before': old, 'after': new})
    return edits


def save_review(source, polished, directory, edits, language):
    directory = Path(directory)
    baseline = directory / 'precision-original.epub'
    manifest = directory / 'precision-review.json'
    if baseline.exists() or manifest.exists():
        raise ValueError('Precision review destination already exists')
    # Attempt directory is private and unique. Never overwrite historical evidence.
    shutil.copyfile(source, baseline)
    data = {'version': 1, 'language': language, 'baseline_sha256': digest(baseline),
            'revision': digest(polished), 'changes': [dict(edit, id=str(i)) for i, edit in enumerate(edits)]}
    with manifest.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False)
    os.chmod(baseline, 0o600)
    os.chmod(manifest, 0o600)


def load_review(output):
    output = Path(output)
    manifest = output.parent / 'precision-review.json'
    baseline = output.parent / 'precision-original.epub'
    if manifest.is_symlink() or baseline.is_symlink() or not manifest.is_file() or not baseline.is_file():
        raise FileNotFoundError('此历史任务没有修改对照记录')
    data = json.loads(manifest.read_text(encoding='utf-8'))
    if data.get('version') != 1 or data.get('revision') != digest(output) or data.get('baseline_sha256') != digest(baseline):
        raise ValueError('精校对照与成品不一致，请联系客服')
    return data, baseline


def export_selection(output, rejected, revision):
    from .precision_polish_service import validate_epub, EPUBCHECK_JAR
    data, baseline = load_review(output)
    if revision != data['revision']:
        raise ValueError('对照版本已改变，请重新打开')
    known = {change['id'] for change in data['changes']}
    if not isinstance(rejected, list) or any(not isinstance(identity, str) or identity not in known for identity in rejected):
        raise ValueError('无效的修改选择')
    rejected = set(rejected)
    accepted = [change for change in data['changes'] if change['id'] not in rejected]
    if not rejected:
        return Path(output), False
    if not accepted:
        return baseline, False
    by_document = {}
    for change in accepted:
        by_document.setdefault(change['document'], []).append(change)
    fd, name = tempfile.mkstemp(prefix='precision-selection-', suffix='.epub')
    os.close(fd)
    destination = Path(name)
    try:
        with zipfile.ZipFile(baseline) as source, zipfile.ZipFile(destination, 'w') as target:
            target.comment = source.comment
            for entry in source.infolist():
                content = source.read(entry.filename)
                if entry.filename in by_document:
                    root = etree.fromstring(content, etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True))
                    nodes = list(root.iter())
                    for change in by_document[entry.filename]:
                        node = nodes[change['node']]
                        if (getattr(node, change['field']) or '') != change['before']:
                            raise ValueError('精校原文定位不一致')
                        setattr(node, change['field'], change['after'])
                    content = etree.tostring(root.getroottree(), encoding='utf-8', xml_declaration=True)
                target.writestr(entry, content)
        validation = validate_epub(destination, EPUBCHECK_JAR)
        if not validation.passed:
            raise ValueError('所选版本未通过 EPUB 校验，未交付')
        return destination, True
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
