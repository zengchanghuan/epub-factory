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
from .models import JobStage, JobStatus, OutputMode, StageStatus
from .storage import job_store

logger = logging.getLogger("epub_factory")

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


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
        suffix = "横排繁体" if job.output_mode == OutputMode.traditional else "横排简体"
        if job.enable_translation:
            suffix += f"-翻译_{job.target_lang}"
            if job.bilingual:
                suffix += "-双语"
        output_path = OUTPUT_DIR / f"{source_name}-{suffix}.epub"

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

        result = converter.convert_file_to_horizontal(
            Path(job.input_path),
            output_path,
            job.output_mode,
            enable_translation=job.enable_translation,
            target_lang=job.target_lang,
            device=job.device.value,
            bilingual=job.bilingual,
            glossary=job.glossary or None,
            progress_callback=on_progress,
            stage_callback=on_stage,
        )
        status, message, error_code = resolve_after_conversion(result)
        if status == JobStatus.failed:
            job_store.update_status(job.id, status, message, error_code=error_code)
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
        error_code = "CONVERT_FAILED"
        if "AI 翻译失败" in message or "翻译流程未完成" in message:
            error_code = "TRANSLATION_FAILED"
        job_store.update_status(job.id, JobStatus.failed, message, error_code=error_code)
        notify_job_completed(
            job.id, JobStatus.failed, message,
            error_code=error_code,
            source_filename=job.source_filename,
        )
        logger.exception(
            "job failed",
            extra={"trace_id": job.trace_id, "job_id": job.id},
        )
