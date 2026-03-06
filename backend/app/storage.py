from datetime import datetime, timezone
from threading import Lock
from typing import Dict, Optional

from .models import Job, JobStatus


class JobStore:
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


job_store = JobStore()

