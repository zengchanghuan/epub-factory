"""
章节级翻译服务：对单个章节内的所有 chunk 做并发翻译，并持久化到 job_chunks。
"""

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List

from app.storage import job_store
from app.domain.manifest_service import build_manifest
from app.domain.chapter_reduce_service import apply_chunk_results
from app.engine.unpacker import EpubUnpacker
from app.models import ChapterKind, ChunkStatus, JobChunk
from app.engine.cleaners.semantics_translator import SemanticsTranslator, SingleChunkResult


@dataclass
class ChunkResult:
    chunk_id: str
    sequence: int
    locator: str
    original_html: str
    translated_html: str
    cached: bool
    error: str | None = None
    model: str | None = None
    base_url: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


@dataclass
class ChapterTranslationResult:
    job_id: str
    chapter_id: str
    file_path: str
    chapter_kind: str
    chunks: List[ChunkResult] = field(default_factory=list)
    skipped: bool = False
    error: str | None = None
    reduced_html: bytes | None = None  # 回写后的整章 HTML，供全书 Reduce 打包使用


async def _translate_chapter_async(job_id: str, chapter_id: str) -> ChapterTranslationResult:
    job = job_store.get(job_id)
    if not job:
        return ChapterTranslationResult(
            job_id=job_id,
            chapter_id=chapter_id,
            file_path="",
            chapter_kind="",
            error="job not found",
        )
    if not job.enable_translation:
        return ChapterTranslationResult(
            job_id=job_id,
            chapter_id=chapter_id,
            file_path="",
            chapter_kind="",
            skipped=True,
        )
    manifest = build_manifest(job.input_path, job_id)
    if manifest.get("error"):
        return ChapterTranslationResult(
            job_id=job_id,
            chapter_id=chapter_id,
            file_path="",
            chapter_kind="",
            error=manifest["error"],
        )
    chapter = next((c for c in manifest["chapters"] if c["chapter_id"] == chapter_id), None)
    if not chapter:
        return ChapterTranslationResult(
            job_id=job_id,
            chapter_id=chapter_id,
            file_path="",
            chapter_kind="",
            error="chapter not in manifest",
        )
    if chapter["chapter_kind"] != ChapterKind.body.value:
        return ChapterTranslationResult(
            job_id=job_id,
            chapter_id=chapter_id,
            file_path=chapter["file_path"],
            chapter_kind=chapter["chapter_kind"],
            skipped=True,
        )
    chunks_spec = chapter.get("chunks") or []
    if not chunks_spec:
        return ChapterTranslationResult(
            job_id=job_id,
            chapter_id=chapter_id,
            file_path=chapter["file_path"],
            chapter_kind=chapter["chapter_kind"],
            skipped=True,
        )
    translator = SemanticsTranslator(
        target_lang=job.target_lang,
        bilingual=job.bilingual,
        glossary=job.glossary or None,
    )
    tasks = [translator.translate_single_chunk_async(c["html"]) for c in chunks_spec]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    chunk_results: List[ChunkResult] = []
    for spec, res in zip(chunks_spec, results):
        if isinstance(res, Exception):
            translator.stats.failed_chunks += 1
            translator.stats.last_error = str(res)
            chunk_results.append(
                ChunkResult(
                    chunk_id=spec["chunk_id"],
                    sequence=spec["sequence"],
                    locator=spec["locator"],
                    original_html=spec["html"],
                    translated_html=spec["html"],
                    cached=False,
                    error=str(res),
                )
            )
            continue
        assert isinstance(res, SingleChunkResult)
        chunk_results.append(
            ChunkResult(
                chunk_id=spec["chunk_id"],
                sequence=spec["sequence"],
                locator=spec["locator"],
                original_html=spec["html"],
                translated_html=res.translated_html,
                cached=res.cached,
                error=res.error,
                model=res.model,
                base_url=res.base_url,
                prompt_tokens=res.prompt_tokens,
                completion_tokens=res.completion_tokens,
                latency_ms=res.latency_ms,
            )
        )
    # 持久化到 job_chunks（若 store 支持）
    upsert = getattr(job_store, "upsert_chunk", None)
    if upsert and chunk_results:
        now = datetime.now(timezone.utc)
        for cr in chunk_results:
            status = ChunkStatus.failed if cr.error else (ChunkStatus.cached if cr.cached else ChunkStatus.translated)
            source_hash = hashlib.sha256(cr.original_html.encode("utf-8", errors="replace")).hexdigest()
            job_chunk = JobChunk(
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
            )
            upsert(job_chunk)
    # 回写本节译文到整章 HTML，供全书 Reduce 使用
    try:
        unpacker = EpubUnpacker(job.input_path)
        book = unpacker.load_book()
        if book:
            orig_content = None
            for item in book.get_items():
                if getattr(item, "get_name", None) and item.get_name() == chapter["file_path"]:
                    orig_content = item.get_content()
                    break
            if orig_content is not None and chunk_results:
                if isinstance(orig_content, str):
                    orig_content = orig_content.encode("utf-8", errors="replace")
                reduced = apply_chunk_results(orig_content, chunk_results, job.bilingual)
                return ChapterTranslationResult(
                    job_id=job_id,
                    chapter_id=chapter_id,
                    file_path=chapter["file_path"],
                    chapter_kind=chapter["chapter_kind"],
                    chunks=chunk_results,
                    reduced_html=reduced,
                )
    except Exception:
        pass
    return ChapterTranslationResult(
        job_id=job_id,
        chapter_id=chapter_id,
        file_path=chapter["file_path"],
        chapter_kind=chapter["chapter_kind"],
        chunks=chunk_results,
    )


def translate_chapter(job_id: str, chapter_id: str) -> ChapterTranslationResult:
    """
    同步入口：翻译指定章节内所有 chunk（章节内并发）。
    供 Celery 任务或单测调用。
    """
    return asyncio.run(_translate_chapter_async(job_id, chapter_id))
