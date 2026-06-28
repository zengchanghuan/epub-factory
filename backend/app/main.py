import json
import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
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
from .models import DeviceProfile, ErrorCode, Job, JobStage, JobStatus, OutputMode, StageStatus, TraditionalVariant
from .storage import job_store
from .auth.deps import get_current_user_optional
from .domain.translation_qa_service import build_translation_qa_report, max_free_retries

# Sentry：若配置了 SENTRY_DSN，在应用启动时初始化，error_reporter 上报才会生效
_sentry_dsn = _os.environ.get("SENTRY_DSN")
if _sentry_dsn:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        sentry_sdk.init(
            dsn=_sentry_dsn,
            environment=_os.environ.get("APP_ENV", "production"),
            traces_sample_rate=float(_os.environ.get("SENTRY_TRACES_RATE", "0.1")),
            integrations=[
                StarletteIntegration(transaction_style="endpoint"),
                FastApiIntegration(transaction_style="endpoint"),
            ],
            # 脱敏：不上报请求 body（可能含用户文件/名词表）
            send_default_pii=False,
        )
    except Exception:
        pass


def _use_celery() -> bool:
    """是否使用 Celery 执行转换（需配置 broker 且任务在持久化 store 中）。"""
    import os
    return bool(os.environ.get("CELERY_BROKER_URL") or os.environ.get("REDIS_URL"))


# ── 格式转换定价（元/次）────────────────────────────────────────────────────
CONVERSION_PRICE_CNY: str = _os.environ.get("CONVERSION_PRICE_CNY", "5.99").strip() or "5.99"

# ── AI 翻译 Token 计费参数 ───────────────────────────────────────────────────
# 每 1000 字符（约等于 1000 token）收取的费用（元）
TRANSLATION_PRICE_PER_1K: float = float(_os.environ.get("TRANSLATION_PRICE_PER_1K", "0.10"))
# 单次最低收费
TRANSLATION_MIN_PRICE: float = float(_os.environ.get("TRANSLATION_MIN_PRICE", "5.99"))
# 单次最高收费（默认不封顶，防止超大文集亏损；如需限制可在 .env 设置）
TRANSLATION_MAX_PRICE: float = float(_os.environ.get("TRANSLATION_MAX_PRICE", "99999"))

# 兼容旧逻辑：若显式设置了 TRANSLATION_PRICE_CNY，优先使用固定价格（方便灰度切换）
_TRANSLATION_FIXED_PRICE: str = _os.environ.get("TRANSLATION_PRICE_CNY", "").strip()
# 历史变量名保留，部分地方仍引用
TRANSLATION_PRICE_CNY: str = _TRANSLATION_FIXED_PRICE or CONVERSION_PRICE_CNY


def _estimate_epub_chars(epub_path: str) -> int:
    """快速统计 EPUB 中可翻译的字符数（用于预估 Token 费用）。"""
    import zipfile
    from html.parser import HTMLParser

    class _StripTags(HTMLParser):
        def __init__(self):
            super().__init__()
            self._buf: list[str] = []
        def handle_data(self, data: str):
            self._buf.append(data)
        def text(self) -> str:
            return "".join(self._buf)

    total = 0
    try:
        with zipfile.ZipFile(epub_path) as zf:
            for name in zf.namelist():
                if name.lower().endswith((".xhtml", ".html", ".htm")):
                    try:
                        raw = zf.read(name).decode("utf-8", errors="ignore")
                        p = _StripTags()
                        p.feed(raw)
                        total += len(p.text().strip())
                    except Exception:
                        pass
    except Exception:
        pass
    return total


def _calc_translation_price(char_count: int) -> str:
    """根据字符数计算翻译费用，返回两位小数字符串（如 '12.80'）。"""
    if _TRANSLATION_FIXED_PRICE:
        return _TRANSLATION_FIXED_PRICE
    price = char_count / 1000.0 * TRANSLATION_PRICE_PER_1K
    price = max(TRANSLATION_MIN_PRICE, min(TRANSLATION_MAX_PRICE, price))
    return f"{price:.2f}"


def _estimate_translation_pricing(epub_path: str, target_lang: str, glossary: Optional[dict] = None) -> dict:
    """
    估算 EPUB 翻译总字符数 + 缓存命中字符数 + 按命中率折扣后的最终价格。

    定价口径与 SemanticsTranslator 的运行时缓存粒度严格一致：单段 inner_html ×
    (target_lang + glossary_hash) 作为缓存 key。这保证"预估省下来的钱"在实际
    执行时确实不会被重复扣 token。

    返回字典：
      total_chars     – 全书可翻译字符总数
      cached_chars    – 已在缓存中命中的字符数（不再产生 token 费用）
      hit_ratio       – cached_chars / total_chars（0.0~1.0）
      billable_chars  – total_chars - cached_chars
      price_cny       – 最终需要支付金额（含 MIN/MAX 价格保护）
      raw_price_cny   – 未折扣前的价格，便于前端展示"节省多少"
    """
    import hashlib
    import zipfile

    glossary = glossary or {}
    glossary_hash = ""
    if glossary:
        items = sorted(glossary.items())
        s = "|".join(f"{k}={v}" for k, v in items)
        glossary_hash = hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]
    cache_lang_key = f"{target_lang}@{glossary_hash}" if glossary_hash else target_lang

    total_chars = 0
    cached_chars = 0
    try:
        from bs4 import BeautifulSoup
        from .engine.translation_cache import TranslationCache
        cache = TranslationCache()
        block_tags = ['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote']

        with zipfile.ZipFile(epub_path) as zf:
            for name in zf.namelist():
                if not name.lower().endswith((".xhtml", ".html", ".htm")):
                    continue
                try:
                    raw = zf.read(name).decode("utf-8", errors="ignore")
                    soup = BeautifulSoup(raw, "html.parser")
                    for block in soup.find_all(block_tags):
                        if block.find(block_tags):
                            continue
                        inner_html = "".join(str(c) for c in block.contents).strip()
                        if not inner_html:
                            continue
                        chars = len(inner_html)
                        total_chars += chars
                        if cache.get(inner_html, cache_lang_key):
                            cached_chars += chars
                except Exception:
                    continue
    except Exception:
        return {
            "total_chars": 0,
            "cached_chars": 0,
            "hit_ratio": 0.0,
            "billable_chars": 0,
            "price_cny": TRANSLATION_PRICE_CNY,
            "raw_price_cny": TRANSLATION_PRICE_CNY,
        }

    billable_chars = max(0, total_chars - cached_chars)
    hit_ratio = (cached_chars / total_chars) if total_chars > 0 else 0.0

    if _TRANSLATION_FIXED_PRICE:
        raw_price = float(_TRANSLATION_FIXED_PRICE)
        price = raw_price * (1 - hit_ratio)
        price = max(TRANSLATION_MIN_PRICE, min(TRANSLATION_MAX_PRICE, price))
    else:
        raw_price_unbounded = total_chars / 1000.0 * TRANSLATION_PRICE_PER_1K
        billable_price = billable_chars / 1000.0 * TRANSLATION_PRICE_PER_1K
        raw_price = max(TRANSLATION_MIN_PRICE, min(TRANSLATION_MAX_PRICE, raw_price_unbounded))
        price = max(TRANSLATION_MIN_PRICE, min(TRANSLATION_MAX_PRICE, billable_price))

    return {
        "total_chars": total_chars,
        "cached_chars": cached_chars,
        "hit_ratio": round(hit_ratio, 3),
        "billable_chars": billable_chars,
        "price_cny": f"{price:.2f}",
        "raw_price_cny": f"{raw_price:.2f}",
    }

# access_token 默认有效期（天），过期后必须重新发起任务才能继续查询/下载。
ACCESS_TOKEN_TTL_DAYS: int = int(_os.environ.get("ACCESS_TOKEN_TTL_DAYS", "7"))

# 短时下载签名：URL 上挂 ?exp=&sig= ，sig = HMAC-SHA256(secret, f"{job_id}:{exp}")。
# 未配置 secret 时签名功能关闭，下载只接受 access_token 鉴权（向后兼容）。
DOWNLOAD_SIGN_SECRET: str = (
    _os.environ.get("DOWNLOAD_SIGN_SECRET")
    or _os.environ.get("ADMIN_SECRET")
    or ""
).strip()
DOWNLOAD_SIGN_TTL_SECONDS: int = int(_os.environ.get("DOWNLOAD_SIGN_TTL_SECONDS", "600"))


def _sign_download(job_id: str) -> tuple:
    """生成 (exp, sig) 用于短时下载 URL；未配置 secret 时返回 (0, '')。"""
    import hashlib
    import hmac as _hmac
    import time as _time
    if not DOWNLOAD_SIGN_SECRET or not job_id:
        return 0, ""
    exp = int(_time.time()) + DOWNLOAD_SIGN_TTL_SECONDS
    msg = f"{job_id}:{exp}".encode("utf-8")
    sig = _hmac.new(DOWNLOAD_SIGN_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return exp, sig


def _verify_download_sig(job_id: str, exp: Optional[int], sig: Optional[str]) -> bool:
    """校验下载 URL 上的签名是否合法且未过期。"""
    import hashlib
    import hmac as _hmac
    import time as _time
    if not DOWNLOAD_SIGN_SECRET or not exp or not sig or not job_id:
        return False
    try:
        exp_int = int(exp)
    except (ValueError, TypeError):
        return False
    if _time.time() > exp_int:
        return False
    msg = f"{job_id}:{exp_int}".encode("utf-8")
    expected = _hmac.new(DOWNLOAD_SIGN_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return _hmac.compare_digest(expected, sig)


def _attach_download_sig(job_id: str, base_url: str) -> str:
    """在 base_url 上追加 ?exp=&sig= 参数；未配置签名时原样返回。"""
    if not base_url:
        return base_url
    exp, sig = _sign_download(job_id)
    if not sig:
        return base_url
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}exp={exp}&sig={sig}"


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
    if (
        job.error_code == ErrorCode.PARTIAL_TRANSLATION.value
        and job.status in (JobStatus.success, JobStatus.failed)
    ):
        return "qa_failed"
    if job.status == JobStatus.success:
        return "completed"
    if job.status == JobStatus.failed:
        return "failed"
    if job.status == JobStatus.cancelled:
        return "cancelled"
    return "running"


def _job_can_download(job: Job) -> bool:
    """只有完整成功的任务才允许下载，部分翻译失败不交付。"""
    return (
        job.status == JobStatus.success
        and bool(job.output_path)
        and job.error_code != ErrorCode.PARTIAL_TRANSLATION.value
    )


def _download_unavailable_detail(job: Job) -> str:
    if job.error_code == ErrorCode.PARTIAL_TRANSLATION.value:
        return "部分段落翻译失败，结果不可下载，请重新翻译"
    return "任务未完成，无法下载"


def _job_qa_report(job: Job) -> dict | None:
    if not job.enable_translation:
        return None
    stats = job.translation_stats or {}
    report = stats.get("qa_report")
    if isinstance(report, dict):
        return report
    return build_translation_qa_report(
        translation_stats=stats,
        output_path=job.output_path,
        error_code=job.error_code,
    )


def _metric_number(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_pipeline_timings(metrics_summary: str | None) -> tuple[list[dict], Optional[float]]:
    if not metrics_summary:
        return [], None
    stages: list[dict] = []
    headline_total = None
    for line in metrics_summary.splitlines():
        total_match = re.search(r"总耗时\s+([0-9]+(?:\.[0-9]+)?)\s*ms", line)
        if total_match:
            headline_total = _metric_number(total_match.group(1), 0.0)
        stage_match = re.search(r"[✅⚠️❌]\s+(.+?)\s+([0-9]+(?:\.[0-9]+)?)\s*ms\s*$", line)
        if stage_match:
            stages.append({
                "name": stage_match.group(1).strip(),
                "ms": round(_metric_number(stage_match.group(2), 0.0)),
            })
    explicit_total = next((s["ms"] for s in stages if str(s.get("name", "")).lower() == "total"), None)
    if explicit_total is not None:
        return stages, float(explicit_total)
    stage_sum = sum(s["ms"] for s in stages)
    return stages, float(headline_total or stage_sum or 0) or None


def _percentile_ms(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = int(round((len(ordered) - 1) * percentile))
    return int(ordered[max(0, min(idx, len(ordered) - 1))])


def _coerce_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _job_wall_elapsed_ms(job: Job) -> int:
    start = _coerce_utc(getattr(job, "created_at", None))
    if not start:
        return 0
    status = getattr(getattr(job, "status", None), "value", getattr(job, "status", ""))
    if status in {"pending", "running"}:
        end = datetime.now(timezone.utc)
    else:
        end = _coerce_utc(getattr(job, "updated_at", None)) or datetime.now(timezone.utc)
    return max(0, int((end - start).total_seconds() * 1000))


def _classify_translation_error(message: str, flags: list | None = None) -> tuple[str, str]:
    text = (message or "").lower()
    flag_set = set(flags or [])
    if "html_tag_mismatch" in flag_set or "html_mismatch" in flag_set or "html_tag_mismatch" in text or "html tag mismatch" in text or "html结构" in text or "tag mismatch" in text:
        return "html_tag_mismatch", "HTML 标签不匹配"
    if "likely_untranslated" in flag_set or "untranslated" in text or "未翻译" in text or "原文" in text:
        return "untranslated_response", "模型返回未翻译内容"
    if "empty_translation" in flag_set:
        return "empty_translation", "空译文"
    if "json" in text or "parse" in text:
        return "json_parse", "模型 JSON 解析失败"
    if "timeout" in text or "timed out" in text or "超时" in text:
        return "timeout", "模型请求超时"
    if "rate limit" in text or "429" in text or "限流" in text:
        return "rate_limit", "模型限流"
    if "connection" in text or "network" in text or "连接" in text:
        return "connection", "网络/连接异常"
    return "other", "其他失败"


def _job_translation_timing(job: Job) -> Optional[dict]:
    if not job.enable_translation:
        return None

    stats = dict(job.translation_stats or {})
    stages, stage_total_ms = _parse_pipeline_timings(job.metrics_summary)
    stats_elapsed_ms = _metric_number(stats.get("elapsed_seconds"), 0.0) * 1000
    wall_elapsed_ms = _job_wall_elapsed_ms(job)
    total_ms = stage_total_ms or (stats_elapsed_ms if stats_elapsed_ms > 0 else None) or (wall_elapsed_ms if wall_elapsed_ms > 0 else None)

    chunks = []
    list_chunks = getattr(job_store, "list_chunks", None)
    if list_chunks:
        try:
            chunks = list_chunks(job.id)
        except Exception:
            chunks = []
    latencies = [int(getattr(c, "latency_ms", 0) or 0) for c in chunks if int(getattr(c, "latency_ms", 0) or 0) > 0]
    retry_counts = [int(getattr(c, "retry_count", 0) or 0) for c in chunks]
    chunk_retry_total = sum(retry_counts)
    chunk_latency_sum_ms = sum(latencies)
    status_counts: dict[str, int] = {}
    prompt_tokens_from_chunks = 0
    completion_tokens_from_chunks = 0
    failed_categories: dict[str, dict] = {}
    for chunk in chunks:
        status = getattr(getattr(chunk, "status", None), "value", getattr(chunk, "status", ""))
        status_counts[status] = status_counts.get(status, 0) + 1
        prompt_tokens_from_chunks += int(getattr(chunk, "prompt_tokens", 0) or 0)
        completion_tokens_from_chunks += int(getattr(chunk, "completion_tokens", 0) or 0)
        message = getattr(chunk, "error_message", "") or ""
        audit = getattr(chunk, "audit_json", None) or {}
        flags = audit.get("flags", []) if isinstance(audit, dict) else []
        risk_level = audit.get("risk_level") if isinstance(audit, dict) else None
        failure_flags = {"likely_untranslated", "html_tag_mismatch", "html_mismatch", "empty_translation", "translation_error"}
        has_failure_flag = bool(set(flags) & failure_flags)
        if status != "failed" and not message and risk_level != "fail" and not has_failure_flag:
            continue
        code, label = _classify_translation_error(message, flags)
        item = failed_categories.setdefault(code, {"code": code, "label": label, "count": 0})
        item["count"] += 1

    model_stage_names = {"TranslateMap", "BookTitle", "Glossary"}
    non_total_stages = [s for s in stages if str(s.get("name", "")).lower() != "total"]
    model_stage_ms = sum(int(s.get("ms") or 0) for s in non_total_stages if s.get("name") in model_stage_names)
    server_stage_ms = sum(
        int(s.get("ms") or 0)
        for s in non_total_stages
        if s.get("name") not in model_stage_names
    )
    model_stage_estimated = False
    if not model_stage_ms and chunk_latency_sum_ms:
        model_stage_estimated = True
        model_stage_ms = min(chunk_latency_sum_ms, int(round(total_ms or chunk_latency_sum_ms)))
    elif not model_stage_ms and stats_elapsed_ms:
        model_stage_estimated = True
        model_stage_ms = round(stats_elapsed_ms)
    if total_ms is None:
        total_ms = model_stage_ms + server_stage_ms

    total_ms = int(round(total_ms or 0))
    if model_stage_ms > total_ms and total_ms > 0:
        model_stage_ms = total_ms
    if not server_stage_ms and total_ms > 0:
        server_stage_ms = max(0, total_ms - model_stage_ms)
    model_share = (model_stage_ms / total_ms) if total_ms > 0 else 0.0
    server_share = (server_stage_ms / total_ms) if total_ms > 0 else 0.0

    total_chunks_from_stats = int(stats.get("total_chunks") or stats.get("chunks_total") or 0)
    total_chunks = max(total_chunks_from_stats, len(chunks))
    if chunks:
        failed_chunks = int(status_counts.get("failed", 0))
        translated_chunks = int(status_counts.get("translated", 0))
        cached_chunks = int(status_counts.get("cached", 0))
    else:
        failed_chunks = int(stats.get("failed_chunks") or 0)
        translated_chunks = int(stats.get("translated_chunks") or 0)
        cached_chunks = int(stats.get("cached_chunks") or 0)
    api_calls_from_stats = int(stats.get("api_calls") or 0)
    api_calls_estimated = False
    if api_calls_from_stats > 0:
        api_calls = api_calls_from_stats
    else:
        api_calls = len(latencies)
        api_calls_estimated = api_calls > 0
    retry_attempts = int(stats.get("retry_attempts") or 0) or chunk_retry_total
    chunk_latency_avg_ms = round(sum(latencies) / len(latencies)) if latencies else 0
    avg_model_call_ms = chunk_latency_avg_ms if api_calls_estimated else (round(model_stage_ms / api_calls) if model_stage_ms and api_calls else 0)
    failure_ratio = (failed_chunks / total_chunks) if total_chunks > 0 else 0.0
    prompt_tokens = int(stats.get("prompt_tokens") or prompt_tokens_from_chunks or 0)
    completion_tokens = int(stats.get("completion_tokens") or completion_tokens_from_chunks or 0)
    total_tokens = int(stats.get("total_tokens") or (prompt_tokens + completion_tokens) or 0)
    optimization_counters = {
        "complex_chunks": int(stats.get("complex_chunks") or 0),
        "complex_singleton_batches": int(stats.get("complex_singleton_batches") or 0),
        "inline_tag_repairs": int(stats.get("inline_tag_repairs") or 0),
    }

    bottleneck = {
        "primary": "unknown",
        "title": "主要瓶颈：数据不足",
        "reason": "当前任务缺少足够的阶段耗时或模型调用数据。",
        "recommendations": ["跑完一次翻译任务后查看该面板，优先看模型耗时占比、重试次数和失败类别。"],
    }
    if retry_attempts > max(3, total_chunks * 0.1) or failed_chunks > 0 or failed_categories:
        bottleneck = {
            "primary": "model_stability",
            "title": "主要瓶颈：模型响应稳定性 / 重试放大",
            "reason": f"当前已记录 {failed_chunks} 个失败段落、{retry_attempts} 次重试，少量坏段落会放大整本书耗时。",
            "recommendations": [
                "优先检查失败类别分布，针对未翻译、HTML 标签不匹配、JSON 解析失败分别优化。",
                "启用动态批大小和动态并发：失败率升高时自动降并发、拆小批次。",
                "把反复失败段落移入补译队列，避免拖住整本书主流程。",
            ],
        }
    elif model_share >= 0.6:
        bottleneck = {
            "primary": "model_latency",
            "title": "主要瓶颈：大模型响应速度",
            "reason": f"模型相关阶段占总耗时约 {round(model_share * 100)}%，chunk P95 延迟约 {_percentile_ms(latencies, 0.95)} ms。",
            "recommendations": [
                "评估 DeepSeek 当前响应耗时和失败率，必要时增加备用模型路由。",
                "调优批大小，减少 API 调用总数，同时避免单批过大导致格式失败。",
                "记录 P95/P99 延迟，识别慢请求并拆分或重试。",
            ],
        }
    elif server_share >= 0.45:
        bottleneck = {
            "primary": "server_pipeline",
            "title": "主要瓶颈：服务器处理 / 打包流程",
            "reason": f"非模型阶段占总耗时约 {round(server_share * 100)}%。",
            "recommendations": [
                "检查服务器 CPU、内存、磁盘 IO 和 swap。",
                "重点观察预处理、Reduce/打包、EpubCheck 哪个阶段最高。",
                "若 CPU/IO 长期高位，再考虑升级服务器或拆 worker。",
            ],
        }
    elif api_calls and total_chunks and (total_chunks / max(api_calls, 1)) < 3:
        bottleneck = {
            "primary": "orchestration",
            "title": "主要瓶颈：请求批量偏碎",
            "reason": "平均每次 API 调用覆盖的段落数偏低，请求开销可能过高。",
            "recommendations": [
                "适当提高批大小上限，减少 API 调用次数。",
                "按章节合并短段落，同时保留 HTML 结构校验。",
            ],
        }

    return {
        "total_ms": total_ms,
        "model_stage_ms": int(round(model_stage_ms)),
        "server_stage_ms": int(round(server_stage_ms)),
        "model_share": round(model_share, 4),
        "server_share": round(server_share, 4),
        "api_calls": api_calls,
        "api_calls_estimated": api_calls_estimated,
        "avg_model_call_ms": avg_model_call_ms,
        "model_stage_estimated": model_stage_estimated,
        "chunk_latency_sum_ms": chunk_latency_sum_ms,
        "chunk_latency_samples": len(latencies),
        "wall_elapsed_ms": wall_elapsed_ms,
        "chunk_latency_avg_ms": chunk_latency_avg_ms,
        "chunk_latency_p95_ms": _percentile_ms(latencies, 0.95),
        "chunk_latency_p99_ms": _percentile_ms(latencies, 0.99),
        "retry_attempts": retry_attempts,
        "chunk_retry_total": chunk_retry_total,
        "chunk_retry_max": max(retry_counts) if retry_counts else 0,
        "total_chunks": total_chunks,
        "translated_chunks": translated_chunks,
        "cached_chunks": cached_chunks,
        "failed_chunks": failed_chunks,
        "failure_ratio": round(failure_ratio, 4),
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": total_tokens,
            "cost_usd": _metric_number(stats.get("cost_usd"), 0.0),
        },
        "optimization_counters": optimization_counters,
        "stage_timings": [
            {
                "name": s["name"],
                "ms": int(s["ms"]),
                "share": round((int(s["ms"]) / total_ms), 4) if total_ms > 0 and str(s.get("name", "")).lower() != "total" else 0,
            }
            for s in stages
        ],
        "failure_categories": sorted(failed_categories.values(), key=lambda item: item["count"], reverse=True),
        "bottleneck": bottleneck,
    }


def _job_to_v2_detail(job: Job, download_url_path: str) -> dict:
    """构建 v2 任务详情响应。"""
    download_url = _attach_download_sig(job.id, download_url_path) if _job_can_download(job) else None
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
        "download_url": download_url,
        "quality_stats": job.quality_stats.to_dict() if job.quality_stats else None,
        "translation_stats": job.translation_stats or None,
        "translation_timing": _job_translation_timing(job),
        "qa_report": _job_qa_report(job),
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
    """
    清洗上传文件名：保留中日韩字符与常见可读符号，剔除路径穿越与控制字符。

    与旧实现相比：
    - 旧版：把非 ASCII 字符（含中文）全部替换为 '_'，导致输出文件名丢失原始书名
    - 新版：仅过滤路径分隔符 ('/', '\\') / NUL / 控制字符 / Windows 非法符号 (<>:"|?*)
            其它字符（含中文/日文/韩文/常用符号）原样保留，最终下载更友好

    任何情况下都不会让 stem 长度超过 120 字符，避免文件系统命名上限。
    """
    base = os.path.basename(filename or "")
    if not base:
        return "upload.bin"
    base = base.replace("\x00", "")
    cleaned_chars = []
    for ch in base:
        cp = ord(ch)
        if cp < 0x20:
            continue
        if ch in ('/', '\\', '<', '>', ':', '"', '|', '?', '*'):
            cleaned_chars.append("_")
        else:
            cleaned_chars.append(ch)
    cleaned = "".join(cleaned_chars).strip().lstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return "upload.bin"

    stem, dot, ext = cleaned.rpartition(".")
    if dot:
        if len(stem) > 120:
            stem = stem[:120].rstrip()
        cleaned = f"{stem}.{ext}"
    else:
        if len(cleaned) > 120:
            cleaned = cleaned[:120].rstrip()
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
       同时若设置了 token_expires_at 且已过期，则拒绝。
    2) 老任务（无 access_token）→ 仅在 LEGACY_ACCESS_WINDOW_DAYS（默认 30 天）内
       允许 session 或 IP 兜底；超过窗口直接拒绝，避免同 NAT 出口 IP 互访。
    """
    token = _extract_job_token(request, job.id)
    if job.access_token:
        if not token or token != job.access_token:
            return False
        expires = getattr(job, "token_expires_at", None)
        if expires is not None:
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expires:
                return False
        return True

    created = job.created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if created is None or (datetime.now(timezone.utc) - created) > timedelta(days=_LEGACY_ACCESS_WINDOW_DAYS):
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
        payload: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        for field in ("trace_id", "job_id", "error_code", "error_message"):
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _build_logger() -> logging.Logger:
    _logger = logging.getLogger("epub_factory")
    _logger.setLevel(logging.INFO)
    _logger.handlers = []
    fmt = JsonFormatter()

    stdout_handler = logging.StreamHandler()
    stdout_handler.setFormatter(fmt)
    _logger.addHandler(stdout_handler)

    # 若配置了 LOG_FILE，同时写到文件（供 LogListener / CLS 采集）
    log_file = _os.environ.get("LOG_FILE")
    if log_file:
        import logging.handlers as _lh
        _os.makedirs(_os.path.dirname(log_file), exist_ok=True)
        file_handler = _lh.RotatingFileHandler(
            log_file, maxBytes=50 * 1024 * 1024, backupCount=7, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        _logger.addHandler(file_handler)

    return _logger


logger = _build_logger()

app = FastAPI(title="EPUB Factory API", version="0.1.0")
init_alipay()

from .auth.router import router as auth_router
app.include_router(auth_router)

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

    supported_exts = (".epub", ".pdf", ".mobi", ".azw3", ".docx", ".md", ".markdown")
    if not any(file.filename.lower().endswith(ext) for ext in supported_exts):
        raise HTTPException(status_code=400, detail="仅支持 .epub, .pdf, .mobi, .azw3, .docx 或 .md 文件")

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
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=ACCESS_TOKEN_TTL_DAYS),
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
        "download_url": _attach_download_sig(job.id, f"/api/v1/jobs/{job.id}/download") if _job_can_download(job) else None,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


@app.get("/api/v1/jobs/{job_id}/download")
def download_result(
    job_id: str,
    request: Request,
    exp: Optional[int] = None,
    sig: Optional[str] = None,
):
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not (_verify_download_sig(job_id, exp, sig) or _authorize_job_access(request, job)):
        raise HTTPException(status_code=403, detail="无权访问该任务（链接可能已过期，请回任务中心重新获取下载链接）")
    if not _job_can_download(job):
        raise HTTPException(status_code=400, detail=_download_unavailable_detail(job))
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
    admin_key: Optional[str] = Form(None),
    # 繁简 v2 参数：词典域 + 精校开关
    lexicon_domains_json: Optional[str] = Form(None),   # JSON 数组字符串，如 '["general","tech"]'
    enable_proper_noun: bool = Form(True),
    enable_precision_polish: bool = Form(False),         # L4 精校开关
    polish_order_no: Optional[str] = Form(None),         # AI 精校支付宝订单号（开启 L4 时必填）
):
    """上传文件并创建后台任务，创建后立即返回（v2 契约）。temperature 不传时：翻译任务默认 1.3，纯格式转换不调用 LLM。"""
    import os as _os
    import hmac as _hmac
    _skip_payment = _os.environ.get("SKIP_PAYMENT_CHECK", "").lower() in ("1", "true", "yes")
    # 管理员测试价：admin_key 匹配 ADMIN_SECRET 时，所有价格强制覆盖为 0.01
    _admin_secret = (_os.environ.get("ADMIN_SECRET") or "").strip()
    _is_admin_test = bool(
        _admin_secret and admin_key and
        _hmac.compare_digest(admin_key.strip(), _admin_secret)
    )
    _TEST_PRICE = "0.01"
    client_session = _get_client_session(request) or uuid.uuid4().hex
    supported_exts = (".epub", ".pdf", ".mobi", ".azw3", ".docx", ".md", ".markdown")
    if not (file.filename and any(file.filename.lower().endswith(ext) for ext in supported_exts)):
        raise HTTPException(status_code=400, detail="仅支持 .epub, .pdf, .mobi, .azw3, .docx 或 .md 文件")

    # 免费配额已关闭，所有转换均走付费流程
    client_ip = get_real_ip(request)

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

    # 解析 lexicon_domains
    lexicon_domains = ["general", "tech", "movie"]
    if lexicon_domains_json:
        try:
            parsed_domains = json.loads(lexicon_domains_json)
            if isinstance(parsed_domains, list):
                lexicon_domains = [str(d) for d in parsed_domains]
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(status_code=400, detail="lexicon_domains_json 格式错误，应为 JSON 数组字符串")

    # 翻译任务不再强制登录：登录用户仍记录 user_id，匿名用户也可直接使用
    current_user = get_current_user_optional(request)

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
            detail=f"文件过大（约 {declared_len // 1024 // 1024}MB），单文件最大支持 {MAX_FILE_SIZE_MB}MB。",
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
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（>{MAX_FILE_SIZE_MB}MB），单文件最大支持 {MAX_FILE_SIZE_MB}MB。",
        )
    file_size = bytes_written

    # ── 通过所有"无副作用"的校验后，才创建支付订单 ────────────────────
    pay_url = None
    qr_code = None
    job_status = JobStatus.pending
    expected_amount = ""
    estimated_chars = 0

    pricing_info = {}
    if not _skip_payment:
        try:
            if enable_translation:
                # 翻译：按 Token 动态定价 + 缓存命中率折扣
                pricing_info = _estimate_translation_pricing(str(input_path), target_lang, glossary)
                estimated_chars = pricing_info.get("total_chars", 0)
                expected_amount = _TEST_PRICE if _is_admin_test else pricing_info.get("price_cny", _calc_translation_price(estimated_chars))
                subject = f"EPUB AI 翻译服务 - {safe_name[:50]}"
                pay_url = create_alipay_page_pay(
                    out_trade_no=job_id,
                    total_amount=expected_amount,
                    subject=subject,
                    return_url=f"https://fixepub.com/?job_id={job_id}",
                )
            else:
                # 格式转换：固定价格。优先尝试扫码支付；若支付宝应用未开通当面付，
                # 回退到已审核通过的电脑网站支付，避免用户看到“支付渠道不可用”。
                from .infra.alipay import create_alipay_precreate
                base_amount = float(_TEST_PRICE) if _is_admin_test else float(CONVERSION_PRICE_CNY)
                if enable_precision_polish:
                    from .engine.cleaners.llm_polish import count_effective_chars, calculate_polish_price
                    char_count = count_effective_chars(str(input_path))
                    estimated_chars = char_count
                    polish_price = calculate_polish_price(char_count) if not _is_admin_test else 0.01
                    base_amount += polish_price
                expected_amount = f"{base_amount:.2f}"
                subject = f"EPUB 格式转换服务 - {safe_name[:50]}"
                disable_precreate = _os.environ.get("ALIPAY_DISABLE_PRECREATE", "").lower() in ("1", "true", "yes")
                try:
                    if disable_precreate:
                        raise RuntimeError("precreate disabled by ALIPAY_DISABLE_PRECREATE")
                    qr_code = create_alipay_precreate(
                        out_trade_no=job_id,
                        total_amount=expected_amount,
                        subject=subject,
                    )
                except Exception as precreate_exc:
                    logger.warning(
                        "Alipay precreate unavailable, fallback to page pay",
                        extra={"job_id": job_id, "error": str(precreate_exc)},
                    )
                    pay_url = create_alipay_page_pay(
                        out_trade_no=job_id,
                        total_amount=expected_amount,
                        subject=subject,
                        return_url=f"https://fixepub.com/?job_id={job_id}",
                    )
            job_status = JobStatus.pending_payment
            logger.info("Created alipay payment order", extra={
                "job_id": job_id, "amount": expected_amount, "translation": enable_translation
            })
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
        token_expires_at=datetime.now(timezone.utc) + timedelta(days=ACCESS_TOKEN_TTL_DAYS),
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
        lexicon_domains=lexicon_domains,
        enable_proper_noun=enable_proper_noun,
        enable_precision_polish=enable_precision_polish,
        precision_polish_order_no=polish_order_no or "",
        user_id=current_user.id if current_user else None,
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
        "message": "请完成支付以启动任务" if job_status == JobStatus.pending_payment else "任务已创建，已进入后台队列",
        "source_filename": job.source_filename,
        "enable_translation": job.enable_translation,
        "target_lang": job.target_lang,
        "bilingual": job.bilingual,
        "device": job.device.value,
        "traditional_variant": job.traditional_variant,
        "created_at": job.created_at.isoformat(),
        "pay_url": pay_url,
        "qr_code": qr_code,
        "amount": expected_amount,
        "estimated_chars": estimated_chars,
        "pricing": pricing_info or None,
    }


@app.post("/api/v2/estimate-polish")
async def estimate_polish_price(
    request: Request,
    file: UploadFile = File(...),
):
    """
    上传 EPUB 文件，解析正文有效字数并返回 AI 精校报价。
    前端在用户勾选「AI 精校」时调用此接口，支付前先告知费用。
    文件仅用于解析字数，不落盘保存。
    """
    from .engine.cleaners.llm_polish import count_effective_chars, calculate_polish_price
    import tempfile

    if not (file.filename and file.filename.lower().endswith(".epub")):
        raise HTTPException(status_code=400, detail="AI 精校仅支持 .epub 文件")

    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
        tmp_path = tmp.name
        content = await file.read()
        tmp.write(content)

    try:
        char_count = count_effective_chars(tmp_path)
        price = calculate_polish_price(char_count)
    finally:
        import os as _os
        try:
            _os.unlink(tmp_path)
        except Exception:
            pass

    return {
        "char_count": char_count,
        "price_cny": f"{price:.2f}",
        "tier": _get_polish_tier_label(char_count),
    }


def _get_polish_tier_label(char_count: int) -> str:
    if char_count <= 150_000:
        return "≤15万字"
    if char_count <= 300_000:
        return "15-30万字"
    if char_count <= 600_000:
        return "30-60万字"
    if char_count <= 1_000_000:
        return "60-100万字"
    return ">100万字"


@app.get("/api/v2/jobs")
def list_jobs_v2(request: Request, limit: int = 100):
    """获取任务中心列表（v2）：登录用户按 user_id 查，匿名用户按 session/IP 查。"""
    current_user = get_current_user_optional(request)
    user_list_fn = getattr(job_store, "list_jobs_by_user_id", None)
    session_list_fn = getattr(job_store, "list_jobs_by_creator_session", None)
    ip_list_fn = getattr(job_store, "list_jobs_by_creator_ip", None)
    all_list_fn = getattr(job_store, "list_jobs", None)
    if not (user_list_fn or session_list_fn or ip_list_fn or all_list_fn):
        return {"items": []}
    client_ip = get_real_ip(request)
    client_session = _get_client_session(request)
    if current_user and user_list_fn:
        jobs = user_list_fn(current_user.id, limit=limit)
    elif session_list_fn and client_session:
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
            "enable_translation": j.enable_translation,
            "error_code": j.error_code,
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
    download_path = f"/api/v2/jobs/{job.id}/download" if _job_can_download(job) else None
    return _job_to_v2_detail(job, download_path)


@app.get("/api/v2/jobs/{job_id}/download")
def download_result_v2(
    job_id: str,
    request: Request,
    exp: Optional[int] = None,
    sig: Optional[str] = None,
):
    """
    下载结果 EPUB（v2）。
    两种鉴权方式任一通过即可：
    1) 短时签名 URL（?exp=&sig=）—— 由 GET /api/v2/jobs/{id} 返回，默认 10 分钟有效；
    2) 任意 _authorize_job_access 通过的方式（access_token / 同会话 / legacy IP 兜底）。
    """
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not (_verify_download_sig(job_id, exp, sig) or _authorize_job_access(request, job)):
        raise HTTPException(status_code=403, detail="无权访问该任务（链接可能已过期，请回任务中心重新获取下载链接）")
    if not _job_can_download(job):
        raise HTTPException(status_code=400, detail=_download_unavailable_detail(job))
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


def _enum_value(value) -> str:
    return getattr(value, "value", str(value))


def _diagnose_error_category(error_message: str | None, audit_flags: list | None = None) -> str:
    msg = (error_message or "").lower()
    flags = set(audit_flags or [])
    if "likely_untranslated" in flags or "untranslated" in msg:
        return "untranslated_response"
    if "empty_translation" in flags or "missing translation" in msg:
        return "empty_translation"
    if "html_tag_mismatch" in flags:
        return "html_mismatch"
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    if "rate limit" in msg or "429" in msg:
        return "rate_limit"
    if "json" in msg or "parse" in msg:
        return "invalid_json"
    if "connect" in msg or "connection" in msg:
        return "connection"
    if error_message:
        return "model_error"
    if flags:
        return "quality_audit"
    return "unknown"


def _short_text(text: str | None, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _translation_diagnostics(job: Job, limit: int = 20) -> dict:
    stats = job.translation_stats or {}
    list_chunks = getattr(job_store, "list_chunks", None)
    chunks = list_chunks(job.id) if list_chunks else []
    status_counts: dict[str, int] = {}
    error_categories: dict[str, int] = {}
    audit_flags_count: dict[str, int] = {}
    failed_items = []
    slow_items = []

    for chunk in chunks:
        status = _enum_value(getattr(chunk, "status", ""))
        status_counts[status] = status_counts.get(status, 0) + 1
        audit = getattr(chunk, "audit_json", None) or {}
        flags = audit.get("flags", []) if isinstance(audit, dict) else []
        risk_level = audit.get("risk_level") if isinstance(audit, dict) else None
        for flag in flags:
            audit_flags_count[flag] = audit_flags_count.get(flag, 0) + 1

        has_issue = (
            status == "failed"
            or bool(getattr(chunk, "error_message", None))
            or risk_level == "fail"
            or "likely_untranslated" in flags
        )
        if has_issue:
            category = _diagnose_error_category(getattr(chunk, "error_message", None), flags)
            error_categories[category] = error_categories.get(category, 0) + 1
            if len(failed_items) < max(1, min(limit, 100)):
                failed_items.append({
                    "chapter_id": getattr(chunk, "chapter_id", ""),
                    "chunk_id": getattr(chunk, "chunk_id", ""),
                    "sequence": getattr(chunk, "sequence", 0),
                    "locator": getattr(chunk, "locator", ""),
                    "status": status,
                    "category": category,
                    "error_message": getattr(chunk, "error_message", None),
                    "retry_count": int(getattr(chunk, "retry_count", 0) or 0),
                    "latency_ms": int(getattr(chunk, "latency_ms", 0) or 0),
                    "model": getattr(chunk, "model", None),
                    "base_url": getattr(chunk, "base_url", None),
                    "risk_level": risk_level,
                    "flags": flags,
                    "source_text": _short_text(getattr(chunk, "source_text", "")),
                    "translated_text": _short_text(getattr(chunk, "translated_text", "")),
                })

        latency = int(getattr(chunk, "latency_ms", 0) or 0)
        if latency > 0:
            slow_items.append((latency, chunk))

    slow_items.sort(key=lambda item: item[0], reverse=True)
    slow_chunks = [
        {
            "chapter_id": getattr(chunk, "chapter_id", ""),
            "chunk_id": getattr(chunk, "chunk_id", ""),
            "latency_ms": latency,
            "retry_count": int(getattr(chunk, "retry_count", 0) or 0),
            "model": getattr(chunk, "model", None),
            "source_text": _short_text(getattr(chunk, "source_text", ""), 120),
        }
        for latency, chunk in slow_items[:10]
    ]

    model_timeout_count = int(stats.get("timeout_errors") or 0)
    connection_count = int(stats.get("connection_errors") or 0)
    likely_causes = []
    if error_categories.get("untranslated_response") or (stats.get("audit_flags_count") or {}).get("likely_untranslated"):
        likely_causes.append("模型返回原文或近似原文，系统已拦截以避免中英混杂交付")
    if error_categories.get("timeout") or model_timeout_count:
        likely_causes.append("模型 API 响应超时，可能与上游服务负载或单批内容过长有关")
    if connection_count:
        likely_causes.append("模型连接出现波动，已触发自动重试")
    if not likely_causes and (int(stats.get("failed_chunks") or 0) > 0):
        likely_causes.append("部分段落重试后仍未达到交付标准")

    return {
        "job_id": job.id,
        "status": _job_to_v2_status(job),
        "error_code": job.error_code,
        "message": job.message,
        "summary": {
            "total_chunks": int(stats.get("total_chunks") or stats.get("chunks_total") or len(chunks) or 0),
            "translated_chunks": int(stats.get("translated_chunks") or 0),
            "cached_chunks": int(stats.get("cached_chunks") or 0),
            "failed_chunks": int(stats.get("failed_chunks") or status_counts.get("failed", 0) or 0),
            "audit_failed_chunks": int(stats.get("audit_failed_chunks") or 0),
            "likely_untranslated": int((stats.get("audit_flags_count") or {}).get("likely_untranslated") or audit_flags_count.get("likely_untranslated", 0) or 0),
            "timeout_errors": model_timeout_count,
            "connection_errors": connection_count,
            "retry_attempts": int(stats.get("retry_attempts") or 0),
            "last_error": stats.get("last_error") or "",
            "model": stats.get("model") or "",
            "elapsed_seconds": stats.get("elapsed_seconds") or 0,
        },
        "status_counts": status_counts,
        "error_categories": error_categories,
        "audit_flags_count": stats.get("audit_flags_count") or audit_flags_count,
        "likely_causes": likely_causes,
        "failed_chunks": failed_items,
        "slow_chunks": slow_chunks,
        "effective_limits": {
            "openai_concurrency_cap": int(os.environ.get("EPUB_TRANSLATION_CONCURRENCY_CAP", "4")),
            "chapter_concurrency_cap": int(os.environ.get("EPUB_CHAPTER_CONCURRENCY_CAP", "2")),
            "batch_max_chars_cap": int(os.environ.get("EPUB_TRANSLATION_BATCH_MAX_CHARS_CAP", "6000")),
            "request_timeout_seconds": float(os.environ.get("OPENAI_REQUEST_TIMEOUT", "90")),
            "max_retries": int(os.environ.get("OPENAI_MAX_RETRIES", "4")),
            "timeout_extra_retries": int(os.environ.get("OPENAI_TIMEOUT_EXTRA_RETRIES", "2")),
        },
    }


@app.get("/api/v2/jobs/{job_id}/stats")
def get_job_stats_v2(job_id: str, request: Request):
    """获取章节/块级聚合统计（v2）。"""
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not _authorize_job_access(request, job):
        raise HTTPException(status_code=403, detail="无权访问该任务")
    return _v2_job_stats(job_id)


@app.get("/api/v2/jobs/{job_id}/translation-diagnostics")
def get_translation_diagnostics_v2(job_id: str, request: Request, limit: int = 20):
    """获取翻译失败诊断：失败 chunk、错误类别、重试/耗时、审计 flags。"""
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not _authorize_job_access(request, job):
        raise HTTPException(status_code=403, detail="无权访问该任务")
    if not job.enable_translation:
        raise HTTPException(status_code=400, detail="该任务不是 AI 翻译任务")
    return _translation_diagnostics(job, limit=limit)


def _v2_job_events(job_id: str) -> list:
    """从 store 的 list_stages 转为 events 列表。"""
    list_stages = getattr(job_store, "list_stages", None)
    if not list_stages:
        return []
    stages = list_stages(job_id)
    items = []
    for s in stages:
        metadata = s.metadata if getattr(s, "metadata", None) else {}
        items.append({
            "time": (s.finished_at or s.started_at).isoformat(),
            "level": metadata.get("level") or "info",
            "stage": s.stage_name,
            "message": metadata.get("message") or s.stage_name,
            "elapsed_ms": s.elapsed_ms,
        })
    return items


@app.get("/api/v2/jobs/{job_id}/events")
def get_job_events_v2(job_id: str, request: Request):
    """获取结构化阶段日志（v2）。"""
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not _authorize_job_access(request, job):
        raise HTTPException(status_code=403, detail="无权访问该任务")
    return {"items": _v2_job_events(job_id)}


def _enqueue_conversion(job: Job, background_tasks: BackgroundTasks | None = None) -> None:
    if _use_celery():
        from app.tasks.job_pipeline import run_conversion
        run_conversion.delay(job.id)
        return
    if background_tasks is not None:
        background_tasks.add_task(process_job, job)
        return
    import threading
    threading.Thread(target=run_job, args=(job.id,), daemon=True).start()


@app.post("/api/v2/jobs/{job_id}/retry-translation")
def retry_translation_v2(job_id: str, request: Request, background_tasks: BackgroundTasks):
    """质检不通过后免费重译：复用原上传文件、原参数、原支付权益。"""
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not _authorize_job_access(request, job):
        raise HTTPException(status_code=403, detail="无权访问该任务")
    if not job.enable_translation:
        raise HTTPException(status_code=400, detail="该任务不是 AI 翻译任务")
    if job.error_code != ErrorCode.PARTIAL_TRANSLATION.value:
        raise HTTPException(status_code=400, detail="当前任务未处于质检失败状态")
    return _restart_translation_job(job, background_tasks, action_label="免费重译")


@app.post("/api/v2/jobs/{job_id}/restart-translation")
def restart_translation_v2(job_id: str, request: Request, background_tasks: BackgroundTasks):
    """重启翻译任务：用于取消、失败后的人工重新排队。"""
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not _authorize_job_access(request, job):
        raise HTTPException(status_code=403, detail="无权访问该任务")
    if not job.enable_translation:
        raise HTTPException(status_code=400, detail="该任务不是 AI 翻译任务")
    return _restart_translation_job(job, background_tasks, action_label="重启翻译")


def _restart_translation_job(job: Job, background_tasks: BackgroundTasks, action_label: str):
    if job.status in (JobStatus.pending, JobStatus.running, JobStatus.pending_payment):
        raise HTTPException(status_code=400, detail="任务正在处理中，不能重复重启")
    if not Path(job.input_path).exists():
        raise HTTPException(status_code=410, detail="原始上传文件已过期，无法重启翻译")

    stats = dict(job.translation_stats or {})
    free_retry_count = int(stats.get("free_retry_count") or 0)
    max_retries = max_free_retries()
    if max_retries >= 0 and free_retry_count >= max_retries:
        raise HTTPException(status_code=400, detail="重译次数已用完，请联系客服处理")

    stats["free_retry_count"] = free_retry_count + 1
    stats["translation_attempt"] = int(stats.get("translation_attempt") or 1) + 1
    stats["restart_count"] = int(stats.get("restart_count") or 0) + 1
    restart_summary = f"{action_label}已排队"
    stats["qa_report"] = {
        "status": "retrying",
        "summary": restart_summary,
        "retryable": False,
        "free_retry_count": stats["free_retry_count"],
        "max_free_retries": max_retries,
        "translation_attempt": stats["translation_attempt"],
        "flags": [],
        "checks": [],
        "score": 0,
    }

    updated = job_store.update_status(
        job.id,
        JobStatus.pending,
        f"{restart_summary}（第 {stats['translation_attempt']} 次尝试）",
        error_code=None,
        translation_stats=stats,
        allow_cancelled_transition=True,
    )
    refreshed = updated or job_store.get(job.id) or job
    add_stage = getattr(job_store, "add_stage", None)
    if add_stage:
        now = datetime.now(timezone.utc)
        add_stage(JobStage(
            job_id=job.id,
            stage_name="translation_restarted",
            status=StageStatus.completed,
            started_at=now,
            finished_at=now,
            metadata={"message": restart_summary, "level": "info"},
        ))
    _enqueue_conversion(refreshed, background_tasks)
    return _job_to_v2_detail(refreshed, None)


@app.post("/api/v2/jobs/{job_id}/recover")
def recover_job_payment(job_id: str, request: Request):
    """
    主动恢复支付（webhook 兜底）：用户付完款回到页面后，前端立刻调这个接口。

    工作流程：
    1) 鉴权：必须通过 _authorize_job_access（token / session / IP）
    2) 仅对 pending_payment 状态有效；其它状态直接回当前状态，前端不要再轮询
    3) 调支付宝 query API 主动查单：
       - TRADE_SUCCESS / TRADE_FINISHED → try_mark_paid + 入队 run_conversion
       - 其它 → 不动，让用户继续等 webhook
    4) 此接口是补偿性的，与 webhook、reconcile cron 形成三层兜底，
       即使支付宝异步通知挂了，用户回到页面就能在 1 秒内拿到正确状态。
    """
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not _authorize_job_access(request, job):
        raise HTTPException(status_code=403, detail="无权访问该任务")
    if job.status != JobStatus.pending_payment:
        return {"job_id": job_id, "status": _job_to_v2_status(job), "recovered": False}

    from .infra.alipay import query_alipay_trade
    trade_status = query_alipay_trade(job_id)
    logger.info(
        "recover_job_payment: trade query result",
        extra={"job_id": job_id, "trade_status": trade_status or "unknown"},
    )

    if trade_status not in ("TRADE_SUCCESS", "TRADE_FINISHED"):
        return {
            "job_id": job_id,
            "status": _job_to_v2_status(job),
            "recovered": False,
            "trade_status": trade_status,
        }

    try_mark = getattr(job_store, "try_mark_paid", None)
    won = bool(try_mark(job.id)) if callable(try_mark) else (
        bool(job_store.update_status(job.id, JobStatus.pending, "支付成功，排队中..."))
    )
    if not won:
        refreshed = job_store.get(job_id)
        return {
            "job_id": job_id,
            "status": _job_to_v2_status(refreshed),
            "recovered": False,
            "trade_status": trade_status,
        }

    if _use_celery():
        from app.tasks.job_pipeline import run_conversion
        run_conversion.delay(job.id)
    else:
        import threading
        threading.Thread(target=run_job, args=(job.id,), daemon=True).start()

    refreshed = job_store.get(job_id)
    return {
        "job_id": job_id,
        "status": _job_to_v2_status(refreshed),
        "recovered": True,
        "trade_status": trade_status,
    }


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
    message = "用户已停止翻译" if job.enable_translation else "用户取消"
    job_store.update_status(job_id, JobStatus.cancelled, message)
    add_stage = getattr(job_store, "add_stage", None)
    if add_stage:
        now = datetime.now(timezone.utc)
        add_stage(JobStage(
            job_id=job_id,
            stage_name="cancelled",
            status=StageStatus.completed,
            started_at=now,
            finished_at=now,
            metadata={"message": message, "level": "warning"},
        ))
    updated = job_store.get(job_id)
    return {
        "job_id": job_id,
        "status": "cancelled",
        "message": message,
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
        total_chunks = int(st.get("total_chunks") or 0)
        cached_chunks = int(st.get("cached_chunks") or 0)
        hit_ratio = (cached_chunks / total_chunks) if total_chunks else 0.0
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
            "total_chunks": total_chunks,
            "cached_chunks": cached_chunks,
            "cache_hit_ratio": round(hit_ratio, 3),
        })
    return {
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_tokens": total_prompt + total_completion,
        "total_cost_usd": round(total_cost, 4),
        "jobs_count": len(by_job),
        "by_job": by_job,
    }


@app.get("/api/v2/admin/balance", include_in_schema=False)
def get_admin_balance(
    request: Request,
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
):
    """查询当前大模型账户余额（仅支持 DeepSeek）。"""
    _require_admin(request, x_admin_key)
    try:
        from app.tasks.balance_check import _fetch_deepseek_balance, _PROVIDER, _WARN_THRESHOLD
        balance = _fetch_deepseek_balance()
        return {
            "provider": _PROVIDER,
            "balance_cny": balance,
            "threshold_cny": _WARN_THRESHOLD,
            "status": "ok" if (balance is not None and balance >= _WARN_THRESHOLD) else ("low" if balance is not None else "error"),
        }
    except Exception as e:
        return {"provider": "unknown", "balance_cny": None, "status": "error", "error": str(e)}


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
            # ── 修复订单（repair_ 前缀）────────────────────────────
            if out_trade_no.startswith("repair_"):
                repair_job_id = out_trade_no[len("repair_"):]
                repair_job = _repair_job_get(repair_job_id)
                if not repair_job:
                    logger.warning("Alipay webhook for unknown repair job", extra={"job_id": out_trade_no})
                    return Response("success")
                expected = REPAIR_PRICE_CNY
                actual = str(total_amount or "").strip()
                if not _amount_equal(actual, expected):
                    logger.warning("Alipay webhook repair amount mismatch", extra={"job_id": out_trade_no})
                    return Response("fail")
                with _repair_jobs_lock:
                    if _repair_jobs.get(repair_job_id, {}).get("status") != "pending_payment":
                        return Response("success")
                    _repair_jobs[repair_job_id]["status"] = "paid"
                logger.info("repair payment confirmed, starting repair", extra={"job_id": repair_job_id})
                _threading.Thread(target=_do_repair_async, args=(repair_job_id,), daemon=True).start()
                return Response("success")

            # ── 翻译/转换订单（原有逻辑）─────────────────────────
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


# ─────────────────────────────────────────────────────────────────
# EPUB 格式修复接口（含支付流程）
# ─────────────────────────────────────────────────────────────────
import threading as _threading

_REPAIR_UPLOAD_DIR = Path(os.environ.get("REPAIR_UPLOAD_DIR", "/tmp/epub-repair"))
_REPAIR_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# 修复单价（元）
REPAIR_PRICE_CNY: str = os.environ.get("REPAIR_PRICE_CNY", "5.99").strip() or "5.99"

# 修复任务内存状态（轻量；不需持久化，重启后重新上传即可）
# {job_id: {"status": "pending_payment"|"paid"|"repaired"|"failed",
#           "out_trade_no": str, "filename": str, "qr_code": str|None}}
_repair_jobs: dict = {}
_repair_jobs_lock = _threading.Lock()


def _repair_job_get(job_id: str) -> Optional[dict]:
    with _repair_jobs_lock:
        return _repair_jobs.get(job_id)


def _repair_job_set(job_id: str, **kwargs) -> None:
    with _repair_jobs_lock:
        job = _repair_jobs.setdefault(job_id, {})
        job.update(kwargs)


def _do_repair_async(job_id: str) -> None:
    """后台线程：执行实际修复，完成后更新状态。"""
    job_dir = _REPAIR_UPLOAD_DIR / job_id
    try:
        epub_files = [f for f in job_dir.glob("*.epub") if "_fixed" not in f.stem]
        if not epub_files:
            _repair_job_set(job_id, status="failed", error="原始文件不存在")
            return
        input_path = epub_files[0]
        fixed_name = input_path.stem + "_fixed.epub"
        output_path = job_dir / fixed_name
        if not output_path.exists():
            from .engine.epub_repairer import repair as do_repair
            do_repair(str(input_path), str(output_path))
        _repair_job_set(job_id, status="repaired", download_filename=fixed_name)
        logger.info("epub_repair done", extra={"job_id": job_id})
    except Exception as e:
        logger.error("epub_repair async failed", exc_info=True, extra={"job_id": job_id})
        _repair_job_set(job_id, status="failed", error=str(e))


@app.post("/api/v2/repair/diagnose")
async def repair_diagnose(
    request: Request,
    file: UploadFile = File(...),
):
    """上传 EPUB，返回诊断报告（不修改文件）。免费。"""
    if not (file.filename and file.filename.lower().endswith(".epub")):
        raise HTTPException(status_code=400, detail="仅支持 .epub 文件")

    content = await file.read()
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过 {MAX_FILE_SIZE_MB}MB 限制",
        )

    job_id = uuid.uuid4().hex
    job_dir = _REPAIR_UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    safe_name = re.sub(r'[^\w.\-]', '_', file.filename)
    input_path = job_dir / safe_name
    input_path.write_bytes(content)

    from .engine.epub_repairer import diagnose
    report = diagnose(str(input_path))

    _repair_job_set(job_id, status="pending_payment", filename=safe_name)

    return {
        "job_id": job_id,
        "filename": safe_name,
        "report": report.to_dict(),
        "price_cny": REPAIR_PRICE_CNY,
    }


@app.post("/api/v2/repair/{job_id}/pay")
async def repair_pay(job_id: str, admin_key: Optional[str] = Form(None)):
    """
    发起支付宝扫码付款。
    返回二维码内容 URL，前端用 qrcode 库或第三方渲染成二维码图片。
    """
    job = _repair_job_get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期，请重新上传")

    if job.get("status") == "repaired":
        return {"job_id": job_id, "status": "repaired"}
    if job.get("status") == "paid":
        return {"job_id": job_id, "status": "paid"}

    _skip_payment = _os.environ.get("SKIP_PAYMENT_CHECK", "").lower() in ("1", "true", "yes")

    import hmac as _hmac
    _admin_secret = (_os.environ.get("ADMIN_SECRET") or "").strip()
    _is_admin_test = bool(
        _admin_secret and admin_key and
        _hmac.compare_digest((admin_key or "").strip(), _admin_secret)
    )
    repair_price = "0.01" if _is_admin_test else REPAIR_PRICE_CNY

    if _skip_payment:
        # 开发模式：直接标记已付款并触发修复
        _repair_job_set(job_id, status="paid")
        _threading.Thread(target=_do_repair_async, args=(job_id,), daemon=True).start()
        return {"job_id": job_id, "status": "paid", "qr_code": None}

    from .infra.alipay import create_alipay_page_pay, create_alipay_precreate
    out_trade_no = f"repair_{job_id}"
    try:
        disable_precreate = _os.environ.get("ALIPAY_DISABLE_PRECREATE", "").lower() in ("1", "true", "yes")
        try:
            if disable_precreate:
                raise RuntimeError("precreate disabled by ALIPAY_DISABLE_PRECREATE")
            qr_code = create_alipay_precreate(
                out_trade_no=out_trade_no,
                total_amount=repair_price,
                subject="FixEpub 格式修复服务",
            )
            pay_url = None
        except Exception as precreate_exc:
            logger.warning(
                "repair pay: precreate unavailable, fallback to page pay",
                extra={"job_id": job_id, "error": str(precreate_exc)},
            )
            qr_code = None
            pay_url = create_alipay_page_pay(
                out_trade_no=out_trade_no,
                total_amount=repair_price,
                subject="FixEpub 格式修复服务",
                return_url=f"https://fixepub.com/epub-repair.html?job_id={job_id}",
            )
    except Exception as e:
        logger.error("repair pay: alipay order failed", exc_info=True)
        raise HTTPException(status_code=502, detail=f"支付发起失败：{e}")

    _repair_job_set(job_id, status="pending_payment", out_trade_no=out_trade_no, qr_code=qr_code, pay_url=pay_url)
    return {"job_id": job_id, "status": "pending_payment", "qr_code": qr_code, "pay_url": pay_url}


@app.get("/api/v2/repair/{job_id}/status")
async def repair_status(job_id: str):
    """轮询修复进度。"""
    job = _repair_job_get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return {
        "job_id": job_id,
        "status": job.get("status", "unknown"),
        "download_filename": job.get("download_filename"),
        "error": job.get("error"),
    }


@app.post("/api/v2/repair/{job_id}/recover")
async def repair_recover_payment(job_id: str):
    """
    修复任务的支付兜底：用户付完款回到页面时，前端立刻调这个接口。

    与 /api/v2/jobs/{id}/recover 完全同源——主动调支付宝查单，
    如果 TRADE_SUCCESS 但本地仍是 pending_payment，立刻补发 _do_repair_async。
    """
    job = _repair_job_get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if job.get("status") != "pending_payment":
        return {"job_id": job_id, "status": job.get("status"), "recovered": False}

    out_trade_no = job.get("out_trade_no") or f"repair_{job_id}"
    from .infra.alipay import query_alipay_trade
    trade_status = query_alipay_trade(out_trade_no)
    logger.info(
        "repair_recover_payment: trade query result",
        extra={"job_id": job_id, "trade_status": trade_status or "unknown"},
    )
    if trade_status not in ("TRADE_SUCCESS", "TRADE_FINISHED"):
        return {"job_id": job_id, "status": job.get("status"), "recovered": False, "trade_status": trade_status}

    with _repair_jobs_lock:
        current = _repair_jobs.get(job_id, {})
        if current.get("status") != "pending_payment":
            return {"job_id": job_id, "status": current.get("status"), "recovered": False}
        _repair_jobs[job_id]["status"] = "paid"
    _threading.Thread(target=_do_repair_async, args=(job_id,), daemon=True).start()
    return {"job_id": job_id, "status": "paid", "recovered": True, "trade_status": trade_status}


@app.get("/api/v2/repair/{job_id}/download")
async def repair_download(job_id: str):
    """下载修复后的 EPUB 文件（需已完成修复）。"""
    job = _repair_job_get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期，请重新上传文件")

    _skip_payment = _os.environ.get("SKIP_PAYMENT_CHECK", "").lower() in ("1", "true", "yes")
    if not _skip_payment and job.get("status") not in ("repaired",):
        raise HTTPException(status_code=402, detail="请先完成支付并等待修复完成")

    job_dir = _REPAIR_UPLOAD_DIR / job_id
    fixed_files = [f for f in job_dir.glob("*_fixed.epub")]
    if not fixed_files:
        raise HTTPException(status_code=404, detail="修复文件不存在，请稍后重试")

    fixed_file = fixed_files[0]
    # RFC 5987 编码处理含中文的文件名
    import urllib.parse
    encoded_name = urllib.parse.quote(fixed_file.name)
    return FileResponse(
        str(fixed_file),
        media_type="application/epub+zip",
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{encoded_name}; "
                f"filename=\"{fixed_file.name.encode('ascii', 'replace').decode()}\""
            )
        },
    )


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
  <url>
    <loc>https://fixepub.com/epub-repair.html</loc>
    <changefreq>monthly</changefreq>
    <priority>0.8</priority>
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
