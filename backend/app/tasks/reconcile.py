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

from app.infra.alipay import query_alipay_trade
from app.infra.celery_app import celery_app
from app.storage import job_store
from app.domain.translation_attempt import attempt_id_from_stats

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

    logger.info("reconcile_payments: start", extra={"count": len(stale)})
    paid = closed = skipped = 0
    timeout_cutoff = datetime.now(timezone.utc) - timedelta(hours=_TIMEOUT_HOURS)

    for job in stale:
        trade_status = query_alipay_trade(job.id)

        if trade_status in ("TRADE_SUCCESS", "TRADE_FINISHED"):
            _handle_paid(job.id)
            paid += 1

        elif trade_status == "TRADE_CLOSED":
            _handle_closed(job.id, reason="支付宝已关单")
            closed += 1

        elif trade_status == "WAIT_BUYER_PAY" and job.created_at < timeout_cutoff:
            # 等待超过 TIMEOUT_HOURS 小时仍未付款，主动关单
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


def _handle_closed(job_id: str, reason: str) -> None:
    """将订单标记为 cancelled。"""
    mark_timeout = getattr(job_store, "mark_payment_timeout", None)
    if callable(mark_timeout):
        mark_timeout(job_id)
    logger.info("reconcile: closed job", extra={"job_id": job_id, "reason": reason})
