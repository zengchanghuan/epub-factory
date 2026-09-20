import hmac
import logging
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from .auth import (AdminBase, AdminSession, COOKIE, credential_version, credentials,
                   digest_token, reject_cross_origin, require_session, throttle, verify_password)
from .orders import PaymentCheck, money, order_view, safe_file
from ..storage_db import JobRecord, _record_to_job
from ..models import JobStage, JobStatus, StageStatus
from ..domain.translation_attempt import new_attempt_id
from ..infra.alipay import query_verified_trade
from ..order_events import milestones, record_event
from ..infra.llm_usage_ledger import get_ledger

logger = logging.getLogger("epub_factory")


class LoginBody(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=512)


class RetryBody(BaseModel):
    acknowledge_cost: bool = False


def make_router(store, upload_dir, output_dir, enqueue):
    router = APIRouter(prefix="/api/admin", tags=["admin-orders"])
    engine = getattr(store, "_engine", None)
    if engine is not None:
        AdminBase.metadata.create_all(engine)
    ledger = get_ledger(engine) if engine is not None else None

    def available():
        if engine is None:
            raise HTTPException(503, "订单看板需要持久化数据库")

    def authorize(request, write=False):
        available()
        return require_session(engine, request, write=write)

    def get_job(job_id):
        job = store.get(job_id)
        if not job:
            raise HTTPException(404, "订单不存在")
        return job

    def order_identity(job):
        if job.batch_id:
            with Session(engine) as session:
                first = session.query(JobRecord).filter_by(batch_id=job.batch_id).order_by(JobRecord.batch_index).first()
                return f"batch_{job.batch_id}", first.expected_amount if first else None
        return job.id, job.expected_amount

    def view(job):
        number, expected = order_identity(job)
        with engine.connect() as conn:
            payment = conn.execute(select(PaymentCheck).where(PaymentCheck.order_no == number)).mappings().first()
        result = order_view(job, dict(payment) if payment else None, upload_dir, output_dir, expected=expected)
        result["cost"]["ledger"] = ledger.summary(job.id, job.translation_stats)
        result["checkout"] = milestones(store, number)
        return result

    def check_payment(job):
        number, expected = order_identity(job)
        result = query_verified_trade(number)
        state, amount, trade_no = "unknown", None, None
        if result:
            amount = result.get("total_amount")
            trade_no = result.get("trade_no")
            state = {"WAIT_BUYER_PAY": "unpaid", "TRADE_CLOSED": "closed",
                     "TRADE_SUCCESS": "paid", "TRADE_FINISHED": "paid"}.get(result.get("trade_status"), "unknown")
            if state == "paid" and (money(expected) is None or money(amount) != money(expected) or money(amount) <= 0):
                state = "amount_mismatch"
        values = dict(status=state, amount=amount, trade_no=trade_no,
                      checked_at=datetime.now(timezone.utc).isoformat())
        # Serialize the upsert for SQLite and use a row lock on other SQL backends.
        with Session(engine) as session:
            if engine.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            record = session.get(PaymentCheck, number, with_for_update=True)
            if record is None:
                record = PaymentCheck(order_no=number)
                session.add(record)
            for key, value in values.items():
                setattr(record, key, value)
            session.commit()
        if state == "paid":
            record_event(store, number, "payment_succeeded", "verified_query")
            # A historical order refresh must not send a new-sale notification.
            if job.status == JobStatus.pending_payment:
                try:
                    from ..domain.payment_email_service import queue_paid_order_email
                    jobs = store.list_jobs_by_batch_id(job.batch_id) if job.batch_id else [job]
                    queue_paid_order_email(number, amount,
                        "batch" if job.batch_id else "translation" if job.enable_translation else "conversion",
                        file_count=len(jobs), is_test_order=any(j.is_test_order for j in jobs))
                except Exception:
                    logger.warning("Paid order email could not be queued", extra={"job_id": number})
        return values

    @router.post("/login")
    def login(body: LoginBody, request: Request, response: Response):
        available()
        reject_cross_origin(request)
        user, encoded = credentials()
        throttle(engine, request)
        password_ok = verify_password(body.password, encoded)
        if not password_ok or not hmac.compare_digest(body.username.encode(), user.encode()):
            raise HTTPException(401, "用户名或密码错误")
        token = secrets.token_urlsafe(32)
        with engine.begin() as conn:
            conn.execute(delete(AdminSession).where(AdminSession.expires <= int(time.time())))
            conn.execute(AdminSession.__table__.insert().values(
                token=digest_token(token), expires=int(time.time()) + 28800, credential=credential_version()))
        response.set_cookie(COOKIE, token, max_age=28800, httponly=True, secure=True,
                            samesite="strict", path="/api/admin")
        response.headers["Cache-Control"] = "no-store"
        return {"username": user, "csrf": token}

    @router.get("/session")
    def session_info(request: Request, response: Response):
        token = authorize(request)
        response.headers["Cache-Control"] = "no-store"
        return {"username": credentials()[0], "csrf": token}

    @router.post("/logout")
    def logout(request: Request, response: Response):
        token = authorize(request, True)
        with engine.begin() as conn:
            conn.execute(delete(AdminSession).where(AdminSession.token == digest_token(token)))
        response.delete_cookie(COOKIE, path="/api/admin", secure=True, httponly=True, samesite="strict")
        return {"ok": True}

    @router.get("/orders")
    def orders(request: Request, q: str = Query("", max_length=200), status: JobStatus | None = None,
               payment: Literal["paid", "unpaid", "closed", "unknown", "amount_mismatch"] | None = None,
               start: date | None = None, end: date | None = None,
               amount: str | None = Query(None, max_length=30), page: int = Query(1, ge=1),
               size: int = Query(25, ge=1, le=100)):
        authorize(request)
        if start and end and start > end:
            raise HTTPException(422, "开始日期不能晚于结束日期")
        if amount is not None and money(amount) is None:
            raise HTTPException(422, "金额无效")
        with Session(engine) as session:
            # Batch children share the first row's price and the same receipt.
            first = session.query(JobRecord.batch_id.label("batch"), JobRecord.expected_amount.label("price")).filter(
                JobRecord.batch_id != "", JobRecord.batch_index == 0).subquery()
            from sqlalchemy import case, cast, Numeric
            number = case((JobRecord.batch_id != "", "batch_" + JobRecord.batch_id), else_=JobRecord.id)
            price = case((JobRecord.batch_id != "", first.c.price), else_=JobRecord.expected_amount)
            query = session.query(JobRecord).outerjoin(first, JobRecord.batch_id == first.c.batch).outerjoin(
                PaymentCheck, PaymentCheck.order_no == number)
            query = query.filter(JobRecord.is_test_order.is_(False))
            query = query.filter(JobRecord.created_at >= datetime(2026, 6, 24, tzinfo=timezone.utc))
            # Exclude test-price orders, including all children of a test batch.
            query = query.filter(or_(price.is_(None), price == "", cast(price, Numeric(16, 2)).notin_([money("0.01"), money("0.02")])))
            if q:
                query = query.filter(or_(JobRecord.id.contains(q, autoescape=True),
                                          JobRecord.source_filename.contains(q, autoescape=True),
                                          number.contains(q, autoescape=True)))
            if status:
                query = query.filter(JobRecord.status == status.value)
            if payment:
                query = query.filter(func.coalesce(PaymentCheck.status, "unknown") == payment)
            if start:
                query = query.filter(JobRecord.created_at >= datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc))
            if end:
                query = query.filter(JobRecord.created_at < datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc))
            if amount is not None:
                query = query.filter(price != "", cast(price, Numeric(16, 2)) == money(amount))
            total = query.count()
            rows = query.order_by(JobRecord.created_at.desc(), JobRecord.id.desc()).offset((page - 1) * size).limit(size).all()
            items = [view(_record_to_job(row)) for row in rows]
        return {"items": items, "total": total, "page": page, "size": size}

    @router.get("/orders/{job_id}/usage")
    def usage_detail(job_id: str, request: Request, page: int = Query(1, ge=1),
                     page_size: int = Query(50, ge=1, le=100)):
        authorize(request)
        job = get_job(job_id)
        return {"summary": ledger.summary(job.id, job.translation_stats),
                "page": page, "page_size": page_size,
                "items": ledger.requests(job.id, page, page_size)}

    @router.get("/orders/{job_id}")
    def detail(job_id: str, request: Request):
        authorize(request)
        job = get_job(job_id)
        result = view(job)
        # Expose only failure/attempt summaries, not raw paths, keys, or provider payloads.
        result["stages"] = [{"name": s.stage_name, "status": s.status.value,
                             "started_at": s.started_at.isoformat() if s.started_at else None,
                             "elapsed_ms": s.elapsed_ms,
                             "previous_failure": s.metadata.get("previous_message") if s.stage_name == "admin_retry" else None} for s in store.list_stages(job_id)[-100:]]
        return result

    @router.post("/orders/{job_id}/payment")
    def refresh_payment(job_id: str, request: Request):
        authorize(request, True)
        job = get_job(job_id)
        check_payment(job)
        return view(job)

    @router.get("/orders/{job_id}/files/{kind}")
    def download(job_id: str, kind: Literal["source", "output"], request: Request):
        authorize(request)
        job = get_job(job_id)
        path = safe_file(job.input_path if kind == "source" else job.output_path,
                         upload_dir if kind == "source" else output_dir)
        if not path:
            raise HTTPException(410, "文件不存在或已过期")
        return FileResponse(path, filename=path.name, media_type="application/octet-stream",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @router.post("/orders/{job_id}/retry")
    def retry(job_id: str, body: RetryBody, request: Request, background: BackgroundTasks):
        authorize(request, True)
        if not body.acknowledge_cost:
            raise HTTPException(422, "请确认重试可能产生模型费用")
        job = get_job(job_id)
        if job.status != JobStatus.failed:
            raise HTTPException(409, "只允许重试失败的订单")
        if not safe_file(job.input_path, upload_dir):
            raise HTTPException(410, "原文件不存在或已过期")
        if check_payment(job)["status"] != "paid":
            raise HTTPException(409, "未能核验支付成功及金额一致，未发起重试")
        now = datetime.now(timezone.utc)
        restarted, reason = store.restart_translation_attempt(
            job.id, attempt_id=new_attempt_id(), action_label="管理员重试", max_free_retries=-1,
            started_at=now, cache_policy="reuse", failed_only=True, expected_updated_at=job.updated_at)
        if reason != "ok":
            raise HTTPException(409, "订单状态已变化，请刷新后再试")
        store.add_stage(JobStage(job_id=job.id, stage_name="admin_retry", status=StageStatus.completed,
                                started_at=now, finished_at=now,
                                metadata={"admin": credentials()[0], "previous_error_code": job.error_code,
                                          "previous_message": job.message, "cache_policy": "reuse"}))
        try:
            enqueue(restarted, background)
        except Exception:
            store.update_status(job.id, status=JobStatus.failed, message="管理员重试入队失败，请检查队列后重试",
                                expected_attempt_id=restarted.translation_stats.get("attempt_id"))
            raise HTTPException(503, "任务入队失败，已保留原文件和缓存")
        return view(restarted)

    return router
