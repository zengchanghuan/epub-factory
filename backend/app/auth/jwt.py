"""JWT 工具：签发与验证访问令牌。"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt

_SECRET_KEY = os.environ.get("JWT_SECRET", "CHANGE_ME_IN_PRODUCTION_PLEASE")
_ALGORITHM = "HS256"
_ACCESS_TOKEN_EXPIRE_DAYS = int(os.environ.get("JWT_EXPIRE_DAYS", "7"))


def create_access_token(user_id: str, expire_days: Optional[int] = None) -> str:
    days = expire_days if expire_days is not None else _ACCESS_TOKEN_EXPIRE_DAYS
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + timedelta(days=days),
    }
    return jwt.encode(payload, _SECRET_KEY, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> Optional[str]:
    """解析 JWT，返回 user_id（sub）；无效或过期返回 None。"""
    try:
        payload = jwt.decode(token, _SECRET_KEY, algorithms=[_ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None
