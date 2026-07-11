"""
整本转换执行器：根据 job_id 从 store 加载任务并执行 convert_file_to_horizontal。

供 FastAPI 进程内（BackgroundTasks）与 Celery Worker 共用；
Worker 使用时需配置持久化 store（DATABASE_URL），否则无法加载 job。
"""

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .cancellation import JobCancelled, raise_if_cancelled
from .converter import converter
from .domain.notification_service import notify_job_completed
from .domain.status_resolver import resolve_after_conversion
from .domain.translation_qa_service import attach_translation_qa_report, audit_translated_epub_output
from .domain.translation_attempt import attempt_id_from_stats, initial_translation_stats
from .error_reporter import report_error
from .models import ErrorCode, JobStage, JobStatus, OutputMode, StageStatus
from .storage import job_store

logger = logging.getLogger("epub_factory")

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _build_output_suffix(job) -> str:
    """
    生成输出文件名的可读后缀，命名遵循"轻量、可读、可还原"原则：

    - 仅转换繁体 → "繁体"
    - 仅转换简体 → "简体"
    - 翻译 → 在转换后缀基础上追加 "_翻译_{lang}"，双语模式追加 "_双语"

    例：
      原文件名：百年孤寂.epub
      简体输出：百年孤寂_简体.epub
      简翻英：  百年孤寂_简体_翻译_en.epub
      简翻英双语：百年孤寂_简体_翻译_en_双语.epub
    """
    parts: list[str] = []
    if job.output_mode == OutputMode.traditional:
        parts.append("繁体")
    else:
        parts.append("简体")
    if job.enable_translation:
        parts.append(f"翻译_{job.target_lang}")
        if job.bilingual:
            parts.append("双语")
    return "_".join(parts)


def _safe_output_stem(stem: str) -> str:
    stem = re.sub(r'[\\/:*?"<>|]+', "_", (stem or "").strip())
    stem = re.sub(r"\s+", " ", stem).strip(" ._")
    return stem[:80] or "output"


def _unique_output_path(path: Path) -> Path:
    if not path.exists():
        return path
    base = path.with_suffix("")
    suffix = path.suffix
    for i in range(2, 100):
        candidate = Path(f"{base}_{i}{suffix}")
        if not candidate.exists():
            return candidate
    return Path(f"{base}_{datetime.now().strftime('%Y%m%d%H%M%S')}{suffix}")


def _rename_output_with_translated_title(job, result, output_path: Path, suffix: str) -> Path:
    if not getattr(job, "enable_translation", False):
        return output_path
    stats = getattr(result, "translation_stats", None) or {}
    translated_title = (stats.get("book_title_translated") or "").strip()
    original_title = (stats.get("book_title_original") or "").strip()
    if not translated_title or translated_title == original_title:
        return output_path
    new_path = _unique_output_path(OUTPUT_DIR / f"{_safe_output_stem(translated_title)}_{suffix}.epub")
    if output_path.exists() and new_path != output_path:
        output_path.replace(new_path)
        return new_path
    return output_path


def _finalize_attempt_output(job, result, working_path: Path, default_path: Path, suffix: str) -> Path:
    """Move an attempt-scoped artifact to its user-facing final filename."""
    translated_path = _rename_output_with_translated_title(job, result, working_path, suffix)
    if translated_path != working_path:
        return translated_path
    final_path = _unique_output_path(default_path)
    if working_path.exists() and working_path != final_path:
        working_path.replace(final_path)
    return final_path


def _apply_final_artifact_audit(job, result, output_path: Path) -> None:
    if not getattr(job, "enable_translation", False):
        return
    audit = audit_translated_epub_output(output_path, target_lang=getattr(job, "target_lang", "zh-CN"))
    stats = dict(getattr(result, "translation_stats", {}) or {})
    stats["artifact_audit"] = audit
    result.translation_stats = stats

    if audit.get("status") not in {"failed", "scan_error"}:
        return

    residual = int(audit.get("residual_blocks") or 0)
    checked = int(audit.get("checked_text_blocks") or 0)
    stats["delivery_gate_failed"] = True
    stats["deliverable"] = False
    result.translation_stats = stats
    result.error_code = ErrorCode.PARTIAL_TRANSLATION.value
    result.validation_passed = False
    if audit.get("status") == "scan_error":
        result.message = (
            f"翻译交付质检执行失败：{audit.get('reason') or '无法读取成品 EPUB'}。"
            "已停止交付，请重新翻译或联系管理员。"
        )
    else:
        result.message = (
            f"翻译交付质检未通过：成品 EPUB 仍有 {residual}/{checked} "
            "个正文段落疑似未翻译。已停止交付，请重新翻译。"
        )


def _convert_filename_stem_for_mode(stem: str, output_mode: OutputMode, traditional_variant: str) -> str:
    """
    根据输出模式对文件名主体做繁简转换，保证下载文件名与正文方向一致。

    - simplified: t2s / tw2s / hk2s
    - traditional: s2t / s2tw / s2hk
    """
    if not stem:
        return stem

    variant = (traditional_variant or "auto").lower()
    simplified_profiles = {"auto": "t2s", "tw": "tw2s", "hk": "hk2s"}
    traditional_profiles = {"auto": "s2t", "tw": "s2tw", "hk": "s2hk"}

    if output_mode == OutputMode.simplified:
        profile = simplified_profiles.get(variant, "t2s")
    elif output_mode == OutputMode.traditional:
        profile = traditional_profiles.get(variant, "s2t")
    else:
        return stem

    try:
        from opencc import OpenCC
        return OpenCC(profile).convert(stem)
    except Exception as exc:
        logger.warning("filename stem convert failed, fallback to original: %s", exc)
        return stem


def run_job(job_id: str, expected_attempt_id: str | None = None) -> None:
    """从 store 加载 job 并执行整本转换，更新状态与输出路径。"""
    job = job_store.get(job_id)
    if not job:
        logger.warning("run_job: job not found", extra={"job_id": job_id})
        return
    if job.status == JobStatus.cancelled:
        logger.info("run_job: job already cancelled", extra={"job_id": job_id})
        return
    job = job_store.get(job_id) or job
    now_utc = datetime.now(timezone.utc)
    translation_stats = None
    attempt_id = ""
    if getattr(job, "enable_translation", False):
        existing_stats = dict(getattr(job, "translation_stats", {}) or {})
        existing_attempt_id = attempt_id_from_stats(existing_stats)
        if expected_attempt_id and existing_attempt_id and expected_attempt_id != existing_attempt_id:
            logger.info(
                "run_job: stale translation attempt ignored",
                extra={"job_id": job.id, "expected_attempt_id": expected_attempt_id},
            )
            return
        stats = initial_translation_stats(existing_stats)
        stats.setdefault("attempt_started_at", now_utc.isoformat())
        attempt_id = expected_attempt_id or attempt_id_from_stats(stats)
        stats["attempt_id"] = attempt_id
        translation_stats = stats
        job.translation_stats = stats
        if not existing_attempt_id:
            job_store.update_status(
                job.id,
                job.status,
                job.message,
                translation_stats=stats,
            )

    def update_job_status(status: JobStatus, message: str = "", **kwargs):
        return job_store.update_status(
            job.id,
            status,
            message,
            expected_attempt_id=attempt_id or None,
            **kwargs,
        )

    logger.info("job started", extra={"trace_id": job.trace_id, "job_id": job.id})
    update_job_status(
        JobStatus.running,
        "开始转换",
        translation_stats=translation_stats,
    )
    output_path: Path | None = None
    attempt_scoped_output = False
    try:
        source_name_raw = Path(job.source_filename).stem
        source_name = _convert_filename_stem_for_mode(
            source_name_raw,
            job.output_mode,
            getattr(job, "traditional_variant", "auto") or "auto",
        )
        suffix = _build_output_suffix(job)
        default_output_path = OUTPUT_DIR / f"{source_name}_{suffix}.epub"
        if attempt_id:
            output_path = OUTPUT_DIR / f".{job.id}-{attempt_id}.epub"
            attempt_scoped_output = True
        else:
            output_path = default_output_path

        last_progress_event: str | None = None

        def record_stage(
            stage_name: str,
            message: str,
            elapsed_ms: Optional[int] = None,
            *,
            level: str = "info",
        ) -> None:
            if not getattr(job_store, "add_stage", None):
                return
            now = datetime.now(timezone.utc)
            stage = JobStage(
                job_id=job.id,
                stage_name=stage_name,
                status=StageStatus.completed,
                started_at=now,
                finished_at=now,
                elapsed_ms=elapsed_ms,
                metadata={
                    "message": message,
                    "level": level,
                    **({"attempt_id": attempt_id} if attempt_id else {}),
                },
            )
            job_store.add_stage(stage)

        def is_cancelled() -> bool:
            current = job_store.get(job.id)
            if not current:
                return True
            if current.status == JobStatus.cancelled:
                return True
            if attempt_id and attempt_id_from_stats(current.translation_stats) != attempt_id:
                return True
            return False

        def check_cancelled() -> None:
            raise_if_cancelled(is_cancelled)

        def on_progress(msg: str) -> None:
            nonlocal last_progress_event
            check_cancelled()
            update_job_status(JobStatus.running, msg)
            if msg and msg != last_progress_event:
                last_progress_event = msg
                record_stage("progress", msg)

        def on_stage(stage_name: str, message: str, elapsed_ms: Optional[int] = None) -> None:
            level = "error" if "fail" in stage_name or "failed" in stage_name else "info"
            record_stage(stage_name, message, elapsed_ms, level=level)

        check_cancelled()
        input_path = Path(job.input_path)
        if input_path.suffix.lower() in [".mobi", ".azw3"]:
            on_progress(f"正在将 {input_path.suffix.upper()[1:]} 格式转换为 EPUB...")
            on_stage("format_convert", f"开始转换 {input_path.suffix} 到 epub")
            start_time = datetime.now()
            import subprocess
            temp_epub = input_path.with_suffix(".epub")
            try:
                subprocess.run(["ebook-convert", str(input_path), str(temp_epub)], check=True, capture_output=True)
                input_path = temp_epub
                elapsed = int((datetime.now() - start_time).total_seconds() * 1000)
                on_stage("format_convert", "格式转换完成", elapsed_ms=elapsed)
                check_cancelled()
            except subprocess.CalledProcessError as e:
                err_msg = e.stderr.decode("utf-8", errors="ignore")
                raise RuntimeError(f"格式转换失败，可能受 DRM 保护或格式损坏。详情: {err_msg[:200]}")
            except FileNotFoundError:
                raise RuntimeError("服务器未安装 ebook-convert (Calibre)，无法转换此格式。")

        fast_translation_enabled = os.environ.get("EPUB_FAST_TRANSLATION", "1").lower() not in ("0", "false", "no")
        check_cancelled()
        if job.enable_translation and input_path.suffix.lower() == ".epub" and fast_translation_enabled:
            from .domain.fast_translation_runner import run_fast_translation_job
            result = run_fast_translation_job(
                job=job,
                input_path=input_path,
                output_path=output_path,
                progress_callback=on_progress,
                stage_callback=on_stage,
                cancel_check=is_cancelled,
            )
        else:
            result = converter.convert_file_to_horizontal(
                input_path,
                output_path,
                job.output_mode,
                enable_translation=job.enable_translation,
                target_lang=job.target_lang,
                device=job.device.value,
                bilingual=job.bilingual,
                glossary=job.glossary or None,
                temperature=getattr(job, "temperature", None),
                translation_model=getattr(job, "translation_model", None),
                traditional_variant=getattr(job, "traditional_variant", "auto") or "auto",
                lexicon_domains=getattr(job, "lexicon_domains", None),
                enable_proper_noun=getattr(job, "enable_proper_noun", True),
                progress_callback=on_progress,
                stage_callback=on_stage,
            )
        check_cancelled()
        if job.enable_translation:
            current_attempt_stats = dict(job.translation_stats or {})
            current_attempt_stats.update(dict(getattr(result, "translation_stats", {}) or {}))
            result.translation_stats = current_attempt_stats
        _apply_final_artifact_audit(job, result, output_path)
        if (
            job.enable_translation
            and result.error_code == ErrorCode.PARTIAL_TRANSLATION.value
            and not getattr(result, "validation_passed", True)
        ):
            on_stage("translation_quality_gate_failed", result.message or "翻译交付质检未通过")
        status, message, error_code = resolve_after_conversion(result)
        if status != JobStatus.failed:
            output_path = _finalize_attempt_output(
                job,
                result,
                output_path,
                default_output_path,
                suffix,
            )
            attempt_scoped_output = False
        if job.enable_translation:
            qa_output_path = (
                None
                if error_code == ErrorCode.PARTIAL_TRANSLATION.value and status == JobStatus.failed
                else output_path
            )
            result.translation_stats = attach_translation_qa_report(
                result.translation_stats,
                output_path=qa_output_path,
                error_code=error_code,
            )
        if status == JobStatus.failed:
            on_stage("failed", message or "任务失败")
            update_job_status(
                status,
                message,
                error_code=error_code,
                quality_stats=result.quality_stats,
                translation_stats=result.translation_stats,
                metrics_summary=result.metrics_summary,
            )
            if attempt_scoped_output and output_path:
                output_path.unlink(missing_ok=True)
            report_error(
                error_code=error_code or ErrorCode.CONVERT_FAILED,
                message=message,
                job_id=job.id,
                trace_id=job.trace_id,
                context={"source_filename": job.source_filename},
            )
            notify_job_completed(
                job.id, status, message,
                error_code=error_code,
                source_filename=job.source_filename,
            )
            logger.warning(
                "job validation failed",
                extra={"trace_id": job.trace_id, "job_id": job.id},
            )
            return
        update_job_status(
            status,
            message,
            output_path=str(output_path),
            error_code=error_code,
            quality_stats=result.quality_stats,
            translation_stats=result.translation_stats,
            metrics_summary=result.metrics_summary,
        )
        notify_job_completed(
            job.id, status, message,
            error_code=error_code,
            output_path=str(output_path),
            source_filename=job.source_filename,
        )
        logger.info("job success", extra={"trace_id": job.trace_id, "job_id": job.id})
    except JobCancelled as exc:
        current = job_store.get(job.id)
        if attempt_id and current and attempt_id_from_stats(current.translation_stats) != attempt_id:
            if attempt_scoped_output and output_path:
                output_path.unlink(missing_ok=True)
            logger.info(
                "job attempt superseded",
                extra={"trace_id": job.trace_id, "job_id": job.id, "attempt_id": attempt_id},
            )
            return
        message = str(exc) or "用户已停止翻译"
        if attempt_scoped_output and output_path:
            output_path.unlink(missing_ok=True)
        if getattr(job_store, "add_stage", None):
            now = datetime.now(timezone.utc)
            job_store.add_stage(JobStage(
                job_id=job.id,
                stage_name="cancelled",
                status=StageStatus.completed,
                started_at=now,
                finished_at=now,
                metadata={
                    "message": message,
                    "level": "warning",
                    **({"attempt_id": attempt_id} if attempt_id else {}),
                },
            ))
        update_job_status(JobStatus.cancelled, message)
        notify_job_completed(
            job.id,
            JobStatus.cancelled,
            message,
            source_filename=job.source_filename,
        )
        logger.info("job cancelled", extra={"trace_id": job.trace_id, "job_id": job.id})
    except Exception as exc:
        current = job_store.get(job.id)
        if attempt_id and current and attempt_id_from_stats(current.translation_stats) != attempt_id:
            if attempt_scoped_output and output_path:
                output_path.unlink(missing_ok=True)
            logger.info(
                "stale job failure ignored",
                extra={"trace_id": job.trace_id, "job_id": job.id, "attempt_id": attempt_id},
            )
            return
        message = str(exc)
        if attempt_scoped_output and output_path:
            output_path.unlink(missing_ok=True)
        error_code = ErrorCode.CONVERT_FAILED
        if "AI 翻译失败" in message or "翻译流程未完成" in message:
            error_code = ErrorCode.TRANSLATION_FAILED
        if getattr(job_store, "add_stage", None):
            now = datetime.now(timezone.utc)
            job_store.add_stage(JobStage(
                job_id=job.id,
                stage_name="failed",
                status=StageStatus.completed,
                started_at=now,
                finished_at=now,
                metadata={
                    "message": message or "任务失败",
                    "level": "error",
                    **({"attempt_id": attempt_id} if attempt_id else {}),
                },
            ))
        update_job_status(JobStatus.failed, message, error_code=error_code)
        report_error(
            error_code=error_code,
            message=message,
            job_id=job.id,
            trace_id=job.trace_id,
            context={"source_filename": job.source_filename},
        )
        notify_job_completed(
            job.id, JobStatus.failed, message,
            error_code=error_code,
            source_filename=job.source_filename,
        )
        logger.exception(
            "job failed",
            extra={"trace_id": job.trace_id, "job_id": job.id},
        )
