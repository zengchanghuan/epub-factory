import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Optional

import os as _os
from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .converter import converter
from .job_runner import run_job
from .models import DeviceProfile, Job, JobStatus, OutputMode, TraditionalVariant
from .storage import job_store

# Sentry：若配置了 SENTRY_DSN，在应用启动时初始化，error_reporter 上报才会生效
_sentry_dsn = _os.environ.get("SENTRY_DSN")
if _sentry_dsn:
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=_sentry_dsn)
    except Exception:
        pass


def _use_celery() -> bool:
    """是否使用 Celery 执行转换（需配置 broker 且任务在持久化 store 中）。"""
    import os
    return bool(os.environ.get("CELERY_BROKER_URL") or os.environ.get("REDIS_URL"))


# ---------- API v2 状态与响应映射 ----------
def _job_to_v2_status(job: Job) -> str:
    """将内部 JobStatus 映射为 API v2 状态字符串。"""
    if job.status == JobStatus.pending:
        return "queued"
    if job.status == JobStatus.running:
        return "running"
    if job.status == JobStatus.success:
        return "partial_completed" if job.error_code == "PARTIAL_TRANSLATION" else "completed"
    if job.status == JobStatus.failed:
        return "failed"
    if job.status == JobStatus.cancelled:
        return "cancelled"
    return "running"


def _job_to_v2_detail(job: Job, download_url_path: str) -> dict:
    """构建 v2 任务详情响应。"""
    return {
        "job_id": job.id,
        "trace_id": job.trace_id,
        "status": _job_to_v2_status(job),
        "message": job.message,
        "source_filename": job.source_filename,
        "output_mode": job.output_mode.value,
        "device": job.device.value,
        "enable_translation": job.enable_translation,
        "target_lang": job.target_lang,
        "bilingual": job.bilingual,
        "error_code": job.error_code,
        "download_url": f"{download_url_path}" if job.output_path else None,
        "quality_stats": job.quality_stats.to_dict() if job.quality_stats else None,
        "translation_stats": job.translation_stats or None,
        "metrics_summary": job.metrics_summary or None,
        "stage_summary": _v2_stage_summary(job),
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


def _v2_stage_summary(job: Job) -> Optional[dict]:
    """从 store 的 list_stages 聚合当前阶段与进度（若有）。"""
    list_stages = getattr(job_store, "list_stages", None)
    if not list_stages:
        return None
    stages = list_stages(job.id)
    if not stages:
        return None
    # 取最后一个阶段作为 current_stage，进度可后续由阶段名推算
    last = stages[-1]
    progress = 0
    if job.status == JobStatus.success or job.status == JobStatus.failed:
        progress = 100
    elif job.status == JobStatus.running and stages:
        # 简单按阶段数估算，后续可改为按 chunk 数
        progress = min(90, len(stages) * 25)
    return {"current_stage": last.stage_name, "progress_percent": progress}

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        if hasattr(record, "trace_id"):
            payload["trace_id"] = record.trace_id
        if hasattr(record, "job_id"):
            payload["job_id"] = record.job_id
        if hasattr(record, "error_code"):
            payload["error_code"] = record.error_code
        if hasattr(record, "error_message"):
            payload["error_message"] = record.error_message
        return json.dumps(payload, ensure_ascii=False)


logger = logging.getLogger("epub_factory")
handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logger.setLevel(logging.INFO)
logger.handlers = [handler]

app = FastAPI(title="EPUB Factory API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def process_job(job: Job) -> None:
    """进程内执行转换（供 BackgroundTasks 使用）；实际逻辑在 job_runner.run_job。"""
    run_job(job.id)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/jobs")
async def create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    output_mode: OutputMode = Form(OutputMode.traditional),
    traditional_variant: TraditionalVariant = Form(TraditionalVariant.auto),
    enable_translation: bool = Form(False),
    target_lang: str = Form("zh-CN"),
    bilingual: bool = Form(False),
    glossary_json: Optional[str] = Form(None),  # JSON 字符串: '{"Harry": "哈利"}'
    device: DeviceProfile = Form(DeviceProfile.generic),
    paypal_order_id: Optional[str] = Form(None),
):
    if enable_translation:
        if not paypal_order_id:
            raise HTTPException(status_code=402, detail="AI 翻译为收费功能，请先完成支付")
        # TODO: 这里应该加一个向 PayPal 服务器验证 order_id 是否真实的逻辑
        # 验证是否 COMPLETED 且金额是对的。为了不阻塞流程，暂以订单号存在为准。
        logger.info("Payment accepted", extra={"paypal_order_id": paypal_order_id})
    if not (file.filename.lower().endswith(".epub") or file.filename.lower().endswith(".pdf")):
        raise HTTPException(status_code=400, detail="仅支持 .epub 或 .pdf 文件")

    glossary: dict = {}
    if glossary_json:
        try:
            parsed = json.loads(glossary_json)
            if isinstance(parsed, dict):
                glossary = {str(k): str(v) for k, v in parsed.items()}
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(status_code=400, detail="glossary_json 格式错误，应为 JSON 对象字符串")

    job_id = uuid.uuid4().hex[:12]
    trace_id = uuid.uuid4().hex
    input_path = UPLOAD_DIR / f"{job_id}-{file.filename}"
    with input_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    job = Job(
        id=job_id,
        source_filename=file.filename,
        output_mode=output_mode,
        trace_id=trace_id,
        input_path=str(input_path),
        enable_translation=enable_translation,
        target_lang=target_lang,
        bilingual=bilingual,
        glossary=glossary,
        device=device,
        traditional_variant=traditional_variant.value,
    )
    job_store.add(job)
    if _use_celery():
        from app.tasks.job_pipeline import run_conversion
        run_conversion.delay(job.id)
    else:
        background_tasks.add_task(process_job, job)
    return {
        "job_id": job.id,
        "trace_id": job.trace_id,
        "status": job.status,
        "enable_translation": job.enable_translation,
        "target_lang": job.target_lang,
        "bilingual": job.bilingual,
        "device": job.device,
        "traditional_variant": job.traditional_variant,
        "message": "任务已创建",
    }


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str):
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "job_id": job.id,
        "trace_id": job.trace_id,
        "source_filename": job.source_filename,
        "output_mode": job.output_mode,
        "enable_translation": job.enable_translation,
        "target_lang": job.target_lang,
        "device": job.device,
        "status": job.status,
        "message": job.message,
        "error_code": job.error_code,
        "quality_stats": job.quality_stats.to_dict() if job.quality_stats else None,
        "translation_stats": job.translation_stats or None,
        "metrics_summary": job.metrics_summary or None,
        "download_url": f"/api/v1/jobs/{job.id}/download" if job.output_path else None,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


@app.get("/api/v1/jobs/{job_id}/download")
def download_result(job_id: str):
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status != JobStatus.success or not job.output_path:
        raise HTTPException(status_code=400, detail="任务未完成，无法下载")
    output_path = Path(job.output_path)
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="结果文件不存在")
    return FileResponse(path=output_path, filename=output_path.name, media_type="application/epub+zip")


# ---------- API v2 骨架 ----------

@app.post("/api/v2/jobs")
async def create_job_v2(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    output_mode: OutputMode = Form(OutputMode.traditional),
    traditional_variant: TraditionalVariant = Form(TraditionalVariant.auto),
    enable_translation: bool = Form(False),
    target_lang: str = Form("zh-CN"),
    bilingual: bool = Form(False),
    glossary_json: Optional[str] = Form(None),
    device: DeviceProfile = Form(DeviceProfile.generic),
    paypal_order_id: Optional[str] = Form(None),
    temperature: Optional[float] = Form(None),
):
    """上传文件并创建后台任务，创建后立即返回（v2 契约）。temperature 不传时：翻译任务默认 1.3，纯格式转换不调用 LLM。"""
    import os as _os
    _skip_payment = _os.environ.get("SKIP_PAYMENT_CHECK", "").lower() in ("1", "true", "yes")
    if enable_translation and not paypal_order_id and not _skip_payment:
        raise HTTPException(status_code=402, detail="AI 翻译为收费功能，请先完成支付")
    if not (file.filename and (file.filename.lower().endswith(".epub") or file.filename.lower().endswith(".pdf"))):
        raise HTTPException(status_code=400, detail="仅支持 .epub 或 .pdf 文件")

    glossary: dict = {}
    if glossary_json:
        try:
            parsed = json.loads(glossary_json)
            if isinstance(parsed, dict):
                glossary = {str(k): str(v) for k, v in parsed.items()}
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(status_code=400, detail="glossary_json 格式错误，应为 JSON 对象字符串")

    if temperature is None and enable_translation:
        temperature = 1.3
    elif not enable_translation:
        temperature = None

    job_id = uuid.uuid4().hex[:12]
    trace_id = uuid.uuid4().hex
    input_path = UPLOAD_DIR / f"{job_id}-{file.filename}"
    with input_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    job = Job(
        id=job_id,
        source_filename=file.filename,
        output_mode=output_mode,
        trace_id=trace_id,
        input_path=str(input_path),
        enable_translation=enable_translation,
        target_lang=target_lang,
        bilingual=bilingual,
        glossary=glossary,
        device=device,
        temperature=temperature,
        traditional_variant=traditional_variant.value,
    )
    job_store.add(job)
    if _use_celery():
        from app.tasks.job_pipeline import run_conversion
        run_conversion.delay(job.id)
    else:
        background_tasks.add_task(process_job, job)

    return {
        "job_id": job.id,
        "trace_id": job.trace_id,
        "status": "queued",
        "message": "任务已创建，已进入后台队列",
        "source_filename": job.source_filename,
        "enable_translation": job.enable_translation,
        "target_lang": job.target_lang,
        "bilingual": job.bilingual,
        "device": job.device.value,
        "traditional_variant": job.traditional_variant,
        "created_at": job.created_at.isoformat(),
    }


@app.get("/api/v2/jobs")
def list_jobs_v2(limit: int = 100):
    """获取任务中心列表（v2）。"""
    list_fn = getattr(job_store, "list_jobs", None)
    if not list_fn:
        return {"items": []}
    jobs = list_fn(limit=limit)
    items = [
        {
            "job_id": j.id,
            "trace_id": j.trace_id,
            "status": _job_to_v2_status(j),
            "message": j.message,
            "source_filename": j.source_filename,
            "created_at": j.created_at.isoformat(),
            "updated_at": j.updated_at.isoformat(),
        }
        for j in jobs
    ]
    return {"items": items}


@app.get("/api/v2/jobs/{job_id}")
def get_job_v2(job_id: str):
    """获取任务详情（v2 契约）。"""
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    download_path = f"/api/v2/jobs/{job.id}/download" if job.output_path else None
    return _job_to_v2_detail(job, download_path)


@app.get("/api/v2/jobs/{job_id}/download")
def download_result_v2(job_id: str):
    """下载结果 EPUB（v2）。"""
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status != JobStatus.success or not job.output_path:
        raise HTTPException(status_code=400, detail="任务未完成，无法下载")
    output_path = Path(job.output_path)
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="结果文件不存在")
    return FileResponse(path=output_path, filename=output_path.name, media_type="application/epub+zip")


def _v2_job_stats(job_id: str) -> dict:
    """从 store 的 chapters/chunks 聚合 stats，若无则返回占位。"""
    job = job_store.get(job_id)
    if not job:
        return {}
    list_chapters = getattr(job_store, "list_chapters", None)
    list_chunks = getattr(job_store, "list_chunks", None)
    summary = {
        "status": _job_to_v2_status(job),
        "chapters_total": 0,
        "chapters_completed": 0,
        "chapters_partial": 0,
        "chapters_failed": 0,
        "chunks_total": 0,
        "chunks_translated": 0,
        "chunks_cached": 0,
        "chunks_failed": 0,
        "tokens_total": 0,
        "cost_usd": 0.0,
    }
    if list_chapters:
        chapters = list_chapters(job_id)
        summary["chapters_total"] = len(chapters)
        from .models import ChapterStatus
        summary["chapters_completed"] = sum(1 for c in chapters if c.status == ChapterStatus.completed)
        summary["chapters_partial"] = sum(1 for c in chapters if c.status == ChapterStatus.partial_completed)
        summary["chapters_failed"] = sum(1 for c in chapters if c.status == ChapterStatus.failed)
    if list_chunks:
        chunks = list_chunks(job_id)
        summary["chunks_total"] = len(chunks)
        from .models import ChunkStatus
        summary["chunks_translated"] = sum(1 for c in chunks if c.status == ChunkStatus.translated)
        summary["chunks_cached"] = sum(1 for c in chunks if c.status == ChunkStatus.cached)
        summary["chunks_failed"] = sum(1 for c in chunks if c.status == ChunkStatus.failed)
    if job.translation_stats:
        summary["tokens_total"] = job.translation_stats.get("total_tokens") or 0
        summary["cost_usd"] = float(job.translation_stats.get("cost_usd") or 0)
    return {"job_id": job_id, "summary": summary}


@app.get("/api/v2/jobs/{job_id}/stats")
def get_job_stats_v2(job_id: str):
    """获取章节/块级聚合统计（v2）。"""
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _v2_job_stats(job_id)


def _v2_job_events(job_id: str) -> list:
    """从 store 的 list_stages 转为 events 列表。"""
    list_stages = getattr(job_store, "list_stages", None)
    if not list_stages:
        return []
    stages = list_stages(job_id)
    return [
        {
            "time": (s.finished_at or s.started_at).isoformat(),
            "level": "info",
            "stage": s.stage_name,
            "message": (s.metadata.get("message") if getattr(s, "metadata", None) else None) or s.stage_name,
        }
        for s in stages
    ]


@app.get("/api/v2/jobs/{job_id}/events")
def get_job_events_v2(job_id: str):
    """获取结构化阶段日志（v2）。"""
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"items": _v2_job_events(job_id)}


@app.post("/api/v2/jobs/{job_id}/cancel")
def cancel_job_v2(job_id: str):
    """取消任务（v2）。仅当任务为 queued 或 running 时可取消。"""
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status not in (JobStatus.pending, JobStatus.running):
        raise HTTPException(status_code=400, detail="当前状态不可取消")
    job_store.update_status(job_id, JobStatus.cancelled, "用户取消")
    updated = job_store.get(job_id)
    return {
        "job_id": job_id,
        "status": "cancelled",
        "message": "已取消",
    }


@app.get("/api/v2/notifications")
def list_notifications_v2(job_id: Optional[str] = None):
    """获取站内通知列表（v2）。"""
    list_fn = getattr(job_store, "list_notifications", None)
    if not list_fn:
        return {"items": []}
    notifications = list_fn(job_id=job_id)
    items = []
    for n in notifications:
        items.append({
            "id": getattr(n, "id", id(n)),
            "job_id": n.job_id,
            "channel": n.channel,
            "status": n.status.value,
            "payload": getattr(n, "payload", None) or {},
            "created_at": n.created_at.isoformat() if hasattr(n.created_at, "isoformat") else str(n.created_at),
        })
    return {"items": items}


@app.get("/api/v2/admin/translation-stats", include_in_schema=False)
def get_admin_translation_stats(
    request: Request,
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
):
    """
    后台用：汇总所有任务的 Token 用量与预估费用（供自己查看成本）。
    若设置环境变量 ADMIN_SECRET，则请求头需带 X-Admin-Key: <ADMIN_SECRET> 或 ?admin_key=<ADMIN_SECRET>。
    """
    secret = _os.environ.get("ADMIN_SECRET")
    if secret:
        key = x_admin_key or request.query_params.get("admin_key")
        if key != secret:
            raise HTTPException(status_code=403, detail="需要有效的管理员密钥")
    list_fn = getattr(job_store, "list_jobs", None)
    if not list_fn:
        return {"total_prompt_tokens": 0, "total_completion_tokens": 0, "total_tokens": 0, "total_cost_usd": 0.0, "jobs_count": 0, "by_job": []}
    jobs = list_fn(limit=500)
    total_prompt = 0
    total_completion = 0
    total_cost = 0.0
    by_job = []
    for job in jobs:
        st = getattr(job, "translation_stats", None) or {}
        if not st:
            continue
        pt = int(st.get("prompt_tokens") or 0)
        ct = int(st.get("completion_tokens") or 0)
        cost = float(st.get("cost_usd") or 0)
        total_prompt += pt
        total_completion += ct
        total_cost += cost
        by_job.append({
            "job_id": job.id,
            "source_filename": job.source_filename,
            "created_at": job.created_at.isoformat() if hasattr(job.created_at, "isoformat") else str(job.created_at),
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
            "cost_usd": round(cost, 4),
        })
    return {
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_tokens": total_prompt + total_completion,
        "total_cost_usd": round(total_cost, 4),
        "jobs_count": len(by_job),
        "by_job": by_job,
    }


@app.get("/api/v2/admin/error-stats", include_in_schema=False)
def get_admin_error_stats(
    request: Request,
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    since: Optional[str] = None,
    limit: int = 50,
):
    """
    后台用：汇总失败任务的错误码分布与最近明细。
    若设置环境变量 ADMIN_SECRET，则请求头需带 X-Admin-Key。
    可选参数 ?since=2024-01-01（ISO 日期）过滤时间范围，?limit=N 控制明细数量。
    """
    secret = _os.environ.get("ADMIN_SECRET")
    if secret:
        key = x_admin_key or request.query_params.get("admin_key")
        if key != secret:
            raise HTTPException(status_code=403, detail="需要有效的管理员密钥")

    list_fn = getattr(job_store, "list_jobs", None)
    if not list_fn:
        return {"total_failed": 0, "by_error_code": {}, "recent": []}

    all_jobs = list_fn(limit=2000)

    # 可选时间过滤
    since_dt = None
    if since:
        try:
            from datetime import datetime as _dt
            since_dt = _dt.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            pass

    failed_jobs = [
        j for j in all_jobs
        if j.status.value == "failed"
        and (since_dt is None or j.created_at >= since_dt)
    ]

    by_error_code: dict = {}
    for j in failed_jobs:
        code = j.error_code or "UNKNOWN"
        by_error_code[code] = by_error_code.get(code, 0) + 1

    recent = []
    for j in failed_jobs[:limit]:
        recent.append({
            "job_id": j.id,
            "source_filename": j.source_filename,
            "error_code": j.error_code or "UNKNOWN",
            "message": j.message or "",
            "output_mode": j.output_mode.value if hasattr(j.output_mode, "value") else str(j.output_mode),
            "enable_translation": j.enable_translation,
            "created_at": j.created_at.isoformat() if hasattr(j.created_at, "isoformat") else str(j.created_at),
        })

    return {
        "total_failed": len(failed_jobs),
        "by_error_code": by_error_code,
        "recent": recent,
    }


# ---------- 前端静态资源（与 API 同域，避免跨域） ----------
_FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

# 避免浏览器请求 /favicon.ico 时返回 404（Chrome 等会默认请求）
@app.get("/favicon.ico", include_in_schema=False)
def _favicon():
    favicon_path = _FRONTEND_DIR / "favicon.ico"
    if favicon_path.is_file():
        return FileResponse(str(favicon_path), media_type="image/x-icon")
    return Response(status_code=204)

if _FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")

