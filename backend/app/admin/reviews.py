"""Private, atomic manual dispositions. Authentication and gateway IO live outside.

The service never issues a refund or infers a receipt from an administrative
note. Its case closure is bound to the observed order/source/ledger context.
"""
from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from sqlalchemy import delete, select, update

from ..models import JobStatus
from ..storage_db import (JobRecord, ChapterRecord, ChunkRecord, OrderReviewRecord, OrderReviewEventRecord,
                          _record_to_job)
from ..domain.checkout_resume import original_checkout, frozen_amount, CheckoutUnavailable
from ..domain.payment_entitlement import (manual_payment_guard, same_amount,
    restart_entitlement_reason, precision_polish_entitlement_reason)
from ..domain.translation_attempt import restarted_translation_stats, new_attempt_id


class OrderReviewError(ValueError):
    def __init__(self, message, status_code=409, code="review_conflict"):
        super().__init__(message)
        self.status_code, self.code = status_code, code


def _json(value):
    def default(item):
        if isinstance(item, datetime):
            return (item.replace(tzinfo=timezone.utc) if item.tzinfo is None
                    else item.astimezone(timezone.utc)).isoformat()
        raise TypeError("Unsupported review context value")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=default)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text(value, name, maximum=4000, required=False):
    if not isinstance(value, str) or len(value) > maximum or "\0" in value:
        raise OrderReviewError(f"{name}格式无效", 422, "invalid_input")
    value = value.strip()
    if required and not value:
        raise OrderReviewError(f"请填写{name}", 422, "invalid_input")
    return value


class OrderReviewService:
    def __init__(self, store, upload_dir, *, cost_provider=None):
        if not getattr(store, "_Session", None) or getattr(store, "_engine", None) is None:
            raise OrderReviewError("人工处理需要持久化数据库", 503, "store_unavailable")
        self.store = store
        self.upload_dir = Path(upload_dir).resolve()
        self.cost_provider = cost_provider

    def _scope(self, session, job_id, *, lock=False):
        if lock and self.store._engine.dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        requested = session.get(JobRecord, job_id)
        if requested is None:
            raise OrderReviewError("订单不存在", 404, "missing")
        batch = requested.batch_id
        if batch:
            leaders = session.scalars(select(JobRecord).where(
                JobRecord.batch_id == batch, JobRecord.batch_index == "0")).all()
            if len(leaders) != 1:
                raise OrderReviewError("批次订单分组不完整，请核对原记录", code="invalid_batch")
            leader_id = leaders[0].id
        else:
            leader_id = requested.id
        if lock:
            # Same leader-first lock order as payment settlement. A no-op write
            # supplies an actual write lock on both SQLite and PostgreSQL.
            session.execute(update(JobRecord).where(JobRecord.id == leader_id).values(status=JobRecord.status))
            if batch:
                session.execute(update(JobRecord).where(JobRecord.batch_id == batch).values(status=JobRecord.status))
            session.expire_all()
        records = (session.scalars(select(JobRecord).where(JobRecord.batch_id == batch)).all()
                   if batch else [session.get(JobRecord, leader_id)])
        try:
            records.sort(key=lambda row: (int(row.batch_index or "0"), row.id))
            if batch and (not records or records[0].id != leader_id
                    or [int(row.batch_index) for row in records] != list(range(len(records)))
                    or any(int(row.batch_size or "0") != len(records) for row in records)
                    or any(row.expected_amount not in {None, ""} for row in records[1:])):
                raise ValueError()
        except (TypeError, ValueError):
            raise OrderReviewError("批次订单分组或整单金额不一致", code="invalid_batch") from None
        if not any(row.id == job_id for row in records):
            raise OrderReviewError("订单分组已变化，请刷新", code="context_changed")
        return ("batch_" + batch if batch else leader_id), records

    def _source(self, job):
        try:
            source = Path(job.input_path or "")
            resolved = source.resolve()
            if not source.is_absolute() or not resolved.is_relative_to(self.upload_dir):
                return {"available": False}
            # macOS exposes /var as an alias of /private/var. Permit aliases
            # above the configured root, never a symlink within that root.
            cursor = source
            while cursor.resolve() != self.upload_dir:
                if cursor.is_symlink() or cursor == cursor.parent:
                    return {"available": False}
                cursor = cursor.parent
            if cursor.is_symlink() and cursor.parent.resolve().is_relative_to(self.upload_dir):
                return {"available": False}
            stat = source.stat()
            if not source.is_file() or stat.st_size <= 0:
                return {"available": False}
            return {"available": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                    "inode": stat.st_ino, "device": stat.st_dev}
        except (OSError, ValueError):
            return {"available": False}

    def _facts(self, records):
        try:
            jobs = [_record_to_job(record) for record in records]
        except (ValueError, TypeError, OverflowError):
            raise OrderReviewError("订单元数据无效，不能自动处理", code="invalid_metadata") from None
        costs = []
        for job in jobs:
            try:
                cost = self.cost_provider(job) if self.cost_provider is not None else None
            except Exception:
                raise OrderReviewError("费用记录暂不可读，请刷新后再处理", 503, "cost_unavailable") from None
            costs.append(cost if isinstance(cost, dict) else {"coverage": "unknown"})
        sources = [self._source(job) for job in jobs]
        context = _hash({"version": 1, "orders": [
            {column.name: getattr(row, column.name) for column in JobRecord.__table__.columns}
            for row in records], "sources": sources, "costs": costs})
        return jobs, sources, costs, context

    @staticmethod
    def _financial_ready(jobs):
        return bool(jobs) and all(job.status == JobStatus.cancelled
            and (job.payment_resolution or {}).get("state") == "paid_review"
            and manual_payment_guard(job) != "refund_recorded" for job in jobs)

    @staticmethod
    def _plan_valid(job):
        # Only the manual disposition is removed for this check. Never create
        # or change a missing purchase snapshot to make fulfillment pass.
        candidate = replace(job, payment_resolution={})
        if job.enable_translation and restart_entitlement_reason(candidate):
            return False
        if job.enable_precision_polish and precision_polish_entitlement_reason(candidate):
            return False
        return True

    def _view(self, number, records, case, facts=None):
        jobs, sources, costs, context = facts or self._facts(records)
        reasons = []
        def reason(code, label):
            if not any(item["code"] == code for item in reasons):
                reasons.append({"code": code, "label": label})
        if any((job.payment_resolution or {}).get("state") == "paid_review" for job in jobs):
            reason("paid_review", "已付款但已取消，需人工决定履约或核验外部退款")
        if any(manual_payment_guard(job) == "refund_recorded" for job in jobs):
            reason("external_refund_recorded", "已登记外部全额退款；本系统未调用退款接口")
        for job, source, cost in zip(jobs, sources, costs):
            if job.status == JobStatus.pending_payment:
                try: original_checkout(job, number, records[0].expected_amount)
                except CheckoutUnavailable: reason("checkout_unknown", "原支付通道不明确，不能自动恢复付款")
            if (job.enable_translation or job.enable_precision_polish) and cost.get("coverage") != "complete":
                reason("cost_unknown", "模型费用记录尚不完整，不按零费用处理")
            if (job.payment_resolution or {}).get("state") == "paid_review":
                if not source["available"]: reason("source_unavailable", "原始文件不可安全读取，不能恢复履约")
                if not self._plan_valid(job): reason("purchase_plan_unknown", "缺少可验证的原购买档位，不能推测补建")
        financial = self._financial_ready(jobs)
        if any((job.payment_resolution or {}).get("state") == "paid_review" for job in jobs) and not financial:
            reason("mixed_batch_state", "批次成员状态不一致，禁止部分履约或部分退款登记")
        try: frozen_amount(jobs[0])
        except CheckoutUnavailable:
            financial = False
            if reasons: reason("amount_unknown", "整单冻结金额缺失或无效")
        closed = bool(case and case.state == "closed" and case.context == context)
        actions = ["note"]
        if reasons and not closed and not any(
                (job.payment_resolution or {}).get("state") == "paid_review" for job in jobs):
            actions.append("close_review")
        if financial and not closed:
            actions.append("record_external_refund")
            if all(source["available"] for source in sources) and all(self._plan_valid(job) for job in jobs):
                actions.append("fulfill")
        return {"order_no": number, "revision": case.revision if case else 0, "context": context,
                "state": "closed" if closed else "open", "needs_attention": bool(reasons) and not closed,
                "reasons": reasons, "allowed_actions": actions, "scope_count": len(jobs)}

    def snapshot(self, job_id, cost_summary=None):
        # A caller-supplied display summary is not authoritative CAS input.
        # Use the same configured provider here and in the action transaction.
        with self.store._Session() as session:
            try:
                number, records = self._scope(session, job_id)
                return self._view(number, records, session.get(OrderReviewRecord, number))
            except OrderReviewError as error:
                if error.code not in {"invalid_batch", "invalid_metadata"}:
                    raise
                requested = session.get(JobRecord, job_id)
                if requested is None:
                    raise
                records = (session.scalars(select(JobRecord).where(
                    JobRecord.batch_id == requested.batch_id)).all()
                    if requested.batch_id else [requested])
                # Broken group metadata may itself be non-numeric. Do not
                # deserialize it as a valid executable Job just to display
                # this explicitly non-actionable diagnostic.
                context = _hash({error.code: [
                    {column.name: getattr(row, column.name) for column in JobRecord.__table__.columns}
                    for row in sorted(records, key=lambda row: row.id)]})
                number = "batch_" + requested.batch_id if requested.batch_id else requested.id
                label = ("批次订单分组或整单金额不一致，不能自动处理" if error.code == "invalid_batch"
                         else "订单元数据无效，不能自动处理")
                return {"order_no": number, "revision": 0,
                    "context": context, "state": "open", "needs_attention": True,
                    "reasons": [{"code": error.code, "label": label}],
                    "allowed_actions": [], "scope_count": len(records)}

    def history(self, job_id, before=None, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise OrderReviewError("历史条数必须为1至100", 422, "invalid_input")
        with self.store._Session() as session:
            number, _ = self._scope(session, job_id)
            query = select(OrderReviewEventRecord).where(OrderReviewEventRecord.order_no == number)
            if before:
                try:
                    if not isinstance(before, str) or len(before) > 512: raise ValueError()
                    value = json.loads(base64.urlsafe_b64decode(before + "=" * (-len(before) % 4)))
                    if value.get("order_no") != number or type(value.get("revision")) is not int or value["revision"] <= 0:
                        raise ValueError()
                except Exception:
                    raise OrderReviewError("历史游标无效或不属于此订单", 400, "invalid_cursor") from None
                query = query.where(OrderReviewEventRecord.revision < value["revision"])
            rows = session.scalars(query.order_by(OrderReviewEventRecord.revision.desc()).limit(limit + 1)).all()
            page = rows[:limit]
            cursor = (base64.urlsafe_b64encode(_json({"order_no": number, "revision": page[-1].revision}).encode())
                      .decode().rstrip("=")) if len(rows) > limit else None
            return {"items": [{"id": row.id, "action": row.action, "actor": row.actor,
                "note": row.note, "evidence": row.evidence, "refund_reference": row.refund_reference,
                "created_at": row.created_at.replace(tzinfo=timezone.utc).isoformat(),
                "result": self._history_result(row.result_json)} for row in page], "next_cursor": cursor}

    @staticmethod
    def _history_result(raw):
        value = json.loads(raw)
        return {"review": value.get("review"), "released": value.get("released", []),
                "prior_artifact_job_ids": sorted(value.get("prior_artifacts", {}))}

    def apply(self, job_id, action, request_id, expected_revision, expected_context, actor, note,
              evidence, refund_reference="", acknowledge_cost=False, trade=None):
        if action not in {"note", "fulfill", "record_external_refund", "close_review"}:
            raise OrderReviewError("不支持的人工操作", 422, "invalid_input")
        try: request_id = str(uuid.UUID(request_id))
        except (ValueError, TypeError, AttributeError):
            raise OrderReviewError("request_id必须是UUID", 422, "invalid_input") from None
        if type(expected_revision) is not int or expected_revision < 0 or not isinstance(expected_context, str) or len(expected_context) != 64:
            raise OrderReviewError("审核版本无效，请刷新", 422, "invalid_input")
        actor = _text(actor, "管理员身份", 128, True)
        note = _text(note, "处理说明", required=True)
        evidence = _text(evidence, "核验依据", required=action != "note")
        refund_reference = _text(refund_reference, "外部退款凭证编号", 256, action == "record_external_refund")
        if type(acknowledge_cost) is not bool:
            raise OrderReviewError("费用确认无效", 422, "invalid_input")
        payload_hash = _hash({"job_id": job_id, "action": action, "actor": actor, "note": note,
            "evidence": evidence, "refund_reference": refund_reference, "acknowledge_cost": acknowledge_cost,
            "revision": expected_revision, "context": expected_context})
        with self.store._Session() as session:
            number, records = self._scope(session, job_id, lock=True)
            event_id = _hash([number, request_id])
            prior = session.get(OrderReviewEventRecord, event_id)
            if prior:
                if prior.payload_hash != payload_hash:
                    raise OrderReviewError("请求编号已用于不同操作", code="idempotency_conflict")
                return {"review": self._view(number, records, session.get(OrderReviewRecord, number)),
                        "released": [], "duplicate": True}
            case = session.get(OrderReviewRecord, number)
            facts = self._facts(records)
            view = self._view(number, records, case, facts)
            if view["revision"] != expected_revision or view["context"] != expected_context:
                raise OrderReviewError("订单或费用信息已变化，请刷新后重新确认", code="context_changed")
            if action not in view["allowed_actions"]:
                raise OrderReviewError("当前订单不允许此操作", code="action_unavailable")
            jobs = facts[0]
            now = datetime.now(timezone.utc)
            released = []
            if action in {"fulfill", "record_external_refund"}:
                if not self._financial_ready(jobs):
                    raise OrderReviewError("只有整单全部已付款待审核的取消任务可处理")
                amount = frozen_amount(jobs[0])
                if action == "fulfill":
                    if not acknowledge_cost:
                        raise OrderReviewError("请确认恢复履约可能产生模型费用", 422, "cost_ack_required")
                    if (not isinstance(trade, dict) or trade.get("out_trade_no") != number
                            or trade.get("trade_status") not in {"TRADE_SUCCESS", "TRADE_FINISHED"}
                            or not same_amount(trade.get("total_amount"), amount)
                            or not isinstance(trade.get("trade_no"), str) or not trade["trade_no"].strip()):
                        raise OrderReviewError("未能核验原订单已付款及金额一致", code="payment_unverified")
                for row, job in zip(records, jobs):
                    resolution = dict(job.payment_resolution or {})
                    if action == "record_external_refund":
                        resolution.update(state="external_refund_recorded", refund_recorded=True,
                                          updated_at=now.isoformat())
                        row.message = "管理员已登记外部全额退款；本系统未调用退款接口"
                        row.error_code = None
                    else:
                        if not self._source(job)["available"] or not self._plan_valid(job):
                            raise OrderReviewError("原稿或原购买档位不可验证，未恢复履约")
                        stats = restarted_translation_stats(job.translation_stats, attempt_id=new_attempt_id(),
                            started_at=now, model=job.translation_model or "", action_label="人工审核恢复履约")
                        row.translation_stats_json = _json(stats)
                        row.precision_polish_status = (stats.get("precision_polish") or {}).get("status", "not_used")
                        row.status, row.message, row.error_code = "pending", "人工审核通过，已排队恢复原单履约", None
                        row.output_path = None  # Prior files remain untouched and are recorded privately below.
                        row.quality_stats_json, row.metrics_summary = "{}", ""
                        session.execute(delete(ChunkRecord).where(ChunkRecord.job_id == row.id))
                        session.execute(delete(ChapterRecord).where(ChapterRecord.job_id == row.id))
                        resolution.update(state="paid", manual_fulfillment=True, updated_at=now.isoformat())
                        released.append(row.id)
                    row.payment_resolution_json = _json(resolution)
                    row.updated_at = now
                    if action == "fulfill":
                        self.store._ensure_dispatch_in_session(session, row, now=now)
            session.flush()
            after_facts = self._facts(records)
            if case is None:
                case = OrderReviewRecord(order_no=number, revision=0, state="open", context=view["context"], updated_at=now)
                session.add(case)
            case.revision += 1
            case.context = after_facts[3]
            if action != "note": case.state = "closed"
            elif not (case.state == "closed" and view["state"] == "closed"): case.state = "open"
            case.updated_at = now
            response = {"review": self._view(number, records, case, after_facts), "released": released, "duplicate": False}
            # The immutable event retains prior artifact pointers privately; no
            # API status or payment_resolution carries notes, identity or evidence.
            audit_result = {"review": response["review"], "released": released,
                            "prior_artifacts": {job.id: job.output_path for job in jobs if job.output_path}}
            session.add(OrderReviewEventRecord(id=event_id, order_no=number, revision=case.revision,
                request_id=request_id, payload_hash=payload_hash, action=action, actor=actor, note=note,
                evidence=evidence, refund_reference=refund_reference, before_context=view["context"],
                after_context=after_facts[3], result_json=_json(audit_result), created_at=now))
            session.commit()
            return response
