"""Deterministic service quotes, not estimates of billed model tokens.

The execution cache includes context that is not frozen before checkout. Until
the complete runtime identity can be proven, neither legacy nor exact-looking
cache entries justify a discount. This module intentionally never opens it.
"""
from __future__ import annotations

import re
import hashlib
import zipfile
from pathlib import Path
from typing import Callable

from bs4 import BeautifulSoup


QUOTE_SCHEMA_VERSION = 2
_CACHE_POLICIES = frozenset({"reuse", "verified", "fresh"})
_BLOCK_TAGS = ["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"]


class QuoteInputError(ValueError):
    """Unreadable quote input, distinct from operator pricing errors."""


def normalized_content_digest(path) -> str:
    """Hash all normalized package members, not ZIP timestamps/compression."""
    digest = hashlib.sha256()
    with zipfile.ZipFile(path) as archive:
        for entry in sorted(archive.infolist(), key=lambda item: item.filename):
            if entry.is_dir():
                continue
            name = entry.filename.encode('utf-8')
            member = hashlib.sha256()
            with archive.open(entry) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    member.update(block)
            digest.update(len(name).to_bytes(8, 'big'))
            digest.update(name)
            digest.update(member.digest())
    return digest.hexdigest()


def freeze_execution_basis(normalized, *, target_lang, translation_quality, translation_model) -> dict:
    """Bind a service quote to its actual adapter output and priced product.

    This is NOT a cache plan: glossary/strategy edits remain allowed and no
    reusable chunks, provider costs or discounts are promised. Adapters produce
    deterministic contents; hashing all members also covers embedded resources
    without treating ZIP timestamps/compression changes as new book content.
    """
    from .payment_entitlement import canonical_model
    return {
        'version': 1,
        'source_sha256': normalized.source_sha256,
        'normalized_content_sha256': normalized_content_digest(normalized.epub_path),
        'normalization_version': normalized.normalization_version,
        'adapter': normalized.adapter,
        'target_lang': target_lang,
        'translation_quality': translation_quality,
        'translation_model': canonical_model(translation_model or 'deepseek-flash'),
    }


def validate_quote_execution(job, normalized) -> None:
    """Check new quotes before the first model call; never reprice old orders."""
    from .payment_entitlement import same_amount
    from .translation_input import TranslationInputError
    pricing = (job.translation_stats or {}).get('translation_pricing') or {}
    if 'execution_basis' not in pricing:
        # Existing orders never recorded this evidence. Do not fabricate a
        # binding from today's configuration or deny their historic entitlement.
        return
    basis = pricing['execution_basis']
    actual = freeze_execution_basis(normalized, target_lang=job.target_lang,
        translation_quality=job.translation_quality, translation_model=job.translation_model)
    if (not isinstance(basis, dict) or basis != actual
            or not same_amount(pricing.get('quoted_amount'), job.expected_amount)
            or not same_amount(pricing.get('price_cny'), job.expected_amount)):
        raise TranslationInputError('quote_execution_mismatch',
            '执行输入或翻译档位与原报价不一致，已停止模型调用。原订单金额保留，请联系管理员核验，勿重复付款。')


def _content_page(raw: bytes, name: str) -> BeautifulSoup:
    try:
        # Preserve legacy fragments and EPUB encoding declarations/BOMs. The
        # upload boundary validates book structure; quoting must not invent a
        # stricter HTML format or silently discard undecodable source bytes.
        content = raw
        declared_encoding = re.match(br'''\s*<\?xml\b[^>]*\bencoding\s*=\s*["'][^"']+["']''', raw[:512], re.I)
        if not declared_encoding and not raw.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")):
            # Short, otherwise valid UTF-8 Chinese can be mistaken for a
            # single-byte encoding by auto-detection. Retain the old normal
            # UTF-8 character basis, but let declared/legacy encodings decode.
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                pass
        soup = BeautifulSoup(content, "html.parser")
        if soup.contains_replacement_characters:
            raise ValueError("Undecodable document bytes")
        return soup
    except Exception as exc:
        raise QuoteInputError(f"无法读取 EPUB 内容页 {name}，未生成翻译报价") from exc


def estimate_quote(
    epub_path: str | Path,
    target_lang: str,
    glossary: dict | None = None,
    *,
    cache_policy: str = "reuse",
    translation_quality: str = "standard",
    translation_model: str | None = None,
    price_for_chars: Callable,
) -> dict:
    """Keep the established leaf-inner-HTML character basis without discounts.

    ``target_lang`` and ``glossary`` remain explicit inputs for call-site
    compatibility; they do not prove reusable runtime context. The supplied
    pricing callback owns tiers, quality/model multipliers and fixed overrides.
    A bad page rejects the whole quote; a valid empty body contributes zero.
    """
    if not isinstance(cache_policy, str) or cache_policy not in _CACHE_POLICIES:
        raise QuoteInputError("cache_policy 仅支持：fresh, reuse, verified")
    total_chars = 0
    content_pages = 0
    try:
        with zipfile.ZipFile(epub_path) as archive:
            for entry in archive.infolist():
                name = entry.filename
                if entry.is_dir() or not name.lower().endswith((".xhtml", ".html", ".htm")):
                    continue
                content_pages += 1
                try:
                    soup = _content_page(archive.read(entry), name)
                except QuoteInputError:
                    raise
                except Exception as exc:
                    raise QuoteInputError(f"无法读取 EPUB 内容页 {name}，未生成翻译报价") from exc
                for block in soup.find_all(_BLOCK_TAGS):
                    if block.find(_BLOCK_TAGS):
                        continue
                    inner_html = "".join(str(child) for child in block.contents).strip()
                    total_chars += len(inner_html)
    except QuoteInputError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise QuoteInputError("无法读取 EPUB 文件，未生成翻译报价") from exc
    if not content_pages:
        raise QuoteInputError("EPUB 未包含可读取的内容页，未生成翻译报价")

    amount = price_for_chars(total_chars)
    return {
        "schema_version": QUOTE_SCHEMA_VERSION,
        "quote_type": "service_quote",
        "cache_policy": cache_policy,
        "cache_discount_status": "disabled" if cache_policy == "fresh" else "deferred",
        "total_chars": total_chars,
        "cached_chars": 0,
        "hit_ratio": None,
        "billable_chars": total_chars,
        "price_cny": amount,
        "raw_price_cny": amount,
    }
