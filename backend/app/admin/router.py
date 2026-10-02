import hmac
import hashlib
import logging
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from .auth import (AdminBase, AdminSession, COOKIE, credential_version, credentials,
                   digest_token, reject_cross_origin, require_session, throttle, verify_password)
from .orders import PaymentCheck, money, order_view, safe_file
from .reviews import OrderReviewError, OrderReviewService
from ..storage_db import JobRecord, OrderReviewRecord, _record_to_job, _sqlite_schema_lock
from ..models import JobStage, JobStatus, StageStatus
from ..domain.translation_attempt import new_attempt_id, attempt_id_from_stats
from ..domain.payment_entitlement import grant_verified_entitlement
from ..infra.alipay import query_verified_trade
from ..order_events import milestones, record_event
from ..infra.llm_usage_ledger import get_ledger

logger = logging.getLogger("epub_factory")


class LoginBody(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=512)


class RetryBody(BaseModel):
    acknowledge_cost: bool = False


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["note", "fulfill", "record_external_refund", "close_review"]
    request_id: UUID
    expected_revision: int = Field(ge=0)
    expected_context: str = Field(pattern=r"^[a-f0-9]{64}$")
    note: str = Field(default="", max_length=4000)
    evidence: str = Field(default="", max_length=4000)
    refund_reference: str = Field(default="", max_length=256)
    acknowledge_cost: bool = False


def make_router(store, upload_dir, output_dir, enqueue):
    router = APIRouter(prefix="/api/admin", tags=["admin-orders"])
    engine = getattr(store, "_engine", None)
    if engine is not None:
        with _sqlite_schema_lock(engine):
            AdminBase.metadata.create_all(engine)
    ledger = get_ledger(engine) if engine is not None else None
    reviews = (OrderReviewService(store, upload_dir,
               cost_provider=lambda job: ledger.summary(job.id, job.translation_stats))
               if engine is not None else None)

    def available():
        if engine is None:
            raise HTTPException(503, "订单看板需要持久化数据库")

    def authorize(request, write=False):
        available()
        return require_session(engine, request, write=write)

    def get_job(job_id):
        try:
            job = store.get(job_id)
        except (TypeError, ValueError, OverflowError):
            # Historical malformed metadata is not an executable order. Never
            # repair it implicitly or leak the failing value/provider payload.
            raise HTTPException(409, "订单历史元数据异常，已禁止操作，请人工核对原记录",
                                headers={"Cache-Control": "no-store"}) from None
        if not job:
            raise HTTPException(404, "订单不存在")
        return job

    def invalid_record_view(row):
        """Read-only diagnostic projection; deliberately never constructs a Job."""
        number = f"batch_{row.batch_id}" if row.batch_id else row.id
        expected, scope_count = row.expected_amount, 1
        price_scope = "原记录金额（历史元数据待核对）"
        if row.batch_id:
            with Session(engine) as session:
                scope_count = session.query(JobRecord).filter_by(batch_id=row.batch_id).count()
                leaders = session.query(JobRecord).filter_by(
                    batch_id=row.batch_id, batch_index="0").limit(2).all()
                if len(leaders) == 1:
                    expected = leaders[0].expected_amount
                    price_scope = "整批冻结价格，勿重复相加（历史元数据待核对）"
        message = "订单历史元数据异常；仅展示原记录摘要，下载及付款、重试、人工处置均已禁止"
        return {
            "id": row.id, "order_no": number, "filename": row.source_filename,
            "created_at": row.created_at.isoformat() if isinstance(row.created_at, datetime) else None,
            "updated_at": row.updated_at.isoformat() if isinstance(row.updated_at, datetime) else None,
            "status": row.status, "price_cny": expected or None, "price_scope": price_scope,
            "batch_id": row.batch_id or None, "translation": bool(row.enable_translation),
            "model": None, "error_code": "ORDER_METADATA_INVALID", "message": message,
            "metadata_invalid": True,
            "cost": {"estimated_usd": None, "known_attempts": None, "attempts": None,
                     "prompt_tokens": None, "completion_tokens": None, "ledger": None,
                     "note": "历史元数据不可解析，模型用量及费用未知，不按零费用处理。"},
            "payment": {"status": "unknown", "amount": None, "checked_at": None},
            "payment_resolution": {}, "checkout": {}, "files": {"source": False, "output": False},
            "stages": [],
            "review": {"order_no": number, "revision": 0,
                       "context": hashlib.sha256(("metadata_invalid:" + row.id).encode()).hexdigest(),
                       "state": "open", "needs_attention": True,
                       "reasons": [{"code": "metadata_invalid", "label": message}],
                       "allowed_actions": [], "scope_count": scope_count},
        }

    def record_view(row, review_cache=None):
        try:
            job = _record_to_job(row)
        except (TypeError, ValueError, OverflowError):
            return invalid_record_view(row)
        return view(job, review_cache)

    def order_identity(job):
        if job.batch_id:
            with Session(engine) as session:
                first = session.query(JobRecord).filter_by(batch_id=job.batch_id).order_by(JobRecord.batch_index).first()
                return f"batch_{job.batch_id}", first.expected_amount if first else None
        return job.id, job.expected_amount

    def view(job, review_cache=None):
        number, expected = order_identity(job)
        with engine.connect() as conn:
            payment = conn.execute(select(PaymentCheck).where(PaymentCheck.order_no == number)).mappings().first()
        result = order_view(job, dict(payment) if payment else None, upload_dir, output_dir, expected=expected)
        result["cost"]["ledger"] = ledger.summary(job.id, job.translation_stats)
        result["checkout"] = milestones(store, number)
        result["payment_resolution"] = dict(getattr(job, "payment_resolution", None) or {})
        try:
            if review_cache is not None and number in review_cache:
                result["review"] = review_cache[number]
            else:
                result["review"] = reviews.snapshot(job.id)
                if review_cache is not None:
                    review_cache[number] = result["review"]
        except OrderReviewError as exc:
            raise HTTPException(exc.status_code, str(exc), headers={"Cache-Control": "no-store"}) from None
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
            grant_verified_entitlement(store, job, amount, "verified_query")
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
    def orders(request: Request, response: Response, q: str = Query("", max_length=200), status: JobStatus | None = None,
               payment: Literal["paid", "unpaid", "closed", "unknown", "amount_mismatch"] | None = None,
               review: Literal["paid_review", "open", "resolved"] | None = None,
               start: date | None = None, end: date | None = None,
               amount: str | None = Query(None, max_length=30), page: int = Query(1, ge=1),
               size: int = Query(25, ge=1, le=100)):
        authorize(request)
        response.headers["Cache-Control"] = "no-store"
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
                PaymentCheck, PaymentCheck.order_no == number).outerjoin(
                OrderReviewRecord, OrderReviewRecord.order_no == number)
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
            if review == "paid_review":
                query = query.filter(JobRecord.error_code == "PAYMENT_REVIEW_REQUIRED")
            elif review:
                # These filters describe recorded case state, not an unbounded
                # scan of dynamically recomputed payment/usage anomalies.
                query = query.filter(OrderReviewRecord.state == ("closed" if review == "resolved" else "open"))
            if start:
                query = query.filter(JobRecord.created_at >= datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc))
            if end:
                query = query.filter(JobRecord.created_at < datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc))
            if amount is not None:
                query = query.filter(price != "", cast(price, Numeric(16, 2)) == money(amount))
            total = query.count()
            rows = query.order_by(JobRecord.created_at.desc(), JobRecord.id.desc()).offset((page - 1) * size).limit(size).all()
            # A batch has one review context. Recompute it once per page, not
            # once per child (which would repeatedly scan the whole batch).
            review_cache = {}
            items = [record_view(row, review_cache) for row in rows]
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
    def detail(job_id: str, request: Request, response: Response):
        authorize(request)
        response.headers["Cache-Control"] = "no-store"
        with Session(engine) as session:
            row = session.get(JobRecord, job_id)
            if row is None:
                raise HTTPException(404, "订单不存在")
            result = record_view(row)
        if result.get("metadata_invalid"):
            return result
        # Expose only failure/attempt summaries, not raw paths, keys, or provider payloads.
        result["stages"] = [{"name": s.stage_name, "status": s.status.value,
                             "started_at": s.started_at.isoformat() if s.started_at else None,
                             "elapsed_ms": s.elapsed_ms,
                             "previous_failure": s.metadata.get("previous_message") if s.stage_name == "admin_retry" else None} for s in store.list_stages(job_id)[-100:]]
        return result

    @router.get("/orders/{job_id}/review-history")
    def review_history(job_id: str, request: Request, response: Response,
                       before: str | None = Query(None, max_length=256),
                       limit: int = Query(20, ge=1, le=100)):
        authorize(request)
        response.headers["Cache-Control"] = "no-store"
        get_job(job_id)
        try:
            return reviews.history(job_id, before=before, limit=limit)
        except OrderReviewError as exc:
            raise HTTPException(exc.status_code, str(exc), headers={"Cache-Control": "no-store"}) from None

    @router.post("/orders/{job_id}/review")
    def resolve_review(job_id: str, body: ReviewBody, request: Request,
                       response: Response, background: BackgroundTasks):
        authorize(request, True)
        response.headers["Cache-Control"] = "no-store"
        get_job(job_id)
        try:
            snapshot = reviews.snapshot(job_id)
            trade = None
            # A completed idempotent retry needs no new gateway request. The
            # transactional service still verifies the request hash and CAS.
            if body.action == "fulfill" and "fulfill" in snapshot["allowed_actions"]:
                try:
                    trade = query_verified_trade(snapshot["order_no"])
                except Exception:
                    raise HTTPException(503, "支付核验暂不可用，未执行履约，请稍后重试",
                                        headers={"Cache-Control": "no-store"}) from None
            result = reviews.apply(job_id, body.action, str(body.request_id),
                body.expected_revision, body.expected_context, actor=credentials()[0],
                note=body.note, evidence=body.evidence,
                refund_reference=body.refund_reference,
                acknowledge_cost=body.acknowledge_cost, trade=trade)
        except OrderReviewError as exc:
            raise HTTPException(exc.status_code, str(exc), headers={"Cache-Control": "no-store"}) from None
        dispatch_pending = False
        for released_id in result.get("released", []):
            current = store.get(released_id)
            if current is None:
                continue
            try:
                enqueue(current, background)
            except Exception:
                # The new attempt and dispatch intent were committed together.
                # Do not turn an accepted paid task into a failed task here.
                dispatch_pending = True
                logger.warning("Reviewed order awaiting durable dispatch", extra={"job_id": released_id})
        result_view = view(get_job(job_id))
        result_view["review_action"] = {"duplicate": bool(result.get("duplicate")),
                                         "dispatch_pending": dispatch_pending}
        return result_view

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
        payment = check_payment(job)
        if payment["status"] != "paid":
            raise HTTPException(409, "未能核验支付成功及金额一致，未发起重试")
        if job.enable_translation:
            # Cost acknowledgement + authenticated admin + fresh gateway proof
            # explicitly authorize a legacy plan that lacks its original quote.
            refreshed_job = store.get(job.id) or job
            grant_verified_entitlement(store, refreshed_job, payment["amount"],
                                       "verified_admin_query", allow_legacy_plan=True)
        now = datetime.now(timezone.utc)
        restarted, reason = store.restart_translation_attempt(
            job.id, attempt_id=new_attempt_id(), action_label="管理员重试", max_free_retries=-1,
            started_at=now, cache_policy="reuse", failed_only=True, expected_updated_at=job.updated_at)
        if reason != "ok":
            raise HTTPException(409, "订单状态已变化，请刷新后再试")
        store.add_stage(JobStage(job_id=job.id, stage_name="admin_retry", status=StageStatus.completed,
                                started_at=now, finished_at=now,
                                metadata={"admin": credentials()[0], "previous_error_code": job.error_code,
                                          "previous_message": job.message, "cache_policy": "reuse",
                                          "attempt_id": attempt_id_from_stats(restarted.translation_stats)}),
                        expected_attempt_id=attempt_id_from_stats(restarted.translation_stats))
        try:
            enqueue(restarted, background)
        except Exception:
            store.update_status(job.id, status=JobStatus.failed, message="管理员重试入队失败，请检查队列后重试",
                                expected_attempt_id=attempt_id_from_stats(restarted.translation_stats),
                                expected_statuses={JobStatus.pending})
            current = store.get(job.id)
            if (current and attempt_id_from_stats(current.translation_stats) == attempt_id_from_stats(restarted.translation_stats)
                    and current.status in {JobStatus.running, JobStatus.success}):
                return view(current)
            raise HTTPException(503, "任务入队失败，已保留原文件和缓存")
        return view(restarted)

    return router
