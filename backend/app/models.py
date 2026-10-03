from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Optional
import uuid


@dataclass
class User:
    id: str
    phone: Optional[str] = None
    google_id: Optional[str] = None
    wechat_openid: Optional[str] = None
    wechat_unionid: Optional[str] = None
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    is_active: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_login_at: Optional[datetime] = None


class JobStatus(str, Enum):
    awaiting_confirmation = "awaiting_confirm"
    confirming = "confirming"
    pending_payment = "pending_payment"
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"
    cancelled = "cancelled"


class OutputMode(str, Enum):
    traditional = "traditional"
    simplified = "simplified"
    original = "original"  # Dedicated text-PDF product only; no CJK normalization.


class DeviceProfile(str, Enum):
    generic = "generic"
    kindle = "kindle"
    apple = "apple"


class TraditionalVariant(str, Enum):
    """OpenCC 地区配置：简体输出时表示繁体来源，繁体输出时表示目标繁体版本。"""
    auto = "auto"   # 通用：t2s / s2t
    tw = "tw"      # 台湾：tw2s / s2tw
    hk = "hk"      # 香港：hk2s / s2hk


class ErrorCode(str, Enum):
    """所有已定义的错误码，集中管理避免魔法字符串。"""
    CONVERT_FAILED = "CONVERT_FAILED"
    TRANSLATION_FAILED = "TRANSLATION_FAILED"
    TRANSLATION_PROVIDER_UNAVAILABLE = "TRANSLATION_PROVIDER_UNAVAILABLE"
    PARTIAL_TRANSLATION = "PARTIAL_TRANSLATION"
    PRECISION_POLISH_FAILED = "PRECISION_POLISH_FAILED"
    EPUB_VALIDATION_FAILED = "EPUB_VALIDATION_FAILED"
    EPUB_VALIDATION_UNAVAILABLE = "EPUB_VALIDATION_UNAVAILABLE"
    PAYMENT_EXPIRED = "PAYMENT_EXPIRED"
    PAYMENT_REVIEW_REQUIRED = "PAYMENT_REVIEW_REQUIRED"
    WORKER_RECOVERY_EXHAUSTED = "WORKER_RECOVERY_EXHAUSTED"


class ChapterKind(str, Enum):
    body = "body"
    nav = "nav"
    copyright = "copyright"
    appendix = "appendix"
    index = "index"
    other = "other"


class ChapterStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    partial_completed = "partial_completed"
    failed = "failed"


class ChunkStatus(str, Enum):
    pending = "pending"
    cached = "cached"
    translated = "translated"
    retrying = "retrying"
    failed = "failed"
    skipped = "skipped"


class StageStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class NotificationStatus(str, Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"


@dataclass
class QualityStats:
    css_cleaned: int = 0
    typography_fixed: int = 0
    toc_generated: int = 0
    stem_protected: int = 0

    def to_dict(self):
        return {
            "css_cleaned": self.css_cleaned,
            "typography_fixed": self.typography_fixed,
            "toc_generated": self.toc_generated,
            "stem_protected": self.stem_protected,
        }


@dataclass
class LexiconStats:
    """L2/L3 词典命中统计，随 ConversionResult 返回。"""
    versions: Dict[str, str] = field(default_factory=dict)   # domain → version
    total_replacements: int = 0
    top_hits: list = field(default_factory=list)              # [{layer, tw, cn, count, domain}]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "versions": self.versions,
            "total_replacements": self.total_replacements,
            "top_hits": self.top_hits[:20],  # 最多返回 20 条
        }

    @classmethod
    def empty(cls) -> "LexiconStats":
        return cls()


@dataclass
class ConversionResult:
    quality_stats: QualityStats = field(default_factory=QualityStats)
    translation_stats: Dict[str, Any] = field(default_factory=dict)
    lexicon_stats: LexiconStats = field(default_factory=LexiconStats)
    metrics_summary: str = ""
    message: str = ""
    error_code: Optional[str] = None
    validation_passed: bool = True  # EpubCheck 通过才可交付；False 时应标为 failed

@dataclass
class Job:
    id: str
    source_filename: str
    output_mode: OutputMode
    trace_id: str
    input_path: str
    access_token: str = ""
    token_expires_at: Optional[datetime] = None  # access_token 过期时间，None 表示永久（兼容旧任务）
    creator_ip: str = ""
    creator_session: str = ""
    user_id: Optional[str] = None  # 登录用户 ID，匿名任务为 None
    is_test_order: bool = False  # Server-verified admin test request or explicit server test bypass.
    expected_amount: str = ""  # 下单时的应付金额（元），webhook 校验依据
    payment_entitlement: Dict[str, Any] = field(default_factory=dict)  # Frozen purchase facts, never inferred from status.
    payment_resolution: Dict[str, Any] = field(default_factory=dict)  # Verified close/late-payment disposition, not execution state.
    batch_id: str = ""  # 批量转换批次；空字符串表示普通单文件任务
    batch_index: int = 0
    batch_size: int = 0
    enable_translation: bool = False
    target_lang: str = "zh-CN"
    bilingual: bool = False
    glossary: Dict[str, str] = field(default_factory=dict)
    device: DeviceProfile = DeviceProfile.generic
    output_path: Optional[str] = None
    temperature: Optional[float] = None
    translation_model: str = "deepseek-flash"
    translation_quality: str = "standard"  # standard | high | literary
    cache_policy: str = "reuse"            # reuse | verified | fresh
    translation_strategy: str = "auto"     # auto | five versioned strategy assets
    traditional_variant: str = "auto"  # auto | tw | hk，仅简体输出时生效
    lexicon_domains: list = field(default_factory=lambda: ["general", "tech", "movie"])
    enable_proper_noun: bool = True
    enable_precision_polish: bool = False   # L4 DeepSeek 精校开关
    precision_polish_order_no: str = ""     # 对应支付宝订单号（开启 L4 时必填）
    polish_char_count: int = 0              # 解析后的正文有效字数（用于阶梯计价）
    status: JobStatus = JobStatus.pending
    message: str = ""
    error_code: Optional[str] = None
    quality_stats: QualityStats = field(default_factory=QualityStats)
    translation_stats: Dict[str, Any] = field(default_factory=dict)
    metrics_summary: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class JobChapter:
    job_id: str
    chapter_id: str
    file_path: str
    chapter_kind: ChapterKind = ChapterKind.body
    status: ChapterStatus = ChapterStatus.pending
    chunk_total: int = 0
    chunk_success: int = 0
    chunk_failed: int = 0
    chunk_cached: int = 0
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error_message: Optional[str] = None


@dataclass
class JobChunk:
    job_id: str
    chapter_id: str
    chunk_id: str
    sequence: int
    locator: str
    source_hash: str
    source_text: str = ""
    translated_text: str = ""
    audit_json: Dict[str, Any] = field(default_factory=dict)
    status: ChunkStatus = ChunkStatus.pending
    cached: bool = False
    model: Optional[str] = None
    base_url: Optional[str] = None
    retry_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class JobStage:
    job_id: str
    stage_name: str
    status: StageStatus = StageStatus.pending
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None
    elapsed_ms: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class JobNotification:
    job_id: str
    channel: str
    status: NotificationStatus = NotificationStatus.pending
    payload: Dict[str, Any] = field(default_factory=dict)
    user_id: Optional[str] = None
    sent_at: Optional[datetime] = None
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


def validate_notification_page(*, job_id=None, user_id=None, limit=21, before=None):
    """Shared scope and cursor contract for the two notification stores."""
    for value in (job_id, user_id):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("notification scope must be a non-empty string")
    if job_id is None and user_id is None:
        raise ValueError("notification pagination requires a job or user scope")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 101:
        raise ValueError("notification limit must be an integer from 1 to 101")
    if before is not None:
        if not isinstance(before, tuple) or len(before) != 2:
            raise ValueError("notification cursor must contain timestamp and id")
        at, identity = before
        if (not isinstance(at, datetime) or at.tzinfo is None
                or at.utcoffset() != timedelta(0)):
            raise ValueError("notification cursor timestamp must be aware UTC")
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 96:
            raise ValueError("notification cursor id is invalid")
