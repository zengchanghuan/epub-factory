import os
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, Optional

from .models import Job, JobChapter, JobChunk, JobNotification, JobStage, JobStatus, QualityStats, StageStatus
from .domain.translation_attempt import restarted_translation_stats


class JobStore:
    """内存存储（无外部依赖，重启后数据丢失）"""

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._stages: Dict[str, list] = {}
        self._chapters: Dict[str, JobChapter] = {}
        self._chunks: Dict[str, JobChunk] = {}
        self._notifications: list = []  # list[JobNotification]
        self._lock = Lock()

    def add(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, limit: int = 100) -> list:
        """列出任务，按创建时间倒序。"""
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return jobs[:limit]

    def list_jobs_by_creator_ip(self, creator_ip: str, limit: int = 100) -> list:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.creator_ip == creator_ip]
            jobs.sort(key=lambda j: j.created_at, reverse=True)
            return jobs[:limit]

    def list_jobs_by_creator_session(self, creator_session: str, limit: int = 100) -> list:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.creator_session == creator_session]
            jobs.sort(key=lambda j: j.created_at, reverse=True)
            return jobs[:limit]

    def list_jobs_by_batch_id(self, batch_id: str) -> list:
        with self._lock:
            jobs = [j for j in self._jobs.values() if getattr(j, "batch_id", "") == batch_id]
            return sorted(jobs, key=lambda j: (getattr(j, "batch_index", 0), j.created_at))

    def add_stage(self, stage: JobStage) -> JobStage:
        """记录阶段事件（内存：追加到列表；持久化：见 storage_db）。"""
        with self._lock:
            if stage.job_id not in self._stages:
                self._stages[stage.job_id] = []
            self._stages[stage.job_id].append(stage)
        return stage

    def list_stages(self, job_id: str) -> list:
        """返回该任务的阶段事件列表，按 started_at 排序。"""
        with self._lock:
            stages = self._stages.get(job_id, [])
            return sorted(stages, key=lambda s: s.started_at)

    def upsert_chapter(self, chapter: JobChapter, expected_attempt_id: Optional[str] = None) -> JobChapter:
        """写入或更新章节级任务状态（内存：按 job_id:chapter_id 覆盖）。"""
        with self._lock:
            job = self._jobs.get(chapter.job_id)
            if expected_attempt_id and (
                not job or str((job.translation_stats or {}).get("attempt_id") or "") != expected_attempt_id
            ):
                return chapter
            self._chapters[f"{chapter.job_id}:{chapter.chapter_id}"] = chapter
        return chapter

    def list_chapters(self, job_id: str) -> list:
        """返回该任务的章节列表，按 chapter_id 排序。"""
        with self._lock:
            out = [c for c in self._chapters.values() if c.job_id == job_id]
            return sorted(out, key=lambda c: c.chapter_id)

    def upsert_chunk(self, chunk: JobChunk, expected_attempt_id: Optional[str] = None) -> JobChunk:
        """写入或更新 chunk 结果（内存：按 job_id:chunk_id 覆盖）。"""
        with self._lock:
            job = self._jobs.get(chunk.job_id)
            if expected_attempt_id and (
                not job or str((job.translation_stats or {}).get("attempt_id") or "") != expected_attempt_id
            ):
                return chunk
            self._chunks[f"{chunk.job_id}:{chunk.chunk_id}"] = chunk
        return chunk

    def clear_translation_progress(self, job_id: str) -> None:
        with self._lock:
            self._chunks = {
                k: c for k, c in self._chunks.items()
                if c.job_id != job_id
            }
            prefix = f"{job_id}:"
            self._chapters = {
                k: c for k, c in self._chapters.items()
                if not k.startswith(prefix)
            }

    def restart_translation_attempt(
        self,
        job_id: str,
        *,
        attempt_id: str,
        action_label: str,
        max_free_retries: int,
        started_at: datetime,
    ) -> tuple[Optional[Job], str]:
        """Atomically validate, reset, and claim a new translation attempt."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None, "missing"
            if job.status in (JobStatus.pending, JobStatus.running, JobStatus.pending_payment):
                return job, "active"
            previous = dict(job.translation_stats or {})
            free_retry_count = int(previous.get("free_retry_count") or 0)
            if max_free_retries >= 0 and free_retry_count >= max_free_retries:
                return job, "retry_limit"

            stats = restarted_translation_stats(
                previous,
                attempt_id=attempt_id,
                started_at=started_at,
                model=getattr(job, "translation_model", "") or "",
                max_free_retries=max_free_retries,
                action_label=action_label,
            )
            self._chunks = {k: c for k, c in self._chunks.items() if c.job_id != job_id}
            prefix = f"{job_id}:"
            self._chapters = {k: c for k, c in self._chapters.items() if not k.startswith(prefix)}
            job.status = JobStatus.pending
            job.message = f"{action_label}已排队（第 {stats['translation_attempt']} 次尝试）"
            job.error_code = None
            job.output_path = None
            job.quality_stats = QualityStats()
            job.translation_stats = stats
            job.metrics_summary = ""
            job.updated_at = started_at
            return job, "ok"

    def list_chunks(self, job_id: str, chapter_id: Optional[str] = None) -> list:
        """返回该任务（可选某章）的 chunk 列表，按 chapter_id、sequence 排序。"""
        with self._lock:
            out = [c for k, c in self._chunks.items() if c.job_id == job_id and (chapter_id is None or c.chapter_id == chapter_id)]
            return sorted(out, key=lambda c: (c.chapter_id, c.sequence))

    def add_notification(self, notification: JobNotification) -> JobNotification:
        """写入站内/邮件通知记录。"""
        with self._lock:
            self._notifications.append(notification)
        return notification

    def list_notifications(self, job_id: Optional[str] = None) -> list:
        """返回通知列表，可选按 job_id 过滤，按创建时间升序。"""
        with self._lock:
            out = [n for n in self._notifications if job_id is None or n.job_id == job_id]
            return sorted(out, key=lambda n: n.created_at)

    def try_mark_paid(self, job_id: str, message: str = "支付成功，排队中...") -> bool:
        """
        将任务从 pending_payment 原子切换到 pending：
        - 返回 True  表示本次调用赢得了竞态（应由调用方负责入队）
        - 返回 False 表示任务不存在 / 已被其他 webhook 处理 / 状态不符
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != JobStatus.pending_payment:
                return False
            job.status = JobStatus.pending
            job.message = message
            job.updated_at = datetime.now(timezone.utc)
            return True

    def try_mark_batch_paid(self, batch_id: str, message: str = "批次支付成功，排队中...") -> bool:
        """原子解锁整批任务；仅首个调用方获得入队权。"""
        with self._lock:
            jobs = sorted(
                [j for j in self._jobs.values() if getattr(j, "batch_id", "") == batch_id],
                key=lambda j: getattr(j, "batch_index", 0),
            )
            if not jobs or jobs[0].status != JobStatus.pending_payment:
                return False
            now = datetime.now(timezone.utc)
            for job in jobs:
                if job.status == JobStatus.pending_payment:
                    job.status = JobStatus.pending
                    job.message = message
                    job.updated_at = now
            return True

    def list_stale_pending_payment(self, min_age_minutes: int = 30) -> list:
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=min_age_minutes)
        with self._lock:
            return [
                j for j in self._jobs.values()
                if j.status == JobStatus.pending_payment and j.created_at < cutoff
            ]

    def mark_payment_timeout(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != JobStatus.pending_payment:
                return False
            job.status = JobStatus.cancelled
            job.message = "支付超时，订单已关闭"
            job.updated_at = datetime.now(timezone.utc)
            return True

    def mark_batch_payment_timeout(self, batch_id: str) -> int:
        with self._lock:
            now = datetime.now(timezone.utc)
            changed = 0
            for job in self._jobs.values():
                if getattr(job, "batch_id", "") == batch_id and job.status == JobStatus.pending_payment:
                    job.status = JobStatus.cancelled
                    job.message = "支付超时，批次订单已关闭"
                    job.updated_at = now
                    changed += 1
            return changed

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        message: str = "",
        error_code: Optional[str] = None,
        output_path: Optional[str] = None,
        quality_stats: Optional[QualityStats] = None,
        translation_stats: Optional[Dict[str, Any]] = None,
        metrics_summary: Optional[str] = None,
        allow_cancelled_transition: bool = False,
        expected_attempt_id: Optional[str] = None,
    ) -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if expected_attempt_id:
                current_attempt_id = str((job.translation_stats or {}).get("attempt_id") or "")
                if current_attempt_id != expected_attempt_id:
                    return job
            if job.status == JobStatus.cancelled and status != JobStatus.cancelled and not allow_cancelled_transition:
                return job
            job.status = status
            job.message = message
            job.error_code = error_code
            if output_path:
                job.output_path = output_path
            if quality_stats:
                job.quality_stats = quality_stats
            if translation_stats is not None:
                merged_stats = dict(getattr(job, "translation_stats", {}) or {})
                if isinstance(translation_stats, dict):
                    merged_stats.update(translation_stats)
                else:
                    merged_stats = translation_stats
                job.translation_stats = merged_stats
            if metrics_summary is not None:
                job.metrics_summary = metrics_summary
            job.updated_at = datetime.now(timezone.utc)
            return job


def _build_job_store():
    """
    根据环境变量自动选择存储后端：
    - DATABASE_URL 已设置 → PersistentJobStore（SQLite 或 PostgreSQL）
    - 未设置 → 内存 JobStore
    """
    if os.environ.get("DATABASE_URL") or os.environ.get("EPUB_PERSISTENT_STORE"):
        from .storage_db import PersistentJobStore
        store = PersistentJobStore()
        print("[JobStore] Using persistent backend (SQLAlchemy)")
        return store
    return JobStore()


job_store = _build_job_store()
