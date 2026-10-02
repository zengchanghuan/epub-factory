"""
持久化任务存储（PostgreSQL / SQLite）

通过环境变量 DATABASE_URL 切换后端：
- 未设置 → 使用 SQLite（路径 ./epub_jobs.db），无需额外服务
- postgresql://... → 连接 PostgreSQL

与原有内存 JobStore（storage.py）保持相同的公共接口，可无缝替换。
"""

import os
from contextlib import contextmanager
from pathlib import Path
import stat
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import unquote, urlsplit

from sqlalchemy import (
    Column, DateTime, Enum, String, Boolean, Text, Float, Integer, Index,
    and_, or_, create_engine, event, inspect, text
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
    User,
    validate_notification_page,
)
from .domain.translation_attempt import restarted_translation_stats
from .domain.payment_entitlement import restart_entitlement_reason
from .domain.dispatch_intent import DISPATCH_FIELDS, build_dispatch_intent, completion_values, timestamp
from .domain.payment_lifecycle_state import is_payment_expired, settlement_values, closed_values
from .domain.execution_state import (EXECUTION_FIELDS, bounded_integer, execution_identity,
                                     running_record, legacy_record, recovery_outbox, recovery_values, migrates_legacy_identity, unstarted_due)
from .domain.job_write_fence import check_job_write, current_job_write_fence, reject_write, translation_stats_for_status, utc_datetime


# ─── ORM 模型 ────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class OrderEventRecord(Base):
    __tablename__ = "order_funnel_events"
    order_no = Column(String(100), primary_key=True)
    event = Column(String(32), primary_key=True)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    source = Column(String(40), nullable=False)


class DispatchRecord(Base):
    """At-least-once queue publication; no payment or book content is stored."""
    __tablename__ = "job_dispatch_outbox"

    dispatch_id = Column(String(64), primary_key=True)
    job_id = Column(String(32), nullable=False, index=True)
    attempt_id = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, index=True)
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)
    next_attempt_at = Column(Float, nullable=False, index=True)
    lease_token = Column(String(32), nullable=False, default="")
    lease_expires_at = Column(Float, nullable=False, default=0, index=True)
    last_error = Column(Text, nullable=False, default="")


class ExecutionRecord(Base):
    __tablename__ = "job_executions"

    execution_id = Column(String(64), primary_key=True)
    job_id = Column(String(32), nullable=False, index=True)
    attempt_id = Column(String(64), nullable=False)
    owner = Column(String(128), nullable=False)
    state = Column(String(16), nullable=False, index=True)
    heartbeat_at = Column(Float, nullable=False, index=True)
    recoveries = Column(Integer, nullable=False, default=0)


class UserRecord(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True)  # UUID
    phone = Column(String(20), unique=True, nullable=True, index=True)
    google_id = Column(String(255), unique=True, nullable=True, index=True)
    wechat_openid = Column(String(128), unique=True, nullable=True, index=True)
    wechat_unionid = Column(String(128), nullable=True, index=True)
    display_name = Column(String(128), nullable=True)
    avatar_url = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    last_login_at = Column(DateTime(timezone=True), nullable=True)


class JobRecord(Base):
    __tablename__ = "epub_jobs"

    id = Column(String(32), primary_key=True)
    trace_id = Column(String(64), nullable=False)
    source_filename = Column(String(512), nullable=False)
    input_path = Column(Text, nullable=False)
    access_token = Column(String(64), nullable=True)
    token_expires_at = Column(DateTime(timezone=True), nullable=True)
    creator_ip = Column(String(64), nullable=True)
    creator_session = Column(String(128), nullable=True)
    is_test_order = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    expected_amount = Column(String(16), nullable=True)
    payment_entitlement_json = Column(Text, nullable=True)
    payment_resolution_json = Column(Text, nullable=True)
    batch_id = Column(String(32), nullable=True, index=True)
    batch_index = Column(String(16), nullable=True, default="0")
    batch_size = Column(String(16), nullable=True, default="0")
    output_path = Column(Text, nullable=True)
    output_mode = Column(String(16), nullable=False, default="simplified")
    enable_translation = Column(Boolean, nullable=False, default=False)
    target_lang = Column(String(16), nullable=False, default="zh-CN")
    bilingual = Column(Boolean, nullable=False, default=False)
    glossary_json = Column(Text, nullable=True)
    device = Column(String(16), nullable=False, default="generic")
    status = Column(String(16), nullable=False, default="pending")
    message = Column(Text, nullable=False, default="")
    error_code = Column(String(64), nullable=True)
    quality_stats_json = Column(Text, nullable=True)
    translation_stats_json = Column(Text, nullable=True)
    metrics_summary = Column(Text, nullable=True)
    temperature = Column(Float, nullable=True)
    translation_model = Column(String(64), nullable=True)
    translation_quality = Column(String(16), nullable=True)
    cache_policy = Column(String(16), nullable=True)
    translation_strategy = Column(String(32), nullable=True)
    traditional_variant = Column(String(16), nullable=True)  # auto | tw | hk
    lexicon_domains = Column(Text, nullable=True)             # JSON 数组，如 ["general","tech"]
    enable_proper_noun = Column(Boolean, nullable=False, default=True)
    lexicon_versions = Column(Text, nullable=True)            # JSON dict，词典版本快照
    enable_precision_polish = Column(Boolean, nullable=False, default=False)
    precision_polish_order_no = Column(String(64), nullable=True)
    precision_polish_status = Column(String(32), nullable=True, default="not_used")
    polish_char_count = Column(String(16), nullable=True, default="0")
    user_id = Column(String(36), nullable=True, index=True)   # 关联到 users.id，匿名任务为 NULL
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
    source_text = Column(Text, nullable=True)
    translated_text = Column(Text, nullable=True)
    audit_json = Column(Text, nullable=True)
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
    __table_args__ = (
        Index("ix_notifications_job_channel_created_id", "job_id", "channel", "created_at", "id"),
        Index("ix_notifications_channel_created_id", "channel", "created_at", "id"),
    )


Index("ix_epub_jobs_user_id_id", JobRecord.user_id, JobRecord.id)


class EmailSubscriptionRecord(Base):
    """Private subscription and durable delivery state; never exposed as notifications."""
    __tablename__ = "job_email_subscriptions"

    job_id = Column(String(80), primary_key=True)
    revision = Column(String(32), nullable=False)
    data_json = Column(Text, nullable=False)


class PaymentEmailRecord(Base):
    """Private merchant receipt outbox, independent of customer subscriptions."""
    __tablename__ = "payment_email_outbox"

    order_no = Column(String(100), primary_key=True)
    revision = Column(String(32), nullable=False)
    status = Column(String(24), nullable=False, index=True)
    next_attempt_at = Column(Float, nullable=False, default=0, index=True)
    lease_until = Column(Float, nullable=False, default=0)
    created_at = Column(Float, nullable=False)
    data_json = Column(Text, nullable=False)


# ─── 数据库连接工厂 ───────────────────────────────────────────────────────────

def _sqlite_schema_lock_path(engine):
    """Canonical local DB identity; SQLite URI names are not filesystem paths."""
    if engine.dialect.name != "sqlite":
        return None
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    if str(engine.url.query.get("uri", "")).lower() in {"true", "1"}:
        if str(engine.url.query.get("mode", "")).lower() == "memory":
            return None
        if database.startswith("file:"):
            uri = urlsplit(database)
            if uri.netloc not in {"", "localhost"}:
                raise ValueError("SQLite schema initialization requires a local database")
            database = unquote(uri.path)
            if not database or database == ":memory:":
                return None
    path = Path(database).resolve()
    return path.with_name(path.name + ".schema-init.lock")


@contextmanager
def _sqlite_schema_lock(engine, *, timeout=30.0):
    """Serialize first connections, DDL checks and upgrades across local workers.

    Keep the sidecar inode permanently: unlinking a lock would let a new worker
    acquire a different lock while another initializer still owns the old one.
    PostgreSQL and independent in-memory databases retain their existing path.
    """
    path = _sqlite_schema_lock_path(engine)
    if path is None:
        yield
        return
    import fcntl
    flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("SQLite schema initialization lock must be a regular file")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("SQLite schema initialization lock timed out") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # Also releases the lock after failure or normal completion.


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

    try:
        # Acquire before the first connection: switching an empty SQLite DB to
        # WAL can itself contend before create_all reaches its check/DDL race.
        with _sqlite_schema_lock(engine):
            Base.metadata.create_all(engine)
            _ensure_compatible_schema_unlocked(engine)
    except BaseException:
        try:
            engine.dispose()
        except Exception:
            pass  # Cleanup failure must not hide the original schema failure.
        raise
    return engine


def _ensure_compatible_schema(engine) -> None:
    # Direct compatibility callers must use the same boundary as startup.
    with _sqlite_schema_lock(engine):
        _ensure_compatible_schema_unlocked(engine)


def _ensure_compatible_schema_unlocked(engine) -> None:
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("epub_jobs")}
    migrations = []
    if "quality_stats_json" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN quality_stats_json TEXT")
    if "translation_stats_json" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN translation_stats_json TEXT")
    if "metrics_summary" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN metrics_summary TEXT")
    if "translation_model" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN translation_model VARCHAR(64)")
    if "temperature" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN temperature FLOAT")
    if "translation_quality" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN translation_quality VARCHAR(16)")
    if "cache_policy" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN cache_policy VARCHAR(16)")
    if "translation_strategy" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN translation_strategy VARCHAR(32)")
    if "traditional_variant" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN traditional_variant VARCHAR(16)")
    if "access_token" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN access_token VARCHAR(64)")
    if "creator_ip" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN creator_ip VARCHAR(64)")
    if "creator_session" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN creator_session VARCHAR(128)")
    if "is_test_order" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN is_test_order BOOLEAN NOT NULL DEFAULT FALSE")
    if "expected_amount" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN expected_amount VARCHAR(16)")
    if "payment_entitlement_json" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN payment_entitlement_json TEXT")
    if "payment_resolution_json" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN payment_resolution_json TEXT")
    if "batch_id" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN batch_id VARCHAR(32)")
    if "batch_index" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN batch_index VARCHAR(16) DEFAULT '0'")
    if "batch_size" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN batch_size VARCHAR(16) DEFAULT '0'")
    if "token_expires_at" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN token_expires_at DATETIME")
    if "user_id" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN user_id VARCHAR(36)")
    if "lexicon_domains" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN lexicon_domains TEXT")
    if "enable_proper_noun" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN enable_proper_noun BOOLEAN DEFAULT 1")
    if "lexicon_versions" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN lexicon_versions TEXT")
    if "enable_precision_polish" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN enable_precision_polish BOOLEAN DEFAULT 0")
    if "precision_polish_order_no" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN precision_polish_order_no VARCHAR(64)")
    if "precision_polish_status" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN precision_polish_status VARCHAR(32) DEFAULT 'not_used'")
    if "polish_char_count" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN polish_char_count VARCHAR(16) DEFAULT '0'")
    if "glossary_json" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN glossary_json TEXT")
    chunk_columns = {col["name"] for col in inspector.get_columns("job_chunks")}
    if "source_text" not in chunk_columns:
        migrations.append("ALTER TABLE job_chunks ADD COLUMN source_text TEXT")
    if "translated_text" not in chunk_columns:
        migrations.append("ALTER TABLE job_chunks ADD COLUMN translated_text TEXT")
    if "audit_json" not in chunk_columns:
        migrations.append("ALTER TABLE job_chunks ADD COLUMN audit_json TEXT")
    with engine.begin() as conn:
        for sql in migrations:
            conn.execute(text(sql))
        # create_all skips indexes on existing tables. Additive and safe to run
        # repeatedly after the legacy user_id column has been installed.
        for sql in (
            "CREATE INDEX IF NOT EXISTS ix_notifications_job_channel_created_id ON notifications (job_id, channel, created_at, id)",
            "CREATE INDEX IF NOT EXISTS ix_notifications_channel_created_id ON notifications (channel, created_at, id)",
            "CREATE INDEX IF NOT EXISTS ix_epub_jobs_user_id_id ON epub_jobs (user_id, id)",
        ):
            conn.execute(text(sql))


# ─── 类型转换工具 ──────────────────────────────────────────────────────────────

def _record_to_job(r: JobRecord) -> Job:
    import json
    stats = QualityStats()
    translation_stats = {}
    payment_entitlement = {}
    payment_resolution = {}
    try:
        parsed_resolution = json.loads(getattr(r, "payment_resolution_json", None) or "{}")
        if isinstance(parsed_resolution, dict):
            payment_resolution = parsed_resolution
    except (TypeError, ValueError):
        pass
    try:
        parsed_entitlement = json.loads(getattr(r, "payment_entitlement_json", None) or "{}")
        if isinstance(parsed_entitlement, dict):
            payment_entitlement = parsed_entitlement
    except (TypeError, ValueError):
        pass
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
    glossary = {}
    raw_glossary = getattr(r, "glossary_json", None)
    if raw_glossary:
        try:
            parsed_glossary = json.loads(raw_glossary)
            if isinstance(parsed_glossary, dict):
                glossary = {str(k): str(v) for k, v in parsed_glossary.items()}
        except Exception:
            glossary = {}

    lexicon_domains = ["general", "tech", "movie"]
    raw_domains = getattr(r, "lexicon_domains", None)
    if raw_domains:
        try:
            lexicon_domains = json.loads(raw_domains)
        except Exception:
            pass

    return Job(
        id=r.id,
        trace_id=r.trace_id,
        source_filename=r.source_filename,
        input_path=r.input_path,
        access_token=getattr(r, "access_token", None) or "",
        token_expires_at=getattr(r, "token_expires_at", None),
        creator_ip=getattr(r, "creator_ip", None) or "",
        creator_session=getattr(r, "creator_session", None) or "",
        is_test_order=bool(getattr(r, "is_test_order", False)),
        expected_amount=getattr(r, "expected_amount", None) or "",
        payment_entitlement=payment_entitlement,
        payment_resolution=payment_resolution,
        batch_id=getattr(r, "batch_id", None) or "",
        batch_index=int(getattr(r, "batch_index", None) or 0),
        batch_size=int(getattr(r, "batch_size", None) or 0),
        output_path=r.output_path,
        output_mode=OutputMode(r.output_mode),
        enable_translation=r.enable_translation,
        target_lang=r.target_lang,
        bilingual=r.bilingual,
        glossary=glossary,
        device=DeviceProfile(r.device),
        temperature=getattr(r, "temperature", None),
        translation_model=getattr(r, "translation_model", None) or "deepseek-flash",
        translation_quality=getattr(r, "translation_quality", None) or "standard",
        cache_policy=getattr(r, "cache_policy", None) or "reuse",
        translation_strategy=getattr(r, "translation_strategy", None) or "auto",
        traditional_variant=getattr(r, "traditional_variant", None) or "auto",
        lexicon_domains=lexicon_domains,
        enable_proper_noun=bool(getattr(r, "enable_proper_noun", True)),
        enable_precision_polish=bool(getattr(r, "enable_precision_polish", False)),
        precision_polish_order_no=getattr(r, "precision_polish_order_no", None) or "",
        polish_char_count=int(getattr(r, "polish_char_count", None) or 0),
        user_id=getattr(r, "user_id", None),
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
    import json
    audit_json = {}
    raw_audit = getattr(r, "audit_json", None)
    if raw_audit:
        try:
            parsed = json.loads(raw_audit)
            if isinstance(parsed, dict):
                audit_json = parsed
        except Exception:
            audit_json = {}
    return JobChunk(
        job_id=r.job_id,
        chapter_id=r.chapter_id,
        chunk_id=r.chunk_id,
        sequence=int(r.sequence),
        locator=r.locator,
        source_hash=r.source_hash,
        source_text=getattr(r, "source_text", None) or "",
        translated_text=getattr(r, "translated_text", None) or "",
        audit_json=audit_json,
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
    import json
    return ChunkRecord(
        id=f"{chunk.job_id}:{chunk.chunk_id}",
        job_id=chunk.job_id,
        chapter_id=chunk.chapter_id,
        chunk_id=chunk.chunk_id,
        sequence=str(chunk.sequence),
        locator=chunk.locator,
        source_hash=chunk.source_hash,
        source_text=getattr(chunk, "source_text", "") or "",
        translated_text=getattr(chunk, "translated_text", "") or "",
        audit_json=json.dumps(getattr(chunk, "audit_json", {}) or {}, ensure_ascii=False),
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
    import uuid
    # 同一阶段可能在同一毫秒内连续记录“开始/完成”。时间戳不能作为唯一后缀，
    # 否则会触发 job_stages 主键冲突并让正常转换误入 SafeMode。
    key = f"{stage.job_id}:{stage.stage_name[:60]}:{uuid.uuid4().hex[:12]}"
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
        created_at=utc_datetime(r.created_at),
        id=r.id,
    )


def _notification_to_record(notification: JobNotification) -> NotificationRecord:
    import json
    return NotificationRecord(
        id=notification.id,
        job_id=notification.job_id,
        user_id=notification.user_id,
        channel=notification.channel,
        status=notification.status.value,
        payload_json=json.dumps(notification.payload or {}),
        sent_at=notification.sent_at,
        error_message=notification.error_message,
        created_at=utc_datetime(notification.created_at),
    )

def _record_to_user(r: UserRecord) -> User:
    return User(
        id=r.id,
        phone=r.phone,
        google_id=r.google_id,
        wechat_openid=r.wechat_openid,
        wechat_unionid=r.wechat_unionid,
        display_name=r.display_name,
        avatar_url=r.avatar_url,
        is_active=r.is_active,
        created_at=r.created_at,
        last_login_at=r.last_login_at,
    )


def _user_to_record(user: User) -> UserRecord:
    return UserRecord(
        id=user.id,
        phone=user.phone,
        google_id=user.google_id,
        wechat_openid=user.wechat_openid,
        wechat_unionid=user.wechat_unionid,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


def _job_to_record(job: Job) -> JobRecord:
    import json
    return JobRecord(
        id=job.id,
        trace_id=job.trace_id,
        source_filename=job.source_filename,
        input_path=job.input_path,
        access_token=getattr(job, "access_token", "") or "",
        token_expires_at=getattr(job, "token_expires_at", None),
        creator_ip=getattr(job, "creator_ip", "") or "",
        creator_session=getattr(job, "creator_session", "") or "",
        is_test_order=bool(getattr(job, "is_test_order", False)),
        expected_amount=getattr(job, "expected_amount", "") or "",
        payment_entitlement_json=json.dumps(getattr(job, "payment_entitlement", {}) or {}),
        payment_resolution_json=json.dumps(getattr(job, "payment_resolution", {}) or {}),
        batch_id=getattr(job, "batch_id", "") or None,
        batch_index=str(getattr(job, "batch_index", 0) or 0),
        batch_size=str(getattr(job, "batch_size", 0) or 0),
        output_path=job.output_path,
        output_mode=job.output_mode.value,
        enable_translation=job.enable_translation,
        target_lang=job.target_lang,
        bilingual=job.bilingual,
        glossary_json=json.dumps(getattr(job, "glossary", {}) or {}),
        device=job.device.value,
        status=job.status.value,
        message=job.message,
        error_code=job.error_code,
        quality_stats_json=json.dumps(job.quality_stats.to_dict()) if job.quality_stats else "{}",
        translation_stats_json=json.dumps(job.translation_stats or {}),
        metrics_summary=job.metrics_summary or "",
        temperature=getattr(job, "temperature", None),
        translation_model=getattr(job, "translation_model", None) or "deepseek-flash",
        translation_quality=getattr(job, "translation_quality", None) or "standard",
        cache_policy=getattr(job, "cache_policy", None) or "reuse",
        translation_strategy=getattr(job, "translation_strategy", None) or "auto",
        traditional_variant=getattr(job, "traditional_variant", None) or "auto",
        lexicon_domains=json.dumps(getattr(job, "lexicon_domains", ["general", "tech", "movie"])),
        enable_proper_noun=bool(getattr(job, "enable_proper_noun", True)),
        enable_precision_polish=bool(getattr(job, "enable_precision_polish", False)),
        precision_polish_order_no=getattr(job, "precision_polish_order_no", None) or None,
        # Compatibility mirror only; the persisted JSON report is authoritative.
        precision_polish_status=((job.translation_stats or {}).get("precision_polish") or {}).get("status", "not_used"),
        polish_char_count=str(getattr(job, "polish_char_count", 0) or 0),
        user_id=getattr(job, "user_id", None),
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
            record = _job_to_record(job)
            session.add(record)
            if job.status == JobStatus.pending:
                self._ensure_dispatch_in_session(session, record)
            session.commit()
            # Match the memory store's first-attempt identity for callers that
            # retain the just-created Job instead of reading it back.
            if job.status == JobStatus.pending:
                job.translation_stats = _record_to_job(record).translation_stats

    @staticmethod
    def _dispatch_dict(record) -> dict:
        return {key: getattr(record, key) for key in DISPATCH_FIELDS}

    def _ensure_dispatch_in_session(self, session, record, *, now=None) -> dict:
        """Caller holds the job row's write lock or is inserting that row."""
        import json
        stats, intent = build_dispatch_intent(_record_to_job(record), now=now)
        record.translation_stats_json = json.dumps(stats, ensure_ascii=False)
        existing = session.get(DispatchRecord, intent["dispatch_id"])
        if existing is not None:
            return self._dispatch_dict(existing)
        session.add(DispatchRecord(**intent))
        return intent

    def ensure_dispatch(self, job_id: str) -> Optional[dict]:
        """Explicit internal recovery; never scans pending as payment proof."""
        from sqlalchemy import update
        with self._Session() as session:
            # A no-op conditional write serializes initialization on SQLite and
            # PostgreSQL alike, including two callers creating the first ID.
            claimed = session.execute(update(JobRecord).where(
                JobRecord.id == job_id, JobRecord.status == JobStatus.pending.value,
            ).values(status=JobStatus.pending.value))
            if (claimed.rowcount or 0) != 1:
                session.rollback()
                return None
            record = session.get(JobRecord, job_id)
            intent = self._ensure_dispatch_in_session(session, record)
            session.commit()
            return dict(intent)

    def claim_dispatch(self, *, job_id=None, lease_seconds=60, now=None) -> Optional[dict]:
        from sqlalchemy import and_, or_, update
        at = timestamp(now)
        eligible = or_(
            and_(DispatchRecord.status == "pending", DispatchRecord.next_attempt_at <= at),
            and_(DispatchRecord.status == "publishing", DispatchRecord.lease_expires_at <= at),
        )
        # CAS, not SELECT ownership: concurrent relays can read the same row but
        # only one wins its lease. A loser advances to another due record.
        with self._Session() as session:
            query = session.query(DispatchRecord.dispatch_id).filter(eligible)
            if job_id is not None:
                query = query.filter(DispatchRecord.job_id == job_id)
            candidates = query.order_by(DispatchRecord.created_at, DispatchRecord.dispatch_id).limit(100).all()
            for (dispatch_id,) in candidates:
                token = uuid.uuid4().hex
                result = session.execute(update(DispatchRecord).where(
                    DispatchRecord.dispatch_id == dispatch_id, eligible,
                ).values(status="publishing", attempts=DispatchRecord.attempts + 1,
                         updated_at=at, lease_token=token,
                         lease_expires_at=at + max(1.0, float(lease_seconds))))
                if (result.rowcount or 0) == 1:
                    record = session.get(DispatchRecord, dispatch_id)
                    intent = self._dispatch_dict(record)
                    session.commit()
                    return intent
            session.rollback()
        return None

    def finish_dispatch(self, dispatch_id, lease_token, *, outcome="sent", error="", retry_delay_seconds=5, now=None) -> bool:
        from sqlalchemy import update
        values = completion_values(outcome=outcome, error=error, retry_delay_seconds=retry_delay_seconds, now=now)
        if not lease_token:
            return False
        with self._Session() as session:
            result = session.execute(update(DispatchRecord).where(
                DispatchRecord.dispatch_id == dispatch_id,
                DispatchRecord.status == "publishing", DispatchRecord.lease_token == lease_token,
            ).values(**values))
            session.commit()
            return (result.rowcount or 0) == 1

    def list_dispatches(self, job_id=None, limit=100) -> list[dict]:
        with self._Session() as session:
            query = session.query(DispatchRecord)
            if job_id is not None:
                query = query.filter(DispatchRecord.job_id == job_id)
            rows = query.order_by(DispatchRecord.created_at, DispatchRecord.dispatch_id).limit(max(0, int(limit))).all()
            return [self._dispatch_dict(record) for record in rows]

    def get_dispatch(self, dispatch_id: str) -> Optional[dict]:
        with self._Session() as session:
            record = session.get(DispatchRecord, dispatch_id)
            return self._dispatch_dict(record) if record is not None else None

    @staticmethod
    def _execution_dict(record):
        return {field: getattr(record, field) for field in EXECUTION_FIELDS}

    def get_execution(self, job_id, attempt_id) -> Optional[dict]:
        from .domain.dispatch_intent import dispatch_identity
        with self._Session() as session:
            row = session.get(ExecutionRecord, dispatch_identity(job_id, attempt_id))
            return self._execution_dict(row) if row else None

    @staticmethod
    def _lock_execution_job(session, job_id, *, status=None):
        from sqlalchemy import update
        predicate = [JobRecord.id == job_id]
        if status is not None:
            predicate.append(JobRecord.status == status.value)
        locked = session.execute(update(JobRecord).where(*predicate).values(status=JobRecord.status))
        if (locked.rowcount or 0) != 1:
            return None
        return session.get(JobRecord, job_id, populate_existing=True)

    def _check_write_locked(self, session, record, job_id, **expectations):
        from .domain.dispatch_intent import dispatch_identity
        job = _record_to_job(record) if record is not None else None
        execution = (session.get(ExecutionRecord, dispatch_identity(job_id, execution_identity(job)))
                     if job is not None and current_job_write_fence() is not None else None)
        return check_job_write(job, job_id, execution=self._execution_dict(execution) if execution else None,
                               **expectations)

    def begin_execution(self, job_id, attempt_id, owner, *, now=None) -> bool:
        from .domain.dispatch_intent import dispatch_identity
        values = running_record(job_id, attempt_id, owner, now=now)
        with self._Session() as session:
            job = self._lock_execution_job(session, job_id, status=JobStatus.pending)
            if job is None or execution_identity(_record_to_job(job)) != attempt_id:
                session.rollback()
                return False
            record = session.get(ExecutionRecord, values["execution_id"])
            if record is None:
                if migrates_legacy_identity(_record_to_job(job), attempt_id):
                    old_key = dispatch_identity(job_id, "")
                    legacy = session.get(ExecutionRecord, old_key)
                    if legacy is not None and legacy.state == "queued" and not legacy.owner:
                        values["recoveries"] = legacy.recoveries
                        legacy.state, legacy.heartbeat_at = "finished", values["heartbeat_at"]
                        old_outbox = session.get(DispatchRecord, old_key)
                        obsolete = recovery_outbox(job_id, "", exhausted=True, now=values["heartbeat_at"],
                            existing=self._dispatch_dict(old_outbox) if old_outbox else None)
                        obsolete["last_error"] = "legacy execution identity migrated"
                        if old_outbox is None:
                            session.add(DispatchRecord(**obsolete))
                        else:
                            for field, value in obsolete.items():
                                setattr(old_outbox, field, value)
                session.add(ExecutionRecord(**values))
            else:
                values["recoveries"] = record.recoveries
                for field, value in values.items():
                    setattr(record, field, value)
            job.status, job.message = JobStatus.running.value, "开始转换"
            job.updated_at = datetime.fromtimestamp(values["heartbeat_at"], timezone.utc)
            session.commit()
            return True

    def heartbeat_execution(self, job_id, attempt_id, owner, *, now=None) -> bool:
        from .domain.dispatch_intent import dispatch_identity
        with self._Session() as session:
            job = self._lock_execution_job(session, job_id, status=JobStatus.running)
            row = session.get(ExecutionRecord, dispatch_identity(job_id, attempt_id))
            if (job is None or execution_identity(_record_to_job(job)) != attempt_id or row is None
                    or row.state != "running" or not owner or row.owner != owner):
                session.rollback()
                return False
            row.heartbeat_at = timestamp(now)
            session.commit()
            return True

    def finish_execution(self, job_id, attempt_id, owner, *, now=None) -> bool:
        from .domain.dispatch_intent import dispatch_identity
        with self._Session() as session:
            job = self._lock_execution_job(session, job_id)
            row = session.get(ExecutionRecord, dispatch_identity(job_id, attempt_id))
            if (job is None or job.status not in {JobStatus.success.value, JobStatus.failed.value, JobStatus.cancelled.value}
                    or execution_identity(_record_to_job(job)) != attempt_id or row is None
                    or row.state != "running" or not owner or row.owner != owner):
                session.rollback()
                return False
            row.state, row.owner, row.heartbeat_at = "finished", "", timestamp(now)
            session.commit()
            return True

    def list_stale_executions(self, *, stale_before, limit=20) -> list[dict]:
        from .domain.dispatch_intent import dispatch_identity
        bound = bounded_integer(limit, name="limit", minimum=1, maximum=100)
        cutoff = timestamp(stale_before)
        with self._Session() as session:
            jobs = session.query(JobRecord).filter_by(status=JobStatus.running.value).all()
            records = session.query(ExecutionRecord).join(JobRecord, JobRecord.id == ExecutionRecord.job_id).filter(
                JobRecord.status == JobStatus.running.value).all()
            indexed = {row.execution_id: row for row in records}
            stale = []
            for record in jobs:
                job = _record_to_job(record)
                row = indexed.get(dispatch_identity(job.id, execution_identity(job)))
                candidate = {**self._execution_dict(row), "legacy": False} if row else legacy_record(job)
                if candidate["state"] == "running" and candidate["heartbeat_at"] <= cutoff:
                    stale.append(candidate)
            stale.sort(key=lambda row: (row["heartbeat_at"], row["execution_id"]))
            return stale[:bound]

    def recover_execution(self, job_id, attempt_id, owner, *, stale_before, now=None, max_recoveries=2) -> str:
        """External execution lease is required in addition to this DB fencing."""
        import json
        from .domain.dispatch_intent import dispatch_identity
        cap = bounded_integer(max_recoveries, name="max_recoveries", minimum=0, maximum=10)
        at, cutoff = timestamp(now), timestamp(stale_before)
        with self._Session() as session:
            job = self._lock_execution_job(session, job_id, status=JobStatus.running)
            if job is None or execution_identity(_record_to_job(job)) != attempt_id:
                session.rollback()
                return "unchanged"
            key = dispatch_identity(job_id, attempt_id)
            execution = session.get(ExecutionRecord, key)
            row = self._execution_dict(execution) if execution else legacy_record(_record_to_job(job))
            if row["state"] != "running" or row["owner"] != owner or row["heartbeat_at"] > cutoff:
                session.rollback()
                return "unchanged"
            exhausted = row["recoveries"] >= cap
            existing_outbox = session.get(DispatchRecord, key)
            outbox = recovery_outbox(job_id, attempt_id, exhausted=exhausted, now=at,
                                     existing=self._dispatch_dict(existing_outbox) if existing_outbox else None)
            values = recovery_values(_record_to_job(job), exhausted=exhausted,
                                     now=datetime.fromtimestamp(at, timezone.utc))
            for field, value in values.items():
                if field == "translation_stats":
                    job.translation_stats_json = json.dumps(value, ensure_ascii=False)
                    if isinstance(value.get("precision_polish"), dict):
                        job.precision_polish_status = value["precision_polish"].get("status") or "not_used"
                else:
                    setattr(job, field, value)
            execution_values = {field: row[field] for field in EXECUTION_FIELDS}
            execution_values.update(state="exhausted" if exhausted else "queued", owner="", heartbeat_at=at,
                                    recoveries=row["recoveries"] if exhausted else row["recoveries"] + 1)
            if execution is None:
                session.add(ExecutionRecord(**execution_values))
            else:
                for field, value in execution_values.items():
                    setattr(execution, field, value)
            if existing_outbox is None:
                session.add(DispatchRecord(**outbox))
            else:
                for field, value in outbox.items():
                    setattr(existing_outbox, field, value)
            session.commit()
            return "exhausted" if exhausted else "recovered"

    def list_unstarted_dispatches(self, *, now=None, grace_seconds=3600, limit=20) -> list[dict]:
        grace = bounded_integer(grace_seconds, name="grace_seconds", minimum=600, maximum=86400)
        bound = bounded_integer(limit, name="limit", minimum=1, maximum=100)
        at = timestamp(now)
        with self._Session() as session:
            candidates = session.query(DispatchRecord, JobRecord).join(JobRecord, JobRecord.id == DispatchRecord.job_id).filter(
                JobRecord.status == JobStatus.pending.value, DispatchRecord.status == "sent",
                DispatchRecord.updated_at <= at - grace).order_by(DispatchRecord.updated_at, DispatchRecord.dispatch_id)
            rows = []
            for dispatch, job in candidates.yield_per(100):
                intent = self._dispatch_dict(dispatch)
                if execution_identity(_record_to_job(job)) == intent["attempt_id"] and unstarted_due(intent, now=at, grace_seconds=grace):
                    rows.append(intent)
                    if len(rows) >= bound:
                        break
            return rows

    def rearm_unstarted_dispatch(self, job_id, attempt_id, *, sent_at, now=None, grace_seconds=3600) -> bool:
        """Exact attempt/sent-version CAS, while caller holds the external lease."""
        from sqlalchemy import update
        from .domain.dispatch_intent import dispatch_identity
        grace = bounded_integer(grace_seconds, name="grace_seconds", minimum=600, maximum=86400)
        at, expected_at = timestamp(now), timestamp(sent_at)
        with self._Session() as session:
            job = self._lock_execution_job(session, job_id, status=JobStatus.pending)
            if job is None or execution_identity(_record_to_job(job)) != attempt_id:
                session.rollback()
                return False
            key = dispatch_identity(job_id, attempt_id)
            row = session.get(DispatchRecord, key)
            if row is None or row.updated_at != expected_at or not unstarted_due(self._dispatch_dict(row), now=at, grace_seconds=grace):
                session.rollback()
                return False
            values = recovery_outbox(job_id, attempt_id, exhausted=False, now=at, existing=self._dispatch_dict(row))
            # Publisher completions don't lock jobs, so also CAS the outbox's
            # observed status and timestamp rather than relying on the job lock.
            changed = session.execute(update(DispatchRecord).where(
                DispatchRecord.dispatch_id == key, DispatchRecord.status == "sent", DispatchRecord.updated_at == expected_at,
            ).values(**values))
            session.commit()
            return (changed.rowcount or 0) == 1

    def save_payment_entitlement(self, job_id: str, entitlement: dict, *, expected: dict) -> Optional[Job]:
        """CAS purchase facts; payment migration must not race a newer snapshot."""
        import json
        from sqlalchemy import update
        with self._Session() as session:
            record = session.get(JobRecord, job_id)
            if record is None:
                return None
            raw = record.payment_entitlement_json
            try:
                current = json.loads(raw or "{}")
            except (ValueError, TypeError):
                return _record_to_job(record)
            if current != expected:
                return _record_to_job(record)
            values = {"payment_entitlement_json": json.dumps(entitlement)}
            if entitlement.get("source") == "server_test_bypass":
                values["is_test_order"] = True
            session.execute(update(JobRecord).where(
                JobRecord.id == job_id, JobRecord.payment_entitlement_json == raw,
            ).values(**values))
            session.commit()
            session.expire_all()
            return _record_to_job(session.get(JobRecord, job_id))

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

    def list_jobs_by_creator_ip(self, creator_ip: str, limit: int = 100) -> list:
        with self._Session() as session:
            from sqlalchemy import desc
            rows = (
                session.query(JobRecord)
                .filter_by(creator_ip=creator_ip)
                .order_by(desc(JobRecord.created_at))
                .limit(limit)
                .all()
            )
            return [_record_to_job(r) for r in rows]

    def list_jobs_by_creator_session(self, creator_session: str, limit: int = 100) -> list:
        with self._Session() as session:
            from sqlalchemy import desc
            rows = (
                session.query(JobRecord)
                .filter_by(creator_session=creator_session)
                .order_by(desc(JobRecord.created_at))
                .limit(limit)
                .all()
            )
            return [_record_to_job(r) for r in rows]

    def list_jobs_by_batch_id(self, batch_id: str) -> list:
        with self._Session() as session:
            rows = (
                session.query(JobRecord)
                .filter_by(batch_id=batch_id)
                .order_by(JobRecord.batch_index, JobRecord.created_at)
                .all()
            )
            return [_record_to_job(r) for r in rows]

    def try_mark_paid(self, job_id: str, message: str = "支付成功，排队中...") -> bool:
        """
        条件原子更新：只有当 status='pending_payment' 时，才切到 'pending'。
        返回 True 表示本次 UPDATE 影响了 1 行（赢得竞态）；返回 False 表示
        任务不存在 / 已被其他 webhook 入队过 / 状态不符。
        """
        from sqlalchemy import update
        with self._Session() as session:
            result = session.execute(
                update(JobRecord)
                .where(JobRecord.id == job_id)
                .where(JobRecord.status == JobStatus.pending_payment.value)
                .values(
                    status=JobStatus.pending.value,
                    message=message,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            if (result.rowcount or 0) == 1:
                self._ensure_dispatch_in_session(session, session.get(JobRecord, job_id))
            session.commit()
            return (result.rowcount or 0) == 1

    def try_mark_batch_paid(self, batch_id: str, message: str = "批次支付成功，排队中...") -> bool:
        """在一个数据库事务内抢占批次主任务并解锁全部子任务。"""
        from sqlalchemy import update
        with self._Session() as session:
            now = datetime.now(timezone.utc)
            # Locking the leader serializes callbacks. Remember only previously
            # unpaid children: unrelated old pending rows are not recovered here.
            pending_ids = [row[0] for row in session.query(JobRecord.id).filter(
                JobRecord.batch_id == batch_id, JobRecord.status == JobStatus.pending_payment.value,
            ).all()]
            leader = session.execute(
                update(JobRecord)
                .where(JobRecord.batch_id == batch_id)
                .where(JobRecord.batch_index == "0")
                .where(JobRecord.status == JobStatus.pending_payment.value)
                .values(
                    status=JobStatus.pending.value,
                    message=message,
                    updated_at=now,
                )
            )
            if (leader.rowcount or 0) != 1:
                session.rollback()
                return False
            session.execute(
                update(JobRecord)
                .where(JobRecord.batch_id == batch_id)
                .where(JobRecord.status == JobStatus.pending_payment.value)
                .values(
                    status=JobStatus.pending.value,
                    message=message,
                    updated_at=now,
                )
            )
            for job_id in pending_ids:
                record = session.get(JobRecord, job_id)
                if record is not None and record.status == JobStatus.pending.value:
                    self._ensure_dispatch_in_session(session, record, now=now)
            session.commit()
            return True

    def list_stale_pending_payment(self, min_age_minutes: int = 30) -> list:
        """
        返回所有停留在 pending_payment 超过 min_age_minutes 分钟的任务。
        用于对账 cron：这些订单 webhook 可能已漏发，需主动调支付宝查单。
        """
        from sqlalchemy import and_
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=min_age_minutes)
        with self._Session() as session:
            rows = (
                session.query(JobRecord)
                .filter(
                    and_(
                        JobRecord.status == JobStatus.pending_payment.value,
                        JobRecord.created_at < cutoff,
                    )
                )
                .order_by(JobRecord.created_at)
                .all()
            )
            return [_record_to_job(r) for r in rows]

    def list_payment_reconciliation_candidates(self, min_age_minutes: int = 30) -> list:
        from sqlalchemy import and_, or_
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=min_age_minutes)
        with self._Session() as session:
            records = session.query(JobRecord).filter(or_(
                and_(JobRecord.status == JobStatus.pending_payment.value, JobRecord.created_at < cutoff),
                JobRecord.status == JobStatus.cancelled.value,
            )).order_by(JobRecord.created_at).all()
            jobs = [_record_to_job(record) for record in records]
            return [job for job in jobs if job.status == JobStatus.pending_payment or is_payment_expired(job)]

    @staticmethod
    def _apply_payment_values(record, values):
        import json
        for field, value in values.items():
            if field == "payment_resolution":
                record.payment_resolution_json = json.dumps(value, ensure_ascii=False)
            elif field == "status":
                record.status = value.value
            else:
                setattr(record, field, value)

    def settle_verified_payment(self, job_id: str, *, batch_id="", source="verified_webhook", amount="") -> dict:
        """Serialize verified receipt disposition and its outbox in one transaction."""
        from sqlalchemy import update
        result = {"released": [], "review": [], "unchanged": []}
        with self._Session() as session:
            predicate = [JobRecord.id == job_id]
            if batch_id:
                predicate.extend([JobRecord.batch_id == batch_id, JobRecord.batch_index == "0"])
            # No-op write locks work on both SQLite and PostgreSQL. Read only
            # after acquiring the lock so a concurrent verified close is seen.
            locked = session.execute(update(JobRecord).where(*predicate).values(status=JobRecord.status))
            if (locked.rowcount or 0) != 1:
                session.rollback()
                return result
            if batch_id:
                session.execute(update(JobRecord).where(JobRecord.batch_id == batch_id).values(status=JobRecord.status))
                records = session.query(JobRecord).filter_by(batch_id=batch_id).all()
                records.sort(key=lambda record: (int(record.batch_index or 0), record.id))
            else:
                records = [session.get(JobRecord, job_id)]
            now = datetime.now(timezone.utc)
            for record in records:
                action, values = settlement_values(_record_to_job(record), source=source, amount=amount, now=now)
                self._apply_payment_values(record, values)
                if action == "released":
                    self._ensure_dispatch_in_session(session, record, now=now)
                result[action].append(record.id)
            session.commit()
            return result

    def mark_payment_timeout(self, job_id: str, *, gateway_confirmed=False) -> bool:
        """Only an explicitly verified gateway close can expire a waiting order."""
        if gateway_confirmed is not True:
            return False
        from sqlalchemy import update
        with self._Session() as session:
            locked = session.execute(update(JobRecord).where(
                JobRecord.id == job_id, JobRecord.status == JobStatus.pending_payment.value,
            ).values(status=JobRecord.status))
            if (locked.rowcount or 0) != 1:
                session.rollback()
                return False
            record = session.get(JobRecord, job_id)
            self._apply_payment_values(record, closed_values(_record_to_job(record)))
            session.commit()
            return True

    def mark_batch_payment_timeout(self, batch_id: str, *, gateway_confirmed=False) -> int:
        if gateway_confirmed is not True:
            return 0
        from sqlalchemy import update
        with self._Session() as session:
            # Same leader-first lock order as settlement avoids reversing locks
            # when a gateway close and successful-payment callback race.
            session.execute(update(JobRecord).where(
                JobRecord.batch_id == batch_id, JobRecord.batch_index == "0",
            ).values(status=JobRecord.status))
            session.execute(update(JobRecord).where(
                JobRecord.batch_id == batch_id, JobRecord.status == JobStatus.pending_payment.value,
            ).values(status=JobRecord.status))
            records = session.query(JobRecord).filter_by(batch_id=batch_id, status=JobStatus.pending_payment.value).all()
            now = datetime.now(timezone.utc)
            for record in records:
                self._apply_payment_values(record, closed_values(_record_to_job(record), batch=True, now=now))
            session.commit()
            return len(records)

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
        allow_cancelled_transition: bool = False,
        expected_attempt_id: Optional[str] = None,
        expected_statuses=None,
        expected_updated_at=None,
    ) -> Optional[Job]:
        import json
        from sqlalchemy import exists, select, update
        with self._Session() as session:
            r = self._lock_execution_job(session, job_id)
            if not self._check_write_locked(session, r, job_id, expected_attempt_id=expected_attempt_id,
                                            expected_statuses=expected_statuses, expected_updated_at=expected_updated_at,
                                            translation_stats=translation_stats):
                return _record_to_job(r) if r else None
            if r.status == JobStatus.cancelled.value and status != JobStatus.cancelled and not allow_cancelled_transition:
                return _record_to_job(r)
            predicate = [JobRecord.id == job_id, JobRecord.status == r.status,
                         JobRecord.updated_at == r.updated_at, JobRecord.translation_stats_json == r.translation_stats_json]
            fence = current_job_write_fence()
            if fence is not None:
                predicate.append(exists(select(ExecutionRecord.execution_id).where(
                    ExecutionRecord.job_id == job_id, ExecutionRecord.attempt_id == fence.attempt_id,
                    ExecutionRecord.state == "running", ExecutionRecord.owner == fence.execution_owner)))
            values = {"status": status.value, "message": message, "error_code": error_code,
                      "updated_at": datetime.now(timezone.utc)}
            if output_path:
                values["output_path"] = output_path
            if quality_stats:
                values["quality_stats_json"] = json.dumps(quality_stats.to_dict())
            merged = translation_stats_for_status(_record_to_job(r), status, translation_stats)
            if merged is not None:
                values["translation_stats_json"] = json.dumps(merged)
                if isinstance(merged, dict) and isinstance(merged.get("precision_polish"), dict):
                    values["precision_polish_status"] = merged["precision_polish"].get("status") or "not_used"
            if metrics_summary is not None:
                values["metrics_summary"] = metrics_summary
            changed = session.execute(update(JobRecord).where(*predicate).values(**values)
                                      .execution_options(synchronize_session=False))
            if (changed.rowcount or 0) != 1:
                session.rollback()
                reject_write()
                return self.get(job_id)
            session.commit()
            session.refresh(r)
            return _record_to_job(r)

    def upsert_chapter(self, chapter: JobChapter, expected_attempt_id: Optional[str] = None, *,
                       expected_statuses=None, expected_updated_at=None) -> Optional[JobChapter]:
        with self._Session() as session:
            job_record = self._lock_execution_job(session, chapter.job_id)
            if not self._check_write_locked(session, job_record, chapter.job_id, expected_attempt_id=expected_attempt_id,
                                            expected_statuses=expected_statuses, expected_updated_at=expected_updated_at):
                return None
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

    def upsert_chunk(self, chunk: JobChunk, expected_attempt_id: Optional[str] = None, *,
                     expected_statuses=None, expected_updated_at=None) -> Optional[JobChunk]:
        with self._Session() as session:
            job_record = self._lock_execution_job(session, chunk.job_id)
            if not self._check_write_locked(session, job_record, chunk.job_id, expected_attempt_id=expected_attempt_id,
                                            expected_statuses=expected_statuses, expected_updated_at=expected_updated_at):
                return None
            record_id = f"{chunk.job_id}:{chunk.chunk_id}"
            existing = session.get(ChunkRecord, record_id)
            if existing:
                existing.chapter_id = chunk.chapter_id
                existing.sequence = str(chunk.sequence)
                existing.locator = chunk.locator
                existing.source_hash = chunk.source_hash
                existing.source_text = getattr(chunk, "source_text", "") or ""
                existing.translated_text = getattr(chunk, "translated_text", "") or ""
                import json
                existing.audit_json = json.dumps(getattr(chunk, "audit_json", {}) or {}, ensure_ascii=False)
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

    def clear_translation_progress(self, job_id: str, *, expected_attempt_id=None, expected_statuses=None,
                                   expected_updated_at=None) -> bool:
        from sqlalchemy import delete
        with self._Session() as session:
            job_record = self._lock_execution_job(session, job_id)
            if not self._check_write_locked(session, job_record, job_id, expected_attempt_id=expected_attempt_id,
                                            expected_statuses=expected_statuses, expected_updated_at=expected_updated_at):
                return False
            session.execute(delete(ChunkRecord).where(ChunkRecord.job_id == job_id))
            session.execute(delete(ChapterRecord).where(ChapterRecord.job_id == job_id))
            session.commit()
            return True

    def restart_translation_attempt(
        self,
        job_id: str,
        *,
        attempt_id: str,
        action_label: str,
        max_free_retries: int,
        started_at: datetime,
        expected_updated_at: datetime | None = None,
        failed_only: bool = False,
        translation_quality: str | None = None,
        cache_policy: str | None = None,
        temperature: float | None = None,
        translation_model: str | None = None,
        translation_strategy: str | None = None,
    ) -> tuple[Optional[Job], str]:
        """Atomically claim a terminal job and replace all per-attempt state."""
        import json
        from sqlalchemy import delete, update

        terminal_statuses = [
            JobStatus.success.value,
            JobStatus.failed.value,
            JobStatus.cancelled.value,
        ]
        if failed_only:
            terminal_statuses = [JobStatus.failed.value]
        with self._Session() as session:
            record = self._lock_execution_job(session, job_id)
            if not record:
                return None, "missing"
            if record.status not in terminal_statuses:
                return _record_to_job(record), "active"
            if expected_updated_at is not None:
                expected_utc = (expected_updated_at.astimezone(timezone.utc) if expected_updated_at.tzinfo
                                else expected_updated_at.replace(tzinfo=timezone.utc))
                record_utc = (record.updated_at.astimezone(timezone.utc) if record.updated_at.tzinfo
                              else record.updated_at.replace(tzinfo=timezone.utc))
                if record_utc != expected_utc:
                    return _record_to_job(record), "active"
            entitlement_error = restart_entitlement_reason(
                _record_to_job(record), translation_quality=translation_quality, translation_model=translation_model,
            )
            if entitlement_error:
                return _record_to_job(record), entitlement_error
            try:
                previous = json.loads(record.translation_stats_json or "{}")
                if not isinstance(previous, dict):
                    previous = {}
            except Exception:
                previous = {}
            free_retry_count = int(previous.get("free_retry_count") or 0)
            if max_free_retries >= 0 and free_retry_count >= max_free_retries:
                return _record_to_job(record), "retry_limit"

            stats = restarted_translation_stats(
                previous,
                attempt_id=attempt_id,
                started_at=started_at,
                model=translation_model or record.translation_model or "",
                max_free_retries=max_free_retries,
                action_label=action_label,
            )
            claimed = session.execute(
                update(JobRecord)
                .where(JobRecord.id == job_id)
                .where(JobRecord.status.in_(terminal_statuses))
                .where(JobRecord.updated_at == record.updated_at)
                .values(
                    status=JobStatus.pending.value,
                    message=f"{action_label}已排队（第 {stats['translation_attempt']} 次尝试）",
                    error_code=None,
                    output_path=None,
                    quality_stats_json="{}",
                    translation_stats_json=json.dumps(stats, ensure_ascii=False),
                    precision_polish_status=(stats.get("precision_polish") or {}).get("status", "not_used"),
                    metrics_summary="",
                    translation_quality=translation_quality or record.translation_quality or "standard",
                    cache_policy=cache_policy or record.cache_policy or "reuse",
                    temperature=temperature if temperature is not None else record.temperature,
                    translation_model=translation_model or record.translation_model or "deepseek-flash",
                    translation_strategy=translation_strategy or record.translation_strategy or "auto",
                    updated_at=started_at,
                )
            )
            if (claimed.rowcount or 0) != 1:
                session.rollback()
                refreshed = self.get(job_id)
                return refreshed, "active"
            session.execute(delete(ChunkRecord).where(ChunkRecord.job_id == job_id))
            session.execute(delete(ChapterRecord).where(ChapterRecord.job_id == job_id))
            session.expire_all()
            self._ensure_dispatch_in_session(session, session.get(JobRecord, job_id), now=started_at)
            session.commit()
            refreshed = session.get(JobRecord, job_id)
            return (_record_to_job(refreshed) if refreshed else None), "ok"

    def begin_translation_confirmation(
        self,
        job_id: str,
        *,
        translation_strategy: str,
        bilingual: bool,
        glossary: dict[str, str],
        translation_preflight: dict,
    ) -> Optional[Job]:
        """Atomically claim an awaiting-confirmation job and save user edits."""
        import json
        from sqlalchemy import update
        with self._Session() as session:
            result = session.execute(
                update(JobRecord)
                .where(JobRecord.id == job_id)
                .where(JobRecord.status == JobStatus.awaiting_confirmation.value)
                .values(
                    status=JobStatus.confirming.value,
                    message="正在创建支付订单...",
                    translation_strategy=translation_strategy,
                    bilingual=bool(bilingual),
                    glossary_json=json.dumps(glossary, ensure_ascii=False),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            if (result.rowcount or 0) != 1:
                session.rollback()
                return None
            record = session.get(JobRecord, job_id)
            try:
                stats = json.loads(record.translation_stats_json or "{}")
                if not isinstance(stats, dict):
                    stats = {}
            except Exception:
                stats = {}
            stats["translation_preflight"] = dict(translation_preflight)
            record.translation_stats_json = json.dumps(stats, ensure_ascii=False)
            session.commit()
            session.refresh(record)
            return _record_to_job(record)

    def finish_translation_confirmation(
        self,
        job_id: str,
        *,
        status: JobStatus,
        message: str,
        expected_amount: str,
        payment_checkout: Optional[dict] = None,
    ) -> Optional[Job]:
        import json
        from sqlalchemy import update
        with self._Session() as session:
            result = session.execute(
                update(JobRecord)
                .where(JobRecord.id == job_id)
                .where(JobRecord.status == JobStatus.confirming.value)
                .values(
                    status=status.value,
                    message=message,
                    expected_amount=expected_amount,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            if (result.rowcount or 0) != 1:
                session.rollback()
                return None
            if status == JobStatus.pending:
                self._ensure_dispatch_in_session(session, session.get(JobRecord, job_id))
            if payment_checkout is not None:
                record = session.get(JobRecord, job_id)
                stats = json.loads(record.translation_stats_json or "{}")
                record.translation_stats_json = json.dumps({**stats, "payment_checkout": payment_checkout}, ensure_ascii=False)
            session.commit()
            record = session.get(JobRecord, job_id)
            return _record_to_job(record) if record else None

    def rollback_translation_confirmation(self, job_id: str, message: str) -> Optional[Job]:
        from sqlalchemy import update
        with self._Session() as session:
            result = session.execute(
                update(JobRecord)
                .where(JobRecord.id == job_id)
                .where(JobRecord.status == JobStatus.confirming.value)
                .values(
                    status=JobStatus.awaiting_confirmation.value,
                    message=message,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            if (result.rowcount or 0) != 1:
                session.rollback()
                return None
            session.commit()
            record = session.get(JobRecord, job_id)
            return _record_to_job(record) if record else None

    def list_chunks(self, job_id: str, chapter_id: Optional[str] = None) -> list[JobChunk]:
        with self._Session() as session:
            query = session.query(ChunkRecord).filter_by(job_id=job_id)
            if chapter_id is not None:
                query = query.filter_by(chapter_id=chapter_id)
            rows = query.order_by(ChunkRecord.chapter_id, ChunkRecord.sequence).all()
            return [_record_to_chunk(r) for r in rows]

    def add_stage(self, stage: JobStage, *, expected_attempt_id=None, expected_statuses=None,
                  expected_updated_at=None) -> Optional[JobStage]:
        with self._Session() as session:
            job_record = self._lock_execution_job(session, stage.job_id)
            if not self._check_write_locked(session, job_record, stage.job_id, expected_attempt_id=expected_attempt_id,
                                            expected_statuses=expected_statuses, expected_updated_at=expected_updated_at):
                return None
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

    def list_notification_page(self, *, job_id=None, user_id=None, limit=21, before=None) -> list[JobNotification]:
        validate_notification_page(job_id=job_id, user_id=user_id, limit=limit, before=before)
        with self._Session() as session:
            query = session.query(NotificationRecord).filter(NotificationRecord.channel == "in_app")
            if job_id is not None:
                query = query.filter(NotificationRecord.job_id == job_id)
            if user_id is not None:
                query = query.join(JobRecord, JobRecord.id == NotificationRecord.job_id).filter(JobRecord.user_id == user_id)
            if before is not None:
                at, identity = before
                query = query.filter(or_(NotificationRecord.created_at < at,
                    and_(NotificationRecord.created_at == at, NotificationRecord.id < identity)))
            rows = query.order_by(NotificationRecord.created_at.desc(), NotificationRecord.id.desc()).limit(limit).all()
            return [_record_to_notification(row) for row in rows]

    def list_jobs_by_user_id(self, user_id: str, limit: int = 100) -> list:
        with self._Session() as session:
            from sqlalchemy import desc
            rows = (
                session.query(JobRecord)
                .filter_by(user_id=user_id)
                .order_by(desc(JobRecord.created_at))
                .limit(limit)
                .all()
            )
            return [_record_to_job(r) for r in rows]

    def claim_jobs_by_session(self, creator_session: str, user_id: str) -> int:
        """将指定 session 下所有未归属的匿名任务批量绑定到 user_id，返回影响行数。"""
        from sqlalchemy import update, and_
        with self._Session() as session:
            result = session.execute(
                update(JobRecord)
                .where(
                    and_(
                        JobRecord.creator_session == creator_session,
                        JobRecord.user_id == None,  # noqa: E711
                    )
                )
                .values(user_id=user_id, updated_at=datetime.now(timezone.utc))
            )
            session.commit()
            return result.rowcount or 0

    # ─── 用户账户 CRUD ─────────────────────────────────────────────────────────

    def get_user(self, user_id: str) -> Optional[User]:
        with self._Session() as session:
            r = session.get(UserRecord, user_id)
            return _record_to_user(r) if r else None

    def get_user_by_phone(self, phone: str) -> Optional[User]:
        with self._Session() as session:
            r = session.query(UserRecord).filter_by(phone=phone).first()
            return _record_to_user(r) if r else None

    def get_user_by_google_id(self, google_id: str) -> Optional[User]:
        with self._Session() as session:
            r = session.query(UserRecord).filter_by(google_id=google_id).first()
            return _record_to_user(r) if r else None

    def get_user_by_wechat_openid(self, openid: str) -> Optional[User]:
        with self._Session() as session:
            r = session.query(UserRecord).filter_by(wechat_openid=openid).first()
            return _record_to_user(r) if r else None

    def create_user(self, user: User) -> User:
        with self._Session() as session:
            record = _user_to_record(user)
            session.add(record)
            session.commit()
            session.refresh(record)
            return _record_to_user(record)

    def update_user(self, user: User) -> User:
        with self._Session() as session:
            r = session.get(UserRecord, user.id)
            if not r:
                raise ValueError(f"User {user.id} not found")
            r.phone = user.phone
            r.google_id = user.google_id
            r.wechat_openid = user.wechat_openid
            r.wechat_unionid = user.wechat_unionid
            r.display_name = user.display_name
            r.avatar_url = user.avatar_url
            r.is_active = user.is_active
            r.last_login_at = user.last_login_at
            session.commit()
            session.refresh(r)
            return _record_to_user(r)
