"""Lossless ZIP-member repair. Unchanged book bytes, links and IDs stay intact."""
import html
from pathlib import Path
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from app.domain.translation_titles import COMMON_ZH_TITLES


def repair_epub(source, output, *, glossary=None, text_edits=()):
    if Path(source).resolve() == Path(output).resolve():
        raise ValueError('修正版必须写入新文件，不能覆盖原书')
    replacements = {**COMMON_ZH_TITLES, **{str(k).strip().casefold(): str(v) for k, v in (glossary or {}).items()}}
    changed = {}
    with zipfile.ZipFile(source) as original:
        members = {i.filename: original.read(i.filename) for i in original.infolist()}
        nav_names = {name for name in members if name.lower().endswith('.ncx')}
        for name in members:
            if name.lower().endswith('.opf'):
                opf = ET.fromstring(members[name])
                for item in opf.findall('.//{*}manifest/{*}item'):
                    if 'nav' in item.get('properties', '').split():
                        nav_names.add(posixpath.normpath(posixpath.join(posixpath.dirname(name), item.get('href', ''))))
        for name in nav_names:
            if name not in members: raise ValueError('导航文件不存在')
            before = members[name].decode('utf-8')
            def label(match):
                old = html.unescape(match['label']).strip()
                new = replacements.get(re.sub(r'\s+', ' ', old).casefold())
                return match['open'] + html.escape(new, quote=False) + match['close'] if new and new != old else match.group()
            after = re.sub(r'(?P<open><(?:[\w-]+:)?(?:text|a)\b[^>]*>)(?P<label>[^<>]*)(?P<close></(?:[\w-]+:)?(?:text|a)>)', label, before)
            if after != before: changed[name] = after.encode('utf-8')
        for edit in text_edits:
            name, old, new = edit['file'], edit['old'], edit['new']
            if not old or not new or name not in members: raise ValueError('定点修订字段无效')
            before = changed.get(name, members[name]).decode('utf-8')
            expected = edit.get('expected_count', 1)
            if type(expected) is not int or expected < 1 or expected > 1000:
                raise ValueError('定点修订匹配次数无效')
            if before.count(old) != expected: raise ValueError('定点修订匹配次数不符；原文版本已变化或匹配歧义')
            changed[name] = before.replace(old, new, expected).encode('utf-8')
        # Validate edited XML before creating the candidate, never edit in place.
        for name, data in changed.items():
            ET.fromstring(data)
        with zipfile.ZipFile(output, 'w') as repaired:
            repaired.comment = original.comment
            for info in original.infolist():
                repaired.writestr(info, changed.get(info.filename, members[info.filename]))
    return {'changed_members': sorted(changed), 'unchanged_members': len(members)-len(changed),
            'output': str(output), 'requires_final_audit': True}
