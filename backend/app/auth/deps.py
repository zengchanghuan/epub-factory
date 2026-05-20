"""FastAPI 依赖注入：解析 JWT，注入当前用户（可选）。"""
from typing import Optional

from fastapi import Header, Request

from ..models import User
from .jwt import decode_access_token


def get_current_user_optional(request: Request) -> Optional[User]:
    """
    从 Authorization: Bearer <token> 解析当前用户。
    无 token / token 无效时返回 None，不抛出异常。
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer "):]
    user_id = decode_access_token(token)
    if not user_id:
        return None

    from ..storage import job_store
    if not hasattr(job_store, "get_user"):
        return None
    return job_store.get_user(user_id)


def require_current_user(request: Request) -> User:
    """必须登录，否则抛 401。"""
    from fastapi import HTTPException
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="需要登录后才能使用该功能")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已被禁用")
    return user
