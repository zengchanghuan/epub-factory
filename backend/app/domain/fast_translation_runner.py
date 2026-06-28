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
import html
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

from app.cancellation import CancelCheck, raise_if_cancelled
from app.converter import converter
from app.domain.book_reduce_service import make_get_chapter_content, reduce_and_package, set_chapter_output
from app.domain.chapter_reduce_service import apply_chunk_results
from app.domain.chapter_translation_service import ChunkResult
from app.domain.failed_chunk_archive import archive_failed_chunk
from app.domain.manifest_service import build_manifest
from app.domain.translation_quality_audit import audit_translation_chunk
from app.domain.translation_qa_service import attach_translation_qa_report
from app.engine.cleaners.semantics_translator import SemanticsTranslator
from app.engine.compiler import EPUBCHECK_JAR
from app.engine.glossary_extractor import verify_and_fix
from app.engine.glossary_service import build_consistent_glossary
from app.engine.unpacker import EpubUnpacker
from app.models import (
    ChapterKind,
    ChapterStatus,
    ChunkStatus,
    ConversionResult,
    ErrorCode,
    JobChapter,
    JobChunk,
    JobStatus,
)
from app.storage import job_store


ProgressCallback = Callable[[str], None]
StageCallback = Callable[[str, str, int | None], None]

logger = logging.getLogger("epub.fast_translation")


def _log_stage(stage: str, latency_ms: float | None = None, **fields: Any) -> None:
    """结构化阶段日志。

    trace_id / job_id 由 run_job 设置的 contextvar 自动注入（见 infra.logging._JsonFormatter），
    这里不再手动拼接；阶段名与耗时等业务字段通过 extra 传入，作为 JSON 字段输出，便于检索。
    """
    extra_fields: dict[str, Any] = {"stage": stage}
    if latency_ms is not None:
        extra_fields["latency_ms"] = round(latency_ms)
    extra_fields.update(fields)
    logger.info(f"fast-translation {stage}", extra={"_extra_fields": extra_fields})


def _short_log(value: Any, limit: int = 180) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def _emit_progress(progress_callback: ProgressCallback | None, message: str) -> None:
    if progress_callback and message:
        progress_callback(message)


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


def _extract_book_title(epub_path: str) -> str:
    unpacker = EpubUnpacker(epub_path)
    book = unpacker.load_book()
    if not book:
        return ""
    try:
        titles = book.get_metadata("DC", "title")
        if titles:
            return str(titles[0][0] or "").strip()
    except Exception:
        pass
    return str(getattr(book, "title", "") or "").strip()


async def _translate_book_title_async(
    *,
    title: str,
    target_lang: str,
    glossary: dict[str, str],
    temperature: float | None,
) -> str:
    title = (title or "").strip()
    if not title or not any(ch.isalpha() for ch in title):
        return title
    translator = SemanticsTranslator(
        target_lang=target_lang,
        bilingual=False,
        glossary=glossary,
        temperature=temperature,
    )
    result = await translator.translate_single_chunk_async(f"<p>{html.escape(title)}</p>")
    if result.error:
        return title
    translated = BeautifulSoup(result.translated_html or "", "html.parser").get_text(" ", strip=True)
    return translated.strip() or title


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
    source_text = BeautifulSoup(cr.original_html or "", "html.parser").get_text(" ", strip=True)
    translated_text = BeautifulSoup(cr.translated_html or "", "html.parser").get_text(" ", strip=True)
    upsert(JobChunk(
        job_id=job_id,
        chapter_id=chapter_id,
        chunk_id=cr.chunk_id,
        sequence=cr.sequence,
        locator=cr.locator,
        source_hash=source_hash,
        source_text=source_text,
        translated_text=translated_text,
        audit_json=cr.audit_json or {},
        status=status,
        cached=cr.cached,
        model=cr.model,
        base_url=cr.base_url,
        retry_count=getattr(cr, "retry_count", 0),
        prompt_tokens=cr.prompt_tokens,
        completion_tokens=cr.completion_tokens,
        latency_ms=cr.latency_ms,
        error_message=cr.error,
        created_at=now,
        updated_at=now,
    ))
    archive_failed_chunk(job_id=job_id, chapter_id=chapter_id, chunk=cr, status=status)


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


def _translation_failures_exceed_delivery_gate(stats: dict[str, Any]) -> bool:
    """大量翻译失败时停止交付，避免生成中英混杂的 EPUB。"""
    failed = int(stats.get("failed_chunks") or 0)
    total = int(stats.get("total_chunks") or 0)
    if failed <= 0 or total <= 0:
        return False
    max_failed = max(0, int(os.environ.get("EPUB_TRANSLATION_MAX_DELIVERABLE_FAILED_CHUNKS", "20")))
    max_ratio = max(0.0, float(os.environ.get("EPUB_TRANSLATION_MAX_DELIVERABLE_FAILED_RATIO", "0.02")))
    return failed > max_failed and (failed / total) > max_ratio


def _translation_delivery_gate_result(
    *,
    pre_result: ConversionResult,
    translation_stats: dict[str, Any],
    timings: list[tuple[str, float]],
    started_all: float,
    failed: int,
    total: int,
    last_error: str,
    progress_callback: ProgressCallback | None = None,
) -> ConversionResult:
    message = (
        f"AI 翻译失败：仍有 {failed}/{total} 个段落未成功翻译。"
        "为避免生成中英混杂的 EPUB，已停止打包；请稍后重试或降低并发后重试。"
        f"最后错误：{last_error}"
    )
    stats = dict(translation_stats)
    stats["delivery_gate_failed"] = True
    stats["deliverable"] = False
    stats = attach_translation_qa_report(
        stats,
        output_path=None,
        error_code=ErrorCode.PARTIAL_TRANSLATION.value,
    )

    total_ms = (time.monotonic() - started_all) * 1000
    timings.append(("Total", total_ms))
    metrics = _metrics_summary(timings)
    _log_stage(
        "translation_quality_gate_failed",
        failed=failed,
        total=total,
        error=last_error,
        error_code=ErrorCode.PARTIAL_TRANSLATION.value,
    )
    _emit_progress(
        progress_callback,
        f"翻译交付质检未通过：仍有 {failed}/{total} 个段落失败，已停止打包。最后错误：{_short_log(last_error)}",
    )
    return ConversionResult(
        quality_stats=pre_result.quality_stats,
        translation_stats=stats,
        lexicon_stats=pre_result.lexicon_stats,
        metrics_summary=metrics,
        message=message,
        error_code=ErrorCode.PARTIAL_TRANSLATION.value,
        validation_passed=False,
    )


async def _translate_manifest_async(
    *,
    job,
    manifest: dict,
    content_by_file: dict[str, bytes],
    glossary: dict[str, str],
    progress_callback: ProgressCallback,
    cancel_check: CancelCheck | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    translator = SemanticsTranslator(
        target_lang=job.target_lang,
        bilingual=job.bilingual,
        glossary=glossary,
        temperature=getattr(job, "temperature", None),
    )
    translator.cancel_check = cancel_check

    body_chapters = [
        ch for ch in manifest.get("chapters", [])
        if ch.get("chapter_kind") == ChapterKind.body.value and (ch.get("chunks") or [])
    ]
    manifest_body_chunk_total = sum(len(ch.get("chunks") or []) for ch in body_chapters)
    configured_chapter_limit = int(os.environ.get("EPUB_CHAPTER_CONCURRENCY", "4"))
    chapter_limit_cap = int(os.environ.get("EPUB_CHAPTER_CONCURRENCY_CAP", "2"))
    chapter_limit = max(1, min(configured_chapter_limit, max(1, chapter_limit_cap)))
    chapter_sem = asyncio.Semaphore(chapter_limit)
    live_stats_interval = max(1.0, float(os.environ.get("EPUB_TRANSLATION_STATS_PUBLISH_INTERVAL", "5")))
    last_live_stats_publish = 0.0
    audit = {
        "glossary_fixed_count": 0,
        "glossary_fixed_terms": {},
        "glossary_unfixable_examples": [],
        "audit_warn_chunks": 0,
        "audit_failed_chunks": 0,
        "audit_flags_count": {},
        "audit_examples": [],
    }

    def _build_translation_stats(*, live: bool, flat_results: list[ChunkResult] | None = None) -> dict[str, Any]:
        stats = translator.stats.to_dict(translator.model)
        stats.update({
            "chapters_total": len(body_chapters),
            "manifest_chunks_total": manifest_body_chunk_total,
            "chunks_total": len(flat_results) if flat_results is not None else manifest_body_chunk_total,
            "chunks_processed": int(stats.get("translated_chunks") or 0)
            + int(stats.get("cached_chunks") or 0)
            + int(stats.get("failed_chunks") or 0),
            "live": live,
            **audit,
        })
        if flat_results is not None:
            stats["chunks_skipped"] = sum(
                1 for cr in flat_results
                if not translator._should_translate(BeautifulSoup(cr.original_html, "html.parser").get_text())
            )
        return stats

    def _publish_translation_stats(
        stats: dict[str, Any],
        *,
        message: str | None = None,
        force: bool = False,
    ) -> None:
        nonlocal last_live_stats_publish
        now = time.monotonic()
        if not force and (now - last_live_stats_publish) < live_stats_interval:
            return
        last_live_stats_publish = now
        update_status = getattr(job_store, "update_status", None)
        if not update_status:
            return
        try:
            current = job_store.get(job.id) if getattr(job_store, "get", None) else None
            current_status = getattr(current, "status", None) or getattr(job, "status", None) or JobStatus.running
            current_message = (
                message
                or getattr(current, "message", None)
                or getattr(job, "message", None)
                or "翻译中..."
            )
            update_status(
                job.id,
                current_status,
                current_message,
                translation_stats=stats,
            )
        except Exception:
            logger.warning("failed to publish live translation stats", exc_info=True)

    def _publish_live_translation_stats(message: str | None = None, *, force: bool = False) -> None:
        _publish_translation_stats(
            _build_translation_stats(live=True),
            message=message,
            force=force,
        )

    def emit_progress(message: str) -> None:
        progress_callback(message)
        _publish_live_translation_stats(message=message)

    translator.progress_callback = emit_progress

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
            raise_if_cancelled(cancel_check)
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
            emit_progress(f"快速翻译 {chapter['file_path']}（{len(specs)} 段）")
            raise_if_cancelled(cancel_check)

            translated = await translator.translate_many_chunks_async(
                [c["html"] for c in specs],
                progress_label=f"快速翻译 {chapter['file_path']}",
            )
            raise_if_cancelled(cancel_check)
            chunk_results: list[ChunkResult] = []
            chunk_statuses: list[ChunkStatus] = []
            chapter_audit_fail = 0
            chapter_audit_warn = 0
            for spec, res in zip(specs, translated):
                raise_if_cancelled(cancel_check)
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

                quality = audit_translation_chunk(
                    original_html=spec["html"],
                    translated_html=translated_html,
                    glossary=glossary if status != ChunkStatus.skipped else {},
                    error_like_checker=translator._looks_like_error_response,
                ).to_dict()
                if res.error:
                    quality["flags"] = list(dict.fromkeys([*quality.get("flags", []), "translation_error"]))
                    quality["risk_level"] = "fail"
                risk_level = quality.get("risk_level")
                if risk_level == "warn":
                    audit["audit_warn_chunks"] += 1
                    chapter_audit_warn += 1
                elif risk_level == "fail":
                    audit["audit_failed_chunks"] += 1
                    chapter_audit_fail += 1
                for flag in quality.get("flags", []):
                    audit["audit_flags_count"][flag] = audit["audit_flags_count"].get(flag, 0) + 1
                if risk_level in ("warn", "fail") and len(audit["audit_examples"]) < 20:
                    audit["audit_examples"].append({
                        "chapter_id": chapter["chapter_id"],
                        "chunk_id": spec["chunk_id"],
                        "risk_level": risk_level,
                        "flags": quality.get("flags", []),
                        "source_text": quality.get("source_text", "")[:160],
                        "translated_text": quality.get("translated_text", "")[:160],
                    })

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
                    retry_count=getattr(res, "retry_count", 0),
                    error_type=getattr(res, "error_type", None),
                    audit_json=quality,
                )
                chunk_results.append(cr)
                chunk_statuses.append(status)
                _upsert_chunk(job.id, chapter["chapter_id"], cr, status)

            original = content_by_file.get(chapter["file_path"])
            raise_if_cancelled(cancel_check)
            if original is not None:
                reduced = apply_chunk_results(original, chunk_results, job.bilingual)
                set_chapter_output(job.id, chapter["file_path"], reduced)
            else:
                emit_progress(f"章节回写失败：{chapter['file_path']} 原始内容缺失")

            failed = sum(1 for c in chunk_results if c.error)
            if failed:
                emit_progress(
                    f"章节翻译存在失败：{chapter['file_path']} 失败 {failed}/{len(chunk_results)} 段。最后错误：{_short_log(translator.stats.last_error)}"
                )
            if chapter_audit_fail:
                emit_progress(
                    f"章节质检未通过：{chapter['file_path']} {chapter_audit_fail} 段需要重译"
                )
            elif chapter_audit_warn:
                emit_progress(
                    f"章节质检提示：{chapter['file_path']} {chapter_audit_warn} 段需要复核"
                )
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
            _publish_live_translation_stats(
                message=f"章节翻译完成：{chapter['file_path']}（失败 {failed}/{len(chunk_results)} 段）",
                force=True,
            )
            return chunk_results

    raise_if_cancelled(cancel_check)
    chapter_results = await asyncio.gather(*(run_chapter(ch) for ch in body_chapters))
    raise_if_cancelled(cancel_check)
    flat_results = [cr for chapter in chapter_results for cr in chapter]
    stats = _build_translation_stats(live=False, flat_results=flat_results)
    _publish_translation_stats(stats, message="快速章节翻译完成", force=True)
    return stats, audit


def run_fast_translation_job(
    *,
    job,
    input_path: Path,
    output_path: Path,
    progress_callback: ProgressCallback,
    stage_callback: StageCallback,
    cancel_check: CancelCheck | None = None,
) -> ConversionResult:
    """
    执行快速翻译主链路。仅面向 EPUB 翻译任务；调用方负责非 EPUB 的回退。
    """
    timings: list[tuple[str, float]] = []
    started_all = time.monotonic()
    _log_stage("start", input=str(input_path))
    raise_if_cancelled(cancel_check)

    with tempfile.TemporaryDirectory(prefix="epub_fast_translate_") as tmp:
        preprocessed = Path(tmp) / "preprocessed.epub"

        raise_if_cancelled(cancel_check)
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
        _log_stage("preprocessing", timings[-1][1])
        raise_if_cancelled(cancel_check)

        t = time.monotonic()
        stage_callback("mapping", "生成章节 Manifest", None)
        manifest = build_manifest(str(preprocessed), job.id)
        if manifest.get("error"):
            _log_stage("mapping_failed", error=manifest["error"])
            progress_callback(f"生成章节 Manifest 失败：{_short_log(manifest['error'])}")
            stage_callback("mapping_failed", f"生成章节 Manifest 失败：{_short_log(manifest['error'])}", None)
            raise RuntimeError(manifest["error"])
        content_by_file = _load_content_by_file(str(preprocessed))
        original_book_title = _extract_book_title(str(preprocessed))
        timings.append(("Manifest", (time.monotonic() - t) * 1000))
        _log_stage("mapping", timings[-1][1], chapters=len(manifest.get("chapters", [])))
        raise_if_cancelled(cancel_check)

        t = time.monotonic()
        stage_callback("glossary", "构建全书术语表", None)
        texts = _extract_texts_from_manifest(manifest)
        glossary_result = build_consistent_glossary(
            texts,
            target_lang=job.target_lang,
            user_glossary=getattr(job, "glossary", None) or {},
            min_count=2,
            max_terms=160,
        )
        glossary = glossary_result.glossary
        timings.append(("Glossary", (time.monotonic() - t) * 1000))
        _log_stage("glossary", timings[-1][1], **glossary_result.stats)
        stage_callback(
            "glossary",
            (
                f"术语表就绪：全局 {len(glossary_result.global_glossary)} 条，"
                f"自动 {len(glossary_result.auto_glossary)} 条，"
                f"用户 {len(getattr(job, 'glossary', {}) or {})} 条"
            ),
            int(timings[-1][1]),
        )
        raise_if_cancelled(cancel_check)

        t = time.monotonic()
        stage_callback("metadata", "翻译书名元数据", None)
        translated_book_title = asyncio.run(_translate_book_title_async(
            title=original_book_title,
            target_lang=job.target_lang,
            glossary=glossary,
            temperature=getattr(job, "temperature", None),
        ))
        timings.append(("BookTitle", (time.monotonic() - t) * 1000))
        _log_stage(
            "metadata", timings[-1][1],
            original_title=original_book_title,
            translated_title=translated_book_title,
        )
        raise_if_cancelled(cancel_check)

        t = time.monotonic()
        stage_callback("translating", "开始快速章节翻译", None)
        translation_stats, _audit = asyncio.run(_translate_manifest_async(
            job=job,
            manifest=manifest,
            content_by_file=content_by_file,
            glossary=glossary,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        ))
        translation_stats.update({
            "glossary_stats": glossary_result.stats,
            "glossary_terms_total": len(glossary),
            "book_title_original": original_book_title,
            "book_title_translated": translated_book_title,
        })
        timings.append(("TranslateMap", (time.monotonic() - t) * 1000))
        _log_stage(
            "translating", timings[-1][1],
            translated=translation_stats.get("translated_chunks"),
            cached=translation_stats.get("cached_chunks"),
            failed=translation_stats.get("failed_chunks"),
            cost_usd=translation_stats.get("cost_usd"),
        )
        raise_if_cancelled(cancel_check)

        if translation_stats.get("all_failed"):
            last_error = translation_stats.get("last_error") or "上游模型调用失败"
            _log_stage("translating_failed", error=last_error)
            progress_callback(f"AI 翻译全失败：未成功写入任何译文。最后错误：{_short_log(last_error)}")
            stage_callback("translating_failed", f"AI 翻译全失败：{_short_log(last_error)}", None)
            raise RuntimeError(f"AI 翻译失败：未成功写入任何译文。最后错误：{last_error}")
        if _translation_failures_exceed_delivery_gate(translation_stats):
            failed = int(translation_stats.get("failed_chunks") or 0)
            total = int(translation_stats.get("total_chunks") or 0)
            last_error = translation_stats.get("last_error") or "部分段落重试后仍未成功翻译"
            stage_callback(
                "translation_quality_gate_failed",
                f"翻译交付质检未通过：仍有 {failed}/{total} 个段落失败，停止打包",
                None,
            )
            return _translation_delivery_gate_result(
                pre_result=pre_result,
                translation_stats=translation_stats,
                timings=timings,
                started_all=started_all,
                failed=failed,
                total=total,
                last_error=last_error,
                progress_callback=progress_callback,
            )

        t = time.monotonic()
        raise_if_cancelled(cancel_check)
        stage_callback("reducing", "回写译文并打包 EPUB", None)
        ok = reduce_and_package(
            str(preprocessed),
            str(output_path),
            make_get_chapter_content(job.id),
            book_title=translated_book_title if translated_book_title != original_book_title else None,
            original_book_title=original_book_title,
        )
        timings.append(("ReducePackage", (time.monotonic() - t) * 1000))
        _log_stage("reducing", timings[-1][1])
        if not ok:
            _log_stage("reducing_failed")
            progress_callback("回写译文并打包 EPUB 失败：Reduce/打包未生成有效结果")
            stage_callback("reducing_failed", "回写译文并打包 EPUB 失败", None)
            raise RuntimeError("快速翻译 Reduce/打包失败")

    t = time.monotonic()
    raise_if_cancelled(cancel_check)
    stage_callback("validating", "校验 EPUB", None)
    validation_passed = _run_epubcheck(output_path)
    timings.append(("EpubCheck", (time.monotonic() - t) * 1000))
    _log_stage("validating", timings[-1][1], passed=validation_passed)
    if not validation_passed:
        progress_callback("EPUB 校验未通过：打包结果不可交付")
        stage_callback("validating_failed", "EPUB 校验未通过：打包结果不可交付", int(timings[-1][1]))

    failed = int(translation_stats.get("failed_chunks") or 0)
    done = int(translation_stats.get("translated_chunks") or 0) + int(translation_stats.get("cached_chunks") or 0)
    audit_review = int(translation_stats.get("audit_warn_chunks") or 0) + int(translation_stats.get("audit_failed_chunks") or 0)
    message = "转换成功"
    error_code = None
    if failed:
        message = f"转换成功，但有 {failed} 个段落翻译失败，成功写入 {done} 个段落。"
        error_code = ErrorCode.PARTIAL_TRANSLATION.value
    elif audit_review:
        message = f"转换成功，但有 {audit_review} 个段落需要复核。"
    if not validation_passed:
        message = "打包成功但 EPUB 校验未通过，结果不可交付"
        error_code = ErrorCode.EPUB_VALIDATION_FAILED.value
    translation_stats = attach_translation_qa_report(
        translation_stats,
        output_path=output_path,
        error_code=error_code,
    )

    total_ms = (time.monotonic() - started_all) * 1000
    timings.append(("Total", total_ms))
    metrics = _metrics_summary(timings)
    _log_stage("done", total_ms, error_code=error_code, validation_passed=validation_passed)

    return ConversionResult(
        quality_stats=pre_result.quality_stats,
        translation_stats=translation_stats,
        lexicon_stats=pre_result.lexicon_stats,
        metrics_summary=metrics,
        message=message,
        error_code=error_code,
        validation_passed=validation_passed,
    )
