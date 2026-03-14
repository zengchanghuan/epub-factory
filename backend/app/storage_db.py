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
    Column, DateTime, Enum, String, Boolean, Text, create_engine, event, inspect, text
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .models import (
    ChapterKind,
    ChapterStatus,
    ChunkStatus,
    DeviceProfile,
    Job,
    JobChapter,
    JobChunk,
    JobNotification,
    JobStage,
    JobStatus,
    NotificationStatus,
    OutputMode,
    QualityStats,
    StageStatus,
)


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
    quality_stats_json = Column(Text, nullable=True)
    translation_stats_json = Column(Text, nullable=True)
    metrics_summary = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class ChapterRecord(Base):
    __tablename__ = "job_chapters"

    id = Column(String(128), primary_key=True)
    job_id = Column(String(32), nullable=False, index=True)
    chapter_id = Column(String(64), nullable=False)
    file_path = Column(Text, nullable=False)
    chapter_kind = Column(String(32), nullable=False, default="body")
    status = Column(String(32), nullable=False, default="pending")
    chunk_total = Column(String(16), nullable=False, default="0")
    chunk_success = Column(String(16), nullable=False, default="0")
    chunk_failed = Column(String(16), nullable=False, default="0")
    chunk_cached = Column(String(16), nullable=False, default="0")
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)


class ChunkRecord(Base):
    __tablename__ = "job_chunks"

    id = Column(String(160), primary_key=True)
    job_id = Column(String(32), nullable=False, index=True)
    chapter_id = Column(String(64), nullable=False)
    chunk_id = Column(String(128), nullable=False)
    sequence = Column(String(16), nullable=False, default="0")
    locator = Column(Text, nullable=False)
    source_hash = Column(String(128), nullable=False)
    status = Column(String(32), nullable=False, default="pending")
    cached = Column(Boolean, nullable=False, default=False)
    model = Column(String(64), nullable=True)
    base_url = Column(Text, nullable=True)
    retry_count = Column(String(16), nullable=False, default="0")
    prompt_tokens = Column(String(16), nullable=False, default="0")
    completion_tokens = Column(String(16), nullable=False, default="0")
    latency_ms = Column(String(16), nullable=False, default="0")
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class StageRecord(Base):
    __tablename__ = "job_stages"

    id = Column(String(96), primary_key=True)
    job_id = Column(String(32), nullable=False, index=True)
    stage_name = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="pending")
    started_at = Column(DateTime(timezone=True), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    elapsed_ms = Column(String(16), nullable=True)
    metadata_json = Column(Text, nullable=True)


class NotificationRecord(Base):
    __tablename__ = "notifications"

    id = Column(String(96), primary_key=True)
    job_id = Column(String(32), nullable=False, index=True)
    user_id = Column(String(64), nullable=True)
    channel = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False, default="pending")
    payload_json = Column(Text, nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)


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
    _ensure_compatible_schema(engine)
    return engine


def _ensure_compatible_schema(engine) -> None:
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("epub_jobs")}
    migrations = []
    if "translation_stats_json" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN translation_stats_json TEXT")
    if "metrics_summary" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN metrics_summary TEXT")
    if not migrations:
        return
    with engine.begin() as conn:
        for sql in migrations:
            conn.execute(text(sql))


# ─── 类型转换工具 ──────────────────────────────────────────────────────────────

def _record_to_job(r: JobRecord) -> Job:
    import json
    stats = QualityStats()
    translation_stats = {}
    if r.quality_stats_json:
        try:
            d = json.loads(r.quality_stats_json)
            stats = QualityStats(**d)
        except Exception:
            pass
    if r.translation_stats_json:
        try:
            translation_stats = json.loads(r.translation_stats_json)
        except Exception:
            translation_stats = {}

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
        quality_stats=stats,
        translation_stats=translation_stats,
        metrics_summary=r.metrics_summary or "",
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def _record_to_chapter(r: ChapterRecord) -> JobChapter:
    return JobChapter(
        job_id=r.job_id,
        chapter_id=r.chapter_id,
        file_path=r.file_path,
        chapter_kind=ChapterKind(r.chapter_kind),
        status=ChapterStatus(r.status),
        chunk_total=int(r.chunk_total),
        chunk_success=int(r.chunk_success),
        chunk_failed=int(r.chunk_failed),
        chunk_cached=int(r.chunk_cached),
        started_at=r.started_at,
        finished_at=r.finished_at,
        error_message=r.error_message,
    )


def _chapter_to_record(chapter: JobChapter) -> ChapterRecord:
    return ChapterRecord(
        id=f"{chapter.job_id}:{chapter.chapter_id}",
        job_id=chapter.job_id,
        chapter_id=chapter.chapter_id,
        file_path=chapter.file_path,
        chapter_kind=chapter.chapter_kind.value,
        status=chapter.status.value,
        chunk_total=str(chapter.chunk_total),
        chunk_success=str(chapter.chunk_success),
        chunk_failed=str(chapter.chunk_failed),
        chunk_cached=str(chapter.chunk_cached),
        started_at=chapter.started_at,
        finished_at=chapter.finished_at,
        error_message=chapter.error_message,
    )


def _record_to_chunk(r: ChunkRecord) -> JobChunk:
    return JobChunk(
        job_id=r.job_id,
        chapter_id=r.chapter_id,
        chunk_id=r.chunk_id,
        sequence=int(r.sequence),
        locator=r.locator,
        source_hash=r.source_hash,
        status=ChunkStatus(r.status),
        cached=r.cached,
        model=r.model,
        base_url=r.base_url,
        retry_count=int(r.retry_count),
        prompt_tokens=int(r.prompt_tokens),
        completion_tokens=int(r.completion_tokens),
        latency_ms=int(r.latency_ms),
        error_message=r.error_message,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def _chunk_to_record(chunk: JobChunk) -> ChunkRecord:
    return ChunkRecord(
        id=f"{chunk.job_id}:{chunk.chunk_id}",
        job_id=chunk.job_id,
        chapter_id=chunk.chapter_id,
        chunk_id=chunk.chunk_id,
        sequence=str(chunk.sequence),
        locator=chunk.locator,
        source_hash=chunk.source_hash,
        status=chunk.status.value,
        cached=chunk.cached,
        model=chunk.model,
        base_url=chunk.base_url,
        retry_count=str(chunk.retry_count),
        prompt_tokens=str(chunk.prompt_tokens),
        completion_tokens=str(chunk.completion_tokens),
        latency_ms=str(chunk.latency_ms),
        error_message=chunk.error_message,
        created_at=chunk.created_at,
        updated_at=chunk.updated_at,
    )


def _record_to_stage(r: StageRecord) -> JobStage:
    import json
    metadata = {}
    if r.metadata_json:
        try:
            metadata = json.loads(r.metadata_json)
        except Exception:
            metadata = {}
    return JobStage(
        job_id=r.job_id,
        stage_name=r.stage_name,
        status=StageStatus(r.status),
        started_at=r.started_at,
        finished_at=r.finished_at,
        elapsed_ms=int(r.elapsed_ms) if r.elapsed_ms is not None else None,
        metadata=metadata,
    )


def _stage_to_record(stage: JobStage) -> StageRecord:
    import json
    key = f"{stage.job_id}:{stage.stage_name}:{int(stage.started_at.timestamp() * 1000)}"
    return StageRecord(
        id=key,
        job_id=stage.job_id,
        stage_name=stage.stage_name,
        status=stage.status.value,
        started_at=stage.started_at,
        finished_at=stage.finished_at,
        elapsed_ms=str(stage.elapsed_ms) if stage.elapsed_ms is not None else None,
        metadata_json=json.dumps(stage.metadata or {}),
    )


def _record_to_notification(r: NotificationRecord) -> JobNotification:
    import json
    payload = {}
    if r.payload_json:
        try:
            payload = json.loads(r.payload_json)
        except Exception:
            payload = {}
    return JobNotification(
        job_id=r.job_id,
        channel=r.channel,
        status=NotificationStatus(r.status),
        payload=payload,
        user_id=r.user_id,
        sent_at=r.sent_at,
        error_message=r.error_message,
        created_at=r.created_at,
    )


def _notification_to_record(notification: JobNotification) -> NotificationRecord:
    import json
    key = f"{notification.job_id}:{notification.channel}:{int(notification.created_at.timestamp() * 1000)}"
    return NotificationRecord(
        id=key,
        job_id=notification.job_id,
        user_id=notification.user_id,
        channel=notification.channel,
        status=notification.status.value,
        payload_json=json.dumps(notification.payload or {}),
        sent_at=notification.sent_at,
        error_message=notification.error_message,
        created_at=notification.created_at,
    )

def _job_to_record(job: Job) -> JobRecord:
    import json
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
        quality_stats_json=json.dumps(job.quality_stats.to_dict()) if job.quality_stats else "{}",
        translation_stats_json=json.dumps(job.translation_stats or {}),
        metrics_summary=job.metrics_summary or "",
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

    def list_jobs(self, limit: int = 100) -> list:
        """列出任务，按创建时间倒序。"""
        with self._Session() as session:
            from sqlalchemy import desc
            rows = (
                session.query(JobRecord)
                .order_by(desc(JobRecord.created_at))
                .limit(limit)
                .all()
            )
            return [_record_to_job(r) for r in rows]

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        message: str = "",
        error_code: Optional[str] = None,
        output_path: Optional[str] = None,
        quality_stats=None,
        translation_stats=None,
        metrics_summary: Optional[str] = None,
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
            if quality_stats:
                import json
                r.quality_stats_json = json.dumps(quality_stats.to_dict())
            if translation_stats is not None:
                import json
                r.translation_stats_json = json.dumps(translation_stats)
            if metrics_summary is not None:
                r.metrics_summary = metrics_summary
            r.updated_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(r)
            return _record_to_job(r)

    def upsert_chapter(self, chapter: JobChapter) -> JobChapter:
        with self._Session() as session:
            record_id = f"{chapter.job_id}:{chapter.chapter_id}"
            existing = session.get(ChapterRecord, record_id)
            if existing:
                existing.file_path = chapter.file_path
                existing.chapter_kind = chapter.chapter_kind.value
                existing.status = chapter.status.value
                existing.chunk_total = str(chapter.chunk_total)
                existing.chunk_success = str(chapter.chunk_success)
                existing.chunk_failed = str(chapter.chunk_failed)
                existing.chunk_cached = str(chapter.chunk_cached)
                existing.started_at = chapter.started_at
                existing.finished_at = chapter.finished_at
                existing.error_message = chapter.error_message
                session.commit()
                session.refresh(existing)
                return _record_to_chapter(existing)
            record = _chapter_to_record(chapter)
            session.add(record)
            session.commit()
            session.refresh(record)
            return _record_to_chapter(record)

    def list_chapters(self, job_id: str) -> list[JobChapter]:
        with self._Session() as session:
            rows = session.query(ChapterRecord).filter_by(job_id=job_id).order_by(ChapterRecord.chapter_id).all()
            return [_record_to_chapter(r) for r in rows]

    def upsert_chunk(self, chunk: JobChunk) -> JobChunk:
        with self._Session() as session:
            record_id = f"{chunk.job_id}:{chunk.chunk_id}"
            existing = session.get(ChunkRecord, record_id)
            if existing:
                existing.chapter_id = chunk.chapter_id
                existing.sequence = str(chunk.sequence)
                existing.locator = chunk.locator
                existing.source_hash = chunk.source_hash
                existing.status = chunk.status.value
                existing.cached = chunk.cached
                existing.model = chunk.model
                existing.base_url = chunk.base_url
                existing.retry_count = str(chunk.retry_count)
                existing.prompt_tokens = str(chunk.prompt_tokens)
                existing.completion_tokens = str(chunk.completion_tokens)
                existing.latency_ms = str(chunk.latency_ms)
                existing.error_message = chunk.error_message
                existing.updated_at = chunk.updated_at
                session.commit()
                session.refresh(existing)
                return _record_to_chunk(existing)
            record = _chunk_to_record(chunk)
            session.add(record)
            session.commit()
            session.refresh(record)
            return _record_to_chunk(record)

    def list_chunks(self, job_id: str, chapter_id: Optional[str] = None) -> list[JobChunk]:
        with self._Session() as session:
            query = session.query(ChunkRecord).filter_by(job_id=job_id)
            if chapter_id is not None:
                query = query.filter_by(chapter_id=chapter_id)
            rows = query.order_by(ChunkRecord.chapter_id, ChunkRecord.sequence).all()
            return [_record_to_chunk(r) for r in rows]

    def add_stage(self, stage: JobStage) -> JobStage:
        with self._Session() as session:
            record = _stage_to_record(stage)
            session.add(record)
            session.commit()
            session.refresh(record)
            return _record_to_stage(record)

    def list_stages(self, job_id: str) -> list[JobStage]:
        with self._Session() as session:
            rows = session.query(StageRecord).filter_by(job_id=job_id).order_by(StageRecord.started_at).all()
            return [_record_to_stage(r) for r in rows]

    def add_notification(self, notification: JobNotification) -> JobNotification:
        with self._Session() as session:
            record = _notification_to_record(notification)
            session.add(record)
            session.commit()
            session.refresh(record)
            return _record_to_notification(record)

    def list_notifications(self, job_id: Optional[str] = None) -> list[JobNotification]:
        with self._Session() as session:
            query = session.query(NotificationRecord)
            if job_id is not None:
                query = query.filter_by(job_id=job_id)
            rows = query.order_by(NotificationRecord.created_at).all()
            return [_record_to_notification(r) for r in rows]
