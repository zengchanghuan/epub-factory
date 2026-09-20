"""Pre-translation book profiling with a deterministic, non-blocking fallback."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from openai import AsyncOpenAI
from app.infra.llm_usage_ledger import accounted_request, billing_stage

from app.engine.unpacker import EpubUnpacker
from .translation_strategy import (
    TRANSLATION_STRATEGIES,
    normalize_translation_strategy,
)

logger = logging.getLogger("epub_factory.book_profiler")

BOOK_PROFILER_VERSION = "book-profiler-v1"

_PROFILER_SYSTEM_PROMPT = """你是一名图书鉴定师。根据提供的 EPUB 元数据、目录和正文抽样，
判断原书的文体、叙事基调、语言复杂度和忠实度风险，并返回一个 JSON 对象。

硬性规则：
1. 只能从输入证据判断；无法确定时写 unknown，不能凭常识补全。
2. recommended_strategy 只能是 neutral_faithful、literary_narrative、
   academic_rigorous、mirror_fidelity、practical_technical 之一。
3. 历史、政治、哲学、法律及高精度技术文献若修辞或结构本身承载信息，
   应优先考虑 mirror_fidelity。
4. 人物代词、身份和关系只有输入有明确证据时才能填写，否则写 unknown。
5. translated_name 使用输入中的 target_language；没有可靠译名时留空。
6. 只返回 JSON，不返回 Markdown。

返回字段：
{
  "genre": "string",
  "subgenres": ["string"],
  "tone": ["string"],
  "complexity": {
    "source_level": "A1|A2|B1|B2|C1|C2|unknown",
    "syntax": "simple|moderate|complex|unknown",
    "terminology_density": "low|medium|high|unknown"
  },
  "narrative": {
    "person": "first|second|third|mixed|unknown",
    "dialogue_ratio": "low|medium|high|unknown",
    "rhetorical_features": ["string"]
  },
  "fidelity_risk": "low|medium|high|unknown",
  "recommended_strategy": "one allowed strategy",
  "confidence": 0.0,
  "book_summary": "用于后续翻译消歧的简短背景，不得加入原文没有的信息",
  "evidence": [{"source": "metadata|toc|preface|first_chapter|distributed_sample", "reason": "string"}],
  "characters": [{
    "source_name": "string",
    "translated_name": "string or empty",
    "aliases": ["string"],
    "pronouns": "string or unknown",
    "role": "string or unknown",
    "relationships": ["string"],
    "evidence": "short source-grounded reason",
    "confidence": 0.0
  }]
}"""

_PREFACE_HINTS = re.compile(
    r"(preface|foreword|introduction|prologue|序|前言|导论|引言)",
    re.IGNORECASE,
)


def _clean_text(value: Any, limit: int) -> str:
    text = BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _flatten_toc(items: Any, *, out: list[str], limit: int = 120) -> None:
    if len(out) >= limit:
        return
    if isinstance(items, (list, tuple)):
        for item in items:
            if len(out) >= limit:
                break
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], (list, tuple)):
                _flatten_toc(item[0], out=out, limit=limit)
                _flatten_toc(item[1], out=out, limit=limit)
                continue
            _flatten_toc(item, out=out, limit=limit)
        return
    title = getattr(items, "title", None)
    href = getattr(items, "href", None)
    label = _clean_text(title or items, 180)
    if label and not label.startswith("<"):
        out.append(f"{label} [{href}]" if href else label)


def _extract_metadata_and_toc(epub_path: str) -> tuple[dict[str, Any], list[str]]:
    metadata: dict[str, Any] = {}
    toc: list[str] = []
    try:
        book = EpubUnpacker(epub_path).load_book()
        if not book:
            return metadata, toc
        for key in ("title", "creator", "language", "subject", "description", "publisher", "date"):
            values = book.get_metadata("DC", key)
            cleaned = [_clean_text(item[0], 600) for item in values if item and _clean_text(item[0], 600)]
            if cleaned:
                metadata[key] = cleaned[:12]
        _flatten_toc(getattr(book, "toc", []), out=toc)
    except Exception as exc:
        logger.warning("book profiler metadata extraction failed: %s", exc)
    return metadata, toc


def build_profiler_input(
    *,
    epub_path: str,
    manifest: dict[str, Any],
    book_title: str = "",
    max_chars: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build bounded profiler content and a persistence-safe sampling audit."""
    limit = max(6000, int(max_chars or os.environ.get("EPUB_BOOK_PROFILER_MAX_CHARS", "18000")))
    metadata, toc = _extract_metadata_and_toc(epub_path)
    if book_title and not metadata.get("title"):
        metadata["title"] = [book_title]

    chapters = [
        chapter for chapter in manifest.get("chapters", [])
        if chapter.get("chapter_kind") == "body" and chapter.get("chunks")
    ]
    chapter_texts: list[tuple[str, str]] = []
    for chapter in chapters:
        text = "\n".join(
            _clean_text(chunk.get("text") or chunk.get("html"), 4000)
            for chunk in chapter.get("chunks") or []
        )
        text = re.sub(r"\n+", "\n", text).strip()
        if text:
            chapter_texts.append((str(chapter.get("file_path") or ""), text))

    selected: list[dict[str, str]] = []
    preface = next(
        ((path, text) for path, text in chapter_texts if _PREFACE_HINTS.search(path) or _PREFACE_HINTS.search(text[:160])),
        None,
    )
    if preface:
        selected.append({"source": "preface", "file": preface[0], "text": preface[1][:4000]})
    if chapter_texts:
        first = chapter_texts[0]
        selected.append({"source": "first_chapter", "file": first[0], "text": first[1][:6000]})
        indexes = sorted({0, len(chapter_texts) // 2, len(chapter_texts) - 1})
        for index in indexes:
            path, text = chapter_texts[index]
            selected.append({
                "source": "distributed_sample",
                "file": path,
                "text": text[:2800],
            })

    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    used = 0
    for sample in selected:
        key = (sample["source"], sample["file"])
        if key in seen or used >= limit:
            continue
        remaining = limit - used
        text = sample["text"][:remaining]
        if not text:
            continue
        deduped.append({**sample, "text": text})
        seen.add(key)
        used += len(text)

    payload = {
        "metadata": metadata,
        "toc": toc,
        "samples": deduped,
    }
    sampling_audit = {
        "metadata_fields": sorted(metadata),
        "toc_entries": len(toc),
        "sample_count": len(deduped),
        "sample_chars": used,
        "samples": [
            {
                "source": sample["source"],
                "file": sample["file"],
                "chars": len(sample["text"]),
                "sha256": hashlib.sha256(sample["text"].encode("utf-8")).hexdigest(),
            }
            for sample in deduped
        ],
    }
    return payload, sampling_audit


def _heuristic_profile(payload: dict[str, Any], *, reason: str) -> dict[str, Any]:
    metadata = json.dumps(payload.get("metadata") or {}, ensure_ascii=False)
    toc = " ".join(payload.get("toc") or [])
    samples = " ".join(str(item.get("text") or "") for item in payload.get("samples") or [])
    corpus = f"{metadata} {toc} {samples[:12000]}"
    lowered = corpus.casefold()

    technical_hits = len(re.findall(
        r"\b(api|installation|configure|configuration|parameter|command|manual|tutorial|algorithm|software|hardware)\b",
        lowered,
    ))
    serious_hits = len(re.findall(
        r"\b(philosoph|epistemolog|politic|revolution|government|constitution|law|legal|history|historical|ideolog|sovereign)\w*",
        lowered,
    ))
    academic_hits = len(re.findall(
        r"\b(theory|methodolog|hypothesis|analysis|research|evidence|sociolog|econom|psycholog)\w*",
        lowered,
    ))
    dialogue_marks = corpus.count('"') + corpus.count("“") + corpus.count("”")
    dialogue_ratio = dialogue_marks / max(1, len(corpus))

    genre = "unknown"
    strategy = "neutral_faithful"
    tone = ["neutral"]
    fidelity_risk = "medium"
    if serious_hits >= 3:
        genre = "philosophy_history_politics"
        strategy = "mirror_fidelity"
        tone = ["serious", "argumentative", "historically_situated"]
        fidelity_risk = "high"
    elif technical_hits >= 3:
        genre = "technical"
        strategy = "practical_technical"
        tone = ["objective", "instructional"]
        fidelity_risk = "high"
    elif academic_hits >= 3:
        genre = "academic_nonfiction"
        strategy = "academic_rigorous"
        tone = ["serious", "analytical"]
        fidelity_risk = "high"
    elif dialogue_ratio > 0.008:
        genre = "fiction"
        strategy = "literary_narrative"
        tone = ["narrative"]
        fidelity_risk = "medium"

    avg_sentence = (
        sum(len(item) for item in re.split(r"[.!?。！？]+", corpus) if item.strip())
        / max(1, len([item for item in re.split(r"[.!?。！？]+", corpus) if item.strip()]))
    )
    syntax = "complex" if avg_sentence >= 120 else ("moderate" if avg_sentence >= 55 else "simple")
    source_level = "C1" if syntax == "complex" else ("B2" if syntax == "moderate" else "B1")
    return {
        "status": "fallback",
        "genre": genre,
        "subgenres": [],
        "tone": tone,
        "complexity": {
            "source_level": source_level,
            "syntax": syntax,
            "terminology_density": "high" if max(serious_hits, technical_hits, academic_hits) >= 6 else "medium",
        },
        "narrative": {
            "person": "unknown",
            "dialogue_ratio": "high" if dialogue_ratio > 0.015 else ("medium" if dialogue_ratio > 0.005 else "low"),
            "rhetorical_features": [],
        },
        "fidelity_risk": fidelity_risk,
        "recommended_strategy": strategy,
        "confidence": 0.35,
        "book_summary": "",
        "evidence": [{"source": "fallback", "reason": reason[:240]}],
        "characters": [],
    }


def _safe_string_list(value: Any, *, limit: int = 12, item_chars: int = 160) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:item_chars] for item in value[:limit] if str(item).strip()]


def _normalize_profile(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("profiler output must be a JSON object")
    complexity = raw.get("complexity") if isinstance(raw.get("complexity"), dict) else {}
    narrative = raw.get("narrative") if isinstance(raw.get("narrative"), dict) else {}
    confidence = float(raw.get("confidence") or 0)
    if not 0 <= confidence <= 1:
        raise ValueError("profiler confidence must be between 0 and 1")
    raw_recommended = str(raw.get("recommended_strategy") or "").strip().lower()
    if raw_recommended not in TRANSLATION_STRATEGIES:
        raise ValueError("profiler recommended an unsupported strategy")
    recommended = normalize_translation_strategy(
        raw_recommended,
        allow_auto=False,
        default="neutral_faithful",
    )

    characters: list[dict[str, Any]] = []
    for item in raw.get("characters") or []:
        if not isinstance(item, dict):
            continue
        source_name = str(item.get("source_name") or "").strip()[:120]
        evidence = str(item.get("evidence") or "").strip()[:320]
        if not source_name or not evidence:
            continue
        try:
            character_confidence = max(0.0, min(1.0, float(item.get("confidence") or 0)))
        except (TypeError, ValueError):
            character_confidence = 0.0
        characters.append({
            "source_name": source_name,
            "translated_name": str(item.get("translated_name") or "").strip()[:120],
            "aliases": _safe_string_list(item.get("aliases"), limit=8, item_chars=100),
            "pronouns": str(item.get("pronouns") or "unknown").strip()[:80],
            "role": str(item.get("role") or "unknown").strip()[:240],
            "relationships": _safe_string_list(item.get("relationships"), limit=10, item_chars=180),
            "evidence": evidence,
            "confidence": round(character_confidence, 3),
        })
        if len(characters) >= 80:
            break

    evidence: list[dict[str, str]] = []
    for item in raw.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip()[:80]
        reason = str(item.get("reason") or "").strip()[:320]
        if source and reason:
            evidence.append({"source": source, "reason": reason})
        if len(evidence) >= 12:
            break

    return {
        "status": "ok",
        "genre": str(raw.get("genre") or "unknown").strip()[:120],
        "subgenres": _safe_string_list(raw.get("subgenres"), limit=8),
        "tone": _safe_string_list(raw.get("tone"), limit=8),
        "complexity": {
            "source_level": str(complexity.get("source_level") or "unknown")[:20],
            "syntax": str(complexity.get("syntax") or "unknown")[:20],
            "terminology_density": str(complexity.get("terminology_density") or "unknown")[:20],
        },
        "narrative": {
            "person": str(narrative.get("person") or "unknown")[:20],
            "dialogue_ratio": str(narrative.get("dialogue_ratio") or "unknown")[:20],
            "rhetorical_features": _safe_string_list(narrative.get("rhetorical_features"), limit=8),
        },
        "fidelity_risk": str(raw.get("fidelity_risk") or "unknown")[:20],
        "recommended_strategy": recommended,
        "confidence": round(confidence, 3),
        "book_summary": str(raw.get("book_summary") or "").strip()[:1200],
        "evidence": evidence,
        "characters": characters,
    }


def _extract_json(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("profiler response did not contain JSON")
    parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("profiler response was not an object")
    return parsed


def _provider_label(base_url: str) -> str:
    host = (urlparse(base_url).hostname or "").casefold()
    if "deepseek" in host:
        return "deepseek"
    if "aliyun" in host or "dashscope" in host:
        return "aliyun"
    return host or "configured_provider"


@billing_stage("book_profile")
async def profile_book_async(
    *,
    epub_path: str,
    manifest: dict[str, Any],
    book_title: str = "",
    model: str | None = None,
    target_lang: str = "zh-CN",
) -> dict[str, Any]:
    payload, sampling = build_profiler_input(
        epub_path=epub_path,
        manifest=manifest,
        book_title=book_title,
    )
    fallback = _heuristic_profile(payload, reason="图书探针模型不可用，使用本地保守规则")
    enabled = os.environ.get("EPUB_BOOK_PROFILER_ENABLED", "1").lower() not in {"0", "false", "no", "off"}
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
    profiler_model = (
        os.environ.get("EPUB_BOOK_PROFILER_MODEL", "").strip()
        or str(model or "").strip()
        or os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
    )
    common = {
        "profiler_version": BOOK_PROFILER_VERSION,
        "profiler_model": profiler_model,
        "profiler_provider": _provider_label(base_url),
        "sampling": sampling,
    }
    if not enabled:
        fallback["evidence"] = [{"source": "fallback", "reason": "图书探针已由配置关闭"}]
        return {**fallback, **common}
    if not api_key or api_key == "dummy":
        fallback["evidence"] = [{"source": "fallback", "reason": "图书探针未配置可用 API Key"}]
        return {**fallback, **common}
    if not payload.get("samples"):
        fallback["evidence"] = [{"source": "fallback", "reason": "未抽取到可分析的正文样本"}]
        return {**fallback, **common}
    from app.infra.llm_guard import ModelNotAllowedError, assert_model_allowed
    try:
        assert_model_allowed(profiler_model, context="book_profiler")
    except ModelNotAllowedError as exc:
        fallback["evidence"] = [{
            "source": "fallback",
            "reason": f"图书探针模型未通过白名单：{str(exc)[:180]}",
        }]
        return {**fallback, **common}

    request_payload = {
        "target_language": target_lang,
        "metadata": payload.get("metadata"),
        "toc": payload.get("toc"),
        "samples": payload.get("samples"),
    }
    http_client = httpx.AsyncClient(
        timeout=float(os.environ.get("EPUB_BOOK_PROFILER_TIMEOUT", "75"))
    )
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        max_retries=0,
        http_client=http_client,
    )
    try:
        kwargs = {
            "model": profiler_model,
            "messages": [
                {"role": "system", "content": _PROFILER_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(request_payload, ensure_ascii=False)},
            ],
            "temperature": 0.1,
            "timeout": float(os.environ.get("EPUB_BOOK_PROFILER_TIMEOUT", "75")),
        }
        if os.environ.get("OPENAI_DISABLE_JSON_RESPONSE_FORMAT", "").lower() not in {"1", "true", "yes"}:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            response = await accounted_request(client.chat.completions.create(**kwargs), model=profiler_model, base_url=base_url)
        except Exception as exc:
            if "response_format" not in str(exc).lower():
                raise
            kwargs.pop("response_format", None)
            response = await accounted_request(client.chat.completions.create(**kwargs), model=profiler_model, base_url=base_url)
        profile = _normalize_profile(_extract_json(response.choices[0].message.content or ""))
        usage = getattr(response, "usage", None)
        profile["usage"] = {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
        }
        return {**profile, **common}
    except Exception as exc:
        logger.warning("book profiler failed; using conservative fallback: %s", exc)
        fallback["evidence"] = [{
            "source": "fallback",
            "reason": f"图书探针失败，使用本地保守规则：{str(exc)[:180]}",
        }]
        return {**fallback, **common}
    finally:
        await client.close()
        await http_client.aclose()


def profile_book(
    *,
    epub_path: str | Path,
    manifest: dict[str, Any],
    book_title: str = "",
    model: str | None = None,
    target_lang: str = "zh-CN",
) -> dict[str, Any]:
    import asyncio

    return asyncio.run(profile_book_async(
        epub_path=str(epub_path),
        manifest=manifest,
        book_title=book_title,
        model=model,
        target_lang=target_lang,
    ))
