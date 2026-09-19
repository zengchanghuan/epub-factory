"""Owner-authorized, optional result-email subscriptions."""
import os
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, StrictStr

from . import completion_email_service as email_service


class NotificationEmailBody(BaseModel):
    email: StrictStr = Field(default="", max_length=254)


def make_completion_email_router(store, authorize_job, list_batch_jobs, authorize_batch, repair_get):
    router = APIRouter(prefix="/api/v2")

    def validate_write(request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "请使用 JSON 保存邮箱")
        origin = request.headers.get("origin")
        if origin:
            base = urlsplit(str(request.base_url))
            allowed = {f"{base.scheme}://{base.netloc}"}
            allowed.update(x.strip().rstrip("/") for x in os.environ.get(
                "CORS_ALLOW_ORIGINS", "https://fixepub.com,https://www.fixepub.com"
            ).split(",") if x.strip())
            if origin.rstrip("/") not in allowed:
                raise HTTPException(403, "请从本站任务页面保存邮箱")

    def owned_job(job_id, request):
        job = store.get(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在")
        if not authorize_job(request, job):
            raise HTTPException(403, "无权访问该任务")
        return job

    def owned_batch(batch_id, request):
        jobs = list_batch_jobs(batch_id)
        if not jobs:
            raise HTTPException(404, "批次不存在")
        if not authorize_batch(request, jobs):
            raise HTTPException(403, "无权访问该批次")
        return jobs

    def owned_repair(job_id):
        # Repair uses the existing unguessable 128-bit task id as its capability.
        if not re.fullmatch(r"[0-9a-f]{32}", job_id) or repair_get(job_id) is None:
            raise HTTPException(404, "修复任务不存在或已过期")
        return "repair:" + job_id

    def save(job_ids, email):
        try:
            email_service.set_email_subscriptions(job_ids, email)
        except email_service.EmailUnavailableError:
            raise HTTPException(503, "邮箱通知暂未启用，请稍后回到本页查看任务并下载") from None
        except email_service.EmailRateLimitError:
            raise HTTPException(429, "此任务的通知邮箱修改次数已达上限，请使用任务页面查看结果") from None
        except ValueError:
            raise HTTPException(422, "请输入一个有效的邮箱地址") from None

    def batch_view(jobs):
        subscriptions = [email_service.get_email_subscription(job.id) for job in jobs]
        enabled = [s for s in subscriptions if s.get("enabled")]
        addresses = {s.get("email", "") for s in enabled}
        states = {s.get("status") for s in enabled}
        return {
            "email": next(iter(addresses)) if len(addresses) == 1 else "",
            "enabled": bool(enabled),
            "available": bool(email_service.email_capabilities().get("available")),
            "status": next(iter(states)) if len(states) == 1 else ("mixed" if states else "disabled"),
            "mixed": len(addresses) > 1 or (bool(enabled) and len(enabled) != len(jobs)),
            "subscribed_count": len(enabled), "total_count": len(jobs), "per_file": True,
        }

    @router.get("/email-capabilities")
    def capabilities():
        return email_service.email_capabilities()

    @router.get("/jobs/{job_id}/notification-email")
    def get_job_email(job_id: str, request: Request):
        owned_job(job_id, request)
        return email_service.get_email_subscription(job_id)

    @router.put("/jobs/{job_id}/notification-email")
    def save_job_email(job_id: str, body: NotificationEmailBody, request: Request):
        validate_write(request)
        owned_job(job_id, request)
        save([job_id], body.email)
        return email_service.get_email_subscription(job_id)

    @router.get("/batches/{batch_id}/notification-email")
    def get_batch_email(batch_id: str, request: Request):
        return batch_view(owned_batch(batch_id, request))

    @router.put("/batches/{batch_id}/notification-email")
    def save_batch_email(batch_id: str, body: NotificationEmailBody, request: Request):
        validate_write(request)
        jobs = owned_batch(batch_id, request)
        save([job.id for job in jobs], body.email)
        return batch_view(jobs)

    @router.get("/repair/{job_id}/notification-email")
    def get_repair_email(job_id: str):
        return email_service.get_email_subscription(owned_repair(job_id))

    @router.put("/repair/{job_id}/notification-email")
    def save_repair_email(job_id: str, body: NotificationEmailBody, request: Request):
        validate_write(request)
        key = owned_repair(job_id)
        save([key], body.email)
        return email_service.get_email_subscription(key)

    return router
