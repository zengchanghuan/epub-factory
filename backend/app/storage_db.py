"""
持久化任务存储（PostgreSQL / SQLite）

通过环境变量 DATABASE_URL 切换后端：
- 未设置 → 使用 SQLite（路径 ./epub_jobs.db），无需额外服务
- postgresql://... → 连接 PostgreSQL

与原有内存 JobStore（storage.py）保持相同的公共接口，可无缝替换。
"""

import os
from datetime import datetime, timedelta, timezone
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
    User,
)
from .domain.translation_attempt import restarted_translation_stats


# ─── ORM 模型 ────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


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
    expected_amount = Column(String(16), nullable=True)
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
    translation_model = Column(String(64), nullable=True)
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
    if "quality_stats_json" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN quality_stats_json TEXT")
    if "translation_stats_json" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN translation_stats_json TEXT")
    if "metrics_summary" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN metrics_summary TEXT")
    if "translation_model" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN translation_model VARCHAR(64)")
    if "traditional_variant" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN traditional_variant VARCHAR(16)")
    if "access_token" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN access_token VARCHAR(64)")
    if "creator_ip" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN creator_ip VARCHAR(64)")
    if "creator_session" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN creator_session VARCHAR(128)")
    if "expected_amount" not in columns:
        migrations.append("ALTER TABLE epub_jobs ADD COLUMN expected_amount VARCHAR(16)")
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
        expected_amount=getattr(r, "expected_amount", None) or "",
        output_path=r.output_path,
        output_mode=OutputMode(r.output_mode),
        enable_translation=r.enable_translation,
        target_lang=r.target_lang,
        bilingual=r.bilingual,
        glossary=glossary,
        device=DeviceProfile(r.device),
        temperature=None,
        translation_model=getattr(r, "translation_model", None) or "deepseek-v4-flash",
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
        expected_amount=getattr(job, "expected_amount", "") or "",
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
        translation_model=getattr(job, "translation_model", None) or "deepseek-v4-flash",
        traditional_variant=getattr(job, "traditional_variant", None) or "auto",
        lexicon_domains=json.dumps(getattr(job, "lexicon_domains", ["general", "tech", "movie"])),
        enable_proper_noun=bool(getattr(job, "enable_proper_noun", True)),
        enable_precision_polish=bool(getattr(job, "enable_precision_polish", False)),
        precision_polish_order_no=getattr(job, "precision_polish_order_no", None) or None,
        precision_polish_status="not_used",
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
            session.commit()
            return (result.rowcount or 0) == 1

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

    def mark_payment_timeout(self, job_id: str) -> bool:
        """将超时未支付任务标记为 cancelled。"""
        from sqlalchemy import update
        with self._Session() as session:
            result = session.execute(
                update(JobRecord)
                .where(JobRecord.id == job_id)
                .where(JobRecord.status == JobStatus.pending_payment.value)
                .values(
                    status=JobStatus.cancelled.value,
                    message="支付超时，订单已关闭",
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()
            return (result.rowcount or 0) == 1

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
    ) -> Optional[Job]:
        with self._Session() as session:
            r = session.get(JobRecord, job_id)
            if not r:
                return None
            if expected_attempt_id:
                import json
                current_stats = {}
                try:
                    current_stats = json.loads(r.translation_stats_json or "{}")
                except Exception:
                    current_stats = {}
                if str((current_stats or {}).get("attempt_id") or "") != expected_attempt_id:
                    return _record_to_job(r)
            if r.status == JobStatus.cancelled.value and status != JobStatus.cancelled and not allow_cancelled_transition:
                return _record_to_job(r)
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
                existing_stats = {}
                if r.translation_stats_json:
                    try:
                        existing_stats = json.loads(r.translation_stats_json)
                        if not isinstance(existing_stats, dict):
                            existing_stats = {}
                    except Exception:
                        existing_stats = {}
                merged = dict(existing_stats)
                if isinstance(translation_stats, dict):
                    merged.update(translation_stats)
                else:
                    merged = translation_stats
                r.translation_stats_json = json.dumps(merged)
            if metrics_summary is not None:
                r.metrics_summary = metrics_summary
            r.updated_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(r)
            return _record_to_job(r)

    def upsert_chapter(self, chapter: JobChapter, expected_attempt_id: Optional[str] = None) -> JobChapter:
        with self._Session() as session:
            if expected_attempt_id:
                import json
                job_record = session.get(JobRecord, chapter.job_id)
                try:
                    job_stats = json.loads(job_record.translation_stats_json or "{}") if job_record else {}
                except Exception:
                    job_stats = {}
                if str((job_stats or {}).get("attempt_id") or "") != expected_attempt_id:
                    return chapter
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

    def upsert_chunk(self, chunk: JobChunk, expected_attempt_id: Optional[str] = None) -> JobChunk:
        with self._Session() as session:
            if expected_attempt_id:
                import json
                job_record = session.get(JobRecord, chunk.job_id)
                try:
                    job_stats = json.loads(job_record.translation_stats_json or "{}") if job_record else {}
                except Exception:
                    job_stats = {}
                if str((job_stats or {}).get("attempt_id") or "") != expected_attempt_id:
                    return chunk
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

    def clear_translation_progress(self, job_id: str) -> None:
        from sqlalchemy import delete
        with self._Session() as session:
            session.execute(delete(ChunkRecord).where(ChunkRecord.job_id == job_id))
            session.execute(delete(ChapterRecord).where(ChapterRecord.job_id == job_id))
            session.commit()

    def restart_translation_attempt(
        self,
        job_id: str,
        *,
        attempt_id: str,
        action_label: str,
        max_free_retries: int,
        started_at: datetime,
    ) -> tuple[Optional[Job], str]:
        """Atomically claim a terminal job and replace all per-attempt state."""
        import json
        from sqlalchemy import delete, update

        terminal_statuses = [
            JobStatus.success.value,
            JobStatus.failed.value,
            JobStatus.cancelled.value,
        ]
        with self._Session() as session:
            record = session.get(JobRecord, job_id)
            if not record:
                return None, "missing"
            if record.status not in terminal_statuses:
                return _record_to_job(record), "active"
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
                model=record.translation_model or "",
                max_free_retries=max_free_retries,
                action_label=action_label,
            )
            claimed = session.execute(
                update(JobRecord)
                .where(JobRecord.id == job_id)
                .where(JobRecord.status.in_(terminal_statuses))
                .values(
                    status=JobStatus.pending.value,
                    message=f"{action_label}已排队（第 {stats['translation_attempt']} 次尝试）",
                    error_code=None,
                    output_path=None,
                    quality_stats_json="{}",
                    translation_stats_json=json.dumps(stats, ensure_ascii=False),
                    metrics_summary="",
                    updated_at=started_at,
                )
            )
            if (claimed.rowcount or 0) != 1:
                session.rollback()
                refreshed = self.get(job_id)
                return refreshed, "active"
            session.execute(delete(ChunkRecord).where(ChunkRecord.job_id == job_id))
            session.execute(delete(ChapterRecord).where(ChapterRecord.job_id == job_id))
            session.commit()
            refreshed = session.get(JobRecord, job_id)
            return (_record_to_job(refreshed) if refreshed else None), "ok"

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
