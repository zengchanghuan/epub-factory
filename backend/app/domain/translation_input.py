"""One deterministic, local-only input boundary for paid translation.

Normal conversion never enters this module. No CJK or model work occurs here;
the shared fast executor owns those stages and all selected translation options.
Inline SVG is limited to a static, self-contained subset, not full SVG support.
"""
from __future__ import annotations

import base64
import hashlib
import os
import posixpath
import re
import tempfile
import zipfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lxml import etree, html

from app.cancellation import JobCancelled, raise_if_cancelled
from app.engine.adapters import html_to_epub_builder
from app.domain.epub_input_integrity import (
    EpubInputError, _References, _checked_members, _xml, validate_epub_resources,
    _CSS_ESCAPE,
)
from app.domain.input_formats import PDF_DISABLED_MESSAGE


NORMALIZATION_VERSION = "translation-input-v1"
_SUPPORTED = {".epub", ".docx", ".md", ".markdown"}
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/svg+xml"}


class TranslationInputError(RuntimeError):
    def __init__(self, reason: str, message: str, source_warnings=None):
        super().__init__(message)
        self.reason = reason
        self.source_warnings = list(source_warnings or [])


@dataclass(frozen=True)
class NormalizedTranslationInput:
    epub_path: Path
    source_sha256: str
    adapter: str
    normalization_version: str = NORMALIZATION_VERSION
    source_warnings: list[str] = field(default_factory=list)


def ensure_translation_executor_available() -> None:
    if os.environ.get("EPUB_FAST_TRANSLATION", "1").strip().lower() in {"0", "false", "no"}:
        raise TranslationInputError("service_disabled", "翻译服务暂时停用，本次不会发起新的模型请求，请稍后重试")


def validate_translation_filename(name) -> None:
    suffix = Path(str(name or "")).suffix.lower()
    if suffix not in _SUPPORTED:
        message = PDF_DISABLED_MESSAGE if suffix == ".pdf" else "AI 翻译仅支持 EPUB、DOCX 或 Markdown；MOBI/AZW3 请先转换为 EPUB"
        raise TranslationInputError("unsupported_input", message)


def _resource_error():
    return TranslationInputError(
        "unsupported_resource", "书稿包含未嵌入或当前适配器不能完整保留的图片/资源，请先打包为完整 EPUB 后翻译；不会自动下载外部资源")


def _check_svg_css(value):
    # Reuse CSS escape normalization, not the looser EPUB image URL grammar.
    # Embedded SVG accepts only explicit local-fragment url() references.
    # Unrecognized/malformed resource functions fail closed, including fonts.
    value = re.sub(r"/\*.*?\*/", "", value, flags=re.S)
    value = _CSS_ESCAPE.sub(lambda m: chr(int(m[1], 16)) if m[1] else m[2], value)
    if (re.search(r"@import(?![\w-])", value, re.I)
            or re.search(r"(?<![\w-])(?:image-set|-webkit-image-set|image)\s*\(", value, re.I)):
        raise _resource_error()
    for match in re.finditer(r"\burl\s*\(", value, re.I):
        fragment = re.match(r'''\s*(?:"(#[^"\r\n]+)"|'(#[^'\r\n]+)'|(#[^\s"'()]+))\s*\)''', value[match.end():])
        if fragment is None:
            raise _resource_error()


def _embedded_image(uri):
    header, separator, encoded = uri.partition(",")
    media_type = header[5:].split(";", 1)[0].lower()
    if not uri.lower().startswith("data:") or not separator or media_type not in _IMAGE_TYPES or not header.lower().endswith(";base64"):
        raise _resource_error()
    try:
        content = base64.b64decode("".join(encoded.split()), validate=True)
        if not content:
            raise ValueError("Empty image")
        if media_type == "image/svg+xml":
            root = _xml(content)
            if etree.QName(root).localname != "svg":
                raise ValueError("Not SVG")
            if root.getroottree().xpath('//processing-instruction("xml-stylesheet")'):
                raise ValueError("External SVG stylesheet instruction")
            for node in root.iter():
                if not isinstance(node.tag, str):
                    continue
                if etree.QName(node).localname.lower() in {
                    "script", "foreignobject", "animate", "animatemotion", "animatetransform", "set", "discard",
                }:
                    raise ValueError("Unsupported active SVG")
                if etree.QName(node).localname == "style":
                    _check_svg_css("".join(node.itertext()))
                for key, value in node.attrib.items():
                    if key.rsplit("}", 1)[-1].lower().startswith("on"):
                        raise ValueError("Unsupported active SVG event")
                    if key.rsplit("}", 1)[-1] == "href" and value and not value.startswith("#"):
                        raise ValueError("External SVG resource")
                    _check_svg_css(value)
        elif media_type == "image/png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Not PNG")
        elif media_type == "image/jpeg" and not content.startswith(b"\xff\xd8"):
            raise ValueError("Not JPEG")
        elif media_type == "image/gif" and not content.startswith((b"GIF87a", b"GIF89a")):
            raise ValueError("Not GIF")
        return hashlib.sha256(content).hexdigest()
    except (ValueError, etree.XMLSyntaxError, EpubInputError) as exc:
        raise _resource_error() from exc


def _check_html_resources(body):
    images = Counter()

    def image(uri):
        images[_embedded_image(uri)] += 1

    def stylesheet(_uri):
        raise _resource_error()

    class References(_References):
        def start(self, tag, attrs):
            local = tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()
            if local in {"iframe", "script", "audio", "video", "embed", "object"}:
                raise _resource_error()
            super().start(tag, attrs)

    parser = etree.HTMLParser(target=References(image, lambda _uri: None, stylesheet), no_network=True)
    try:
        etree.fromstring(body.encode("utf-8"), parser)
        tree = html.fragment_fromstring(body, create_parent="div")
        readable = tree.xpath(".//text()[not(ancestor::style) and not(ancestor::script)]")
        if not any(value.strip() for value in readable):
            raise TranslationInputError("no_body_text", "书稿没有可翻译的正文文字")
    except (etree.XMLSyntaxError, etree.ParserError) as exc:
        raise TranslationInputError("invalid_input", "适配后的正文无法安全读取") from exc
    return images


def _docx_images(path):
    """Compare referenced source images with actual mammoth output, not logs.

    Headers/footers/drawing formats mammoth omits are explicitly unsupported;
    unused packaged media is not mistaken for a required visible resource.
    """
    expected = Counter()
    try:
        with zipfile.ZipFile(path) as archive:
            members = _checked_members(archive)
            if "word/document.xml" not in members:
                raise ValueError("Missing Word document")
            # Mammoth does not visit header/footer stories or altChunk sources.
            # Empty default stories are harmless, but silently losing actual
            # prose at a paid translation boundary is not a supported fallback.
            for name in sorted(members):
                if not name.startswith("word/") or not name.endswith(".xml"):
                    continue
                root = _xml(archive.read(name))
                local = etree.QName(root).localname
                if local in {"hdr", "ftr"} and any(
                    node.text and node.text.strip() for node in root.iter()
                    if isinstance(node.tag, str) and etree.QName(node).localname in {"t", "instrText"}
                ):
                    raise TranslationInputError("unsupported_content", "DOCX 的页眉/页脚包含正文文字，当前适配器不能完整保留；请先转换为 EPUB 后翻译")
                if any(isinstance(node.tag, str) and etree.QName(node).localname in {"altChunk", "oMath", "oMathPara"}
                       for node in root.iter()):
                    raise TranslationInputError("unsupported_content", "DOCX 包含当前适配器不能完整保留的嵌入内容或公式，请先转换为 EPUB 后翻译")
            for name in sorted(members):
                if not name.startswith("word/") or not name.endswith(".rels") or "/_rels/" not in name:
                    continue
                directory, relationship_file = name.rsplit("/_rels/", 1)
                owner = posixpath.join(directory, relationship_file[:-5])
                if owner not in members:
                    continue
                root = _xml(archive.read(owner))
                relations = _xml(archive.read(name))
                references = Counter(value for node in root.iter() for key, value in node.attrib.items()
                                     if key.startswith("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"))
                for relation in relations:
                    if not relation.get("Type", "").endswith("/image") or relation.get("Id") not in references:
                        continue
                    target = relation.get("Target", "")
                    parts = urlsplit(target)
                    if relation.get("TargetMode") == "External" or parts.scheme or parts.netloc or "\\" in target:
                        raise _resource_error()
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(owner), unquote(parts.path)))
                    if target.startswith(("/", "../")) or target not in members:
                        raise _resource_error()
                    digest = hashlib.sha256(archive.read(target)).hexdigest()
                    expected[digest] += references[relation.get("Id")]
        return expected
    except TranslationInputError:
        raise
    except (OSError, ValueError, KeyError, etree.XMLSyntaxError, zipfile.BadZipFile, EpubInputError) as exc:
        raise TranslationInputError("invalid_input", "DOCX 文件结构或资源不完整，请检查原稿后重试") from exc


def _copy_and_hash(source, target, cancel_check):
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        output = target.open("wb") if target is not None else None
        try:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                raise_if_cancelled(cancel_check)
                digest.update(chunk)
                if output:
                    output.write(chunk)
        finally:
            if output:
                output.close()
    raise_if_cancelled(cancel_check)
    return digest.hexdigest()


def _adapt(adapter, path):
    try:
        return adapter(path)
    except JobCancelled:
        raise
    except RuntimeError as exc:
        reason = "service_disabled" if isinstance(exc.__cause__, ModuleNotFoundError) else "invalid_input"
        message = ("翻译格式适配服务暂不可用，请稍后重试" if reason == "service_disabled"
                   else "书稿无法完整转换为 EPUB，请检查格式与资源后重试")
        raise TranslationInputError(reason, message) from exc


@contextmanager
def normalized_translation_input(input_path: Path, *, source_name: str | None = None, cancel_check=None):
    ensure_translation_executor_available()
    source = Path(input_path)
    validate_translation_filename(source.name)
    raise_if_cancelled(cancel_check)
    suffix = source.suffix.lower()
    if suffix == ".epub":
        try:
            digest = _copy_and_hash(source, None, cancel_check)
        except OSError as exc:
            raise TranslationInputError("invalid_input", "无法读取原稿文件，请重新上传") from exc
        yield NormalizedTranslationInput(source, digest, "epub")
        return
    with tempfile.TemporaryDirectory(prefix="epub_translation_input_") as directory:
        root = Path(directory)
        # The real adapter sees the original filename, not a job-prefixed upload
        # or a random temporary name, so its fallback title is stable as well.
        name = Path(str(source_name or source.name).replace("\\", "/")).name
        if Path(name).suffix.lower() != suffix:
            raise TranslationInputError("invalid_input", "原稿名称与上传格式不一致")
        local = root / name
        try:
            digest = _copy_and_hash(source, local, cancel_check)
            if suffix == ".docx":
                expected_images = _docx_images(local)
                from app.engine.adapters.docx_adapter import docx_to_html
                body, metadata = _adapt(docx_to_html, local)
                adapter = "docx"
            else:
                # The existing adapter's errors='replace' must not turn damaged
                # source bytes into silently billable replacement characters.
                local.read_bytes().decode("utf-8", errors="strict")
                from app.engine.adapters.markdown_adapter import md_to_html
                body, metadata = _adapt(md_to_html, local)
                expected_images = None
                adapter = "markdown"
            raise_if_cancelled(cancel_check)
            images = _check_html_resources(body)
            if expected_images is not None and images != expected_images:
                raise _resource_error()
            output = root / "normalized.epub"
            html_to_epub_builder.build(body, metadata, output, deterministic=True)
            with output.open("rb") as stream:
                warnings = validate_epub_resources(stream)
            raise_if_cancelled(cancel_check)
        except TranslationInputError:
            raise
        except (OSError, UnicodeError, EpubInputError, ValueError, zipfile.BadZipFile) as exc:
            raise TranslationInputError("invalid_input", "书稿无法完整转换为 EPUB，请检查编码与资源后重试") from exc
        yield NormalizedTranslationInput(output, digest, adapter, source_warnings=list(warnings or []))
