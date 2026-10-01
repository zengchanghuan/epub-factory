"""
全书 Reduce 与打包：将各章回写后的 HTML 合并进内存 Book，执行 TOC 重建与打包。
"""

import ebooklib
from ebooklib import epub
import posixpath
import base64
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager, ExitStack
from pathlib import Path
from typing import Callable, Iterable, Optional

from bs4 import BeautifulSoup

from app.domain.manifest_service import build_manifest
from app.domain.chapter_reduce_service import BILINGUAL_STYLE
from app.engine.unpacker import EpubUnpacker
from app.engine.toc_rebuilder import TocRebuilder
from app.engine.packager import EpubPackager

# Version 2 never reads legacy basename-only files, even for legacy callers.
_REDUCE_WORK_DIR = Path(__file__).resolve().parent.parent.parent / "reduce_work"
_REDUCE_SCHEMA = 2


def _safe_component(value: str, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', value):
        raise ValueError(f'Invalid reduce {field}')
    return value


def _epub_resource_path(file_path: str) -> str:
    """Validate an already-decoded package resource name, never a URL href.

    Literal percent escapes, Unicode normalization forms and case remain
    distinct. Only the digest is used as a filesystem filename.
    """
    if (not isinstance(file_path, str) or not file_path or '\\' in file_path
            or any(ord(char) < 32 or ord(char) == 127 for char in file_path)
            or any(part in {'', '.', '..'} for part in file_path.split('/'))
            or re.match(r'^[A-Za-z]:', file_path)):
        raise ValueError('Invalid EPUB reduce resource path')
    try:
        file_path.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise ValueError('Invalid EPUB reduce resource encoding') from exc
    return file_path


def _safe_key(file_path: str) -> str:
    return hashlib.sha256(_epub_resource_path(file_path).encode('utf-8')).hexdigest() + '.json'


def _scope_components(job_id: str, attempt_id: str, execution_owner: str | None = None) -> tuple[str, ...]:
    # Mac deployments often use case-insensitive filesystems. Hash even the
    # validated scope IDs so Job-A/job-a and Attempt-A/attempt-a cannot alias.
    components = ('v2', hashlib.sha256(job_id.encode('ascii')).hexdigest(), 'attempts',
                  hashlib.sha256(attempt_id.encode('ascii')).hexdigest())
    if execution_owner is not None:
        components += ('owners', hashlib.sha256(execution_owner.encode('ascii')).hexdigest())
    return components


@contextmanager
def _reduce_directory(job_id: str, attempt_id: str, *, create: bool, execution_owner: str | None = None):
    """Walk through directory descriptors, never following child symlinks."""
    job_id = _safe_component(job_id, 'job_id')
    attempt_id = _safe_component(attempt_id, 'attempt_id')
    if execution_owner is not None:
        execution_owner = _safe_component(execution_owner, 'execution_owner')
    components = _scope_components(job_id, attempt_id, execution_owner)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        descriptor = None
        try:
            if create:
                _REDUCE_WORK_DIR.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(_REDUCE_WORK_DIR, flags)
            stack.callback(os.close, descriptor)
            for component in components:
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                descriptor = os.open(component, flags, dir_fd=descriptor)
                stack.callback(os.close, descriptor)
        except FileNotFoundError:
            if create:
                raise
            descriptor = None
        yield descriptor


def set_chapter_output(job_id: str, file_path: str, content: bytes, *, attempt_id: str = 'legacy',
                       execution_owner: str | None = None) -> Path:
    """Atomically write one attempt-scoped, identity-checked chapter artifact."""
    job_id = _safe_component(job_id, 'job_id')
    attempt_id = _safe_component(attempt_id, 'attempt_id')
    if execution_owner is not None:
        execution_owner = _safe_component(execution_owner, 'execution_owner')
    file_path = _epub_resource_path(file_path)
    if not isinstance(content, bytes):
        raise TypeError('Reduced chapter content must be bytes')
    if not content.strip():
        raise ValueError('Reduced chapter content must not be empty')
    key = _safe_key(file_path)
    payload = json.dumps({
        'schema': _REDUCE_SCHEMA, 'job_id': job_id, 'attempt_id': attempt_id, 'file_path': file_path,
        'execution_owner': execution_owner,
        'sha256': hashlib.sha256(content).hexdigest(), 'content': base64.b64encode(content).decode('ascii'),
    }, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    with _reduce_directory(job_id, attempt_id, create=True, execution_owner=execution_owner) as directory:
        try:
            existing = os.stat(key, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ValueError('Reduce artifact is not a regular file')
        temporary = '.tmp-' + uuid.uuid4().hex
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             mode=0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, 'wb') as destination:
                destination.write(payload)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, key, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
    return _REDUCE_WORK_DIR.joinpath(*_scope_components(job_id, attempt_id, execution_owner), key)


def get_chapter_output(job_id: str, file_path: str, *, attempt_id: str = 'legacy',
                       execution_owner: str | None = None) -> Optional[bytes]:
    """Read only the exact version/job/attempt/resource; corruption is fatal."""
    job_id = _safe_component(job_id, 'job_id')
    attempt_id = _safe_component(attempt_id, 'attempt_id')
    if execution_owner is not None:
        execution_owner = _safe_component(execution_owner, 'execution_owner')
    file_path = _epub_resource_path(file_path)
    with _reduce_directory(job_id, attempt_id, create=False, execution_owner=execution_owner) as directory:
        if directory is None:
            return None
        try:
            descriptor = os.open(_safe_key(file_path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, 'rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError('Reduce artifact is not a regular file')
            raw = source.read()
    try:
        payload = json.loads(raw)
        expected = (_REDUCE_SCHEMA, job_id, attempt_id, file_path)
        actual = tuple(payload.get(key) for key in ('schema', 'job_id', 'attempt_id', 'file_path'))
        if actual != expected:
            raise ValueError('identity mismatch')
        if payload.get('execution_owner') != execution_owner:
            raise ValueError('execution owner mismatch')
        content = base64.b64decode(payload['content'], validate=True)
        if not content.strip() or hashlib.sha256(content).hexdigest() != payload['sha256']:
            raise ValueError('content checksum mismatch')
        return content
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError('Invalid or mismatched reduce artifact') from exc


def make_get_chapter_content(job_id: str, *, attempt_id: str = 'legacy',
                             required_files: Iterable[str] = (),
                             execution_owner: str | None = None) -> Callable[[str], Optional[bytes]]:
    """Freeze the attempt in the closure; required translated files fail closed."""
    job_id = _safe_component(job_id, 'job_id')
    attempt_id = _safe_component(attempt_id, 'attempt_id')
    if execution_owner is not None:
        execution_owner = _safe_component(execution_owner, 'execution_owner')
    required = frozenset(_epub_resource_path(path) for path in required_files)
    def get_content(file_path):
        content = get_chapter_output(job_id, file_path, attempt_id=attempt_id, execution_owner=execution_owner)
        if content is None and file_path in required:
            raise FileNotFoundError('Required chapter output is missing for this translation attempt')
        return content
    return get_content


def _sync_book_title_metadata(book, title: str) -> None:
    title = (title or "").strip()
    if not title:
        return
    book.title = title
    dc_ns = "http://purl.org/dc/elements/1.1/"
    book.metadata.setdefault(dc_ns, {})
    book.metadata[dc_ns]["title"] = [(title, {})]


def _sync_document_title_text(content: bytes, original_title: str, translated_title: str) -> bytes:
    original_title = (original_title or "").strip()
    translated_title = (translated_title or "").strip()
    if not translated_title:
        return content

    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)
    soup = BeautifulSoup(text, "html.parser")
    changed = False

    for title_tag in soup.find_all("title"):
        if title_tag.get_text(strip=True) != translated_title:
            title_tag.clear()
            title_tag.append(translated_title)
            changed = True

    if original_title:
        for node in soup.find_all(string=True):
            if original_title in str(node):
                node.replace_with(str(node).replace(original_title, translated_title))
                changed = True

    return soup.encode(formatter="html", encoding="utf-8") if changed else content


def reduce_and_package(
    input_epub_path: str,
    output_epub_path: str,
    get_chapter_content: Callable[[str], Optional[bytes]],
    direction: str = "ltr",
    book_title: str | None = None,
    original_book_title: str | None = None,
    target_lang: str | None = None,
    glossary: dict[str, str] | None = None,
    source_warnings: list[str] | None = None,
) -> bool:
    """
    加载 EPUB，用回调提供的章节内容覆盖对应文档，再 TOC 重建并打包。

    :param input_epub_path: 原始 EPUB 路径
    :param output_epub_path: 输出 EPUB 路径
    :param get_chapter_content: (file_path) -> 回写后的章节 HTML 字节，若为 None 则保留原文
    :param direction: 书籍方向，默认 ltr
    :return: 打包是否成功
    """
    unpacker = EpubUnpacker(input_epub_path)
    book = unpacker.load_book()
    if not book:
        return False
    if source_warnings is not None:
        source_warnings[:] = list(dict.fromkeys([*source_warnings, *unpacker.source_warnings]))
    if hasattr(book, "direction"):
        book.direction = direction
    elif hasattr(book, "set_direction"):
        book.set_direction(direction)
    if book_title:
        _sync_book_title_metadata(book, book_title)

    manifest = build_manifest(input_epub_path, "reduce")
    if manifest.get("error"):
        return False
    file_path_to_content: dict[str, bytes] = {}
    for ch in manifest.get("chapters", []):
        if ch.get("chapter_kind") != "body":
            continue
        fp = ch.get("file_path")
        if not fp:
            continue
        content = get_chapter_content(fp)
        if content is not None:
            file_path_to_content[fp] = content

    bilingual_documents = []
    changed_chapter_content = False
    for item in book.get_items():
        if item is None:
            continue
        if item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        name = item.get_name() if hasattr(item, "get_name") else None
        if not name:
            continue
        if name in file_path_to_content:
            changed_chapter_content |= file_path_to_content[name] != item.get_content()
            item.set_content(file_path_to_content[name])
            if BeautifulSoup(file_path_to_content[name], "html.parser").select_one(".epub-translated"):
                bilingual_documents.append(item)
            continue
        if book_title and "titlepage" in Path(name).name.lower():
            item.set_content(_sync_document_title_text(item.get_content(), original_book_title or "", book_title))

    if bilingual_documents:
        css_name = "css/epub-factory-bilingual.css"
        existing_names = {item.get_name() for item in book.get_items()}
        while css_name in existing_names:
            css_name = css_name.replace(".css", "-new.css")
        stylesheet = epub.EpubItem(
            uid="epub-factory-bilingual-layout", file_name=css_name,
            media_type="text/css", content=BILINGUAL_STYLE.encode("utf-8"),
        )
        existing_ids = {item.get_id() for item in book.get_items()}
        while stylesheet.id in existing_ids:
            stylesheet.id += "-new"
        book.add_item(stylesheet)
        for item in bilingual_documents:
            if not item.title:
                document = BeautifulSoup(item.get_content(), "html.parser")
                heading = document.find("title") or document.find(["h1", "h2"])
                item.title = (heading.get_text(" ", strip=True) if heading else "") or book_title or book.title or "Chapter"
            item.add_link(href=posixpath.relpath(css_name, posixpath.dirname(item.get_name()) or "."),
                          rel="stylesheet", type="text/css")

    rebuilder = TocRebuilder()
    book = rebuilder.rebuild(
        book,
        original_book_title=original_book_title,
        translated_book_title=book_title,
        target_lang=target_lang,
        glossary=glossary,
        source_warnings=source_warnings,
        # Legacy translation callers may omit target_lang. An exact identity
        # roundtrip is not translation and must retain original TOC labels.
        synchronize_titles=changed_chapter_content,
    )
    packager = EpubPackager(book, output_epub_path)
    return packager.save()
