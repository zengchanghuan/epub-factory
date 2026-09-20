"""Durable, per-request LLM accounting, independent of translation success/cache.

Only identifiers and numeric usage are persisted. No prompts, book contents,
credentials, response text or raw error messages enter this ledger.
"""
import asyncio
from collections import Counter, defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from functools import wraps
from pathlib import Path
import threading
import uuid

from sqlalchemy import BigInteger, Boolean, Column, DateTime, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Session

from .llm_pricing import catalog, price_usage, provider_host, utc


class AccountingError(BaseException):
    """Fail closed without the ordinary provider fallback/retry spending again."""


class LedgerBase(DeclarativeBase):
    pass


class UsageAttempt(LedgerBase):
    __tablename__ = "llm_usage_attempts"
    job_id = Column(String(32), primary_key=True)
    attempt_id = Column(String(64), primary_key=True)
    started_at = Column(DateTime(timezone=True), nullable=False)
    prior_usage_untracked = Column(Boolean, nullable=False, default=False)


class UsageRequest(LedgerBase):
    __tablename__ = "llm_usage_requests"
    id = Column(String(32), primary_key=True)
    job_id = Column(String(32), index=True, nullable=False)
    attempt_id = Column(String(64), nullable=False)
    stage = Column(String(40), nullable=False)
    provider = Column(String(253), nullable=False)
    requested_model = Column(String(128), nullable=False)
    response_model = Column(String(128))
    response_id = Column(String(256))
    provider_request_id = Column(String(256))
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True))
    request_status = Column(String(32), nullable=False)
    error_type = Column(String(80))
    http_status = Column(BigInteger)
    usage_status = Column(String(32), nullable=False)
    prompt_tokens = Column(BigInteger)
    completion_tokens = Column(BigInteger)
    total_tokens = Column(BigInteger)
    cache_hit_tokens = Column(BigInteger)
    cache_miss_tokens = Column(BigInteger)
    reasoning_tokens = Column(BigInteger)
    price_status = Column(String(40), nullable=False)
    currency = Column(String(3))
    calculated_cost = Column(String(80))
    rate_version = Column(String(100))
    rate_snapshot = Column(Text)
    time_band = Column(String(16))
    bill_currency = Column(String(3))
    bill_amount = Column(String(80))
    bill_source_sha256 = Column(String(64))
    bill_imported_at = Column(DateTime(timezone=True))


_scope = ContextVar("llm_usage_scope", default=None)
_stage = ContextVar("llm_usage_stage", default="body")
_lock = threading.RLock()
_ledgers = {}
_default_engine = None


def now():
    return datetime.now(timezone.utc)


def field(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def counter(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1 else None


def normalize_usage(response):
    usage = field(response, "usage")
    result = {key: None for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                                   "cache_hit_tokens", "cache_miss_tokens", "reasoning_tokens")}
    result["usage_status"] = "missing" if usage is None else "invalid"
    if usage is None:
        return result
    prompt, completion = counter(field(usage, "prompt_tokens")), counter(field(usage, "completion_tokens"))
    total = counter(field(usage, "total_tokens"))
    hit = counter(field(usage, "prompt_cache_hit_tokens"))
    miss = counter(field(usage, "prompt_cache_miss_tokens"))
    cached = counter(field(field(usage, "prompt_tokens_details"), "cached_tokens"))
    reasoning = counter(field(field(usage, "completion_tokens_details"), "reasoning_tokens"))
    result.update(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total,
                  cache_hit_tokens=hit if hit is not None else cached, cache_miss_tokens=miss,
                  reasoning_tokens=reasoning)
    if prompt is None or completion is None or prompt + completion > 2**63 - 1:
        return result
    if field(usage, "total_tokens") is not None and total != prompt + completion:
        return result
    # Absent cache counters mean unknown, not cache misses. One reported side
    # can be derived from the provider's total input; contradictions are invalid.
    hit = result["cache_hit_tokens"]
    if (hit is not None and hit > prompt) or (miss is not None and miss > prompt):
        return result
    if hit is not None and miss is not None and hit + miss != prompt:
        return result
    if cached is not None and hit is not None and cached != hit:
        return result
    if field(field(usage, "prompt_tokens_details"), "cached_tokens") is not None and cached is None:
        return result
    if field(field(usage, "completion_tokens_details"), "reasoning_tokens") is not None and reasoning is None:
        return result
    for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
        if field(usage, key) is not None and counter(field(usage, key)) is None:
            return result
    if reasoning is not None and reasoning > completion:
        return result
    result.update(usage_status="complete", total_tokens=prompt + completion,
                  cache_hit_tokens=hit if hit is not None else prompt - miss if miss is not None else None,
                  cache_miss_tokens=miss if miss is not None else prompt - hit if hit is not None else None)
    return result


def identifier(value, length):
    return value[:length] if isinstance(value, str) else None


def add_amount(totals, currency, value):
    with localcontext() as context:
        context.prec = 100
        totals[currency] = totals.get(currency, Decimal(0)) + Decimal(value)


class UsageLedger:
    def __init__(self, engine):
        self.engine = engine
        LedgerBase.metadata.create_all(engine)

    def start_attempt(self, job_id, attempt_id, prior_usage_untracked=False, existing_stats=None):
        existing_stats = existing_stats or {}
        with Session(self.engine) as session:
            # SQLite serializes starts; PostgreSQL job execution leases already
            # exclude competing starts of the same attempt.
            if self.engine.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            if session.get(UsageAttempt, (job_id, attempt_id)) is None:
                prior_usage_untracked = prior_usage_untracked or any(
                    (existing_stats.get(key) or 0) > 0 for key in ("api_calls", "prompt_tokens", "completion_tokens"))
                if session.get(UsageAttempt, (job_id, "preflight")) is None and any(
                    existing_stats.get(key) for key in ("translation_preflight", "book_profile", "glossary_stats", "literary_style_guide")):
                    # Cached old preparations may have incurred paid calls even
                    # when the old body counters are zero. Never erase them.
                    prior_usage_untracked = True
                session.add(UsageAttempt(job_id=job_id, attempt_id=attempt_id, started_at=now(),
                                         prior_usage_untracked=prior_usage_untracked))
                session.commit()

    def begin(self, job_id, attempt_id, stage, base_url, model):
        request_id = uuid.uuid4().hex
        started = now()
        prices = catalog()  # Freeze the versioned catalog before the paid call.
        hint = price_usage(provider_host(base_url), model, None,
                           dict(usage_status="complete",prompt_tokens=0,completion_tokens=0,
                                cache_hit_tokens=0,cache_miss_tokens=0), started, started, prices)
        with Session(self.engine) as session:
            session.add(UsageRequest(id=request_id, job_id=job_id, attempt_id=attempt_id,
                                     stage=stage, provider=provider_host(base_url), requested_model=model[:128],
                                     started_at=started, request_status="in_flight", usage_status="missing",
                                     price_status="in_flight", rate_version=hint['rate_version'],
                                     rate_snapshot=hint['rate_snapshot'], currency=hint['currency'], time_band=hint['time_band']))
            session.commit()
        return request_id, started, prices

    def finish(self, reservation, response=None, error=None):
        request_id, started, prices = reservation
        ended = now()
        usage = normalize_usage(response)
        with Session(self.engine) as session:
            if self.engine.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = session.get(UsageRequest, request_id, with_for_update=True)
            if row.request_status != "in_flight":
                return  # Completion is idempotent; never increment a counter twice.
            response_model = identifier(field(response, "model"), 128)
            price = price_usage(row.provider, row.requested_model, response_model, usage, started, ended, prices)
            response_id = identifier(field(response, "id"), 256)
            if response_id and session.scalar(select(UsageRequest.id).where(
                UsageRequest.provider == row.provider, UsageRequest.response_id == response_id,
                UsageRequest.id != row.id).limit(1)):
                # Keep the call evidence, but don't silently charge a replayed
                # provider response twice. Match it against the provider bill.
                price.update(price_status="duplicate_response_id", calculated_cost=None)
            for key, value in {**usage, **price}.items():
                setattr(row, key, value)
            row.ended_at = ended
            row.response_model = response_model
            row.response_id = response_id
            row.provider_request_id = (identifier(field(response, "_request_id"), 256)
                                       or identifier(getattr(error, "request_id", None), 256))
            row.request_status = "cancelled" if isinstance(error, asyncio.CancelledError) else "error" if error else "response"
            row.error_type = type(error).__name__[:80] if error else None
            row.http_status = counter(getattr(error, "status_code", None)) if error else None
            session.commit()

    def summary(self, job_id, stats=None):
        stats = stats or {}
        with Session(self.engine) as session:
            attempts = list(session.scalars(select(UsageAttempt).where(UsageAttempt.job_id == job_id)))
            # A large book can have thousands of requests. Aggregation doesn't
            # need their tariff JSON snapshots, ids or errors in memory.
            rows = list(session.execute(select(
                UsageRequest.stage, UsageRequest.total_tokens, UsageRequest.price_status,
                UsageRequest.currency, UsageRequest.calculated_cost, UsageRequest.bill_amount,
                UsageRequest.bill_currency, UsageRequest.prompt_tokens, UsageRequest.completion_tokens,
                UsageRequest.cache_hit_tokens, UsageRequest.cache_miss_tokens, UsageRequest.usage_status,
            ).where(UsageRequest.job_id == job_id)))
        tracked = {a.attempt_id for a in attempts}
        legacy = sum(1 for item in [*stats.get("cost_history", []), stats]
                     if item.get("attempt_id") not in tracked
                     and ((item.get("api_calls") or 0) > 0 or (item.get("prompt_tokens") or 0) > 0))
        # Retry history sometimes lacks identifiers/counters altogether.
        prior_tracked = len(tracked - {"preflight", stats.get("attempt_id")})
        # A first attempt awaiting payment has not spent anything on body yet.
        # Only previous attempts are inferred from the retry counter; a missing
        # current attempt needs positive call/token evidence above.
        legacy = max(legacy, max(0, int(stats.get("translation_attempt") or 1) - 1 - prior_tracked)) if stats else legacy
        legacy += sum(bool(a.prior_usage_untracked) for a in attempts)
        calculated, bills = defaultdict(Decimal), defaultdict(Decimal)
        stages = defaultdict(lambda: {"requests": 0, "priced_requests": 0, "total_tokens": 0})
        for row in rows:
            stage = stages[row.stage]
            stage["requests"] += 1
            stage["total_tokens"] += row.total_tokens or 0
            if row.price_status == "priced":
                add_amount(calculated, row.currency, row.calculated_cost)
                stage["priced_requests"] += 1
            if row.bill_amount is not None:
                add_amount(bills, row.bill_currency, row.bill_amount)
        unknown = sum(r.price_status != "priced" for r in rows)
        result = dict(source="request_ledger", scope="recorded_requests_so_far", coverage="complete" if attempts and not unknown and not legacy else "incomplete" if attempts else "historical_unknown",
                      requests=len(rows), priced_requests=len(rows) - unknown, pending_requests=unknown,
                      historical_untracked_attempts=legacy, tracked_attempts=len(attempts),
                      calculated_totals={k: format(v, "f") for k, v in calculated.items()},
                      bill_imported_totals={k: format(v, "f") for k, v in bills.items()},
                      bill_matched_requests=sum(r.bill_amount is not None for r in rows),
                      prompt_tokens=sum(r.prompt_tokens or 0 for r in rows),
                      completion_tokens=sum(r.completion_tokens or 0 for r in rows),
                      cache_hit_tokens=sum(r.cache_hit_tokens or 0 for r in rows),
                      cache_miss_tokens=sum(r.cache_miss_tokens or 0 for r in rows),
                      tokens_complete=bool(attempts) and not legacy and all(r.usage_status == "complete" for r in rows),
                      cache_tokens_complete=bool(attempts) and not legacy and all(r.cache_hit_tokens is not None and r.cache_miss_tokens is not None for r in rows),
                      pending_reasons=dict(Counter(r.price_status for r in rows if r.price_status != "priced")), stages=dict(stages),
                      note="截至当前按逐请求实际 usage 与留存价目计算，含前期分析、失败响应及历次重译；运行中仍会增加，不是供应商实际扣款。待核实请求和未记录历史不按 0 元处理，不同币种不相加。账单导入值需另行核验供应商原始账单。")
        return result

    def requests(self, job_id, page=1, page_size=50):
        with Session(self.engine) as session:
            rows = list(session.scalars(select(UsageRequest).where(UsageRequest.job_id == job_id)
                                        .order_by(UsageRequest.started_at, UsageRequest.id)
                                        .offset((page - 1) * page_size).limit(page_size)))
            return [{column.name: (utc(value).isoformat() if isinstance(value, datetime) else value)
                     for column in UsageRequest.__table__.columns
                     if column.name != "rate_snapshot"
                     for value in [getattr(row, column.name)]} for row in rows]

    def import_bill(self, request_id, *, response_id, provider, amount, currency, source_sha256, dry_run=False):
        """Match an operator-supplied provider bill row, never guess by book size.

        The source hash is traceability, NOT cryptographic verification that the
        supplier issued a bill. Importing is explicit, idempotent and local.
        """
        amount = Decimal(str(amount))
        if not amount.is_finite() or amount < 0 or not -40 <= amount.as_tuple().exponent <= 40 or len(format(amount, "f")) > 80:
            raise ValueError("invalid bill amount")
        if len(currency) != 3 or not currency.isalpha() or not currency.isupper():
            raise ValueError("invalid bill currency")
        if len(source_sha256) != 64 or any(c not in "0123456789abcdef" for c in source_sha256):
            raise ValueError("bill source must be a sha256")
        with Session(self.engine) as session:
            if self.engine.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = session.get(UsageRequest, request_id, with_for_update=True)
            if row is None or not response_id or row.response_id != response_id or row.provider != provider:
                raise ValueError("bill request/provider mismatch")
            if row.bill_amount is not None:
                if (Decimal(row.bill_amount), row.bill_currency, row.bill_source_sha256) != (amount, currency, source_sha256):
                    raise ValueError("conflicting bill import")
                return
            duplicate = session.scalar(select(UsageRequest.id).where(
                UsageRequest.provider == provider, UsageRequest.response_id == response_id,
                UsageRequest.bill_amount.is_not(None), UsageRequest.id != request_id).limit(1))
            if duplicate:
                raise ValueError("provider bill response already matched")
            if dry_run:
                return
            row.bill_amount = format(amount, "f")
            row.bill_currency, row.bill_source_sha256, row.bill_imported_at = currency, source_sha256, now()
            session.commit()


def get_ledger(engine=None):
    global _default_engine
    with _lock:
        if engine is None:
            if _default_engine is None:
                import os
                url = os.environ.get("DATABASE_URL") or "sqlite:///" + str(Path(__file__).resolve().parents[2] / "epub_jobs.db")
                _default_engine = create_engine(url, pool_pre_ping=True,
                                               connect_args={"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {})
            engine = _default_engine
        if engine not in _ledgers:
            _ledgers[engine] = UsageLedger(engine)
        return _ledgers[engine]


@contextmanager
def usage_scope(job_id, attempt_id, *, engine=None, prior_usage_untracked=False, existing_stats=None):
    try:
        ledger = get_ledger(engine)
        ledger.start_attempt(job_id, attempt_id, prior_usage_untracked, existing_stats)
    except Exception as exc:
        raise AccountingError("无法建立模型费用账本，未发起新的付费请求") from exc
    token = _scope.set((ledger, job_id, attempt_id))
    try:
        yield ledger
    finally:
        _scope.reset(token)


def billing_stage(stage):
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            token = _stage.set(stage)
            try:
                return await function(*args, **kwargs)
            finally:
                _stage.reset(token)
        return wrapped
    return decorate


def _reserve(model, base_url, stage):
    scope = _scope.get()
    if not scope:
        return None
    ledger, job_id, attempt_id = scope
    try:
        return ledger, ledger.begin(job_id, attempt_id, stage or _stage.get(), base_url, model)
    except Exception as exc:
        raise AccountingError("模型费用记录失败，未发起新的付费请求") from exc


def _finish(reservation, response=None, error=None):
    if reservation:
        try:
            reservation[0].finish(reservation[1], response=response, error=error)
        except Exception as exc:
            # The pre-call reservation remains pending. Do not retry the paid
            # request solely because persisting accounting failed afterwards.
            raise AccountingError("模型请求已结束但费用落盘失败，请核对待核实记录；停止新增付费请求") from exc


async def accounted_request(operation, *, model, base_url, stage=None, usage_observer=None):
    try:
        reservation = _reserve(model, base_url, stage)
    except AccountingError:
        if hasattr(operation, "close"):
            operation.close()
        raise
    try:
        response = await operation
    except BaseException as exc:
        _finish(reservation, error=exc)
        raise
    _finish(reservation, response=response)
    if usage_observer:
        usage_observer(normalize_usage(response))
    return response


def accounted_call(operation, *, model, base_url, response_usage=lambda value: value, stage=None):
    reservation = _reserve(model, base_url, stage)
    try:
        response = operation()
    except BaseException as exc:
        _finish(reservation, error=exc)
        raise
    try:
        envelope = response_usage(response)
    except Exception:
        envelope = None
    _finish(reservation, response=envelope)
    return response
