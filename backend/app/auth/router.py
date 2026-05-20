"""
认证路由 /api/v2/auth/*
包含：手机号短信、Google OAuth、微信 OAuth、用户信息、任务归属
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel

from ..models import User
from .jwt import create_access_token
from .deps import get_current_user_optional, require_current_user
from . import sms as _sms
from . import google as _google
from . import wechat as _wechat

router = APIRouter(prefix="/api/v2/auth", tags=["auth"])


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

def _get_store():
    from ..storage import job_store
    return job_store


def _find_or_create_user(**kwargs) -> User:
    """
    通用查或建用户逻辑。kwargs 为查询字段（phone / google_id / wechat_openid）。
    找到已有用户则更新最后登录时间；否则创建新用户。
    """
    store = _get_store()
    user: Optional[User] = None

    if "phone" in kwargs and kwargs["phone"]:
        user = store.get_user_by_phone(kwargs["phone"])
    elif "google_id" in kwargs and kwargs["google_id"]:
        user = store.get_user_by_google_id(kwargs["google_id"])
    elif "wechat_openid" in kwargs and kwargs["wechat_openid"]:
        user = store.get_user_by_wechat_openid(kwargs["wechat_openid"])

    now = datetime.now(timezone.utc)
    if user:
        # 更新最后登录时间及可能的新字段
        for k, v in kwargs.items():
            if v is not None:
                setattr(user, k, v)
        user.last_login_at = now
        store.update_user(user)
        return user

    new_user = User(
        id=str(uuid.uuid4()),
        created_at=now,
        last_login_at=now,
        **{k: v for k, v in kwargs.items() if hasattr(User, k)},
    )
    return store.create_user(new_user)


def _build_auth_response(user: User) -> dict:
    token = create_access_token(user.id)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "user": {
            "id": user.id,
            "display_name": user.display_name,
            "avatar_url": user.avatar_url,
            "phone": user.phone,
        },
    }


# ─── 手机号短信 ───────────────────────────────────────────────────────────────

class SmsSendRequest(BaseModel):
    phone: str


class SmsVerifyRequest(BaseModel):
    phone: str
    code: str
    session_id: Optional[str] = None  # 登录后自动归属匿名任务


@router.post("/sms/send")
def sms_send(body: SmsSendRequest):
    """发送验证码（60s 限流）。"""
    phone = body.phone.strip()
    if not phone or len(phone) < 8:
        raise HTTPException(status_code=400, detail="手机号格式错误")
    code, ok = _sms.generate_and_store_code(phone)
    if not ok:
        raise HTTPException(status_code=429, detail="发送太频繁，请 60 秒后再试")
    sent = _sms.send_sms(phone, code)
    if not sent:
        raise HTTPException(status_code=500, detail="短信发送失败，请稍后重试")
    return {"message": "验证码已发送"}


@router.post("/sms/verify")
def sms_verify(body: SmsVerifyRequest):
    """校验验证码，登录/注册，返回 JWT。"""
    phone = body.phone.strip()
    if not _sms.verify_code(phone, body.code):
        raise HTTPException(status_code=400, detail="验证码错误或已过期")

    user = _find_or_create_user(phone=phone)
    _maybe_claim_jobs(user.id, body.session_id)
    return _build_auth_response(user)


# ─── Google OAuth（暂未启用，配置 GOOGLE_CLIENT_ID 后自动激活）────────────────

import os as _os

@router.get("/google/authorize")
def google_authorize(redirect_to: Optional[str] = None):
    if not _os.environ.get("GOOGLE_CLIENT_ID"):
        raise HTTPException(status_code=501, detail="Google 登录暂未开放")
    url = _google.get_authorize_url(state=redirect_to or "")
    return {"authorize_url": url}


@router.get("/google/callback")
def google_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    if not _os.environ.get("GOOGLE_CLIENT_ID"):
        raise HTTPException(status_code=501, detail="Google 登录暂未开放")
    if error or not code:
        raise HTTPException(status_code=400, detail=f"Google OAuth 失败: {error}")

    userinfo = _google.exchange_code(code)
    if not userinfo or not userinfo.get("google_id"):
        raise HTTPException(status_code=502, detail="无法获取 Google 用户信息")

    user = _find_or_create_user(
        google_id=userinfo["google_id"],
        display_name=userinfo.get("name"),
        avatar_url=userinfo.get("picture"),
    )
    token = create_access_token(user.id)

    import urllib.parse
    front = state or "/"
    if not front.startswith("http"):
        front = f"/{front.lstrip('/')}"
    sep = "#" if "#" not in front else "&"
    redirect_url = f"{front}{sep}access_token={urllib.parse.quote(token)}"
    return RedirectResponse(url=redirect_url)


# ─── 微信 OAuth（暂未启用，配置 WECHAT_APP_ID 后自动激活）──────────────────────

@router.get("/wechat/authorize")
def wechat_authorize(state: str = "STATE"):
    if not _os.environ.get("WECHAT_APP_ID"):
        raise HTTPException(status_code=501, detail="微信登录暂未开放")
    url = _wechat.get_authorize_url(state=state)
    return RedirectResponse(url=url)


@router.get("/wechat/callback")
def wechat_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    if not _os.environ.get("WECHAT_APP_ID"):
        raise HTTPException(status_code=501, detail="微信登录暂未开放")
    if not code:
        raise HTTPException(status_code=400, detail="微信 OAuth 回调缺少 code")

    userinfo = _wechat.exchange_code(code)
    if not userinfo or not userinfo.get("openid"):
        raise HTTPException(status_code=502, detail="无法获取微信用户信息")

    user = _find_or_create_user(
        wechat_openid=userinfo["openid"],
        wechat_unionid=userinfo.get("unionid"),
        display_name=userinfo.get("nickname"),
        avatar_url=userinfo.get("headimgurl"),
    )
    token = create_access_token(user.id)

    import urllib.parse
    front = state or "/"
    if not front.startswith("http"):
        front = f"/{front.lstrip('/')}"
    sep = "#" if "#" not in front else "&"
    redirect_url = f"{front}{sep}access_token={urllib.parse.quote(token)}"
    return RedirectResponse(url=redirect_url)


# ─── 当前用户信息 ─────────────────────────────────────────────────────────────

@router.get("/me")
def get_me(request: Request):
    """获取当前登录用户信息，未登录返回 null。"""
    user = get_current_user_optional(request)
    if not user:
        return {"user": None}
    return {
        "user": {
            "id": user.id,
            "display_name": user.display_name,
            "avatar_url": user.avatar_url,
            "phone": user.phone,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        }
    }


# ─── 历史任务归属 ─────────────────────────────────────────────────────────────

class ClaimJobsRequest(BaseModel):
    session_id: str


@router.post("/claim-jobs")
def claim_jobs(body: ClaimJobsRequest, request: Request):
    """登录后，将当前匿名 session 下的历史任务批量归属到账号。"""
    user = require_current_user(request)
    store = _get_store()
    if not hasattr(store, "claim_jobs_by_session"):
        return {"claimed": 0, "message": "当前存储不支持任务归属"}
    count = store.claim_jobs_by_session(body.session_id, user.id)
    return {"claimed": count, "message": f"已归属 {count} 个历史任务"}


# ─── 内部工具 ─────────────────────────────────────────────────────────────────

def _maybe_claim_jobs(user_id: str, session_id: Optional[str]) -> None:
    """sms/verify 等接口登录后自动归属匿名任务（session_id 由前端传入）。"""
    if not session_id:
        return
    store = _get_store()
    if hasattr(store, "claim_jobs_by_session"):
        store.claim_jobs_by_session(session_id, user_id)
