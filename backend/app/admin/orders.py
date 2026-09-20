"""Sanitized order views. Price is the expected charge, never a payment receipt."""
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import Column, String
from .auth import AdminBase


class PaymentCheck(AdminBase):
    __tablename__ = "admin_payment_checks"
    order_no = Column(String(100), primary_key=True)
    status = Column(String(32), nullable=False)
    amount = Column(String(40), nullable=True)
    checked_at = Column(String(40), nullable=False)
    trade_no = Column(String(100), nullable=True)


def money(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number >= 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def safe_file(path, root):
    if not path:
        return None
    candidate = Path(path).resolve()
    if not candidate.is_relative_to(Path(root).resolve()) or not candidate.is_file():
        return None
    return candidate


def cost_view(stats):
    history = list(stats.get("cost_history") or [])
    snapshots = history + [stats]
    known = []
    for item in snapshots:
        cost = money(item.get("cost_usd"))
        # Reset counters and empty attempts are not evidence of a free request.
        tokens = (item.get("prompt_tokens") or 0) + (item.get("completion_tokens") or 0)
        if cost is not None and cost > 0 and tokens > 0:
            known.append(cost)
    return {
        "estimated_usd": str(sum(known)) if known else None,
        "known_attempts": len(known),
        "attempts": int(stats.get("translation_attempt") or 1),
        "prompt_tokens": stats.get("prompt_tokens"),
        "completion_tokens": stats.get("completion_tokens"),
        "note": "模型用量估算（USD），非供应商账单；输入/输出 Token 为本次尝试。仅累计已保留记录，早期重试、未返回用量的请求及其他模型调用可能缺失。",
    }


def order_view(job, payment, upload_dir, output_dir, *, expected=None):
    stats = job.translation_stats or {}
    return {
        "id": job.id, "order_no": f"batch_{job.batch_id}" if job.batch_id else job.id,
        "filename": job.source_filename, "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(), "status": job.status.value,
        "price_cny": expected if expected is not None else (job.expected_amount or None),
        "price_scope": "整批价格，勿重复相加" if job.batch_id else "订单价格",
        "batch_id": job.batch_id or None, "translation": job.enable_translation,
        "model": job.translation_model if job.enable_translation else None,
        "error_code": job.error_code, "message": job.message,
        "cost": cost_view(stats),
        "payment": payment or {"status": "unknown", "amount": None, "checked_at": None},
        "files": {"source": bool(safe_file(job.input_path, upload_dir)),
                  "output": bool(safe_file(job.output_path, output_dir))},
    }
