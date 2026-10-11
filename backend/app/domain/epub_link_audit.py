"""Read-only final-artifact links, including footnotes and cross-chapter links."""
from collections import defaultdict
import posixpath
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup


def audit_epub_links(archive, sample_limit=12):
    report = {"links_checked": 0, "links_broken": 0, "links_unsafe": 0,
              "duplicate_anchors": 0, "nested_links": 0, "link_samples": []}
    names = set(archive.namelist())
    targets, links = {}, []

    def record(file, reason):
        if len(report["link_samples"]) < max(0, min(sample_limit, 100)):
            report["link_samples"].append({"file": file, "reason": reason})

    for name in sorted(names):
        if not name.lower().endswith((".html", ".htm", ".xhtml", ".svg")):
            continue
        soup = BeautifulSoup(archive.read(name), "html.parser")
        anchors = defaultdict(set)
        for node in soup.find_all(True):
            identifiers = [node.get("id"), node.get("xml:id")]
            if node.name == "a":
                identifiers.append(node.get("name"))
            for identifier in identifiers:
                if identifier:
                    anchors[str(identifier)].add(id(node))
            if node.name in {"a", "area", "use", "image"}:
                for key in ("href", "xlink:href"):
                    if node.has_attr(key):
                        links.append((name, str(node[key])))
            if node.name == "a" and node.find_parent("a") is not None:
                report["nested_links"] += 1
                record(name, "nested_link")
        targets[name] = anchors
        duplicates = sum(len(owners) > 1 for owners in anchors.values())
        report["duplicate_anchors"] += duplicates
        if duplicates:
            record(name, "duplicate_anchor")

    for name, href in links:
        report["links_checked"] += 1
        try:
            uri = urlsplit(href.strip())
            if uri.scheme.lower() in {"javascript", "vbscript", "data"}:
                raise ValueError("unsafe_scheme")
            if uri.scheme or uri.netloc:
                continue  # Never fetch/validate external URLs.
            decoded = unquote(uri.path)
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), decoded)) if decoded else name
            if target.startswith(("/", "../")) or "\\" in decoded or "\x00" in decoded:
                raise ValueError("unsafe_path")
            if target not in names:
                report["links_broken"] += 1
                record(name, "target_file_missing")
            elif uri.fragment and target in targets:
                fragment = unquote(uri.fragment)
                if len(targets.get(target, {}).get(fragment, set())) != 1:
                    report["links_broken"] += 1
                    record(name, "target_anchor_missing_or_ambiguous")
        except ValueError:
            report["links_unsafe"] += 1
            record(name, "unsafe_target")
    return report
