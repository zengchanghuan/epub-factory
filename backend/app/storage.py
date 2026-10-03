import os
import uuid
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Dict, Optional

from .models import (Job, JobChapter, JobChunk, JobNotification, JobStage, JobStatus,
                     QualityStats, StageStatus, validate_notification_page)
from .domain.translation_attempt import restarted_translation_stats
from .domain.payment_entitlement import restart_entitlement_reason
from .domain.dispatch_intent import build_dispatch_intent, completion_values, due, timestamp
from .domain.payment_lifecycle_state import is_payment_expired, settlement_values, closed_values
from .domain.execution_state import (EXECUTION_FIELDS, bounded_integer, execution_identity,
                                     running_record, legacy_record, recovery_outbox, recovery_values, migrates_legacy_identity, unstarted_due)
from .domain.job_write_fence import check_job_write, utc_datetime, translation_stats_for_status


class JobStore:
    """内存存储（无外部依赖，重启后数据丢失）"""

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._stages: Dict[str, list] = {}
        self._chapters: Dict[str, JobChapter] = {}
        self._chunks: Dict[str, JobChunk] = {}
        self._notifications: list = []  # list[JobNotification]
        self._dispatches: Dict[str, dict] = {}
        self._executions: Dict[str, dict] = {}
        self._lock = RLock()

    def add(self, job: Job) -> None:
        with self._lock:
            if job.status == JobStatus.pending:
                stats, intent = self._prepare_dispatch_locked(job)
                job.translation_stats = stats
                self._dispatches.setdefault(intent["dispatch_id"], intent)
            self._jobs[job.id] = deepcopy(job)

    def _prepare_dispatch_locked(self, job: Job, *, now=None) -> tuple[dict, dict]:
        stats, intent = build_dispatch_intent(job, now=now)
        return stats, self._dispatches.get(intent["dispatch_id"], intent)

    def ensure_dispatch(self, job_id: str) -> Optional[dict]:
        """Explicit internal recovery; pending status alone is not payment proof."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != JobStatus.pending:
                return None
            stats, intent = self._prepare_dispatch_locked(job)
            job.translation_stats = stats
            self._dispatches.setdefault(intent["dispatch_id"], intent)
            return dict(intent)

    def claim_dispatch(self, *, job_id=None, lease_seconds=60, now=None) -> Optional[dict]:
        at = timestamp(now)
        with self._lock:
            rows = sorted(self._dispatches.values(), key=lambda row: (row["created_at"], row["dispatch_id"]))
            for row in rows:
                if (job_id is None or row["job_id"] == job_id) and due(row, at):
                    row.update(status="publishing", attempts=row["attempts"] + 1,
                               updated_at=at, lease_token=uuid.uuid4().hex,
                               lease_expires_at=at + max(1.0, float(lease_seconds)))
                    return dict(row)
        return None

    def finish_dispatch(self, dispatch_id, lease_token, *, outcome="sent", error="", retry_delay_seconds=5, now=None) -> bool:
        values = completion_values(outcome=outcome, error=error, retry_delay_seconds=retry_delay_seconds, now=now)
        with self._lock:
            row = self._dispatches.get(dispatch_id)
            if not row or row["status"] != "publishing" or not lease_token or row["lease_token"] != lease_token:
                return False
            row.update(values)
            return True

    def list_dispatches(self, job_id=None, limit=100) -> list[dict]:
        with self._lock:
            rows = [row for row in self._dispatches.values() if job_id is None or row["job_id"] == job_id]
            rows.sort(key=lambda row: (row["created_at"], row["dispatch_id"]))
            return [dict(row) for row in rows[:max(0, int(limit))]]

    def get_dispatch(self, dispatch_id: str) -> Optional[dict]:
        with self._lock:
            row = self._dispatches.get(dispatch_id)
            return dict(row) if row is not None else None

    def get_execution(self, job_id, attempt_id) -> Optional[dict]:
        from .domain.dispatch_intent import dispatch_identity
        with self._lock:
            row = self._executions.get(dispatch_identity(job_id, attempt_id))
            return dict(row) if row is not None else None

    def begin_execution(self, job_id, attempt_id, owner, *, now=None) -> bool:
        from .domain.dispatch_intent import dispatch_identity
        record = running_record(job_id, attempt_id, owner, now=now)
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != JobStatus.pending or execution_identity(job) != attempt_id:
                return False
            existing = self._executions.get(record["execution_id"])
            record["recoveries"] = existing["recoveries"] if existing else 0
            old_key = dispatch_identity(job_id, "")
            legacy = self._executions.get(old_key) if not existing and migrates_legacy_identity(job, attempt_id) else None
            if legacy and legacy["state"] == "queued" and not legacy["owner"]:
                record["recoveries"] = legacy["recoveries"]
                obsolete = recovery_outbox(job_id, "", exhausted=True, now=record["heartbeat_at"],
                                           existing=self._dispatches.get(old_key))
                obsolete["last_error"] = "legacy execution identity migrated"
                legacy.update(state="finished", heartbeat_at=record["heartbeat_at"])
                self._dispatches[old_key] = obsolete
            self._executions[record["execution_id"]] = record
            job.status, job.message = JobStatus.running, "开始转换"
            job.updated_at = datetime.fromtimestamp(record["heartbeat_at"], timezone.utc)
            return True

    def heartbeat_execution(self, job_id, attempt_id, owner, *, now=None) -> bool:
        from .domain.dispatch_intent import dispatch_identity
        with self._lock:
            job = self._jobs.get(job_id)
            row = self._executions.get(dispatch_identity(job_id, attempt_id))
            if (not job or job.status != JobStatus.running or execution_identity(job) != attempt_id
                    or not row or row["state"] != "running" or not owner or row["owner"] != owner):
                return False
            row["heartbeat_at"] = timestamp(now)
            return True

    def finish_execution(self, job_id, attempt_id, owner, *, now=None) -> bool:
        from .domain.dispatch_intent import dispatch_identity
        with self._lock:
            job = self._jobs.get(job_id)
            row = self._executions.get(dispatch_identity(job_id, attempt_id))
            if (not job or job.status not in {JobStatus.success, JobStatus.failed, JobStatus.cancelled} or execution_identity(job) != attempt_id
                    or not row or row["state"] != "running" or not owner or row["owner"] != owner):
                return False
            row.update(state="finished", owner="", heartbeat_at=timestamp(now))
            return True

    def list_stale_executions(self, *, stale_before, limit=20) -> list[dict]:
        from .domain.dispatch_intent import dispatch_identity
        bound = bounded_integer(limit, name="limit", minimum=1, maximum=100)
        cutoff = timestamp(stale_before)
        with self._lock:
            stale = []
            for job in self._jobs.values():
                if job.status != JobStatus.running:
                    continue
                row = self._executions.get(dispatch_identity(job.id, execution_identity(job)))
                candidate = {**row, "legacy": False} if row else legacy_record(job)
                if candidate["state"] == "running" and candidate["heartbeat_at"] <= cutoff:
                    stale.append(candidate)
            stale.sort(key=lambda row: (row["heartbeat_at"], row["execution_id"]))
            return stale[:bound]

    def recover_execution(self, job_id, attempt_id, owner, *, stale_before, now=None, max_recoveries=2) -> str:
        """Caller must hold the external lease; repeat liveness checks atomically."""
        from .domain.dispatch_intent import dispatch_identity
        cap = bounded_integer(max_recoveries, name="max_recoveries", minimum=0, maximum=10)
        at, cutoff = timestamp(now), timestamp(stale_before)
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != JobStatus.running or execution_identity(job) != attempt_id:
                return "unchanged"
            key = dispatch_identity(job_id, attempt_id)
            row = self._executions.get(key) or legacy_record(job)
            if row["state"] != "running" or row["owner"] != owner or row["heartbeat_at"] > cutoff:
                return "unchanged"
            exhausted = row["recoveries"] >= cap
            outbox = recovery_outbox(job_id, attempt_id, exhausted=exhausted, now=at, existing=self._dispatches.get(key))
            values = recovery_values(job, exhausted=exhausted, now=datetime.fromtimestamp(at, timezone.utc))
            execution = {field: row[field] for field in EXECUTION_FIELDS}
            execution.update(state="exhausted" if exhausted else "queued", owner="", heartbeat_at=at,
                             recoveries=row["recoveries"] if exhausted else row["recoveries"] + 1)
            for field, value in values.items():
                setattr(job, field, JobStatus(value) if field == "status" else value)
            self._dispatches[key], self._executions[key] = outbox, execution
            return "exhausted" if exhausted else "recovered"

    def list_unstarted_dispatches(self, *, now=None, grace_seconds=3600, limit=20) -> list[dict]:
        grace = bounded_integer(grace_seconds, name="grace_seconds", minimum=600, maximum=86400)
        bound = bounded_integer(limit, name="limit", minimum=1, maximum=100)
        at = timestamp(now)
        with self._lock:
            rows = []
            for intent in self._dispatches.values():
                job = self._jobs.get(intent["job_id"])
                if (job and job.status == JobStatus.pending and execution_identity(job) == intent["attempt_id"]
                        and unstarted_due(intent, now=at, grace_seconds=grace)):
                    rows.append(dict(intent))
            rows.sort(key=lambda row: (row["updated_at"], row["dispatch_id"]))
            return rows[:bound]

    def rearm_unstarted_dispatch(self, job_id, attempt_id, *, sent_at, now=None, grace_seconds=3600) -> bool:
        """Caller owns the execution lease; queue waiting never uses a recovery."""
        from .domain.dispatch_intent import dispatch_identity
        grace = bounded_integer(grace_seconds, name="grace_seconds", minimum=600, maximum=86400)
        at, expected_at = timestamp(now), timestamp(sent_at)
        with self._lock:
            job = self._jobs.get(job_id)
            key = dispatch_identity(job_id, attempt_id)
            row = self._dispatches.get(key)
            if (not job or job.status != JobStatus.pending or execution_identity(job) != attempt_id
                    or not row or row["updated_at"] != expected_at or not unstarted_due(row, now=at, grace_seconds=grace)):
                return False
            self._dispatches[key] = recovery_outbox(job_id, attempt_id, exhausted=False, now=at, existing=row)
            return True

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return deepcopy(self._jobs.get(job_id))

    def save_payment_entitlement(self, job_id: str, entitlement: dict, *, expected: dict) -> Optional[Job]:
        """Compare-and-swap trusted purchase facts without changing execution state."""
        import copy
        with self._lock:
            job = self._jobs.get(job_id)
            if job and (job.payment_entitlement or {}) == expected:
                job.payment_entitlement = copy.deepcopy(entitlement)
                if entitlement.get("source") == "server_test_bypass":
                    job.is_test_order = True
            return deepcopy(job)

    def list_jobs(self, limit: int = 100) -> list:
        """列出任务，按创建时间倒序。"""
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return deepcopy(jobs[:limit])

    def list_jobs_by_creator_ip(self, creator_ip: str, limit: int = 100) -> list:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.creator_ip == creator_ip]
            jobs.sort(key=lambda j: j.created_at, reverse=True)
            return deepcopy(jobs[:limit])

    def list_jobs_by_creator_session(self, creator_session: str, limit: int = 100) -> list:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.creator_session == creator_session]
            jobs.sort(key=lambda j: j.created_at, reverse=True)
            return deepcopy(jobs[:limit])

    def list_jobs_by_batch_id(self, batch_id: str) -> list:
        with self._lock:
            jobs = [j for j in self._jobs.values() if getattr(j, "batch_id", "") == batch_id]
            return deepcopy(sorted(jobs, key=lambda j: (getattr(j, "batch_index", 0), j.created_at)))

    def _check_write_locked(self, job_id, **expectations):
        from .domain.dispatch_intent import dispatch_identity
        job = self._jobs.get(job_id)
        execution = self._executions.get(dispatch_identity(job_id, execution_identity(job))) if job else None
        return check_job_write(job, job_id, execution=execution, **expectations)

    def add_stage(self, stage: JobStage, *, expected_attempt_id=None, expected_statuses=None,
                  expected_updated_at=None) -> Optional[JobStage]:
        """记录阶段事件（内存：追加到列表；持久化：见 storage_db）。"""
        with self._lock:
            if not self._check_write_locked(stage.job_id, expected_attempt_id=expected_attempt_id,
                                           expected_statuses=expected_statuses, expected_updated_at=expected_updated_at):
                return None
            if stage.job_id not in self._stages:
                self._stages[stage.job_id] = []
            self._stages[stage.job_id].append(deepcopy(stage))
            return deepcopy(stage)

    def list_stages(self, job_id: str) -> list:
        """返回该任务的阶段事件列表，按 started_at 排序。"""
        with self._lock:
            stages = self._stages.get(job_id, [])
            return deepcopy(sorted(stages, key=lambda s: s.started_at))

    def upsert_chapter(self, chapter: JobChapter, expected_attempt_id: Optional[str] = None, *,
                       expected_statuses=None, expected_updated_at=None) -> Optional[JobChapter]:
        """写入或更新章节级任务状态（内存：按 job_id:chapter_id 覆盖）。"""
        with self._lock:
            if not self._check_write_locked(chapter.job_id, expected_attempt_id=expected_attempt_id,
                                           expected_statuses=expected_statuses, expected_updated_at=expected_updated_at):
                return None
            self._chapters[f"{chapter.job_id}:{chapter.chapter_id}"] = deepcopy(chapter)
            return deepcopy(chapter)

    def list_chapters(self, job_id: str) -> list:
        """返回该任务的章节列表，按 chapter_id 排序。"""
        with self._lock:
            out = [c for c in self._chapters.values() if c.job_id == job_id]
            return deepcopy(sorted(out, key=lambda c: c.chapter_id))

    def upsert_chunk(self, chunk: JobChunk, expected_attempt_id: Optional[str] = None, *,
                     expected_statuses=None, expected_updated_at=None) -> Optional[JobChunk]:
        """写入或更新 chunk 结果（内存：按 job_id:chunk_id 覆盖）。"""
        with self._lock:
            if not self._check_write_locked(chunk.job_id, expected_attempt_id=expected_attempt_id,
                                           expected_statuses=expected_statuses, expected_updated_at=expected_updated_at):
                return None
            self._chunks[f"{chunk.job_id}:{chunk.chunk_id}"] = deepcopy(chunk)
            return deepcopy(chunk)

    def clear_translation_progress(self, job_id: str, *, expected_attempt_id=None, expected_statuses=None,
                                   expected_updated_at=None) -> bool:
        with self._lock:
            if not self._check_write_locked(job_id, expected_attempt_id=expected_attempt_id,
                                           expected_statuses=expected_statuses, expected_updated_at=expected_updated_at):
                return False
            self._chunks = {
                k: c for k, c in self._chunks.items()
                if c.job_id != job_id
            }
            prefix = f"{job_id}:"
            self._chapters = {
                k: c for k, c in self._chapters.items()
                if not k.startswith(prefix)
            }
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
        """Atomically validate, reset, and claim a new translation attempt."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None, "missing"
            if job.status in (
                JobStatus.awaiting_confirmation,
                JobStatus.confirming,
                JobStatus.pending,
                JobStatus.running,
                JobStatus.pending_payment,
            ):
                return deepcopy(job), "active"
            if failed_only and job.status != JobStatus.failed:
                return deepcopy(job), "active"
            if expected_updated_at is not None and utc_datetime(job.updated_at) != utc_datetime(expected_updated_at):
                return deepcopy(job), "active"
            entitlement_error = restart_entitlement_reason(
                job, translation_quality=translation_quality, translation_model=translation_model,
            )
            if entitlement_error:
                return deepcopy(job), entitlement_error
            previous = dict(job.translation_stats or {})
            free_retry_count = int(previous.get("free_retry_count") or 0)
            if max_free_retries >= 0 and free_retry_count >= max_free_retries:
                return deepcopy(job), "retry_limit"

            stats = restarted_translation_stats(
                previous,
                attempt_id=attempt_id,
                started_at=started_at,
                model=translation_model or getattr(job, "translation_model", "") or "",
                max_free_retries=max_free_retries,
                action_label=action_label,
            )
            stats, intent = self._prepare_dispatch_locked(replace(job, translation_stats=stats), now=started_at)
            self._chunks = {k: c for k, c in self._chunks.items() if c.job_id != job_id}
            prefix = f"{job_id}:"
            self._chapters = {k: c for k, c in self._chapters.items() if not k.startswith(prefix)}
            if translation_quality is not None:
                job.translation_quality = translation_quality
            if cache_policy is not None:
                job.cache_policy = cache_policy
            if temperature is not None:
                job.temperature = temperature
            if translation_model is not None:
                job.translation_model = translation_model
            if translation_strategy is not None:
                job.translation_strategy = translation_strategy
            job.status = JobStatus.pending
            job.message = f"{action_label}已排队（第 {stats['translation_attempt']} 次尝试）"
            job.error_code = None
            job.output_path = None
            job.quality_stats = QualityStats()
            job.translation_stats = stats
            job.metrics_summary = ""
            job.updated_at = started_at
            self._dispatches.setdefault(intent["dispatch_id"], intent)
            return deepcopy(job), "ok"

    def begin_translation_confirmation(
        self,
        job_id: str,
        *,
        translation_strategy: str,
        bilingual: bool,
        glossary: dict[str, str],
        translation_preflight: dict[str, Any],
    ) -> Optional[Job]:
        """Atomically claim and persist a user-confirmed preflight."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != JobStatus.awaiting_confirmation:
                return None
            job.status = JobStatus.confirming
            job.message = "正在创建支付订单..."
            job.translation_strategy = translation_strategy
            job.bilingual = bool(bilingual)
            job.glossary = deepcopy(glossary)
            stats = dict(job.translation_stats or {})
            stats["translation_preflight"] = deepcopy(translation_preflight)
            job.translation_stats = stats
            job.updated_at = datetime.now(timezone.utc)
            return deepcopy(job)

    def finish_pdf_preparation(self, job_id, attempt_id, owner, prepared_plan) -> Optional[Job]:
        """Fence preparation completion and retire its execution in one lock."""
        from .domain.dispatch_intent import dispatch_identity
        from .domain.pdf_product import validate_pdf_plan, validate_pdf_job_plan
        with self._lock:
            job = self._jobs.get(job_id)
            row = self._executions.get(dispatch_identity(job_id, attempt_id))
            if (not job or job.status != JobStatus.running or execution_identity(job) != attempt_id
                    or not owner or not row or row["state"] != "running" or row["owner"] != owner):
                return None
            try:
                current = validate_pdf_plan(validate_pdf_job_plan(job), phase="preparing")
                prepared = validate_pdf_plan(prepared_plan, phase="prepared")
            except ValueError:
                return None
            if (any(current[key] != prepared[key] for key in ("source_sha256", "source_bytes", "amount"))
                    or prepared["amount"] != job.expected_amount):
                return None
            now = datetime.now(timezone.utc)
            job.translation_stats = {**(job.translation_stats or {}), "pdf_conversion": prepared}
            job.status, job.message = JobStatus.awaiting_confirmation, "PDF 已完成转换与结构校验，请确认后付款"
            job.output_path, job.error_code, job.updated_at = None, None, now
            row.update(state="finished", owner="", heartbeat_at=timestamp(now))
            return deepcopy(job)

    def confirm_pdf_conversion(self, job_id, *, plan_id, confirmed_plan, status, message,
                               payment_checkout=None, allow_test_bypass=False) -> Optional[Job]:
        """Commit a frozen checkout without a crash-stranded confirming state.

        The caller signs the local payment URL and verifies retained files
        before this CAS. Only explicit server test authority can enqueue free
        delivery; its entitlement and outbox are committed with the plan.
        """
        from .domain.pdf_product import validate_pdf_plan, pdf_plan_identity, validate_pdf_job_plan
        from .domain.payment_entitlement import manual_payment_guard, quote_entitlement
        from .domain.checkout_resume import original_checkout, checkout_created_at
        if type(allow_test_bypass) is not bool or status not in {JobStatus.pending, JobStatus.pending_payment}:
            return None
        if (status == JobStatus.pending) != allow_test_bypass:
            return None
        with self._lock:
            job = self._jobs.get(job_id)
            if (not job or job.status != JobStatus.awaiting_confirmation or manual_payment_guard(job)
                    or (job.payment_resolution or {}).get("state") in {"paid", "closed"}
                    or "payment_checkout" in (job.translation_stats or {})
                    or (allow_test_bypass and (job.is_test_order is not True or payment_checkout is not None))):
                return None
            try:
                current = validate_pdf_plan(validate_pdf_job_plan(job), phase="prepared")
                confirmed = validate_pdf_plan(confirmed_plan, phase="confirmed")
            except ValueError:
                return None
            if (current["plan_id"] != plan_id or confirmed["plan_id"] != plan_id
                    or pdf_plan_identity(current) != pdf_plan_identity(confirmed)
                    or current["amount"] != job.expected_amount):
                return None
            entitlement = deepcopy(job.payment_entitlement or {})
            if entitlement and (entitlement.get("order_no") != job.id
                                or entitlement.get("amount") != job.expected_amount
                                or entitlement.get("state") not in {"quoted", "test_authorized"}
                                or (entitlement.get("state") == "test_authorized"
                                    and (not allow_test_bypass or entitlement.get("source") != "server_test_bypass"))):
                return None
            stats = deepcopy(job.translation_stats or {})
            stats.update(pdf_conversion=confirmed, attempt_id=uuid.uuid4().hex)
            if allow_test_bypass:
                entitlement = {**entitlement, **quote_entitlement(job, test_bypass=True)}
            else:
                if payment_checkout is None:
                    return None
                stats["payment_checkout"] = deepcopy(payment_checkout)
                try:
                    candidate = replace(job, translation_stats=stats)
                    original_checkout(candidate, job.id, job.expected_amount)
                    checkout_created_at(candidate)
                except ValueError:
                    return None
            dispatch = self._prepare_dispatch_locked(replace(job, translation_stats=stats)) if allow_test_bypass else None
            if dispatch:
                stats, intent = dispatch
                self._dispatches.setdefault(intent["dispatch_id"], intent)
            job.translation_stats, job.payment_entitlement = stats, entitlement
            job.status, job.message, job.updated_at = status, message, datetime.now(timezone.utc)
            job.output_path = None
            return deepcopy(job)

    def begin_pdf_confirmation(self, job_id, *, plan_id, confirmed_plan) -> Optional[Job]:
        """Claim this exact prepared artifact; delivery gets a fresh attempt."""
        from .domain.pdf_product import validate_pdf_plan, pdf_plan_identity, validate_pdf_job_plan
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != JobStatus.awaiting_confirmation:
                return None
            try:
                current = validate_pdf_plan(validate_pdf_job_plan(job), phase="prepared")
                confirmed = validate_pdf_plan(confirmed_plan, phase="confirmed")
            except ValueError:
                return None
            if (current["plan_id"] != plan_id or confirmed["plan_id"] != plan_id
                    or pdf_plan_identity(current) != pdf_plan_identity(confirmed)
                    or current["amount"] != job.expected_amount):
                return None
            job.translation_stats = {**(job.translation_stats or {}), "pdf_conversion": confirmed,
                                     "attempt_id": uuid.uuid4().hex}
            job.status, job.message = JobStatus.confirming, "正在创建支付订单..."
            job.output_path, job.updated_at = None, datetime.now(timezone.utc)
            return deepcopy(job)

    def finish_pdf_confirmation(self, job_id, *, attempt_id, status, message,
                                payment_checkout=None) -> Optional[Job]:
        from .domain.pdf_product import validate_pdf_plan, validate_pdf_job_plan
        with self._lock:
            job = self._jobs.get(job_id)
            if (not job or job.status != JobStatus.confirming or execution_identity(job) != attempt_id
                    or status not in {JobStatus.pending, JobStatus.pending_payment}
                    or (status == JobStatus.pending and not job.is_test_order)):
                return None
            entitlement = job.payment_entitlement or {}
            if status == JobStatus.pending and not (
                    entitlement.get("state") == "test_authorized"
                    and entitlement.get("source") == "server_test_bypass"
                    and entitlement.get("order_no") == job.id
                    and entitlement.get("amount") == job.expected_amount):
                return None
            try:
                plan = validate_pdf_plan(validate_pdf_job_plan(job), phase="confirmed")
            except ValueError:
                return None
            if plan["amount"] != job.expected_amount:
                return None
            stats = deepcopy(job.translation_stats or {})
            if payment_checkout is not None:
                stats["payment_checkout"] = deepcopy(payment_checkout)
            if status == JobStatus.pending_payment:
                from .domain.checkout_resume import original_checkout, checkout_created_at
                if payment_checkout is None:
                    return None
                try:
                    candidate = replace(job, translation_stats=stats)
                    original_checkout(candidate, job.id, job.expected_amount)
                    checkout_created_at(candidate)
                except ValueError:
                    return None
            dispatch = self._prepare_dispatch_locked(replace(job, translation_stats=stats)) if status == JobStatus.pending else None
            if dispatch:
                stats, intent = dispatch
                self._dispatches.setdefault(intent["dispatch_id"], intent)
            job.translation_stats = stats
            job.status, job.message, job.updated_at = status, message, datetime.now(timezone.utc)
            return deepcopy(job)

    def rollback_pdf_confirmation(self, job_id, *, attempt_id, message) -> Optional[Job]:
        from .domain.pdf_product import validate_pdf_plan, validate_pdf_job_plan
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != JobStatus.confirming or execution_identity(job) != attempt_id:
                return None
            try:
                plan = validate_pdf_plan(validate_pdf_job_plan(job), phase="confirmed")
            except ValueError:
                return None
            if plan["amount"] != job.expected_amount:
                return None
            plan = {key: value for key, value in plan.items() if key not in {"confirmed_at", "accepted_warnings"}}
            plan["phase"] = "prepared"
            job.translation_stats = {**(job.translation_stats or {}), "pdf_conversion": plan}
            job.status, job.message, job.updated_at = JobStatus.awaiting_confirmation, message, datetime.now(timezone.utc)
            return deepcopy(job)

    def finish_translation_confirmation(
        self,
        job_id: str,
        *,
        status: JobStatus,
        message: str,
        expected_amount: str,
        payment_checkout: Optional[Dict[str, Any]] = None,
    ) -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != JobStatus.confirming:
                return None
            dispatch = self._prepare_dispatch_locked(job) if status == JobStatus.pending else None
            job.status = status
            job.message = message
            job.expected_amount = expected_amount
            job.updated_at = datetime.now(timezone.utc)
            if dispatch:
                job.translation_stats, intent = dispatch
                self._dispatches.setdefault(intent["dispatch_id"], intent)
            if payment_checkout is not None:
                job.translation_stats = {**(job.translation_stats or {}), "payment_checkout": deepcopy(payment_checkout)}
            return deepcopy(job)

    def rollback_translation_confirmation(self, job_id: str, message: str) -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != JobStatus.confirming:
                return None
            job.status = JobStatus.awaiting_confirmation
            job.message = message
            job.updated_at = datetime.now(timezone.utc)
            return deepcopy(job)

    def list_chunks(self, job_id: str, chapter_id: Optional[str] = None) -> list:
        """返回该任务（可选某章）的 chunk 列表，按 chapter_id、sequence 排序。"""
        with self._lock:
            out = [c for k, c in self._chunks.items() if c.job_id == job_id and (chapter_id is None or c.chapter_id == chapter_id)]
            return deepcopy(sorted(out, key=lambda c: (c.chapter_id, c.sequence)))

    def add_notification(self, notification: JobNotification) -> JobNotification:
        """写入站内/邮件通知记录。"""
        with self._lock:
            if any(saved.id == notification.id for saved in self._notifications):
                raise ValueError("notification id already exists")
            saved = deepcopy(notification)
            saved.created_at = utc_datetime(saved.created_at)
            self._notifications.append(saved)
            return deepcopy(saved)

    def list_notifications(self, job_id: Optional[str] = None) -> list:
        """返回通知列表，可选按 job_id 过滤，按创建时间升序。"""
        with self._lock:
            out = [n for n in self._notifications if job_id is None or n.job_id == job_id]
            return deepcopy(sorted(out, key=lambda n: n.created_at))

    def list_notification_page(self, *, job_id=None, user_id=None, limit=21, before=None) -> list[JobNotification]:
        """Public in-app page; current job ownership, never notification.user_id."""
        validate_notification_page(job_id=job_id, user_id=user_id, limit=limit, before=before)
        with self._lock:
            rows = []
            for notification in self._notifications:
                if notification.channel != "in_app" or (job_id is not None and notification.job_id != job_id):
                    continue
                if user_id is not None:
                    job = self._jobs.get(notification.job_id)
                    if job is None or job.user_id != user_id:
                        continue
                key = (utc_datetime(notification.created_at), notification.id)
                if before is None or key < before:
                    rows.append(notification)
            rows.sort(key=lambda n: (utc_datetime(n.created_at), n.id), reverse=True)
            page = deepcopy(rows[:limit])
            for notification in page:
                notification.created_at = utc_datetime(notification.created_at)
            return page

    def try_mark_paid(self, job_id: str, message: str = "支付成功，排队中...") -> bool:
        """
        将任务从 pending_payment 原子切换到 pending：
        - 返回 True 表示本次调用已原子解锁并保存投递意图
        - 返回 False 表示任务不存在 / 已被其他 webhook 处理 / 状态不符
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != JobStatus.pending_payment:
                return False
            stats, intent = self._prepare_dispatch_locked(job)
            job.status = JobStatus.pending
            job.message = message
            job.updated_at = datetime.now(timezone.utc)
            job.translation_stats = stats
            self._dispatches.setdefault(intent["dispatch_id"], intent)
            return True

    def try_mark_batch_paid(self, batch_id: str, message: str = "批次支付成功，排队中...") -> bool:
        """原子解锁整批任务；仅首个调用方获得入队权。"""
        with self._lock:
            jobs = sorted(
                [j for j in self._jobs.values() if getattr(j, "batch_id", "") == batch_id],
                key=lambda j: getattr(j, "batch_index", 0),
            )
            if not jobs or getattr(jobs[0], "batch_index", 0) != 0 or jobs[0].status != JobStatus.pending_payment:
                return False
            now = datetime.now(timezone.utc)
            pending = [(job, *self._prepare_dispatch_locked(job, now=now)) for job in jobs if job.status == JobStatus.pending_payment]
            for job, stats, intent in pending:
                job.status = JobStatus.pending
                job.message = message
                job.updated_at = now
                job.translation_stats = stats
                self._dispatches.setdefault(intent["dispatch_id"], intent)
            return True

    def list_stale_pending_payment(self, min_age_minutes: int = 30) -> list:
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=min_age_minutes)
        with self._lock:
            return deepcopy([
                j for j in self._jobs.values()
                if j.status == JobStatus.pending_payment and j.created_at < cutoff
            ])

    def list_payment_reconciliation_candidates(self, min_age_minutes: int = 30) -> list:
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=min_age_minutes)
        with self._lock:
            rows = [job for job in self._jobs.values()
                    if (job.status == JobStatus.pending_payment and job.created_at < cutoff)
                    or is_payment_expired(job)]
            return deepcopy(sorted(rows, key=lambda job: job.created_at))

    def settle_verified_payment(self, job_id: str, *, batch_id="", source="verified_webhook", amount="") -> dict:
        """Trusted receipt only: persist disposition and dispatch in one lock."""
        result = {"released": [], "review": [], "unchanged": []}
        with self._lock:
            leader = self._jobs.get(job_id)
            if not leader or (batch_id and (leader.batch_id != batch_id or leader.batch_index != 0)):
                return result
            jobs = ([leader] if not batch_id else sorted(
                (job for job in self._jobs.values() if job.batch_id == batch_id), key=lambda job: (job.batch_index, job.id)))
            now = datetime.now(timezone.utc)
            prepared = []
            for job in jobs:
                action, values = settlement_values(job, source=source, amount=amount, now=now)
                dispatch = self._prepare_dispatch_locked(replace(job, **values), now=now) if action == "released" else None
                prepared.append((job, action, values, dispatch))
            # All fallible preparation precedes the first mutation (batch rollback).
            for job, action, values, dispatch in prepared:
                for field, value in values.items():
                    setattr(job, field, value)
                if dispatch:
                    job.translation_stats, intent = dispatch
                    self._dispatches.setdefault(intent["dispatch_id"], intent)
                result[action].append(job.id)
            return result

    def mark_payment_timeout(self, job_id: str, *, gateway_confirmed=False) -> bool:
        if gateway_confirmed is not True:
            return False
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != JobStatus.pending_payment:
                return False
            for field, value in closed_values(job).items():
                setattr(job, field, value)
            return True

    def mark_batch_payment_timeout(self, batch_id: str, *, gateway_confirmed=False) -> int:
        if gateway_confirmed is not True:
            return 0
        with self._lock:
            now = datetime.now(timezone.utc)
            prepared = [(job, closed_values(job, batch=True, now=now)) for job in self._jobs.values()
                        if job.batch_id == batch_id and job.status == JobStatus.pending_payment]
            for job, values in prepared:
                for field, value in values.items():
                    setattr(job, field, value)
            return len(prepared)

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        message: str = "",
        error_code: Optional[str] = None,
        output_path: Optional[str] = None,
        quality_stats: Optional[QualityStats] = None,
        translation_stats: Optional[Dict[str, Any]] = None,
        metrics_summary: Optional[str] = None,
        allow_cancelled_transition: bool = False,
        expected_attempt_id: Optional[str] = None,
        expected_statuses=None,
        expected_updated_at=None,
    ) -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not self._check_write_locked(job_id, expected_attempt_id=expected_attempt_id,
                                           expected_statuses=expected_statuses, expected_updated_at=expected_updated_at,
                                           translation_stats=translation_stats):
                return deepcopy(job)
            if job.status == JobStatus.cancelled and status != JobStatus.cancelled and not allow_cancelled_transition:
                return deepcopy(job)
            merged_stats = translation_stats_for_status(job, status, translation_stats)
            job.status = status
            job.message = message
            job.error_code = error_code
            if output_path:
                job.output_path = output_path
            if quality_stats:
                job.quality_stats = deepcopy(quality_stats)
            if merged_stats is not None:
                job.translation_stats = deepcopy(merged_stats)
            if metrics_summary is not None:
                job.metrics_summary = metrics_summary
            job.updated_at = datetime.now(timezone.utc)
            return deepcopy(job)


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
