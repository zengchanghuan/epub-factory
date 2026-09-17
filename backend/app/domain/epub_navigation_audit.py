"""Audit the navigation a reader actually uses, without modifying its links."""
import posixpath
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from app.domain.translation_residual_policy import residual_category


def audit_epub_navigation(archive, preserved_terms=(), sample_limit=12):
    names = set(archive.namelist())
    nav_names = set()
    for name in names:
        if name.lower().endswith('.opf'):
            package = ET.fromstring(archive.read(name))
            for item in package.findall('.//{*}manifest/{*}item'):
                if 'nav' in item.get('properties', '').split():
                    nav_names.add(posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(item.get('href', '')))))
    records = []
    for name in sorted(names):
        if name.lower().endswith('.ncx'):
            root = ET.fromstring(archive.read(name))
            for point in root.findall('.//{*}navPoint'):
                label = point.find('./{*}navLabel/{*}text')
                content = point.find('./{*}content')
                if label is not None and content is not None:
                    records.append((name, ''.join(label.itertext()).strip(), content.get('src', '')))
        elif name in nav_names:
            soup = BeautifulSoup(archive.read(name), 'html.parser')
            for nav in soup.find_all('nav'):
                if 'toc' not in str(nav.get('epub:type') or '').split():
                    continue
                for link in nav.find_all('a', href=True):
                    records.append((name, link.get_text('', strip=False).strip(), link['href']))
    report = {'navigation_labels_checked': len(records), 'navigation_residual_labels': 0,
              'navigation_broken_targets': 0, 'navigation_samples': []}
    seen = set()
    target_ids = {}
    for name, label, href in records:
        parsed = urlsplit(href)
        if parsed.scheme or parsed.netloc:
            continue  # Do not fetch external resources.
        path = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(parsed.path))) if parsed.path else name
        fragment = unquote(parsed.fragment)
        key = (path, fragment, label)
        if key in seen:
            continue  # NCX and NAV usually repeat the same entry.
        seen.add(key)
        reason = ''
        if residual_category(label, title_like=True, preserved_terms=preserved_terms):
            report['navigation_residual_labels'] += 1
            reason = 'untranslated_navigation_label'
        if path not in names:
            report['navigation_broken_targets'] += 1
            reason = 'navigation_target_missing'
        elif fragment:
            if path not in target_ids:
                target = BeautifulSoup(archive.read(path), 'html.parser')
                target_ids[path] = {str(tag['id']) for tag in target.find_all(id=True)}
            if fragment not in target_ids[path]:
                report['navigation_broken_targets'] += 1
                reason = 'navigation_anchor_missing'
        if reason and len(report['navigation_samples']) < sample_limit:
            report['navigation_samples'].append({'file': name, 'title': label[:200], 'href': href[:400], 'category': reason})
    return report
