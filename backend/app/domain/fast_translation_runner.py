"""
快速翻译执行器：预处理 EPUB 后，按章节/块执行 MapReduce 翻译。

目标：
- 保留现有 ExtremeCompiler 的非 LLM 清洗能力；
- 翻译阶段使用全书 glossary + chunk batch 并发，提高速度并降低术语漂移；
- 将章节/chunk 结果写入 store，供任务详情、断点续跑和质量审计使用。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bs4 import BeautifulSoup

from app.converter import converter
from app.domain.book_reduce_service import make_get_chapter_content, reduce_and_package, set_chapter_output
from app.domain.chapter_reduce_service import apply_chunk_results
from app.domain.chapter_translation_service import ChunkResult
from app.domain.manifest_service import build_manifest
from app.engine.cleaners.semantics_translator import SemanticsTranslator
from app.engine.compiler import EPUBCHECK_JAR
from app.engine.glossary_extractor import build_auto_glossary, merge_glossaries, verify_and_fix
from app.engine.unpacker import EpubUnpacker
from app.models import (
    ChapterKind,
    ChapterStatus,
    ChunkStatus,
    ConversionResult,
    ErrorCode,
    JobChapter,
    JobChunk,
)
from app.storage import job_store


ProgressCallback = Callable[[str], None]
StageCallback = Callable[[str, str, int | None], None]

logger = logging.getLogger("epub.fast_translation")


def _log_stage(job, stage: str, latency_ms: float | None = None, **fields: Any) -> None:
    """结构化阶段日志：统一带上 job_id / trace_id，便于线上链路追溯。"""
    parts = [
        f"job_id={getattr(job, 'id', None)}",
        f"trace_id={getattr(job, 'trace_id', None)}",
        f"stage={stage}",
    ]
    if latency_ms is not None:
        parts.append(f"latency_ms={latency_ms:.0f}")
    for key, value in fields.items():
        parts.append(f"{key}={value}")
    logger.info("[fast-translation] " + " ".join(parts))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_bytes(content: Any) -> bytes:
    if content is None:
        return b""
    if isinstance(content, bytes):
        return content
    if isinstance(content, bytearray):
        return bytes(content)
    return str(content).encode("utf-8", errors="replace")


def _extract_texts_from_manifest(manifest: dict) -> list[str]:
    texts: list[str] = []
    for chapter in manifest.get("chapters", []):
        if chapter.get("chapter_kind") != ChapterKind.body.value:
            continue
        for chunk in chapter.get("chunks") or []:
            text = (chunk.get("text") or "").strip()
            if text:
                texts.append(text)
    return texts


def _load_content_by_file(epub_path: str) -> dict[str, bytes]:
    unpacker = EpubUnpacker(epub_path)
    book = unpacker.load_book()
    if not book:
        raise RuntimeError("Failed to load preprocessed EPUB for translation")
    out: dict[str, bytes] = {}
    for item in book.get_items():
        name = item.get_name() if hasattr(item, "get_name") else None
        if not name:
            continue
        out[name] = _as_bytes(item.get_content())
    return out


def _chapter_status(chunk_results: list[ChunkResult]) -> ChapterStatus:
    if not chunk_results:
        return ChapterStatus.completed
    failed = sum(1 for c in chunk_results if c.error)
    ok = len(chunk_results) - failed
    if failed == 0:
        return ChapterStatus.completed
    if ok == 0:
        return ChapterStatus.failed
    return ChapterStatus.partial_completed


def _upsert_chapter(chapter: JobChapter) -> None:
    upsert = getattr(job_store, "upsert_chapter", None)
    if upsert:
        upsert(chapter)


def _upsert_chunk(job_id: str, chapter_id: str, cr: ChunkResult, status: ChunkStatus) -> None:
    upsert = getattr(job_store, "upsert_chunk", None)
    if not upsert:
        return
    now = _now()
    source_hash = hashlib.sha256(cr.original_html.encode("utf-8", errors="replace")).hexdigest()
    upsert(JobChunk(
        job_id=job_id,
        chapter_id=chapter_id,
        chunk_id=cr.chunk_id,
        sequence=cr.sequence,
        locator=cr.locator,
        source_hash=source_hash,
        status=status,
        cached=cr.cached,
        model=cr.model,
        base_url=cr.base_url,
        retry_count=0,
        prompt_tokens=cr.prompt_tokens,
        completion_tokens=cr.completion_tokens,
        latency_ms=cr.latency_ms,
        error_message=cr.error,
        created_at=now,
        updated_at=now,
    ))


def _run_epubcheck(output_path: Path) -> bool:
    jar = os.path.abspath(EPUBCHECK_JAR)
    if not os.path.exists(jar):
        return True

    json_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            json_path = tmp.name
        subprocess.run(
            ["java", "-jar", jar, str(output_path), "--json", json_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        messages = data.get("messages", [])
        fatals = sum(1 for m in messages if m.get("severity") == "FATAL")
        errors = sum(1 for m in messages if m.get("severity") == "ERROR")
        return fatals == 0 and errors == 0
    except Exception:
        return False
    finally:
        if json_path:
            try:
                os.unlink(json_path)
            except OSError:
                pass


def _metrics_summary(timings: list[tuple[str, float]]) -> str:
    total = sum(ms for _, ms in timings)
    lines = [
        "",
        "────────────────────────────────────────────────────",
        f"⏱  Pipeline [fast-translation] — 总耗时 {total:.0f} ms",
        "────────────────────────────────────────────────────",
    ]
    for name, ms in timings:
        lines.append(f"  ✅ {name:<28} {ms:>7.1f} ms")
    lines.append("────────────────────────────────────────────────────")
    return "\n".join(lines)


async def _translate_manifest_async(
    *,
    job,
    manifest: dict,
    content_by_file: dict[str, bytes],
    glossary: dict[str, str],
    progress_callback: ProgressCallback,
) -> tuple[dict[str, Any], dict[str, Any]]:
    translator = SemanticsTranslator(
        target_lang=job.target_lang,
        bilingual=job.bilingual,
        glossary=glossary,
        temperature=getattr(job, "temperature", None),
    )

    body_chapters = [
        ch for ch in manifest.get("chapters", [])
        if ch.get("chapter_kind") == ChapterKind.body.value and (ch.get("chunks") or [])
    ]
    chapter_limit = max(1, int(os.environ.get("EPUB_CHAPTER_CONCURRENCY", "4")))
    chapter_sem = asyncio.Semaphore(chapter_limit)
    audit = {
        "glossary_fixed_count": 0,
        "glossary_fixed_terms": {},
        "glossary_unfixable_examples": [],
    }

    for ch in manifest.get("chapters", []):
        kind = ChapterKind(ch.get("chapter_kind", ChapterKind.body.value))
        chunks = ch.get("chunks") or []
        status = ChapterStatus.pending if kind == ChapterKind.body and chunks else ChapterStatus.completed
        _upsert_chapter(JobChapter(
            job_id=job.id,
            chapter_id=ch["chapter_id"],
            file_path=ch["file_path"],
            chapter_kind=kind,
            status=status,
            chunk_total=len(chunks),
        ))

    async def run_chapter(chapter: dict) -> list[ChunkResult]:
        async with chapter_sem:
            started = _now()
            specs = chapter.get("chunks") or []
            _upsert_chapter(JobChapter(
                job_id=job.id,
                chapter_id=chapter["chapter_id"],
                file_path=chapter["file_path"],
                chapter_kind=ChapterKind.body,
                status=ChapterStatus.running,
                chunk_total=len(specs),
                started_at=started,
            ))
            progress_callback(f"快速翻译 {chapter['file_path']}（{len(specs)} 段）")

            translated = await translator.translate_many_chunks_async([c["html"] for c in specs])
            chunk_results: list[ChunkResult] = []
            chunk_statuses: list[ChunkStatus] = []
            for spec, res in zip(specs, translated):
                status = ChunkStatus.failed if res.error else (ChunkStatus.cached if res.cached else ChunkStatus.translated)
                raw_text = BeautifulSoup(spec["html"], "html.parser").get_text()
                if not translator._should_translate(raw_text):
                    status = ChunkStatus.skipped

                translated_html = res.translated_html
                if glossary and not res.error and status != ChunkStatus.skipped:
                    fixed_html, verify = verify_and_fix(spec["html"], translated_html, glossary)
                    translated_html = fixed_html
                    if verify.fixed_count:
                        audit["glossary_fixed_count"] += verify.fixed_count
                        for term, count in verify.fixed_terms.items():
                            audit["glossary_fixed_terms"][term] = audit["glossary_fixed_terms"].get(term, 0) + count
                    for item in verify.unfixable_examples:
                        if len(audit["glossary_unfixable_examples"]) < 20:
                            audit["glossary_unfixable_examples"].append(item)

                cr = ChunkResult(
                    chunk_id=spec["chunk_id"],
                    sequence=spec["sequence"],
                    locator=spec["locator"],
                    original_html=spec["html"],
                    translated_html=translated_html,
                    cached=res.cached,
                    error=res.error,
                    model=res.model,
                    base_url=res.base_url,
                    prompt_tokens=res.prompt_tokens,
                    completion_tokens=res.completion_tokens,
                    latency_ms=res.latency_ms,
                )
                chunk_results.append(cr)
                chunk_statuses.append(status)
                _upsert_chunk(job.id, chapter["chapter_id"], cr, status)

            original = content_by_file.get(chapter["file_path"])
            if original is not None:
                reduced = apply_chunk_results(original, chunk_results, job.bilingual)
                set_chapter_output(job.id, chapter["file_path"], reduced)

            failed = sum(1 for c in chunk_results if c.error)
            # 按最终 status 统计，避免 skip 块（translate_many 返回 cached=True）被误计为缓存命中
            cached = sum(1 for s in chunk_statuses if s == ChunkStatus.cached)
            _upsert_chapter(JobChapter(
                job_id=job.id,
                chapter_id=chapter["chapter_id"],
                file_path=chapter["file_path"],
                chapter_kind=ChapterKind.body,
                status=_chapter_status(chunk_results),
                chunk_total=len(chunk_results),
                chunk_success=len(chunk_results) - failed,
                chunk_failed=failed,
                chunk_cached=cached,
                started_at=started,
                finished_at=_now(),
                error_message=translator.stats.last_error if failed else None,
            ))
            return chunk_results

    chapter_results = await asyncio.gather(*(run_chapter(ch) for ch in body_chapters))
    flat_results = [cr for chapter in chapter_results for cr in chapter]
    stats = translator.stats.to_dict(translator.model)
    stats.update({
        "chapters_total": len(body_chapters),
        "chunks_total": len(flat_results),
        "chunks_skipped": sum(
            1 for cr in flat_results
            if not translator._should_translate(BeautifulSoup(cr.original_html, "html.parser").get_text())
        ),
        **audit,
    })
    return stats, audit


def run_fast_translation_job(
    *,
    job,
    input_path: Path,
    output_path: Path,
    progress_callback: ProgressCallback,
    stage_callback: StageCallback,
) -> ConversionResult:
    """
    执行快速翻译主链路。仅面向 EPUB 翻译任务；调用方负责非 EPUB 的回退。
    """
    timings: list[tuple[str, float]] = []
    started_all = time.monotonic()
    _log_stage(job, "start", input=str(input_path))

    with tempfile.TemporaryDirectory(prefix="epub_fast_translate_") as tmp:
        preprocessed = Path(tmp) / "preprocessed.epub"

        t = time.monotonic()
        stage_callback("preprocessing", "开始预处理 EPUB（非 LLM 清洗）", None)
        pre_result = converter.convert_file_to_horizontal(
            input_path,
            preprocessed,
            job.output_mode,
            enable_translation=False,
            target_lang=job.target_lang,
            device=job.device.value,
            bilingual=False,
            glossary=None,
            temperature=None,
            traditional_variant=getattr(job, "traditional_variant", "auto") or "auto",
            lexicon_domains=getattr(job, "lexicon_domains", None),
            enable_proper_noun=getattr(job, "enable_proper_noun", True),
            progress_callback=progress_callback,
            stage_callback=stage_callback,
        )
        timings.append(("Preprocess", (time.monotonic() - t) * 1000))
        _log_stage(job, "preprocessing", timings[-1][1])

        t = time.monotonic()
        stage_callback("mapping", "生成章节 Manifest", None)
        manifest = build_manifest(str(preprocessed), job.id)
        if manifest.get("error"):
            _log_stage(job, "mapping_failed", error=manifest["error"])
            raise RuntimeError(manifest["error"])
        content_by_file = _load_content_by_file(str(preprocessed))
        timings.append(("Manifest", (time.monotonic() - t) * 1000))
        _log_stage(job, "mapping", timings[-1][1], chapters=len(manifest.get("chapters", [])))

        t = time.monotonic()
        stage_callback("glossary", "构建全书术语表", None)
        texts = _extract_texts_from_manifest(manifest)
        auto_glossary = build_auto_glossary(texts, target_lang=job.target_lang, min_count=2, max_terms=200)
        glossary = merge_glossaries(getattr(job, "glossary", None) or {}, auto_glossary)
        timings.append(("Glossary", (time.monotonic() - t) * 1000))
        _log_stage(job, "glossary", timings[-1][1], auto=len(auto_glossary), total=len(glossary))
        stage_callback(
            "glossary",
            f"术语表就绪：自动 {len(auto_glossary)} 条，用户 {len(getattr(job, 'glossary', {}) or {})} 条",
            int(timings[-1][1]),
        )

        t = time.monotonic()
        stage_callback("translating", "开始快速章节翻译", None)
        translation_stats, _audit = asyncio.run(_translate_manifest_async(
            job=job,
            manifest=manifest,
            content_by_file=content_by_file,
            glossary=glossary,
            progress_callback=progress_callback,
        ))
        timings.append(("TranslateMap", (time.monotonic() - t) * 1000))
        _log_stage(
            job, "translating", timings[-1][1],
            translated=translation_stats.get("translated_chunks"),
            cached=translation_stats.get("cached_chunks"),
            failed=translation_stats.get("failed_chunks"),
            cost_usd=translation_stats.get("cost_usd"),
        )

        if translation_stats.get("all_failed"):
            last_error = translation_stats.get("last_error") or "上游模型调用失败"
            _log_stage(job, "translating_failed", error=last_error)
            raise RuntimeError(f"AI 翻译失败：未成功写入任何译文。最后错误：{last_error}")

        t = time.monotonic()
        stage_callback("reducing", "回写译文并打包 EPUB", None)
        ok = reduce_and_package(
            str(preprocessed),
            str(output_path),
            make_get_chapter_content(job.id),
        )
        timings.append(("ReducePackage", (time.monotonic() - t) * 1000))
        _log_stage(job, "reducing", timings[-1][1])
        if not ok:
            _log_stage(job, "reducing_failed")
            raise RuntimeError("快速翻译 Reduce/打包失败")

    t = time.monotonic()
    stage_callback("validating", "校验 EPUB", None)
    validation_passed = _run_epubcheck(output_path)
    timings.append(("EpubCheck", (time.monotonic() - t) * 1000))
    _log_stage(job, "validating", timings[-1][1], passed=validation_passed)

    failed = int(translation_stats.get("failed_chunks") or 0)
    done = int(translation_stats.get("translated_chunks") or 0) + int(translation_stats.get("cached_chunks") or 0)
    message = "转换成功"
    error_code = None
    if failed:
        message = f"转换成功，但有 {failed} 个段落翻译失败，成功写入 {done} 个段落。"
        error_code = ErrorCode.PARTIAL_TRANSLATION.value
    if not validation_passed:
        message = "打包成功但 EPUB 校验未通过，结果不可交付"
        error_code = ErrorCode.EPUB_VALIDATION_FAILED.value

    total_ms = (time.monotonic() - started_all) * 1000
    timings.append(("Total", total_ms))
    metrics = _metrics_summary(timings)
    _log_stage(job, "done", total_ms, error_code=error_code, validation_passed=validation_passed)

    return ConversionResult(
        quality_stats=pre_result.quality_stats,
        translation_stats=translation_stats,
        lexicon_stats=pre_result.lexicon_stats,
        metrics_summary=metrics,
        message=message,
        error_code=error_code,
        validation_passed=validation_passed,
    )
