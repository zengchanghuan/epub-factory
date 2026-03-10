from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Optional


class JobStatus(str, Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"


class OutputMode(str, Enum):
    traditional = "traditional"
    simplified = "simplified"


class DeviceProfile(str, Enum):
    generic = "generic"
    kindle = "kindle"
    apple = "apple"


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
    status: JobStatus = JobStatus.pending
    message: str = ""
    error_code: Optional[str] = None
    quality_stats: QualityStats = field(default_factory=QualityStats)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

