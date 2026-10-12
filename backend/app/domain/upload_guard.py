"""Reuse recent identical orders within an authenticated user or browser session."""
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException
from app.infra.execution_lease import execution_lease, ExecutionLeaseUnavailable
from app.models import JobStatus


@contextmanager
def upload_guard(store, *, user_id, client_session, paths, options, allow_access_recovery=True):
    scope = f"user:{user_id}" if user_id else f"session:{client_session}" if client_session else ""
    if not scope:
        yield ("", None)
        return
    sources = []
    for path in paths:
        digest = hashlib.sha256()
        with Path(path).open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        sources.append([Path(path).suffix.lower(), digest.hexdigest()])
    fingerprint = hashlib.sha256(json.dumps([scope, sorted(sources), options],
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    try:
        with execution_lease(f"upload:{fingerprint}", "") as lease:
            if lease is None:
                raise HTTPException(409, "相同文件正在创建订单，请稍后在任务中心查看，勿重复付款。")
            if user_id and hasattr(store, "list_jobs_by_user_id"):
                jobs = store.list_jobs_by_user_id(user_id, limit=1000)
            else:
                jobs = store.list_jobs_by_creator_session(client_session, limit=1000)
            cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            for job in jobs:
                created = job.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                stats = job.translation_stats or {}
                if (created >= cutoff and (job.status != JobStatus.cancelled or stats.get("payment_verified")
                        or (job.payment_resolution or {}).get("state") in {"paid", "paid_review"}
                        or (job.payment_entitlement or {}).get("state") == "paid")
                        and stats.get("upload_fingerprint") == fingerprint):
                    # Matching content/settings is required in addition to the
                    # authenticated owner or original anonymous session. Never
                    # recover a claimed account's token from its former session.
                    owner_matches = (job.user_id == user_id if user_id else
                                     not job.user_id and bool(client_session)
                                     and job.creator_session == client_session)
                    expires = job.token_expires_at
                    if expires is not None and expires.tzinfo is None:
                        expires = expires.replace(tzinfo=timezone.utc)
                    can_recover = (allow_access_recovery and owner_matches and job.access_token
                                   and (expires is None or expires > datetime.now(timezone.utc)))
                    detail = {"code": "duplicate_upload", "job_id": job.id,
                        "batch_id": job.batch_id,
                        "message": "已找到相同文件和设置的订单，正在打开原任务；请勿重复付款。"}
                    if can_recover:
                        detail["access_token"] = job.access_token
                    else:
                        detail["message"] = "已找到原订单，但当前无法恢复访问，请使用原任务链接或联系客服；请勿重复付款。"
                    raise HTTPException(409, detail, headers={"Cache-Control": "no-store"})
            yield fingerprint, lease
    except ExecutionLeaseUnavailable:
        raise HTTPException(503, "订单去重服务暂不可用，请稍后重试。") from None
