"""
持久化任务存储（PostgreSQL / SQLite）

通过环境变量 DATABASE_URL 切换后端：
- 未设置 → 使用 SQLite（路径 ./epub_jobs.db），无需额外服务
- postgresql://... → 连接 PostgreSQL

与原有内存 JobStore（storage.py）保持相同的公共接口，可无缝替换。
"""

import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Column, DateTime, Enum, String, Boolean, Text, create_engine, event
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .models import DeviceProfile, Job, JobStatus, OutputMode


# ─── ORM 模型 ────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class JobRecord(Base):
    __tablename__ = "epub_jobs"

    id = Column(String(32), primary_key=True)
    trace_id = Column(String(64), nullable=False)
    source_filename = Column(String(512), nullable=False)
    input_path = Column(Text, nullable=False)
    output_path = Column(Text, nullable=True)
    output_mode = Column(String(16), nullable=False, default="simplified")
    enable_translation = Column(Boolean, nullable=False, default=False)
    target_lang = Column(String(16), nullable=False, default="zh-CN")
    bilingual = Column(Boolean, nullable=False, default=False)
    device = Column(String(16), nullable=False, default="generic")
    status = Column(String(16), nullable=False, default="pending")
    message = Column(Text, nullable=False, default="")
    error_code = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


# ─── 数据库连接工厂 ───────────────────────────────────────────────────────────

def _make_engine():
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        db_file = os.path.join(os.path.dirname(__file__), "..", "epub_jobs.db")
        url = f"sqlite:///{os.path.abspath(db_file)}"

    engine = create_engine(url, pool_pre_ping=True)

    # SQLite 需要开启外键约束
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _set_pragma(conn, _rec):
            conn.execute("PRAGMA journal_mode=WAL")

    Base.metadata.create_all(engine)
    return engine


# ─── 类型转换工具 ──────────────────────────────────────────────────────────────

def _record_to_job(r: JobRecord) -> Job:
    return Job(
        id=r.id,
        trace_id=r.trace_id,
        source_filename=r.source_filename,
        input_path=r.input_path,
        output_path=r.output_path,
        output_mode=OutputMode(r.output_mode),
        enable_translation=r.enable_translation,
        target_lang=r.target_lang,
        bilingual=r.bilingual,
        device=DeviceProfile(r.device),
        status=JobStatus(r.status),
        message=r.message or "",
        error_code=r.error_code,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def _job_to_record(job: Job) -> JobRecord:
    return JobRecord(
        id=job.id,
        trace_id=job.trace_id,
        source_filename=job.source_filename,
        input_path=job.input_path,
        output_path=job.output_path,
        output_mode=job.output_mode.value,
        enable_translation=job.enable_translation,
        target_lang=job.target_lang,
        bilingual=job.bilingual,
        device=job.device.value,
        status=job.status.value,
        message=job.message,
        error_code=job.error_code,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


# ─── 持久化 JobStore ──────────────────────────────────────────────────────────

class PersistentJobStore:
    """与内存 JobStore 接口完全兼容的 SQLAlchemy 实现"""

    def __init__(self, engine=None):
        self._engine = engine or _make_engine()
        self._Session = sessionmaker(bind=self._engine)

    def add(self, job: Job) -> None:
        with self._Session() as session:
            session.add(_job_to_record(job))
            session.commit()

    def get(self, job_id: str) -> Optional[Job]:
        with self._Session() as session:
            r = session.get(JobRecord, job_id)
            return _record_to_job(r) if r else None

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        message: str = "",
        error_code: Optional[str] = None,
        output_path: Optional[str] = None,
    ) -> Optional[Job]:
        with self._Session() as session:
            r = session.get(JobRecord, job_id)
            if not r:
                return None
            r.status = status.value
            r.message = message
            r.error_code = error_code
            if output_path:
                r.output_path = output_path
            r.updated_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(r)
            return _record_to_job(r)
