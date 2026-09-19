"""
支付对账 Celery 任务。

每天定时（默认凌晨 2:00）扫描所有停留在 pending_payment 超过
RECONCILE_STALE_MINUTES（默认 30）分钟的订单，主动调支付宝查单 API：

  TRADE_SUCCESS / TRADE_FINISHED  → 补发 try_mark_paid + 入队 run_conversion
  TRADE_CLOSED                    → 标记 cancelled（订单已关闭）
  WAIT_BUYER_PAY + 超过超时阈值    → 标记 cancelled（等待太久，主动关单）
  查询失败 / None                  → 跳过，下次再试

环境变量：
  RECONCILE_STALE_MINUTES   订单被视为"滞留"的最小等待时长（分，默认 30）
  RECONCILE_TIMEOUT_HOURS   多少小时后强制关单（默认 2 小时）
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from app.infra.alipay import query_verified_trade
from app.infra.celery_app import celery_app
from app.storage import job_store
from app.models import JobStatus
from app.domain.translation_attempt import attempt_id_from_stats
from app.order_events import record_event

logger = logging.getLogger("epub_factory.reconcile")

_STALE_MINUTES = int(os.environ.get("RECONCILE_STALE_MINUTES", "30"))
_TIMEOUT_HOURS = int(os.environ.get("RECONCILE_TIMEOUT_HOURS", "2"))


@celery_app.task(name="jobs.reconcile_payments", bind=True, max_retries=0)
def reconcile_payments(self) -> dict:
    """
    对账入口：扫描滞留订单 → 查支付宝 → 补偿/关单。
    bind=True 便于在日志里拿到 task_id；max_retries=0 避免失败重复运行。
    """
    stale = job_store.list_stale_pending_payment(min_age_minutes=_STALE_MINUTES)
    if not stale:
        logger.info("reconcile_payments: no stale jobs", extra={"count": 0})
        return {"checked": 0, "paid": 0, "closed": 0, "skipped": 0}

    # 一个批次会有多个 pending_payment 子任务；对账只处理 batch_index=0 的主任务。
    stale = [job for job in stale if not getattr(job, "batch_id", "") or getattr(job, "batch_index", 0) == 0]
    logger.info("reconcile_payments: start", extra={"count": len(stale)})
    paid = closed = skipped = 0
    timeout_cutoff = datetime.now(timezone.utc) - timedelta(hours=_TIMEOUT_HOURS)

    for job in stale:
        batch_id = getattr(job, "batch_id", "") or ""
        order_no = f"batch_{batch_id}" if batch_id else job.id
        trade = query_verified_trade(order_no)
        # The SDK query is signature-verified. Keep the identity guard here as
        # well so an unrelated response can never release this order.
        if not trade or trade.get("out_trade_no") != order_no:
            skipped += 1
            continue
        trade_status = trade.get("trade_status")

        if trade_status in ("TRADE_SUCCESS", "TRADE_FINISHED"):
            expected = str(getattr(job, "expected_amount", "") or "").strip()
            if not expected and not batch_id:
                # Older single-file orders predate expected_amount. Their
                # historical flat price must not inherit the new repair price.
                expected = os.environ.get("TRANSLATION_PRICE_CNY", "5.99").strip()
            if not _amount_matches(trade.get("total_amount"), expected):
                logger.warning("reconcile: paid amount mismatch", extra={"job_id": order_no})
                skipped += 1
                continue
            record_event(job_store, order_no, "payment_succeeded", "verified_query")
            _queue_paid_email(job, order_no, expected)
            if batch_id:
                _handle_batch_paid(batch_id)
            else:
                _handle_paid(job.id)
            paid += 1

        elif trade_status == "TRADE_CLOSED":
            if batch_id:
                _handle_batch_closed(batch_id, reason="支付宝已关单")
            else:
                _handle_closed(job.id, reason="支付宝已关单")
            closed += 1

        elif trade_status == "WAIT_BUYER_PAY" and job.created_at < timeout_cutoff:
            # 等待超过 TIMEOUT_HOURS 小时仍未付款，主动关单
            if batch_id:
                _handle_batch_closed(batch_id, reason=f"等待支付超过 {_TIMEOUT_HOURS} 小时，自动关单")
            else:
                _handle_closed(job.id, reason=f"等待支付超过 {_TIMEOUT_HOURS} 小时，自动关单")
            closed += 1

        else:
            # 查询失败或正常等待中，跳过本轮
            logger.info(
                "reconcile_payments: skip",
                extra={"job_id": job.id, "trade_status": trade_status},
            )
            skipped += 1

    summary = {"checked": len(stale), "paid": paid, "closed": closed, "skipped": skipped}
    logger.info("reconcile_payments: done", extra=summary)
    return summary


def _amount_matches(actual, expected: str) -> bool:
    try:
        received, frozen = Decimal(str(actual)), Decimal(expected)
        return received.is_finite() and frozen.is_finite() and received > 0 and received == frozen
    except (ValueError, InvalidOperation, TypeError):
        return False


def _queue_paid_email(job, order_no: str, amount: str) -> None:
    """Persist a verified receipt; a mail failure must not block paid work."""
    try:
        from app.domain.payment_email_service import queue_paid_order_email
        batch_id = getattr(job, "batch_id", "") or ""
        list_batch = getattr(job_store, "list_jobs_by_batch_id", None)
        jobs = (list_batch(batch_id) if batch_id and callable(list_batch) else [job]) or [job]
        queue_paid_order_email(
            order_no, amount,
            "batch" if batch_id else ("translation" if job.enable_translation else "conversion"),
            file_count=len(jobs) or 1,
            is_test_order=any(getattr(item, "is_test_order", False) for item in jobs),
        )
    except Exception:
        logger.warning("reconcile: payment email could not be queued", extra={"job_id": order_no})


def _handle_paid(job_id: str) -> None:
    """补发支付成功：try_mark_paid + 入队 run_conversion。"""
    try_mark = getattr(job_store, "try_mark_paid", None)
    won = bool(try_mark(job_id)) if callable(try_mark) else False
    if not won:
        logger.info("reconcile: already paid/processed", extra={"job_id": job_id})
        return

    logger.info("reconcile: mark paid, dispatching job", extra={"job_id": job_id})
    try:
        from app.tasks.job_pipeline import run_conversion
        job = job_store.get(job_id)
        run_conversion.delay(
            job_id,
            attempt_id_from_stats(job.translation_stats) if job and job.enable_translation else "",
        )
    except Exception as e:
        logger.error(f"reconcile: failed to dispatch job {job_id}: {e}", exc_info=True)


def _handle_batch_paid(batch_id: str) -> None:
    try_mark = getattr(job_store, "try_mark_batch_paid", None)
    if not callable(try_mark) or not try_mark(batch_id):
        logger.info("reconcile: batch already paid/processed", extra={"job_id": f"batch_{batch_id}"})
        return
    list_batch = getattr(job_store, "list_jobs_by_batch_id", None)
    jobs = list_batch(batch_id) if callable(list_batch) else []
    try:
        from app.tasks.job_pipeline import run_conversion
        for job in jobs:
            if job.status == JobStatus.pending:
                run_conversion.delay(job.id, "")
    except Exception as e:
        logger.error(f"reconcile: failed to dispatch batch {batch_id}: {e}", exc_info=True)


def _handle_closed(job_id: str, reason: str) -> None:
    """将订单标记为 cancelled。"""
    mark_timeout = getattr(job_store, "mark_payment_timeout", None)
    if callable(mark_timeout):
        mark_timeout(job_id)
    logger.info("reconcile: closed job", extra={"job_id": job_id, "reason": reason})


def _handle_batch_closed(batch_id: str, reason: str) -> None:
    mark_timeout = getattr(job_store, "mark_batch_payment_timeout", None)
    if callable(mark_timeout):
        mark_timeout(batch_id)
    logger.info("reconcile: closed batch", extra={"job_id": f"batch_{batch_id}", "reason": reason})
