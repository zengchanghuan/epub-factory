from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


class JobStatus(str, Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"
    cancelled = "cancelled"


class OutputMode(str, Enum):
    traditional = "traditional"
    simplified = "simplified"


class DeviceProfile(str, Enum):
    generic = "generic"
    kindle = "kindle"
    apple = "apple"


class TraditionalVariant(str, Enum):
    """繁体来源，仅当 output_mode=simplified 时用于 OpenCC 繁→简配置。"""
    auto = "auto"   # 通用 t2s
    tw = "tw"      # 台湾 tw2s
    hk = "hk"      # 香港 hk2s


class ErrorCode(str, Enum):
    """所有已定义的错误码，集中管理避免魔法字符串。"""
    CONVERT_FAILED = "CONVERT_FAILED"
    TRANSLATION_FAILED = "TRANSLATION_FAILED"
    PARTIAL_TRANSLATION = "PARTIAL_TRANSLATION"
    EPUB_VALIDATION_FAILED = "EPUB_VALIDATION_FAILED"


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
class ConversionResult:
    quality_stats: QualityStats = field(default_factory=QualityStats)
    translation_stats: Dict[str, Any] = field(default_factory=dict)
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
    enable_translation: bool = False
    target_lang: str = "zh-CN"
    bilingual: bool = False
    glossary: Dict[str, str] = field(default_factory=dict)
    device: DeviceProfile = DeviceProfile.generic
    output_path: Optional[str] = None
    temperature: Optional[float] = None
    traditional_variant: str = "auto"  # auto | tw | hk，仅简体输出时生效
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

