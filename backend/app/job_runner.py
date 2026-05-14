"""
整本转换执行器：根据 job_id 从 store 加载任务并执行 convert_file_to_horizontal。

供 FastAPI 进程内（BackgroundTasks）与 Celery Worker 共用；
Worker 使用时需配置持久化 store（DATABASE_URL），否则无法加载 job。
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .converter import converter
from .domain.notification_service import notify_job_completed
from .domain.status_resolver import resolve_after_conversion
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


def run_job(job_id: str) -> None:
    """从 store 加载 job 并执行整本转换，更新状态与输出路径。"""
    job = job_store.get(job_id)
    if not job:
        logger.warning("run_job: job not found", extra={"job_id": job_id})
        return

    logger.info("job started", extra={"trace_id": job.trace_id, "job_id": job.id})
    job_store.update_status(job.id, JobStatus.running, "开始转换")
    try:
        source_name = Path(job.source_filename).stem
        suffix = _build_output_suffix(job)
        output_path = OUTPUT_DIR / f"{source_name}_{suffix}.epub"

        def on_progress(msg: str) -> None:
            job_store.update_status(job.id, JobStatus.running, msg)

        def on_stage(stage_name: str, message: str, elapsed_ms: Optional[int] = None) -> None:
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
                metadata={"message": message},
            )
            job_store.add_stage(stage)

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
            except subprocess.CalledProcessError as e:
                err_msg = e.stderr.decode("utf-8", errors="ignore")
                raise RuntimeError(f"格式转换失败，可能受 DRM 保护或格式损坏。详情: {err_msg[:200]}")
            except FileNotFoundError:
                raise RuntimeError("服务器未安装 ebook-convert (Calibre)，无法转换此格式。")

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
            traditional_variant=getattr(job, "traditional_variant", "auto") or "auto",
            progress_callback=on_progress,
            stage_callback=on_stage,
        )
        status, message, error_code = resolve_after_conversion(result)
        if status == JobStatus.failed:
            job_store.update_status(job.id, status, message, error_code=error_code)
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
        job_store.update_status(
            job.id,
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
    except Exception as exc:
        message = str(exc)
        error_code = ErrorCode.CONVERT_FAILED
        if "AI 翻译失败" in message or "翻译流程未完成" in message:
            error_code = ErrorCode.TRANSLATION_FAILED
        job_store.update_status(job.id, JobStatus.failed, message, error_code=error_code)
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
