"""Trusted purchase facts, independent of the mutable execution status.

Only server-verified payment hooks and the explicit server test configuration
may authorize a quote. Browser events and successful book execution are never
payment evidence. Legacy plans are recovered only before any prior restart.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


VERIFIED_SOURCES = frozenset({"verified_webhook", "verified_query", "verified_admin_query"})
AUTHORIZED_SOURCES = VERIFIED_SOURCES | {"legacy_verified_event", "server_test_bypass"}
FLASH_ALIASES = {"deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}
QUALITY_CHOICES = {"standard", "high", "literary"}


def canonical_model(model: str) -> str:
    return "deepseek-flash" if model in FLASH_ALIASES else model


def same_amount(actual, expected) -> bool:
    try:
        a, b = Decimal(str(actual)), Decimal(str(expected))
        return a.is_finite() and b.is_finite() and a > 0 and a == b
    except (InvalidOperation, ValueError, TypeError):
        return False


def quote_entitlement(job, *, test_bypass=False) -> dict:
    """Freeze the selected product when the server creates the order."""
    snapshot = {
        "version": 1,
        "state": "test_authorized" if test_bypass else "quoted",
        "source": "server_test_bypass" if test_bypass else "server_quote",
        "order_no": job.id,
        "amount": job.expected_amount,
        "translation_model": canonical_model(job.translation_model or "deepseek-flash"),
        "translation_quality": job.translation_quality or "standard",
        "authorized_at": datetime.now(timezone.utc).isoformat() if test_bypass else None,
    }
    if getattr(job, "enable_precision_polish", False):
        quote = (job.translation_stats or {}).get("precision_polish") or {}
        snapshot["product"] = "conversion_precision_polish"
        snapshot["precision_polish"] = {
            "enabled": True, "version": 1,
            "char_count": int(getattr(job, "polish_char_count", 0) or 0),
            "amount": str(quote.get("quoted_amount") or ""),
            "output_mode": getattr(job.output_mode, "value", job.output_mode),
        }
    return snapshot


def _had_restart(job) -> bool:
    stats = job.translation_stats or {}
    try:
        return (int(stats.get("translation_attempt") or 1) > 1
                or int(stats.get("restart_count") or 0) > 0
                or int(stats.get("free_retry_count") or 0) > 0
                or bool(stats.get("cost_history")))
    except (TypeError, ValueError):
        return True


def grant_test_entitlement(store, job) -> dict:
    """Only the caller's explicit server test configuration may reach here."""
    current = dict(getattr(job, "payment_entitlement", {}) or {})
    if current.get("state") in {"paid", "test_authorized"}:
        return current
    snapshot = dict(current) if current else quote_entitlement(job)
    snapshot.update(state="test_authorized", source="server_test_bypass",
                    authorized_at=datetime.now(timezone.utc).isoformat())
    saved = store.save_payment_entitlement(job.id, snapshot, expected=current)
    return dict(saved.payment_entitlement or {}) if saved else {}


def grant_verified_entitlement(store, job, amount, source, *, allow_legacy_plan=False) -> dict:
    """Called after signature/order verification; never overwrite a paid plan.

An authenticated administrator may explicitly approve a previously mutable
legacy plan after a fresh verified query. Ordinary callbacks cannot do that.
"""
    if source not in VERIFIED_SOURCES:
        raise ValueError("Untrusted payment authorization source")
    if not job.enable_translation and not getattr(job, "enable_precision_polish", False):
        return {}
    current = dict(getattr(job, "payment_entitlement", {}) or {})
    if current.get("state") in {"paid", "test_authorized"}:
        return current
    if not current and _had_restart(job) and not (allow_legacy_plan and source == "verified_admin_query"):
        return {}
    snapshot = dict(current) if current else quote_entitlement(job)
    expected = snapshot.get("amount") or job.expected_amount or "5.99"
    if not same_amount(amount, expected):
        raise ValueError("Payment does not match the frozen entitlement")
    snapshot.update(state="paid", source=source, amount=str(expected),
                    authorized_at=datetime.now(timezone.utc).isoformat())
    if allow_legacy_plan and not current:
        snapshot["legacy_plan_approved"] = True
    saved = store.save_payment_entitlement(job.id, snapshot, expected=current)
    return dict(saved.payment_entitlement or {}) if saved else {}


def precision_polish_entitlement_reason(job) -> str:
    """Paid conversion add-on must be backed by the server's combined quote."""
    if not getattr(job, "enable_precision_polish", False):
        return ""
    if (job.enable_translation
            or getattr(job.output_mode, "value", job.output_mode) != "simplified"):
        return "unsupported_precision_polish"
    entitlement = getattr(job, "payment_entitlement", {}) or {}
    if entitlement.get("state") not in {"paid", "test_authorized"}:
        return "payment_review_required"
    if entitlement.get("source") not in AUTHORIZED_SOURCES or entitlement.get("order_no") != job.id:
        return "payment_review_required"
    if entitlement.get("state") == "test_authorized":
        if entitlement.get("source") != "server_test_bypass" or not job.is_test_order:
            return "payment_review_required"
    elif not same_amount(entitlement.get("amount"), job.expected_amount):
        return "entitlement_mismatch"
    quote = (job.translation_stats or {}).get("precision_polish") or {}
    purchased = entitlement.get("precision_polish") or {}
    if (entitlement.get("product") != "conversion_precision_polish"
            or purchased.get("enabled") is not True
            or purchased.get("output_mode") != "simplified"
            or purchased.get("char_count") != getattr(job, "polish_char_count", 0)
            or not purchased.get("char_count")
            or not same_amount(purchased.get("amount"), quote.get("quoted_amount"))):
        return "entitlement_mismatch"
    return ""


def recover_legacy_entitlement(store, job) -> dict:
    """Lazy, evidence-backed migration; no network and no execution-state guess."""
    current = dict(getattr(job, "payment_entitlement", {}) or {})
    if current or not job.enable_translation or not hasattr(store, "_Session"):
        return current
    from app.storage_db import OrderEventRecord
    with store._Session() as session:
        event = session.get(OrderEventRecord, (job.id, "payment_succeeded"))
        if event is None or event.source not in VERIFIED_SOURCES:
            return {}
        evidence_at = event.occurred_at.isoformat()
    if _had_restart(job):
        return {}
    # Original preflight stores strategy/glossary, not a purchase model/quality.
    # Before the first restart those purchase fields have no public mutator.
    snapshot = quote_entitlement(job)
    snapshot.update(state="paid", source="legacy_verified_event",
                    amount=job.expected_amount or "5.99", authorized_at=evidence_at)
    if not same_amount(snapshot["amount"], snapshot["amount"]):
        return {}
    saved = store.save_payment_entitlement(job.id, snapshot, expected={})
    return dict(saved.payment_entitlement or {}) if saved else {}


def restart_entitlement_reason(job, *, translation_quality=None, translation_model=None) -> str:
    """Pure check used inside the store's restart transaction/lock."""
    if not job.enable_translation:
        return ""
    entitlement = getattr(job, "payment_entitlement", {}) or {}
    if not entitlement:
        return "payment_review_required"
    if entitlement.get("state") not in {"paid", "test_authorized"}:
        return "payment_required"
    if entitlement.get("source") not in AUTHORIZED_SOURCES or entitlement.get("order_no") != job.id:
        return "payment_review_required"
    if entitlement.get("state") == "test_authorized":
        if entitlement.get("source") != "server_test_bypass" or not job.is_test_order:
            return "payment_review_required"
    elif not same_amount(entitlement.get("amount"), job.expected_amount or "5.99"):
        return "payment_review_required"
    purchased_quality = entitlement.get("translation_quality")
    purchased_model = entitlement.get("translation_model")
    if purchased_quality not in QUALITY_CHOICES or purchased_model not in {"deepseek-flash", "deepseek-v4-pro"}:
        return "payment_review_required"
    quality = translation_quality or job.translation_quality or "standard"
    model = canonical_model(translation_model or job.translation_model or "deepseek-flash")
    if quality != purchased_quality or model != purchased_model:
        return "entitlement_mismatch"
    return ""
