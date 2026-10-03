"""
整本转换执行器：根据 job_id 从 store 加载任务并执行 convert_file_to_horizontal。

供 FastAPI 进程内（BackgroundTasks）与 Celery Worker 共用；
Worker 使用时需配置持久化 store（DATABASE_URL），否则无法加载 job。
"""

import logging
import os
import re
import shutil
import tempfile
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from billiard.exceptions import SoftTimeLimitExceeded

from .cancellation import JobCancelled, raise_if_cancelled
from .converter import converter
from .domain.notification_service import notify_job_completed
from .domain.status_resolver import resolve_after_conversion
from .domain.translation_qa_service import attach_translation_qa_report, audit_translated_epub_output, build_translation_qa_report
from .domain.translation_attempt import attempt_id_from_stats, initial_translation_stats
from .error_reporter import report_error
from .models import ErrorCode, JobStage, JobStatus, OutputMode, StageStatus
from .storage import job_store
from .infra.execution_lease import execution_lease, execution_identity, ExecutionLeaseLost, ExecutionLeaseBusy
from .infra.execution_heartbeat import ExecutionHeartbeat
from .infra.llm_errors import ProviderAccountUnavailable
from .infra.llm_usage_ledger import usage_scope, AccountingError
from .infra.llm_gateway import GatewayControlError
from .domain.translation_residual_policy import confirmed_preserved_terms
from .domain.payment_entitlement import precision_polish_entitlement_reason
from .domain.translation_input import (
    normalized_translation_input, ensure_translation_executor_available,
    validate_translation_filename, TranslationInputError,
)
from .domain.job_write_fence import job_write_scope, JobWriteConflict
from .domain.pdf_product import (is_pdf_job, validate_pdf_job, prepare_pdf_artifact,
                                 copy_prepared_pdf, PdfProductError)

logger = logging.getLogger("epub_factory")

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _build_output_suffix(job) -> str:
    """
    生成输出文件名的可读后缀，命名遵循"轻量、可读、可还原"原则：

    - 仅转换繁体 → "繁体"
    - 仅转换简体 → "简体"
    - 翻译 → 在转换后缀基础上追加 "_翻译_{lang}"，双语模式追加 "_双语"

    例：
      原文件名：百年孤寂.epub
      简体输出：百年孤寂_简体.epub
      简翻英：  百年孤寂_简体_翻译_en.epub
      简翻英双语：百年孤寂_简体_翻译_en_双语.epub
    """
    if is_pdf_job(job):
        return "原文"
    parts: list[str] = []
    if job.output_mode == OutputMode.traditional:
        parts.append("繁体")
    else:
        parts.append("简体")
    if job.enable_translation:
        parts.append(f"翻译_{job.target_lang}")
        if job.bilingual:
            parts.append("双语")
    return "_".join(parts)


def _safe_output_stem(stem: str) -> str:
    stem = re.sub(r'[\\/:*?"<>|]+', "_", (stem or "").strip())
    stem = re.sub(r"\s+", " ", stem).strip(" ._")
    return stem[:80] or "output"


def _unique_output_path(path: Path) -> Path:
    if not path.exists():
        return path
    base = path.with_suffix("")
    suffix = path.suffix
    for i in range(2, 100):
        candidate = Path(f"{base}_{i}{suffix}")
        if not candidate.exists():
            return candidate
    return Path(f"{base}_{datetime.now().strftime('%Y%m%d%H%M%S')}{suffix}")


def _rename_output_with_translated_title(job, result, output_path: Path, suffix: str) -> Path:
    if not getattr(job, "enable_translation", False):
        return output_path
    stats = getattr(result, "translation_stats", None) or {}
    translated_title = (stats.get("book_title_translated") or "").strip()
    original_title = (stats.get("book_title_original") or "").strip()
    if not translated_title or translated_title == original_title:
        return output_path
    new_path = _unique_output_path(output_path.parent / f"{_safe_output_stem(translated_title)}_{suffix}.epub")
    if output_path.exists() and new_path != output_path:
        output_path.replace(new_path)
        return new_path
    return output_path


def _finalize_attempt_output(job, result, working_path: Path, default_path: Path, suffix: str) -> Path:
    """Choose a readable name *inside* this executor's private directory.

    A filename is not publication: only the fenced success transaction exposes
    its path. Never replace an artifact belonging to a different execution.
    """
    translated_path = _rename_output_with_translated_title(job, result, working_path, suffix)
    if translated_path != working_path:
        return translated_path
    if working_path.name == default_path.name:
        return working_path
    final_path = _unique_output_path(working_path.parent / default_path.name)
    if working_path.exists() and working_path != final_path:
        working_path.replace(final_path)
    return final_path


def _apply_final_artifact_audit(job, result, output_path: Path) -> None:
    if not getattr(job, "enable_translation", False):
        return
    # A previous stage already failed; do not replace its useful error with a
    # secondary "missing output" error or hide EPUBCheck failures.
    if not result.validation_passed:
        return
    audit = audit_translated_epub_output(
        output_path, target_lang=getattr(job, "target_lang", "zh-CN"),
        bilingual=bool(getattr(job, "bilingual", False)),
        preserved_terms=confirmed_preserved_terms(getattr(job, "glossary", {})),
    )
    stats = dict(getattr(result, "translation_stats", {}) or {})
    stats["artifact_audit"] = audit
    qa = build_translation_qa_report(
        translation_stats=stats, output_path=output_path,
        error_code=getattr(result, "error_code", None),
    )
    stats["qa_report"] = qa
    stats["deliverable"] = qa["can_deliver"]
    stats["delivery_gate_failed"] = qa["delivery_status"] == "failed"
    result.translation_stats = stats

    if qa["delivery_status"] != "failed":
        return

    residual = int(audit.get("residual_blocks") or 0)
    checked = int(audit.get("checked_text_blocks") or 0)
    stats["delivery_gate_failed"] = True
    stats["deliverable"] = False
    result.translation_stats = stats
    result.error_code = ErrorCode.PARTIAL_TRANSLATION.value
    result.validation_passed = False
    if audit.get("status") == "scan_error":
        result.message = (
            f"翻译交付质检执行失败：{audit.get('reason') or '无法读取成品 EPUB'}。"
            "已停止交付，请重新翻译或联系管理员。"
        )
    elif residual:
        result.message = (
            f"翻译交付质检未通过：成品 EPUB 仍有 {residual}/{checked} "
            "个正文段落疑似未翻译。已停止交付，请重新翻译。"
        )
    else:
        result.message = f"翻译交付质检未通过：{qa['summary']}。已停止交付，请查看失败诊断。"


def _run_precision_polish_stage(job, result, output_path, *, cancel_check, stage_callback, persist_stats):
    """A paid add-on is a required, independently audited delivery stage."""
    from .domain.precision_polish_service import PrecisionPolishError, run_precision_polish

    base_stats = dict(job.translation_stats or {})
    base_stats.update(dict(result.translation_stats or {}))
    quote = dict((job.translation_stats or {}).get("precision_polish") or {})
    polished_path = output_path.with_name(output_path.stem + ".polished.epub")

    def publish(snapshot):
        # Runtime counters may never replace the frozen commercial terms.
        precision = {**quote, **snapshot}
        for key in ("quoted_amount", "char_count", "order_no"):
            if key in quote:
                precision[key] = quote[key]
        base_stats["precision_polish"] = precision
        job.translation_stats = dict(base_stats)
        result.translation_stats = dict(base_stats)
        persist_stats(dict(base_stats))
        return precision

    if not result.validation_passed:
        publish({"status": "failed", "reason": "conversion_validation_failed",
                 "refund_required": not job.is_test_order, "validation_passed": False})
        return
    started = time.monotonic()
    stage_callback("precision_polish", "开始 AI 精校：按上下文检查风险词，不改写正文", None)
    publish({"status": "running", "api_calls": 0, "reviewed": 0, "changed": 0})
    try:
        stats = run_precision_polish(output_path, polished_path, cancel_check=cancel_check,
                                     stats_callback=publish)
        if stats.get("status") == "no_candidates":
            # New quotes reject zero candidates. A historical paid order that
            # reaches this state still needs fee review, not a success claim.
            raise PrecisionPolishError("no_candidates", "没有可完成精校的正文风险段",
                                       stats={**stats, "status": "no_candidates"})
        if stats.get("status") != "completed" or not stats.get("validation_passed"):
            raise PrecisionPolishError("incomplete_review", "精校未完成或最终校验未通过",
                                       stats={**stats, "status": "failed"})
        raise_if_cancelled(cancel_check)
        if not polished_path.is_file():
            raise PrecisionPolishError("missing_polished_output", "精校成品不存在",
                                       stats={**stats, "status": "failed"})
        polished_path.replace(output_path)
        final = publish({**stats, "refund_required": False})
        elapsed = int((time.monotonic() - started) * 1000)
        message = f"AI 精校已检查 {final.get('reviewed', 0)} 段，修改 {final.get('changed', 0)} 段"
        stage_callback("precision_polish_completed", message, elapsed)
        result.message = f"转换完成；{message}"
    except PrecisionPolishError as exc:
        failure = {**getattr(exc, "stats", {}), "reason": getattr(exc, "reason", str(exc)),
                   "refund_required": not job.is_test_order, "validation_passed": False}
        if failure.get("status") != "no_candidates":
            failure["status"] = "failed"
        publish(failure)
        result.validation_passed = False
        result.error_code = ErrorCode.PRECISION_POLISH_FAILED.value
        result.message = "AI 精校未完成，已停止交付；精校费用请联系客服核验，未自动退款。"
        stage_callback("precision_polish_failed", result.message, int((time.monotonic() - started) * 1000))
    finally:
        polished_path.unlink(missing_ok=True)


def _convert_filename_stem_for_mode(stem: str, output_mode: OutputMode, traditional_variant: str) -> str:
    """
    根据输出模式对文件名主体做繁简转换，保证下载文件名与正文方向一致。

    - simplified: t2s / tw2s / hk2s
    - traditional: s2t / s2tw / s2hk
    """
    if not stem:
        return stem

    variant = (traditional_variant or "auto").lower()
    simplified_profiles = {"auto": "t2s", "tw": "tw2s", "hk": "hk2s"}
    traditional_profiles = {"auto": "s2t", "tw": "s2tw", "hk": "s2hk"}

    if output_mode == OutputMode.simplified:
        profile = simplified_profiles.get(variant, "t2s")
    elif output_mode == OutputMode.traditional:
        profile = traditional_profiles.get(variant, "s2t")
    else:
        return stem

    try:
        from opencc import OpenCC
        return OpenCC(profile).convert(stem)
    except Exception as exc:
        logger.warning("filename stem convert failed, fallback to original: %s", exc)
        return stem


def run_job(job_id: str, expected_attempt_id: str | None = None, *, retry_if_busy: bool = False) -> None:
    """Ignore stale/terminal deliveries and admit one executor per attempt."""
    job = job_store.get(job_id)
    if not job:
        return
    if job.status != JobStatus.pending:
        return
    current_attempt = attempt_id_from_stats(job.translation_stats)
    # None is an old caller without a captured identity. An explicitly empty
    # first-conversion identity must not adopt a newer retry's nonempty ID.
    if expected_attempt_id is not None and expected_attempt_id != current_attempt:
        return
    identity = execution_identity(job)
    with execution_lease(job_id, identity) as lease:
        if lease is None:
            logger.info("duplicate job delivery ignored", extra={"job_id": job_id})
            if retry_if_busy:
                raise ExecutionLeaseBusy("同一次翻译已被执行器占用，延后核验，不重复执行")
            return
        # None permits a legacy caller to adopt the FIRST observed attempt,
        # not a different attempt created while acquiring this attempt's lock.
        # Otherwise the runner and recovery scanner would hold different keys.
        _run_job_locked(job_id, current_attempt, lease)


def _run_job_locked(job_id: str, expected_attempt_id: str | None, lease) -> None:
    """从 store 加载 job 并执行整本转换，更新状态与输出路径。"""
    job = job_store.get(job_id)
    if not job:
        logger.warning("run_job: job not found", extra={"job_id": job_id})
        return
    if job.status != JobStatus.pending:
        logger.info("run_job: job not executable", extra={"job_id": job_id})
        return
    current_attempt = attempt_id_from_stats(job.translation_stats)
    if expected_attempt_id is not None and expected_attempt_id != current_attempt:
        logger.info("run_job: stale queued attempt ignored", extra={"job_id": job_id})
        return
    now_utc = datetime.now(timezone.utc)
    translation_stats = None
    attempt_id = current_attempt
    if getattr(job, "enable_translation", False) or getattr(job, "enable_precision_polish", False):
        existing_stats = dict(getattr(job, "translation_stats", {}) or {})
        existing_attempt_id = attempt_id_from_stats(existing_stats)
        if expected_attempt_id and existing_attempt_id and expected_attempt_id != existing_attempt_id:
            logger.info(
                "run_job: stale translation attempt ignored",
                extra={"job_id": job.id, "expected_attempt_id": expected_attempt_id},
            )
            return
        stats = initial_translation_stats(existing_stats)
        if not existing_attempt_id:
            # Older AI jobs have no attempt. Persist the same deterministic
            # identity used to acquire their lease; never switch to a random key.
            stats["attempt_id"] = execution_identity(job)
        stats.setdefault("attempt_started_at", now_utc.isoformat())
        attempt_id = expected_attempt_id or attempt_id_from_stats(stats)
        stats["attempt_id"] = attempt_id
        translation_stats = stats
        job.translation_stats = stats
        if not existing_attempt_id:
            initialized = job_store.update_status(
                job.id,
                job.status,
                job.message,
                translation_stats=stats,
                expected_attempt_id="",
                expected_statuses={JobStatus.pending},
            )
            if (not initialized or initialized.status != JobStatus.pending
                    or attempt_id_from_stats(initialized.translation_stats) != attempt_id):
                logger.info("legacy attempt initialization rejected", extra={"job_id": job.id})
                return

    lease.assert_owned()
    if not job_store.begin_execution(job.id, attempt_id, lease.owner):
        logger.info("job execution admission rejected", extra={"job_id": job.id})
        return
    heartbeat = ExecutionHeartbeat(job_store, job.id, attempt_id, lease)
    try:
        heartbeat.start()
        lease.assert_owned()
        with job_write_scope(job.id, attempt_id, lease.owner):
            try:
                _execute_admitted_job(job, attempt_id, translation_stats, lease)
            except JobWriteConflict:
                # Includes conflicts raised from an error/cancellation handler.
                # They are not a fresh cancellation and must never write back.
                logger.info("obsolete executor stopped writing", extra={"job_id": job.id})
    finally:
        heartbeat.stop()
        # Soft timeout/lost process is deliberately still running. Only a real
        # terminal transition may finalize metadata; the scanner owns recovery.
        try:
            current = job_store.get(job.id)
            if current and current.status in {JobStatus.success, JobStatus.failed, JobStatus.cancelled}:
                job_store.finish_execution(job.id, attempt_id, lease.owner)
        except Exception as exc:
            logger.warning("Execution metadata finalization unavailable (%s)", type(exc).__name__,
                           extra={"job_id": job.id})


def _execute_admitted_job(job, attempt_id: str, translation_stats, lease) -> None:
    def update_job_status(status: JobStatus, message: str = "", **kwargs):
        lease.assert_owned()
        return job_store.update_status(
            job.id,
            status,
            message,
            expected_attempt_id=attempt_id,
            **kwargs,
        )

    logger.info("job started", extra={"trace_id": job.trace_id, "job_id": job.id})
    update_job_status(
        JobStatus.running,
        "开始转换",
        translation_stats=translation_stats,
    )
    output_path: Path | None = None
    output_directory: Path | None = None
    artifact_committed = False
    publication_attempted = False
    try:
        pdf_job = is_pdf_job(job)
        pdf_plan = validate_pdf_job(job, OUTPUT_DIR) if pdf_job else None
        if pdf_job and (not attempt_id or pdf_plan["phase"] not in {"preparing", "confirmed"}):
            raise PdfProductError("invalid_plan")
        if job.enable_translation:
            ensure_translation_executor_available()
            validate_translation_filename(job.input_path)
        if getattr(job, "enable_precision_polish", False):
            reason = precision_polish_entitlement_reason(job)
            if reason:
                raise RuntimeError("AI 精校付款权益未核验或与原订单不符，已停止执行，请联系管理员核验。")
            if Path(job.input_path).suffix.lower() != ".epub":
                raise RuntimeError("AI 精校目前仅支持 EPUB 的普通简体转换")
        source_name_raw = Path(job.source_filename).stem
        source_name = _safe_output_stem(source_name_raw) if pdf_job else _convert_filename_stem_for_mode(
            source_name_raw,
            job.output_mode,
            getattr(job, "traditional_variant", "auto") or "auto",
        )
        suffix = _build_output_suffix(job)
        default_output_path = OUTPUT_DIR / f"{source_name}_{suffix}.epub"
        # This also isolates ordinary conversions whose first attempt is "".
        # mkdtemp exclusively creates the directory; no old executor can replace
        # a new executor's bytes even when its final status write is rejected.
        output_directory = Path(tempfile.mkdtemp(prefix=".execution-", dir=OUTPUT_DIR))
        output_path = output_directory / default_output_path.name

        last_progress_event: str | None = None
        last_progress_recorded_at = 0.0

        def record_stage(
            stage_name: str,
            message: str,
            elapsed_ms: Optional[int] = None,
            *,
            level: str = "info",
        ) -> None:
            lease.assert_owned()
            if not getattr(job_store, "add_stage", None):
                return
            now = datetime.now(timezone.utc)
            stage = JobStage(
                job_id=job.id,
                stage_name=stage_name,
                status=StageStatus.completed,
                started_at=now,
                finished_at=now,
                elapsed_ms=elapsed_ms,
                metadata={
                    "message": message,
                    "level": level,
                    "attempt_id": attempt_id,
                    "execution_owner": lease.owner,
                },
            )
            job_store.add_stage(stage)

        def is_cancelled() -> bool:
            lease.assert_owned()
            current = job_store.get(job.id)
            if not current:
                return True
            if current.status != JobStatus.running:
                return True
            if attempt_id_from_stats(current.translation_stats) != attempt_id:
                return True
            execution = job_store.get_execution(job.id, attempt_id)
            if (not execution or execution.get("state") != "running"
                    or execution.get("owner") != lease.owner):
                raise JobWriteConflict("执行器归属已变化，停止旧执行器")
            return False

        def check_cancelled() -> None:
            raise_if_cancelled(is_cancelled)

        def on_progress(msg: str) -> None:
            nonlocal last_progress_event, last_progress_recorded_at
            check_cancelled()
            update_job_status(JobStatus.running, msg)
            if msg and msg != last_progress_event:
                last_progress_event = msg
                now = time.monotonic()
                if msg.startswith("快速翻译 ") and now - last_progress_recorded_at < 5.0:
                    return
                last_progress_recorded_at = now
                record_stage("progress", msg)

        def on_stage(stage_name: str, message: str, elapsed_ms: Optional[int] = None) -> None:
            check_cancelled()
            level = "error" if "fail" in stage_name or "failed" in stage_name else "info"
            record_stage(stage_name, message, elapsed_ms, level=level)

        check_cancelled()
        if pdf_job and pdf_plan["phase"] == "preparing":
            on_stage("pdf_preparing", "正在准备保留原文的 EPUB；此阶段不创建支付订单")
            prepared = prepare_pdf_artifact(job, OUTPUT_DIR, cancel_check=is_cancelled)
            check_cancelled()
            on_stage("pdf_prepared", "PDF 已完成转换及 EPUB 校验，等待确认")
            # This commits a private plan and retires this preparation executor
            # together. Never assign output_path or announce paid completion.
            # On an ambiguous commit failure retain the private prepared file;
            # it is not inside output_directory and cannot be deleted below.
            saved = job_store.finish_pdf_preparation(job.id, attempt_id, lease.owner, prepared)
            if saved is None:
                raise JobWriteConflict("PDF 预备结果已被新状态取代，旧执行器停止提交")
            return
        input_path = Path(job.input_path)
        if input_path.suffix.lower() in [".mobi", ".azw3"]:
            on_progress(f"正在将 {input_path.suffix.upper()[1:]} 格式转换为 EPUB...")
            on_stage("format_convert", f"开始转换 {input_path.suffix} 到 epub")
            start_time = datetime.now()
            import subprocess
            temp_epub = input_path.with_suffix(".epub")
            try:
                subprocess.run(["ebook-convert", str(input_path), str(temp_epub)], check=True, capture_output=True)
                input_path = temp_epub
                elapsed = int((datetime.now() - start_time).total_seconds() * 1000)
                on_stage("format_convert", "格式转换完成", elapsed_ms=elapsed)
                check_cancelled()
            except subprocess.CalledProcessError as e:
                err_msg = e.stderr.decode("utf-8", errors="ignore")
                raise RuntimeError(f"格式转换失败，可能受 DRM 保护或格式损坏。详情: {err_msg[:200]}")
            except FileNotFoundError:
                raise RuntimeError("服务器未安装 ebook-convert (Calibre)，无法转换此格式。")

        check_cancelled()
        accounting = usage_scope(job.id, attempt_id or "conversion", engine=getattr(job_store, "_engine", None),
                                 existing_stats=job.translation_stats) if (
                                     job.enable_translation or getattr(job, "enable_precision_polish", False)) else nullcontext()
        with accounting:
            if pdf_job:
                on_stage("pdf_delivery", "核对并交付已确认的原文 EPUB，不重新解析 PDF")
                result = copy_prepared_pdf(job, output_path, OUTPUT_DIR, cancel_check=is_cancelled)
            elif job.enable_translation:
                from .domain.fast_translation_runner import run_fast_translation_job
                on_stage("normalizing_input", "统一解析翻译输入，保留已确认的翻译设定")
                with normalized_translation_input(
                    input_path, source_name=job.source_filename, cancel_check=is_cancelled,
                ) as normalized:
                    previous_input = (job.translation_stats or {}).get("translation_input") or {}
                    if previous_input.get("source_sha256") and previous_input["source_sha256"] != normalized.source_sha256:
                        raise TranslationInputError("source_changed", "原文件已变化，请重新上传并确认翻译设定")
                    job.translation_stats = {
                        **dict(job.translation_stats or {}),
                        "translation_input": {
                            "version": normalized.normalization_version, "adapter": normalized.adapter,
                            "source_sha256": normalized.source_sha256,
                        },
                        "source_warnings": list(dict.fromkeys([
                            *((job.translation_stats or {}).get("source_warnings") or []),
                            *normalized.source_warnings,
                        ])),
                    }
                    update_job_status(JobStatus.running, "翻译输入解析完成", translation_stats=job.translation_stats)
                    result = run_fast_translation_job(
                        job=job, input_path=normalized.epub_path, output_path=output_path,
                        progress_callback=on_progress, stage_callback=on_stage, cancel_check=is_cancelled,
                    )
            else:
                result = converter.convert_file_to_horizontal(
                    input_path,
                    output_path,
                    job.output_mode,
                    enable_translation=job.enable_translation,
                    target_lang=job.target_lang,
                    device=job.device.value,
                    bilingual=job.bilingual,
                    glossary=job.glossary or None,
                    temperature=getattr(job, "temperature", None),
                    translation_model=getattr(job, "translation_model", None),
                    traditional_variant=getattr(job, "traditional_variant", "auto") or "auto",
                    lexicon_domains=getattr(job, "lexicon_domains", None),
                    enable_proper_noun=getattr(job, "enable_proper_noun", True),
                    progress_callback=on_progress,
                    stage_callback=on_stage,
                )
            if getattr(job, "enable_precision_polish", False):
                _run_precision_polish_stage(
                    job, result, output_path, cancel_check=is_cancelled, stage_callback=on_stage,
                    persist_stats=lambda stats: update_job_status(
                        JobStatus.running, "正在执行 AI 精校", translation_stats=stats),
                )
        check_cancelled()
        if job.enable_translation or getattr(job, "enable_precision_polish", False):
            current_attempt_stats = dict(job.translation_stats or {})
            current_attempt_stats.update(dict(getattr(result, "translation_stats", {}) or {}))
            result.translation_stats = current_attempt_stats
        _apply_final_artifact_audit(job, result, output_path)
        if (
            job.enable_translation
            and result.error_code == ErrorCode.PARTIAL_TRANSLATION.value
            and not getattr(result, "validation_passed", True)
        ):
            on_stage("translation_quality_gate_failed", result.message or "翻译交付质检未通过")
        status, message, error_code = resolve_after_conversion(result)
        if status != JobStatus.failed:
            check_cancelled()
            output_path = _finalize_attempt_output(
                job,
                result,
                output_path,
                default_output_path,
                suffix,
            )
        if job.enable_translation:
            qa_output_path = (
                None
                if error_code == ErrorCode.PARTIAL_TRANSLATION.value and status == JobStatus.failed
                else output_path
            )
            result.translation_stats = attach_translation_qa_report(
                result.translation_stats,
                output_path=qa_output_path,
                error_code=error_code,
            )
        if status == JobStatus.failed:
            on_stage("failed", message or "任务失败")
            update_job_status(
                status,
                message,
                error_code=error_code,
                quality_stats=result.quality_stats,
                translation_stats=result.translation_stats,
                metrics_summary=result.metrics_summary,
            )
            report_error(
                error_code=error_code or ErrorCode.CONVERT_FAILED,
                message=message,
                job_id=job.id,
                trace_id=job.trace_id,
                context={"source_filename": job.source_filename},
            )
            notify_job_completed(
                job.id, status, message,
                error_code=error_code,
                source_filename=job.source_filename,
            )
            logger.warning(
                "job validation failed",
                extra={"trace_id": job.trace_id, "job_id": job.id},
            )
            return
        publication_attempted = True
        update_job_status(
            status,
            message,
            output_path=str(output_path),
            error_code=error_code,
            quality_stats=result.quality_stats,
            translation_stats=result.translation_stats,
            metrics_summary=result.metrics_summary,
        )
        artifact_committed = True
        notify_job_completed(
            job.id, status, message,
            error_code=error_code,
            output_path=str(output_path),
            source_filename=job.source_filename,
        )
        logger.info("job success", extra={"trace_id": job.trace_id, "job_id": job.id})
    except SoftTimeLimitExceeded:
        # Preserve current progress/checkpoints and the running execution row.
        # Bounded durable recovery, not a new purchase/manual restart, resumes it.
        logger.warning("job soft time limit reached; awaiting durable recovery", extra={"job_id": job.id})
        raise
    except ExecutionLeaseLost:
        logger.error("execution lease lost; old executor stopped", extra={"job_id": job.id})
        # Never overwrite a new owner's status or artifact after losing ownership.
        raise
    except JobWriteConflict:
        raise
    except JobCancelled as exc:
        current = job_store.get(job.id)
        if current and (current.status != JobStatus.running
                        or attempt_id_from_stats(current.translation_stats) != attempt_id):
            logger.info(
                "job attempt superseded",
                extra={"trace_id": job.trace_id, "job_id": job.id, "attempt_id": attempt_id},
            )
            return
        message = str(exc) or "用户已停止翻译"
        if getattr(job_store, "add_stage", None):
            now = datetime.now(timezone.utc)
            job_store.add_stage(JobStage(
                job_id=job.id,
                stage_name="cancelled",
                status=StageStatus.completed,
                started_at=now,
                finished_at=now,
                metadata={
                    "message": message,
                    "level": "warning",
                    "attempt_id": attempt_id,
                    "execution_owner": lease.owner,
                },
            ))
        cancelled_stats = None
        if getattr(job, "enable_precision_polish", False):
            cancelled_stats = dict(job.translation_stats or {})
            cancelled_stats["precision_polish"] = {
                **dict(cancelled_stats.get("precision_polish") or {}), "status": "cancelled",
                "reason": "cancelled", "validation_passed": False,
                "refund_required": not job.is_test_order,
            }
        update_job_status(JobStatus.cancelled, message,
                          **({"translation_stats": cancelled_stats} if cancelled_stats is not None else {}))
        notify_job_completed(
            job.id,
            JobStatus.cancelled,
            message,
            source_filename=job.source_filename,
        )
        logger.info("job cancelled", extra={"trace_id": job.trace_id, "job_id": job.id})
    except (AccountingError, GatewayControlError, Exception) as exc:
        current = job_store.get(job.id)
        if current and (current.status != JobStatus.running
                        or attempt_id_from_stats(current.translation_stats) != attempt_id):
            logger.info(
                "stale job failure ignored",
                extra={"trace_id": job.trace_id, "job_id": job.id, "attempt_id": attempt_id},
            )
            return
        message = str(exc)
        error_code = ErrorCode.CONVERT_FAILED
        failure_stats = None
        if isinstance(exc, AccountingError):
            error_code = ErrorCode.TRANSLATION_FAILED
        if isinstance(exc, GatewayControlError):
            error_code = ErrorCode.TRANSLATION_FAILED if job.enable_translation else ErrorCode.CONVERT_FAILED
            failure_stats = dict(getattr(current, "translation_stats", {}) or job.translation_stats or {})
            failure_stats.update(model_governor_blocked=True, last_error=message, live=False, deliverable=False)
        if isinstance(exc, TranslationInputError) and job.enable_translation:
            error_code = ErrorCode.TRANSLATION_FAILED
        if isinstance(exc, ProviderAccountUnavailable):
            error_code = ErrorCode.TRANSLATION_PROVIDER_UNAVAILABLE
            failure_stats = dict(getattr(current, "translation_stats", {}) or job.translation_stats or {})
            failure_stats.update(provider_blocked=True, provider_error=exc.reason,
                                 blocked_provider=exc.provider, last_error=message,
                                 live=False, deliverable=False)
            failure_stats = attach_translation_qa_report(failure_stats, error_code=error_code.value)
        if "AI 翻译失败" in message or "翻译流程未完成" in message:
            error_code = ErrorCode.TRANSLATION_FAILED
        if getattr(job, "enable_precision_polish", False):
            error_code = ErrorCode.PRECISION_POLISH_FAILED
            failure_stats = dict(failure_stats if failure_stats is not None else
                                 (getattr(current, "translation_stats", {}) or job.translation_stats or {}))
            failure_stats["precision_polish"] = {
                **dict(failure_stats.get("precision_polish") or {}), "status": "failed",
                "reason": type(exc).__name__, "validation_passed": False,
                "refund_required": not job.is_test_order,
            }
        if getattr(job_store, "add_stage", None):
            now = datetime.now(timezone.utc)
            job_store.add_stage(JobStage(
                job_id=job.id,
                stage_name="failed",
                status=StageStatus.completed,
                started_at=now,
                finished_at=now,
                metadata={
                    "message": message or "任务失败",
                    "level": "error",
                    "attempt_id": attempt_id,
                    "execution_owner": lease.owner,
                },
            ))
        update_job_status(JobStatus.failed, message, error_code=error_code,
                          **({"translation_stats": failure_stats} if failure_stats is not None else {}))
        report_error(
            error_code=error_code,
            message=message,
            job_id=job.id,
            trace_id=job.trace_id,
            context={"source_filename": job.source_filename},
        )
        notify_job_completed(
            job.id, JobStatus.failed, message,
            error_code=error_code,
            source_filename=job.source_filename,
        )
        logger.exception(
            "job failed",
            extra={"trace_id": job.trace_id, "job_id": job.id},
        )
    finally:
        if output_directory is not None and not artifact_committed:
            remove_private_directory = True
            if publication_attempted:
                # commit may have succeeded even if refresh/transport/soft-timeout
                # raised before update_status returned. Never delete a possibly
                # committed artifact; an unavailable DB leaves a private orphan,
                # not a successful job with a missing download.
                try:
                    published = job_store.get(job.id)
                    remove_private_directory = not (
                        published and published.output_path and output_path
                        and Path(published.output_path) == output_path
                    )
                except Exception:
                    remove_private_directory = False
                    logger.warning("output commit uncertain; private artifact retained", extra={"job_id": job.id})
            if remove_private_directory:
                shutil.rmtree(output_directory, ignore_errors=True)
