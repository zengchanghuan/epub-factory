import os
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, Optional

from .models import Job, JobStatus


class JobStore:
    """内存存储（无外部依赖，重启后数据丢失）"""

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = Lock()

    def add(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        message: str = "",
        error_code: Optional[str] = None,
        output_path: Optional[str] = None,
    ) -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            job.status = status
            job.message = message
            job.error_code = error_code
            if output_path:
                job.output_path = output_path
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

