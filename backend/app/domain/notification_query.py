"""Bounded public notification projection and scope-bound keyset cursors.

Authorization stays at the HTTP boundary. Cursor data is only a position,
never authorization; every page must re-check its user or job capability.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import re

from app.models import ErrorCode


def _utc(value):
    if not isinstance(value, datetime):
        raise ValueError("invalid notification timestamp")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _scope(job_id, user_id):
    if not job_id and not user_id:
        raise ValueError("notification scope is required")
    return hashlib.sha256(json.dumps([job_id, user_id], separators=(",", ":")).encode()).hexdigest()


def _decode_cursor(cursor, scope):
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024 or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
            raise ValueError()
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {"v", "scope", "at", "id"}
                or type(value["v"]) is not int or value["v"] != 1 or value["scope"] != scope
                or not isinstance(value["at"], str) or len(value["at"]) > 64
                or not isinstance(value["id"], str) or not 1 <= len(value["id"]) <= 96):
            raise ValueError()
        at = datetime.fromisoformat(value["at"])
        if at.tzinfo is None or at.utcoffset().total_seconds() != 0:
            raise ValueError()
        return at, value["id"]
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError) as exc:
        raise ValueError("通知游标无效或不属于当前查询范围") from exc


def _encode_cursor(notification, scope):
    value = {"v": 1, "scope": scope, "at": _utc(notification.created_at).isoformat(), "id": notification.id}
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def public_notification(notification):
    # Historical payloads may predate the write-side privacy guard. Never
    # expose arbitrary message text, filenames, email addresses or capabilities.
    payload = notification.payload if isinstance(notification.payload, dict) else {}
    status = str(payload.get("status") or "")
    messages = {"success": "任务已完成", "completed": "任务已完成", "failed": "任务处理失败，请查看订单状态或联系客服",
                "cancelled": "任务已取消"}
    if status not in messages:
        status = "unknown"
    completed_at = _utc(notification.created_at)
    try:
        if isinstance(payload.get("completed_at"), str):
            completed_at = _utc(datetime.fromisoformat(payload["completed_at"]))
    except (ValueError, TypeError, OverflowError):
        pass
    error = payload.get("error_code")
    error = error if isinstance(error, str) and error in {item.value for item in ErrorCode} else None
    return {
        "id": notification.id, "job_id": notification.job_id, "channel": "in_app",
        "status": notification.status.value, "created_at": _utc(notification.created_at).isoformat(),
        "payload": {"job_id": notification.job_id, "status": status,
                    "message": messages.get(status, "任务状态已更新"), "error_code": error,
                    "completed_at": completed_at.isoformat()},
    }


def list_notification_page(store, *, job_id=None, user_id=None, limit=20, cursor=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("通知每页数量须为 1–100")
    scope = _scope(job_id, user_id)
    before = _decode_cursor(cursor, scope) if cursor is not None else None
    rows = store.list_notification_page(job_id=job_id, user_id=user_id, limit=limit + 1, before=before)
    page = rows[:limit]
    return {"items": [public_notification(row) for row in page],
            "next_cursor": _encode_cursor(page[-1], scope) if len(rows) > limit else None}
