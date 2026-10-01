"""Durable, bounded admission for model work performed before payment.

Reservations are conservative dispatch units, NOT billed tokens or money.
They are never refunded on errors/missing usage. Cache entries are scoped to
the uploader; account/IP and book budgets survive retries and API restarts.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

from sqlalchemy import BigInteger, Column, String, Text, delete
from sqlalchemy.orm import DeclarativeBase, Session

from app.infra.llm_gateway import GatewayControlError, dispatch_budget_scope
from app.infra.llm_usage_ledger import get_ledger


class PreflightAdmissionError(GatewayControlError):
    def __init__(self, message, *, status_code=429):
        super().__init__(message)
        self.status_code = status_code


class Base(DeclarativeBase):
    pass


class PreflightBudget(Base):
    __tablename__ = "llm_preflight_budgets"
    key = Column(String(200), primary_key=True)
    day = Column(String(10), index=True, nullable=False)
    requests = Column(BigInteger, nullable=False, default=0)
    units = Column(BigInteger, nullable=False, default=0)


class PreflightCache(Base):
    __tablename__ = "llm_preflight_cache"
    key = Column(String(64), primary_key=True)
    day = Column(String(10), index=True, nullable=False)
    state = Column(String(16), nullable=False)
    owner = Column(String(32), nullable=False)
    expires_at = Column(BigInteger, nullable=False)
    payload = Column(Text)


_schema_lock = threading.Lock()


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _setting(name, default, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
        if not 1 <= value <= maximum:
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise PreflightAdmissionError("预分析预算配置无效，未发起模型请求", status_code=503) from None


def fingerprint(epub_path, *, source_sha256=None, options=None):
    digest = hashlib.sha256()
    with Path(epub_path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    normalized_sha256 = digest.hexdigest()
    source_sha256 = source_sha256 or normalized_sha256
    # Include behavior and credential configuration by digest only; never store
    # credentials, raw user identifiers, addresses, or filenames in these keys.
    prefixes = ("EPUB_BOOK_PROFILER_", "EPUB_GLOSSARY_", "EPUB_PREFLIGHT_")
    exact = {"OPENAI_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY", "EPUB_DEFAULT_TRANSLATION_MODEL",
             "OPENAI_DISABLE_JSON_RESPONSE_FORMAT", "LLM_MODEL_ALLOWLIST"}
    config = {key: value for key, value in os.environ.items() if key in exact or key.startswith(prefixes)}
    # Adapters may derive a title from the filename. Preserve the raw-source
    # budget identity, but never reuse analysis for different normalized input.
    version = {"schema": "preflight-admission-v1", "source": source_sha256,
               "normalized_source": normalized_sha256,
               "options": options or {}, "config": config}
    return source_sha256, _digest(json.dumps(version, ensure_ascii=False, sort_keys=True))


class PreflightAdmission:
    def __init__(self, *, engine, source_sha256, config_hash, principal, budget_subjects):
        self.engine = get_ledger(engine).engine
        if self.engine.dialect.name not in {"sqlite", "postgresql"}:
            raise PreflightAdmissionError("预分析预算存储不支持当前数据库", status_code=503)
        self.book_requests = _setting("EPUB_PREFLIGHT_BOOK_REQUESTS", 8, 1000)
        self.book_units = _setting("EPUB_PREFLIGHT_BOOK_UNITS", 200_000, 100_000_000)
        self.subject_requests = _setting("EPUB_PREFLIGHT_DAILY_REQUESTS", 64, 10000)
        self.subject_units = _setting("EPUB_PREFLIGHT_DAILY_UNITS", 1_000_000, 1_000_000_000)
        self.cache_seconds = _setting("EPUB_PREFLIGHT_CACHE_SECONDS", 3600, 86400)
        self.day = datetime.now(timezone.utc).date().isoformat()
        self.owner = uuid.uuid4().hex
        self.cache_key = _digest(self.day + "|" + principal + "|" + config_hash)
        subjects = sorted({_digest(str(value)) for value in budget_subjects})
        if not subjects:
            raise PreflightAdmissionError("预分析缺少预算主体，未发起模型请求", status_code=503)
        # Changing client-session or model/strategy must not reset these limits.
        self.limits = {}
        for subject in subjects:
            self.limits[f"subject:{self.day}:{subject}"] = (self.subject_requests, self.subject_units)
            self.limits[f"book:{self.day}:{subject}:{source_sha256}"] = (self.book_requests, self.book_units)
        self.cached = None
        with _schema_lock:
            Base.metadata.create_all(self.engine)

    @contextmanager
    def transaction(self):
        try:
            with Session(self.engine) as session:
                if self.engine.dialect.name == "sqlite":
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                yield session
                session.commit()
        except GatewayControlError:
            raise
        except Exception as exc:
            raise PreflightAdmissionError("预分析预算状态暂不可确认，未发起新的模型请求", status_code=503) from exc

    def insert_missing(self, session, table, values):
        if self.engine.dialect.name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            from sqlalchemy.dialects.postgresql import insert
        session.execute(insert(table).values(**values).on_conflict_do_nothing(index_elements=["key"]))

    def claim(self):
        with self.transaction() as session:
            self.insert_missing(session, PreflightCache, dict(
                key=self.cache_key, day=self.day, state="new", owner="", expires_at=0,
            ))
            row = session.get(PreflightCache, self.cache_key, with_for_update=True)
            if row.state == "ready" and row.expires_at > time.time():
                self.cached = json.loads(row.payload)
                return self.cached
            if row.state == "running":
                # No unsafe TTL takeover while the prior provider call may be
                # alive. After a crash this key stays blocked for this UTC day.
                raise PreflightAdmissionError("同一书稿正在分析或上次分析状态待确认，请勿重复上传", status_code=409)
            row.state, row.owner, row.payload = "running", self.owner, None
            row.expires_at = int(time.time()) + self.cache_seconds
            # Bound retention opportunistically without deleting today's live
            # owners or resetting their counters. This is not a model retry.
            cutoff = (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()
            session.execute(delete(PreflightCache).where(PreflightCache.day < cutoff))
            session.execute(delete(PreflightBudget).where(PreflightBudget.day < cutoff))
        return None

    def reserve(self, estimated_tokens, *, messages, max_output_tokens=None, **unused):
        if not max_output_tokens or not 1 <= int(max_output_tokens) <= 16384:
            raise PreflightAdmissionError("预分析请求缺少明确输出上限，未发起模型请求", status_code=503)
        # UTF-8 bytes plus framing intentionally over-reserve input. The ledger,
        # not this guard, remains the source of actual token usage and cost.
        units = len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + 512 + int(max_output_tokens)
        with self.transaction() as session:
            cache = session.get(PreflightCache, self.cache_key, with_for_update=True)
            if not cache or cache.state != "running" or cache.owner != self.owner:
                raise PreflightAdmissionError("预分析执行身份已失效，未发起模型请求", status_code=409)
            records = []
            for key, (requests_limit, units_limit) in sorted(self.limits.items()):
                self.insert_missing(session, PreflightBudget, dict(key=key, day=self.day, requests=0, units=0))
                row = session.get(PreflightBudget, key, with_for_update=True)
                if row.requests + 1 > requests_limit or row.units + units > units_limit:
                    raise PreflightAdmissionError("今日预分析预算已达上限，未创建支付订单，请稍后再试")
                records.append(row)
            for row in records:
                row.requests += 1
                row.units += units

    def finish(self, payload=None):
        serialized = json.dumps(payload, ensure_ascii=False) if payload is not None else None
        if serialized is not None and len(serialized.encode("utf-8")) > 2_000_000:
            serialized = None  # Do not persist unbounded sampled model data.
        with self.transaction() as session:
            row = session.get(PreflightCache, self.cache_key, with_for_update=True)
            if row and row.state == "running" and row.owner == self.owner:
                row.state = "ready" if serialized is not None else "failed"
                row.payload = serialized
                row.expires_at = int(time.time()) + self.cache_seconds


@contextmanager
def admitted_preflight(*, engine, source_sha256, config_hash, principal, budget_subjects):
    admission = PreflightAdmission(engine=engine, source_sha256=source_sha256, config_hash=config_hash,
                                   principal=principal, budget_subjects=budget_subjects)
    admission.claim()
    if admission.cached is not None:
        yield admission
        return
    try:
        with dispatch_budget_scope(admission.reserve):
            yield admission
    except BaseException:
        # A failed cache write is not permission to replay a paid request. Keep
        # running/unknown if storage cannot confirm release, and preserve the
        # original accounting/control exception for the job/API boundary.
        try:
            admission.finish()
        except GatewayControlError:
            pass
        raise
