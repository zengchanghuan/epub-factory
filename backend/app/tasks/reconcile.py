"""
支付对账 Celery 任务。

每天定时（默认凌晨 2:00）扫描滞留未付与系统到期关闭订单，主动查单：

  TRADE_SUCCESS / TRADE_FINISHED  → 原子结算并补发，用户取消则人工处理
  TRADE_CLOSED                    → 以已验证网关结果关闭本地待付款任务
  WAIT_BUYER_PAY + 超过超时阈值    → 请求网关关单，再查单优先处理竞态付款
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

from app.infra.alipay import query_verified_trade, close_verified_trade
from app.infra.celery_app import celery_app
from app.storage import job_store
from app.models import JobStatus
from app.domain.job_dispatch_service import dispatch_pending
from app.domain.payment_entitlement import grant_verified_entitlement
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
    # Complement the API-side retry worker without requiring another payment
    # query, and without manufacturing intents for arbitrary pending jobs.
    dispatch_pending(job_store, _publish_conversion)
    stale = job_store.list_payment_reconciliation_candidates(min_age_minutes=_STALE_MINUTES)
    if not stale:
        logger.info("reconcile_payments: no stale jobs", extra={"count": 0})
        return {"checked": 0, "paid": 0, "closed": 0, "skipped": 0}

    # A cancelled leader does not erase still-unpaid/expired children. Resolve
    # one frozen aggregate order whenever any child needs reconciliation.
    orders, batches = [], set()
    for job in stale:
        batch_id = getattr(job, "batch_id", "") or ""
        if not batch_id:
            orders.append(job)
        elif batch_id not in batches:
            batches.add(batch_id)
            leader = next((row for row in job_store.list_jobs_by_batch_id(batch_id)
                           if row.batch_index == 0), None)
            if leader is not None:
                orders.append(leader)
    stale = orders
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

        close_confirmed = False
        # SQLite reloads DateTime columns without tzinfo, while PostgreSQL and
        # the in-memory store preserve it. Persisted naive values are UTC.
        created_at_utc = (job.created_at.astimezone(timezone.utc) if job.created_at.tzinfo
                          else job.created_at.replace(tzinfo=timezone.utc))
        if trade_status == "WAIT_BUYER_PAY" and created_at_utc < timeout_cutoff:
            # The gateway, not a local clock, decides whether closing won the
            # race with payment. Unknown/failed close is never a closed order.
            receipt = close_verified_trade(order_no)
            close_confirmed = bool(receipt and receipt.get("out_trade_no") == order_no)
            latest = query_verified_trade(order_no)
            if latest and latest.get("out_trade_no") == order_no:
                trade = latest
                trade_status = trade.get("trade_status")
            elif not close_confirmed:
                skipped += 1
                continue

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
            if (job.enable_translation or getattr(job, "enable_precision_polish", False)) and not batch_id:
                grant_verified_entitlement(job_store, job, trade["total_amount"], "verified_query")
            record_event(job_store, order_no, "payment_succeeded", "verified_query")
            _queue_paid_email(job, order_no, expected)
            if batch_id:
                _handle_batch_paid(batch_id, amount=expected)
            else:
                _handle_paid(job.id, amount=expected)
            paid += 1

        elif trade_status == "TRADE_CLOSED" or close_confirmed:
            if batch_id:
                _handle_batch_closed(batch_id, reason="支付宝已关单")
            else:
                _handle_closed(job.id, reason="支付宝已关单")
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


def _publish_conversion(job_id: str, expected_attempt_id: str) -> None:
    from app.infra.job_dispatch_publisher import publish_conversion
    publish_conversion(job_id, expected_attempt_id)


def _handle_paid(job_id: str, *, amount="") -> None:
    """Verified payment release and durable per-attempt dispatch."""
    job_store.settle_verified_payment(job_id, amount=amount, source="verified_query")
    job_store.ensure_dispatch(job_id)
    dispatch_pending(job_store, _publish_conversion, job_id=job_id)


def _handle_batch_paid(batch_id: str, *, amount="") -> None:
    jobs = job_store.list_jobs_by_batch_id(batch_id)
    leader = next((job for job in jobs if job.batch_index == 0), None)
    if leader is None:
        return
    job_store.settle_verified_payment(leader.id, batch_id=batch_id, amount=amount, source="verified_query")
    for job in job_store.list_jobs_by_batch_id(batch_id):
        if job.status == JobStatus.pending:
            job_store.ensure_dispatch(job.id)
            dispatch_pending(job_store, _publish_conversion, job_id=job.id)


def _handle_closed(job_id: str, reason: str) -> None:
    """将订单标记为 cancelled。"""
    mark_timeout = getattr(job_store, "mark_payment_timeout", None)
    if callable(mark_timeout):
        mark_timeout(job_id, gateway_confirmed=True)
    logger.info("reconcile: closed job", extra={"job_id": job_id, "reason": reason})


def _handle_batch_closed(batch_id: str, reason: str) -> None:
    mark_timeout = getattr(job_store, "mark_batch_payment_timeout", None)
    if callable(mark_timeout):
        mark_timeout(batch_id, gateway_confirmed=True)
    logger.info("reconcile: closed batch", extra={"job_id": f"batch_{batch_id}", "reason": reason})
