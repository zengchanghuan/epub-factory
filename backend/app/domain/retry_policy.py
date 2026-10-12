"""Customer retry admission and compact, immutable attempt outcomes."""
import hashlib
import json


def outcome(stats, *, status, error_code, message, finished_at):
    qa = stats.get("qa_report") or {}
    signature = hashlib.sha256(json.dumps(
        [error_code or "", sorted(qa.get("flags") or [])], ensure_ascii=False).encode()).hexdigest()
    return {
        "attempt_id": stats.get("attempt_id"), "attempt": stats.get("translation_attempt", 1),
        "status": status, "error_code": error_code, "message": (message or "")[:500],
        "finished_at": finished_at, "failure_signature": signature,
        "qa_status": qa.get("status"), "qa_flags": list(qa.get("flags") or []),
        "validation": stats.get("epub_validation"), "artifact": stats.get("artifact_manifest"),
        "rejected_artifact": stats.get("rejected_artifact"),
    }


def blocked_reason(stats, *, error_code, limit):
    if limit < 0:  # Explicit administrator review path.
        return None
    if int(stats.get("free_retry_count") or 0) >= limit:
        return "retry_limit"
    if error_code in {"EPUB_VALIDATION_FAILED", "EPUB_VALIDATION_UNAVAILABLE"}:
        return "manual_review"
    history = stats.get("attempt_history") or []
    if error_code and history and error_code != "TRANSLATION_PROVIDER_UNAVAILABLE":
        current = outcome(stats, status="failed", error_code=error_code, message="", finished_at="")
        if (history[-1].get("status") == "failed"
                and history[-1].get("failure_signature") == current["failure_signature"]):
            return "manual_review"
    return None
