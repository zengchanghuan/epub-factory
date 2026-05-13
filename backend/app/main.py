import json
import logging
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
import os as _os

# Load environment variables from .env file if it exists
dotenv_path = Path(__file__).resolve().parent.parent / ".env"
if dotenv_path.exists():
    load_dotenv(dotenv_path)

from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .converter import converter
from .infra.rate_limiter import MAX_FILE_SIZE_BYTES, MAX_FILE_SIZE_MB, get_real_ip, rate_limiter
from .infra.alipay import init_alipay, create_alipay_page_pay, verify_alipay_notification
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


# 翻译任务的应付金额（元）；通过环境变量集中管理，避免 hardcode 在 webhook 校验里。
TRANSLATION_PRICE_CNY: str = _os.environ.get("TRANSLATION_PRICE_CNY", "0.01").strip() or "0.01"


def _amount_equal(a: str, b: str) -> bool:
    """容错的金额相等判断：忽略尾随零、空白与符号差异（"0.01" == "0.010" == " 0.01 "）。"""
    from decimal import Decimal, InvalidOperation
    try:
        return Decimal(a) == Decimal(b)
    except (InvalidOperation, ValueError, TypeError):
        return False


# ---------- API v2 状态与响应映射 ----------
def _job_to_v2_status(job: Job) -> str:
    """将内部 JobStatus 映射为 API v2 状态字符串。"""
    if job.status == JobStatus.pending_payment:
        return "pending_payment"
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


def _safe_upload_name(filename: str) -> str:
    """Sanitize upload filename to avoid path traversal and bad chars."""
    base = os.path.basename(filename or "")
    if not base:
        return "upload.bin"
    # Keep only conservative chars.
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    if cleaned.startswith("."):
        cleaned = cleaned.lstrip(".")
    return cleaned or "upload.bin"


def _extract_job_token(request: Request, job_id: str) -> Optional[str]:
    header_token = request.headers.get("X-Job-Token")
    if header_token:
        return header_token.strip()
    query_token = request.query_params.get("token")
    if query_token:
        return query_token.strip()
    cookie_token = request.cookies.get(f"job_token_{job_id}")
    if cookie_token:
        return cookie_token.strip()
    return None


def _get_client_session(request: Request) -> str:
    return request.headers.get("X-Client-Session", "").strip()


def _anonymize_ip(ip: str) -> str:
    """
    对 IP 做最小可观测脱敏：
    - IPv4: 1.2.3.4 → 1.2.3.0     （保留 /24，足以用于地区/运营商粗粒度统计）
    - IPv6: a:b:c:d::1 → a:b:c:d:: （保留 /64）
    - 其他/解析失败：返回空字符串，避免泄漏原文
    """
    import ipaddress
    if not ip:
        return ""
    try:
        obj = ipaddress.ip_address(ip.strip())
    except ValueError:
        return ""
    if isinstance(obj, ipaddress.IPv4Address):
        parts = ip.split(".")
        if len(parts) == 4:
            parts[3] = "0"
            return ".".join(parts)
        return ""
    # IPv6: 保留前 64 位
    try:
        net = ipaddress.IPv6Network(f"{obj}/64", strict=False)
        return f"{net.network_address}"
    except ValueError:
        return ""


def _require_admin(request: Request, x_admin_key: Optional[str]) -> None:
    """
    强制 /api/v2/admin/* 接口的访问鉴权：
    - 未配置环境变量 ADMIN_SECRET → 503，避免运维忘配时整套 admin 接口裸开
    - 配置了但 key 不对 / 缺失 → 403
    - 使用 hmac.compare_digest 做常量时间比较，避免侧信道
    """
    import hmac
    secret = (_os.environ.get("ADMIN_SECRET") or "").strip()
    if not secret:
        logger.warning("admin endpoint accessed but ADMIN_SECRET not configured")
        raise HTTPException(status_code=503, detail="管理员接口未启用")
    key = (x_admin_key or request.query_params.get("admin_key") or "").strip()
    if not key or not hmac.compare_digest(key.encode("utf-8"), secret.encode("utf-8")):
        raise HTTPException(status_code=403, detail="需要有效的管理员密钥")


_LEGACY_ACCESS_WINDOW_DAYS = int(_os.environ.get("LEGACY_ACCESS_WINDOW_DAYS", "30"))


def _authorize_job_access(request: Request, job: Job) -> bool:
    """
    匿名安全的访问鉴权：
    1) 任务有 access_token → 严格按 X-Job-Token / cookie / ?token= 校验；
    2) 老任务（无 access_token）→ 仅在 LEGACY_ACCESS_WINDOW_DAYS（默认 30 天）内
       允许 session 或 IP 兜底；超过窗口直接拒绝，避免同 NAT 出口 IP 互访。
    """
    token = _extract_job_token(request, job.id)
    if job.access_token:
        return bool(token and token == job.access_token)

    from datetime import datetime, timedelta, timezone as _tz
    created = job.created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=_tz.utc)
    if created is None or (datetime.now(_tz.utc) - created) > timedelta(days=_LEGACY_ACCESS_WINDOW_DAYS):
        return False

    creator_session = (job.creator_session or "").strip()
    if creator_session and creator_session == _get_client_session(request):
        return True
    creator_ip = (job.creator_ip or "").strip()
    return bool(creator_ip and creator_ip == get_real_ip(request))

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
init_alipay()

_allowed_origins_raw = _os.environ.get("CORS_ALLOW_ORIGINS", "https://fixepub.com,https://www.fixepub.com")
_allowed_origins = [o.strip() for o in _allowed_origins_raw.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 静态文件挂载在 "/" 时，任何被丢进 frontend/ 的文件都会被服务出去；
# 这里用一道黑名单兜底：明显属于"凭据 / 数据库 / 备份 / 日志"的扩展名直接 404，
# 避免运维误把 .env / .bak / .csv / *.db 之类放进前端目录被公网下载。
_DENY_PATH_SUFFIXES = (
    ".bak", ".backup", ".old", ".orig", ".swp", ".tmp",
    ".log", ".sql", ".sqlite", ".sqlite3", ".db", ".dbf",
    ".env", ".ini", ".conf", ".cfg",
    ".csv", ".tsv", ".xlsx",
    ".pem", ".key", ".crt", ".cer", ".p12", ".pfx",
    ".pyc", ".pyo",
    ".git", ".gitignore",
)


@app.middleware("http")
async def _block_sensitive_files(request: Request, call_next):
    path = request.url.path.lower()
    if any(path.endswith(suf) for suf in _DENY_PATH_SUFFIXES) or "/.git" in path or "/.env" in path:
        return Response(status_code=404)
    return await call_next(request)


import requests

def process_job(job: Job) -> None:
    """进程内执行转换（供 BackgroundTasks 使用）；实际逻辑在 job_runner.run_job。"""
    run_job(job.id)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/jobs")
async def create_job(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    output_mode: OutputMode = Form(OutputMode.traditional),
    traditional_variant: TraditionalVariant = Form(TraditionalVariant.auto),
    enable_translation: bool = Form(False),
    target_lang: str = Form("zh-CN"),
    bilingual: bool = Form(False),
    glossary_json: Optional[str] = Form(None),  # JSON 字符串: '{"Harry": "哈利"}'
    device: DeviceProfile = Form(DeviceProfile.generic),
    out_trade_no: Optional[str] = Form(None),
):
    import os as _os
    _skip_payment = _os.environ.get("SKIP_PAYMENT_CHECK", "").lower() in ("1", "true", "yes")

    # v1 端点没有接入真实的支付订单校验，禁止从 v1 申请 AI 翻译。
    # 付费翻译统一走 /api/v2/jobs（带支付宝下单 + 异步回调验签）。
    if enable_translation and not _skip_payment:
        raise HTTPException(
            status_code=410,
            detail="付费翻译已下线在 v1 接口，请改用 POST /api/v2/jobs（含支付下单与回调验签）",
        )

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
    safe_name = _safe_upload_name(file.filename)
    input_path = UPLOAD_DIR / f"{job_id}-{safe_name}"
    with input_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    client_session = _get_client_session(request) or uuid.uuid4().hex
    job = Job(
        id=job_id,
        source_filename=safe_name,
        output_mode=output_mode,
        trace_id=trace_id,
        input_path=str(input_path),
        access_token=uuid.uuid4().hex,
        creator_ip=get_real_ip(request),
        creator_session=client_session,
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
        "access_token": job.access_token,
        "status": job.status,
        "enable_translation": job.enable_translation,
        "target_lang": job.target_lang,
        "bilingual": job.bilingual,
        "device": job.device,
        "traditional_variant": job.traditional_variant,
        "message": "任务已创建",
    }


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not _authorize_job_access(request, job):
        raise HTTPException(status_code=403, detail="无权访问该任务")
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
def download_result(job_id: str, request: Request):
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not _authorize_job_access(request, job):
        raise HTTPException(status_code=403, detail="无权访问该任务")
    if job.status != JobStatus.success or not job.output_path:
        raise HTTPException(status_code=400, detail="任务未完成，无法下载")
    output_path = Path(job.output_path)
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="结果文件不存在")
    return FileResponse(path=output_path, filename=output_path.name, media_type="application/epub+zip")


# ---------- API v2 骨架 ----------

@app.post("/api/v2/jobs")
async def create_job_v2(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    output_mode: OutputMode = Form(OutputMode.traditional),
    traditional_variant: TraditionalVariant = Form(TraditionalVariant.auto),
    enable_translation: bool = Form(False),
    target_lang: str = Form("zh-CN"),
    bilingual: bool = Form(False),
    glossary_json: Optional[str] = Form(None),
    device: DeviceProfile = Form(DeviceProfile.generic),
    out_trade_no: Optional[str] = Form(None),
    temperature: Optional[float] = Form(None),
):
    """上传文件并创建后台任务，创建后立即返回（v2 契约）。temperature 不传时：翻译任务默认 1.3，纯格式转换不调用 LLM。"""
    import os as _os
    _skip_payment = _os.environ.get("SKIP_PAYMENT_CHECK", "").lower() in ("1", "true", "yes")
    client_session = _get_client_session(request) or uuid.uuid4().hex
    if not (file.filename and (file.filename.lower().endswith(".epub") or file.filename.lower().endswith(".pdf"))):
        raise HTTPException(status_code=400, detail="仅支持 .epub 或 .pdf 文件")

    # ── 免费配额检查（仅对非付费任务生效；付费翻译不受免费次数限制） ───────────────
    client_ip = get_real_ip(request)
    if not enable_translation:
        allowed, count = rate_limiter.check_and_increment(client_ip)
        if not allowed:
            from app.infra.rate_limiter import FREE_DAILY_LIMIT
            raise HTTPException(
                status_code=429,
                detail=f"今日免费转换次数已用完（每日上限 {FREE_DAILY_LIMIT} 次），请明天再试。",
            )

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

    if enable_translation and not _skip_payment and out_trade_no:
        raise HTTPException(status_code=400, detail="不允许客户端自带订单号")

    job_id = uuid.uuid4().hex[:12]
    trace_id = uuid.uuid4().hex
    safe_name = _safe_upload_name(file.filename)
    input_path = UPLOAD_DIR / f"{job_id}-{safe_name}"

    # ── Content-Length 预拦（攻击者可伪造，仅用作"明显过大"的快速短路） ──
    try:
        declared_len = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared_len = 0
    # multipart 表单还会带表单字段开销，宽放 64KB 阈值
    if declared_len and declared_len > MAX_FILE_SIZE_BYTES + 64 * 1024:
        if not enable_translation:
            rate_limiter.reset_ip(client_ip)
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（约 {declared_len // 1024 // 1024}MB），免费转换最大支持 {MAX_FILE_SIZE_MB}MB。",
        )

    # ── 写盘 + 流式大小校验：边读边数，超阈值立刻中断 ─────────────────
    bytes_written = 0
    over_size = False
    try:
        with input_path.open("wb") as buffer:
            while True:
                chunk = file.file.read(1024 * 1024)  # 1MB / chunk
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > MAX_FILE_SIZE_BYTES:
                    over_size = True
                    break
                buffer.write(chunk)
    except Exception:
        input_path.unlink(missing_ok=True)
        raise

    if over_size:
        input_path.unlink(missing_ok=True)
        if not enable_translation:
            rate_limiter.reset_ip(client_ip)
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（>{MAX_FILE_SIZE_MB}MB），免费转换最大支持 {MAX_FILE_SIZE_MB}MB。",
        )
    file_size = bytes_written

    # ── 通过所有"无副作用"的校验后，才创建支付订单 ────────────────────
    pay_url = None
    job_status = JobStatus.pending
    expected_amount = ""
    if enable_translation and not _skip_payment:
        try:
            expected_amount = TRANSLATION_PRICE_CNY
            pay_url = create_alipay_page_pay(
                out_trade_no=job_id,
                total_amount=expected_amount,
                subject=f"EPUB AI 翻译服务 - {_safe_upload_name(file.filename)[:50]}",
                return_url=f"https://fixepub.com/?job_id={job_id}"
            )
            job_status = JobStatus.pending_payment
            logger.info("Created alipay payment order", extra={"job_id": job_id})
        except Exception as e:
            input_path.unlink(missing_ok=True)
            logger.error(f"Failed to create alipay order: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="支付渠道暂时不可用，请稍后重试或联系客服")

    job = Job(
        id=job_id,
        source_filename=safe_name,
        output_mode=output_mode,
        trace_id=trace_id,
        input_path=str(input_path),
        access_token=uuid.uuid4().hex,
        creator_ip=client_ip,
        creator_session=client_session,
        expected_amount=expected_amount,
        enable_translation=enable_translation,
        target_lang=target_lang,
        bilingual=bilingual,
        glossary=glossary,
        device=device,
        temperature=temperature,
        traditional_variant=traditional_variant.value,
        status=job_status,
    )
    job_store.add(job)

    if job_status == JobStatus.pending:
        if _use_celery():
            from app.tasks.job_pipeline import run_conversion
            run_conversion.delay(job.id)
        else:
            background_tasks.add_task(process_job, job)

    return {
        "job_id": job.id,
        "trace_id": job.trace_id,
        "access_token": job.access_token,
        "status": _job_to_v2_status(job),
        "message": "请完成支付以启动翻译任务" if job_status == JobStatus.pending_payment else "任务已创建，已进入后台队列",
        "source_filename": job.source_filename,
        "enable_translation": job.enable_translation,
        "target_lang": job.target_lang,
        "bilingual": job.bilingual,
        "device": job.device.value,
        "traditional_variant": job.traditional_variant,
        "created_at": job.created_at.isoformat(),
        "pay_url": pay_url,
    }


@app.get("/api/v2/jobs")
def list_jobs_v2(request: Request, limit: int = 100):
    """获取任务中心列表（v2）。"""
    session_list_fn = getattr(job_store, "list_jobs_by_creator_session", None)
    ip_list_fn = getattr(job_store, "list_jobs_by_creator_ip", None)
    all_list_fn = getattr(job_store, "list_jobs", None)
    list_fn = session_list_fn or ip_list_fn or all_list_fn
    if not list_fn:
        return {"items": []}
    client_ip = get_real_ip(request)
    client_session = _get_client_session(request)
    if session_list_fn and client_session:
        jobs = session_list_fn(client_session, limit=limit)
    elif ip_list_fn:
        jobs = ip_list_fn(client_ip, limit=limit)
    else:
        jobs = [j for j in all_list_fn(limit=limit) if j.creator_ip == client_ip]
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
def get_job_v2(job_id: str, request: Request):
    """获取任务详情（v2 契约）。"""
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not _authorize_job_access(request, job):
        raise HTTPException(status_code=403, detail="无权访问该任务")
    download_path = f"/api/v2/jobs/{job.id}/download" if job.output_path else None
    return _job_to_v2_detail(job, download_path)


@app.get("/api/v2/jobs/{job_id}/download")
def download_result_v2(job_id: str, request: Request):
    """下载结果 EPUB（v2）。"""
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not _authorize_job_access(request, job):
        raise HTTPException(status_code=403, detail="无权访问该任务")
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
def get_job_stats_v2(job_id: str, request: Request):
    """获取章节/块级聚合统计（v2）。"""
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not _authorize_job_access(request, job):
        raise HTTPException(status_code=403, detail="无权访问该任务")
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
def get_job_events_v2(job_id: str, request: Request):
    """获取结构化阶段日志（v2）。"""
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not _authorize_job_access(request, job):
        raise HTTPException(status_code=403, detail="无权访问该任务")
    return {"items": _v2_job_events(job_id)}


@app.post("/api/v2/jobs/{job_id}/cancel")
def cancel_job_v2(job_id: str, request: Request):
    """取消任务（v2）。仅当任务为 queued 或 running 时可取消。"""
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not _authorize_job_access(request, job):
        raise HTTPException(status_code=403, detail="无权访问该任务")
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
    需要环境变量 ADMIN_SECRET，并通过请求头 X-Admin-Key 或 ?admin_key= 传递。
    """
    _require_admin(request, x_admin_key)
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
    需要环境变量 ADMIN_SECRET，并通过请求头 X-Admin-Key 或 ?admin_key= 传递。
    可选参数 ?since=2024-01-01（ISO 日期）过滤时间范围，?limit=N 控制明细数量。
    """
    _require_admin(request, x_admin_key)

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


# ---------- 支付回调 Webhook (支付宝/微信) ----------

@app.post("/api/v2/webhooks/alipay", include_in_schema=False)
async def alipay_webhook(request: Request):
    """
    接收支付宝异步通知
    """
    try:
        # 支付宝通常是 application/x-www-form-urlencoded
        body = await request.body()
        if not body:
            return Response("fail")
            
        params = {}
        if request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            form_data = await request.form()
            params = dict(form_data)
        else:
            # Fallback for testing/other content types
            try:
                params = await request.json()
            except:
                pass
                
        if not params:
            return Response("fail")
            
        # 验签
        is_valid = verify_alipay_notification(params.copy())
        if not is_valid:
            logger.warning("Alipay webhook signature verification failed")
            return Response("fail")
            
        trade_status = params.get("trade_status")
        out_trade_no = params.get("out_trade_no")
        app_id = params.get("app_id")
        seller_id = params.get("seller_id")
        total_amount = params.get("total_amount")

        expected_app_id = _os.environ.get("ALIPAY_APP_ID", "").strip()
        expected_seller_id = _os.environ.get("ALIPAY_SELLER_ID", "").strip()

        if expected_app_id and app_id != expected_app_id:
            logger.warning("Alipay webhook app_id mismatch", extra={"job_id": out_trade_no or ""})
            return Response("fail")
        if expected_seller_id and seller_id != expected_seller_id:
            logger.warning("Alipay webhook seller_id mismatch", extra={"job_id": out_trade_no or ""})
            return Response("fail")
        
        if trade_status == "TRADE_SUCCESS" and out_trade_no:
            job = job_store.get(out_trade_no)
            if not job:
                # 未知订单：告诉支付宝"已收到"避免无限重试，但记日志以便排查
                logger.warning("Alipay webhook for unknown job", extra={"job_id": out_trade_no})
                return Response("success")

            # 订单金额二次校验：必须等于下单时落库的 expected_amount。
            # 兼容旧数据：若 expected_amount 为空（老任务），回退到环境定价。
            expected = (getattr(job, "expected_amount", "") or TRANSLATION_PRICE_CNY).strip()
            actual = str(total_amount or "").strip()
            if not _amount_equal(actual, expected):
                logger.warning("Alipay webhook amount mismatch", extra={"job_id": out_trade_no})
                return Response("fail")

            # 条件原子更新：只有"首次确认支付成功"的 webhook 会拿到 True，
            # 后续重试 / 并发回调一律返回 False，避免重复入队 → 重复消费 Token。
            try_mark = getattr(job_store, "try_mark_paid", None)
            won_race = bool(try_mark(job.id)) if callable(try_mark) else (
                job.status == JobStatus.pending_payment
                and bool(job_store.update_status(job.id, JobStatus.pending, "支付成功，排队中..."))
            )
            if not won_race:
                logger.info("Alipay webhook ignored (already processed)", extra={"job_id": out_trade_no})
                return Response("success")

            logger.info(f"Alipay payment verified, starting job {out_trade_no}")
            if _use_celery():
                from app.tasks.job_pipeline import run_conversion
                run_conversion.delay(job.id)
            else:
                import threading
                threading.Thread(target=process_job, args=(job,)).start()

        return Response("success")
        
    except Exception as e:
        logger.error(f"Error processing alipay webhook: {e}", exc_info=True)
        return Response("fail")




# ---------- 流量统计 ----------

@app.post("/api/v2/track/pv", include_in_schema=False)
async def track_pv(request: Request):
    """记录页面访问（PV）。"""
    import json as _json
    from datetime import datetime, timezone
    
    ip = _anonymize_ip(get_real_ip(request))
    ua = request.headers.get("user-agent", "")
    ts = datetime.now(timezone.utc).isoformat()
    
    # 存入 visits.jsonl
    pv_file = BASE_DIR / "visits.jsonl"
    try:
        with pv_file.open("a", encoding="utf-8") as f:
            f.write(_json.dumps({"ts": ts, "ip": ip, "ua": ua}, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/v2/admin/visits", include_in_schema=False)
def get_admin_visits(
    request: Request,
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    days: int = 7,
):
    """查看访问统计。需要 ADMIN_SECRET 鉴权。"""
    import json as _json
    from datetime import datetime, timedelta, timezone
    _require_admin(request, x_admin_key)

    pv_file = BASE_DIR / "visits.jsonl"
    stats = {"total_pv": 0, "unique_ips": set(), "daily": {}}
    
    if pv_file.exists():
        limit_date = (datetime.now(timezone.utc) - timedelta(days=days)).date()
        try:
            with pv_file.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        v = _json.loads(line)
                        dt = datetime.fromisoformat(v["ts"])
                        if dt.date() < limit_date: continue
                        
                        date_str = dt.date().isoformat()
                        stats["total_pv"] += 1
                        stats["unique_ips"].add(v["ip"])
                        
                        if date_str not in stats["daily"]:
                            stats["daily"][date_str] = {"pv": 0, "uv": set()}
                        stats["daily"][date_str]["pv"] += 1
                        stats["daily"][date_str]["uv"].add(v["ip"])
                    except Exception:
                        continue
        except Exception:
            pass
            
    # 转为列表并计算 UV 数量
    daily_list = []
    for d, s in sorted(stats["daily"].items(), reverse=True):
        daily_list.append({"date": d, "pv": s["pv"], "uv": len(s["uv"])})
        
    return {
        "total_pv": stats["total_pv"],
        "total_uv": len(stats["unique_ips"]),
        "daily": daily_list
    }


# ---------- 用户反馈 ----------

@app.post("/api/v2/feedback")
async def submit_feedback(request: Request):
    """接收用户对转换结果的反馈，写入结构化日志并持久化到 JSONL 文件。"""
    import json as _json
    from datetime import datetime, timezone

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体须为 JSON")

    job_id = str(body.get("job_id") or "").strip()[:32]
    feedback_type = str(body.get("type") or "other").strip()[:32]
    message = str(body.get("message") or "").strip()[:2000]

    if not message:
        raise HTTPException(status_code=400, detail="反馈内容不能为空")

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "type": feedback_type,
        "message": message,
        "ip": _anonymize_ip(get_real_ip(request)),
    }

    # 结构化日志（可被日志聚合系统收集）
    logger.info("user_feedback", extra={"job_id": job_id, "feedback_type": feedback_type})

    # 持久化到 JSONL 文件
    feedback_file = BASE_DIR / "feedback.jsonl"
    try:
        with feedback_file.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"feedback write error: {e}")

    return {"ok": True, "message": "感谢您的反馈！"}


# ---------- SEO：robots.txt / sitemap.xml ----------

@app.get("/api/v2/admin/feedback", include_in_schema=False)
def get_admin_feedback(
    request: Request,
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    limit: int = 100,
):
    """查看用户反馈列表。需要 ADMIN_SECRET 鉴权。"""
    import json as _json
    _require_admin(request, x_admin_key)
    feedback_file = BASE_DIR / "feedback.jsonl"
    items = []
    if feedback_file.exists():
        try:
            lines = feedback_file.read_text(encoding="utf-8").strip().splitlines()
            for line in reversed(lines[-limit:]):
                try:
                    items.append(_json.loads(line))
                except Exception:
                    pass
        except Exception:
            pass
    return {"items": items, "total": len(items)}


@app.get("/robots.txt", include_in_schema=False)
def robots_txt():
    content = "\n".join([
        "User-agent: *",
        "Allow: /",
        "Disallow: /api/",
        "Disallow: /admin.html",
        "Sitemap: https://fixepub.com/sitemap.xml",
    ])
    return Response(content=content, media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml():
    content = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://fixepub.com/</loc>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>"""
    return Response(content=content, media_type="application/xml")


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

