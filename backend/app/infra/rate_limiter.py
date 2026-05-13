"""
IP 日限流器（SQLite 持久化）

策略：
- 每个 IP 每天最多 FREE_DAILY_LIMIT 次免费转换（默认 3）
- 文件大小上限 MAX_FILE_SIZE_MB（默认 20MB）
- 可通过环境变量覆盖，便于测试和运维调整
"""

import ipaddress
import os
import sqlite3
from datetime import date
from pathlib import Path
from threading import Lock

FREE_DAILY_LIMIT: int = int(os.environ.get("FREE_DAILY_LIMIT", "3"))
MAX_FILE_SIZE_MB: int = int(os.environ.get("MAX_FILE_SIZE_MB", "20"))
MAX_FILE_SIZE_BYTES: int = MAX_FILE_SIZE_MB * 1024 * 1024

# 反向代理白名单：只有当 TCP 对端 IP 落在这些 CIDR 内时，才信任请求里的
# X-Real-IP / X-Forwarded-For 头。默认仅信任本机回环（Nginx → 127.0.0.1:8000）。
_TRUSTED_PROXIES_RAW = os.environ.get("TRUSTED_PROXIES", "127.0.0.1/32,::1/128")
_TRUSTED_PROXY_NETS: list = []
for _cidr in _TRUSTED_PROXIES_RAW.split(","):
    _cidr = _cidr.strip()
    if not _cidr:
        continue
    try:
        _TRUSTED_PROXY_NETS.append(ipaddress.ip_network(_cidr, strict=False))
    except ValueError:
        continue

_DEFAULT_DB = str(Path(__file__).resolve().parent.parent.parent / "rate_limit.db")


class RateLimiter:
    """基于 SQLite 的 IP 日限流器，线程安全。"""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or _DEFAULT_DB
        self._lock = Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ip_daily_usage (
                    ip   TEXT NOT NULL,
                    date TEXT NOT NULL,
                    cnt  INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (ip, date)
                )
                """
            )
            conn.commit()

    def get_count(self, ip: str) -> int:
        """返回该 IP 今日已用次数。"""
        today = date.today().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cnt FROM ip_daily_usage WHERE ip=? AND date=?",
                (ip, today),
            ).fetchone()
        return row[0] if row else 0

    def check_and_increment(self, ip: str) -> tuple[bool, int]:
        """
        检查是否还有剩余配额，有则计数 +1 并返回 (True, new_count)。
        无则返回 (False, current_count)，不修改计数。
        """
        today = date.today().isoformat()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT cnt FROM ip_daily_usage WHERE ip=? AND date=?",
                    (ip, today),
                ).fetchone()
                current = row[0] if row else 0
                if current >= FREE_DAILY_LIMIT:
                    return False, current
                conn.execute(
                    """
                    INSERT INTO ip_daily_usage (ip, date, cnt) VALUES (?, ?, 1)
                    ON CONFLICT(ip, date) DO UPDATE SET cnt = cnt + 1
                    """,
                    (ip, today),
                )
                conn.commit()
                return True, current + 1

    def reset_ip(self, ip: str) -> None:
        """清除某 IP 今日计数（测试 / 管理员手动重置用）。"""
        today = date.today().isoformat()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM ip_daily_usage WHERE ip=? AND date=?",
                (ip, today),
            )
            conn.commit()


def get_real_ip(request) -> str:
    """
    提取真实客户端 IP。
    安全要点：只有当请求来自 TRUSTED_PROXIES 白名单内的反向代理时，才信任
    上游设置的 X-Real-IP / X-Forwarded-For 头；否则一律以 TCP 对端为准，
    防止外部攻击者伪造 IP 绕过限流或冒用旧任务的 legacy IP 鉴权兜底。
    """
    peer_host = request.client.host if request.client else ""
    peer_trusted = False
    if peer_host:
        try:
            peer_ip = ipaddress.ip_address(peer_host)
            peer_trusted = any(peer_ip in net for net in _TRUSTED_PROXY_NETS)
        except ValueError:
            peer_trusted = False

    if peer_trusted:
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
    return peer_host or "unknown"


rate_limiter = RateLimiter()
