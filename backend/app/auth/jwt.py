"""JWT 工具：签发与验证访问令牌。"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from jose import JWTError, jwt

_SECRET_KEY = os.environ.get("JWT_SECRET", "")
_PUBLIC_PLACEHOLDERS = frozenset({
    "CHANGE_ME_IN_PRODUCTION_PLEASE",
    "replace_with_a_long_random_secret_key",
})
_ALGORITHM = "HS256"
_ACCESS_TOKEN_EXPIRE_DAYS = int(os.environ.get("JWT_EXPIRE_DAYS", "7"))


def _configured_secret() -> Optional[str]:
    # Do not silently sign/accept account identities with a published example
    # key. Preserve explicitly configured key bytes; this is not a new password
    # strength policy or a change to the existing token algorithm/lifetime.
    if not isinstance(_SECRET_KEY, str) or not _SECRET_KEY.strip() or _SECRET_KEY.strip() in _PUBLIC_PLACEHOLDERS:
        return None
    return _SECRET_KEY


def create_access_token(user_id: str, expire_days: Optional[int] = None) -> str:
    secret = _configured_secret()
    if secret is None:
        raise HTTPException(status_code=503, detail="账号登录服务配置未就绪，请联系管理员")
    days = expire_days if expire_days is not None else _ACCESS_TOKEN_EXPIRE_DAYS
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + timedelta(days=days),
    }
    return jwt.encode(payload, secret, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> Optional[str]:
    """解析 JWT，返回 user_id（sub）；无效或过期返回 None。"""
    secret = _configured_secret()
    if secret is None:
        return None
    try:
        payload = jwt.decode(token, secret, algorithms=[_ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None
