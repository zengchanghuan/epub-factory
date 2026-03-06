from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


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
class Job:
    id: str
    source_filename: str
    output_mode: OutputMode
    trace_id: str
    input_path: str
    enable_translation: bool = False
    target_lang: str = "zh-CN"
    device: DeviceProfile = DeviceProfile.generic
    output_path: Optional[str] = None
    status: JobStatus = JobStatus.pending
    message: str = ""
    error_code: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

