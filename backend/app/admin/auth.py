"""Independent admin credentials, revocable sessions, and database login throttling."""
import hashlib
import hmac
import os
import secrets
import time

from fastapi import HTTPException, Request
from sqlalchemy import Column, Integer, String, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase


class AdminBase(DeclarativeBase):
    pass


class AdminSession(AdminBase):
    __tablename__ = "admin_sessions"
    token = Column(String(64), primary_key=True)
    expires = Column(Integer, nullable=False)
    credential = Column(String(64), nullable=False)


class LoginBucket(AdminBase):
    __tablename__ = "admin_login_buckets"
    key = Column(String(80), primary_key=True)
    count = Column(Integer, nullable=False)
    expires = Column(Integer, nullable=False)


COOKIE = "epub_admin_session"


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600000).hex()
    return f"pbkdf2_sha256$600000${salt}${digest}"


def credentials():
    username = os.environ.get("ADMIN_USERNAME", "tristan")
    encoded = os.environ.get("ADMIN_PASSWORD_HASH", "")
    if not encoded:
        raise HTTPException(503, "管理员密码尚未配置，请运行 scripts/setup-admin.py")
    return username, encoded


def credential_version():
    user, encoded = credentials()
    return hashlib.sha256((user + encoded).encode()).hexdigest()


def verify_password(password, encoded):
    try:
        algorithm, rounds, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or int(rounds) != 600000:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds)).hex()
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


def digest_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def throttle(engine, request):
    now = int(time.time())
    # Do not trust a caller-controlled forwarded-for header. Limit across all workers.
    ip = request.client.host if request.client else "unknown"
    for scope, limit in (("global", 40), (hashlib.sha256(ip.encode()).hexdigest(), 10)):
        key = f"{now // 300}:{scope}"
        try:
            with engine.begin() as conn:
                conn.execute(LoginBucket.__table__.insert().values(key=key, count=0, expires=now + 600))
        except IntegrityError:
            pass
        with engine.begin() as conn:
            conn.execute(delete(LoginBucket).where(LoginBucket.expires < now))
            claimed = conn.execute(update(LoginBucket).where(LoginBucket.key == key, LoginBucket.count < limit)
                                   .values(count=LoginBucket.count + 1))
            if claimed.rowcount != 1:
                raise HTTPException(429, "登录尝试过多，请 5 分钟后重试")


def require_session(engine, request: Request, *, write=False):
    token = request.cookies.get(COOKIE, "")
    if not token or len(token) > 128:
        raise HTTPException(401, "请登录管理员账号")
    with engine.connect() as conn:
        session = conn.execute(select(AdminSession).where(AdminSession.token == digest_token(token))).mappings().first()
    if not session or session["expires"] <= time.time() or session["credential"] != credential_version():
        raise HTTPException(401, "管理员登录已失效")
    if write:
        csrf = request.headers.get("X-CSRF-Token", "")
        if not csrf or not hmac.compare_digest(csrf.encode(), token.encode()):
            raise HTTPException(403, "请求校验失败，请刷新页面")
        reject_cross_origin(request)
    return token


def reject_cross_origin(request):
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "不允许跨站请求")
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "不允许跨站请求")
