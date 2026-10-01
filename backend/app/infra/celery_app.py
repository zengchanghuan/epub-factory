import os
from pathlib import Path

from dotenv import load_dotenv

# Producers, Beat and either worker role must agree on configuration before
# constructing the app. Task imports (including Compiler) are too late.
# Use the project's file, never an unrelated current working directory, and
# preserve explicitly exported systemd/container/shell values.
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_init
from kombu import Exchange, Queue


BOOK_QUEUE = "celery"  # Retain the production queue and its existing backlog.
HOUSEKEEPING_QUEUE = "housekeeping"
BOOK_TASKS = ("jobs.run_conversion", "jobs.translate_chapter")
HOUSEKEEPING_TASKS = ("jobs.reconcile_payments", "infra.check_balance", "infra.health.ping")


def _housekeeping_limits() -> tuple[int, int]:
    def limit(name: str, default: int) -> int:
        try:
            value = int(os.environ.get(name, str(default)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer number of seconds") from exc
        if not 1 <= value <= 3600:
            raise ValueError(f"{name} must be between 1 and 3600 seconds")
        return value
    # Preserve the existing reconciliation budget: it scans outstanding orders
    # and may make many gateway queries. Queue isolation is not a new timeout
    # policy or real-time SLA; operators can explicitly choose shorter limits.
    soft = limit("CELERY_HOUSEKEEPING_SOFT_TIME_LIMIT", 1500)
    hard = limit("CELERY_HOUSEKEEPING_TIME_LIMIT", 1800)
    if hard <= soft:
        raise ValueError("CELERY_HOUSEKEEPING_TIME_LIMIT must exceed CELERY_HOUSEKEEPING_SOFT_TIME_LIMIT")
    return soft, hard


def validate_worker_queues(sender=None, **_kwargs) -> None:
    """Refuse mixed workers before the pool/consumer starts.

    Celery's Signal.send catches ordinary Exception, so a startup safety gate
    must use SystemExit. Producers and unrelated Celery apps are unaffected.
    """
    app = getattr(sender, "app", None)
    if app is None or not app.conf.get("epub_queue_isolation", False):
        return
    queues = set(app.amqp.queues.consume_from)
    role = app.conf.get("epub_worker_role")
    expected = {"book": BOOK_QUEUE, "housekeeping": HOUSEKEEPING_QUEUE}
    if (len(queues) != 1 or not queues <= {BOOK_QUEUE, HOUSEKEEPING_QUEUE}
            or (role is not None and (role not in expected or queues != {expected[role]}))):
        raise SystemExit("FixEpub workers must consume exactly one queue: use python -m app.infra.worker book|housekeeping")
    # worker_init runs before Celery resolves the pool implementation and before
    # the pool bootstep applies autoscale. Inspect both raw options here, not
    # max_concurrency (which does not exist yet), before any consumer can start.
    from celery.concurrency.prefork import TaskPool as PreforkPool
    pool = getattr(sender, "pool_cls", None)
    if pool is not PreforkPool and pool not in {
        "prefork", "processes", "celery.concurrency.prefork:TaskPool",
        "celery.concurrency.prefork.TaskPool",
    }:
        raise SystemExit("FixEpub workers require the prefork pool for task time limits")
    if HOUSEKEEPING_QUEUE in queues:
        if getattr(sender, "concurrency", None) != 1:
            raise SystemExit("FixEpub housekeeping worker requires concurrency=1")
        if (getattr(sender, "options", None) or {}).get("autoscale") is not None:
            raise SystemExit("FixEpub housekeeping worker does not allow autoscale")


def _default_result_backend(broker_url: str) -> str:
    if broker_url.endswith("/0"):
        return broker_url[:-2] + "/1"
    return broker_url


def build_celery_app() -> Celery:
    broker_url = os.environ.get("CELERY_BROKER_URL") or os.environ.get("REDIS_URL") or "redis://127.0.0.1:6379/0"
    result_backend = os.environ.get("CELERY_RESULT_BACKEND") or _default_result_backend(broker_url)

    app = Celery(
        "epub_factory",
        broker=broker_url,
        backend=result_backend,
        include=[
            "app.tasks.health",
            "app.tasks.job_pipeline",
            "app.tasks.translate",
            "app.tasks.reconcile",
            "app.tasks.balance_check",
        ],
    )

    # 对账定时：每天凌晨 2:00（Asia/Shanghai）执行一次
    reconcile_hour = int(os.environ.get("RECONCILE_CRON_HOUR", "2"))
    reconcile_minute = int(os.environ.get("RECONCILE_CRON_MINUTE", "0"))

    # ── 资源约束（针对 2C2G/4G 小机型）─────────────────────────────────
    # 1. worker_concurrency=1：单 worker 进程，防止两个翻译任务并发吃爆内存。
    #    EPUB BS4 解析峰值可达 4× 文件大小，2GiB 内存机器并发 2 必 OOM。
    # 2. task_time_limit / soft_time_limit：长翻译任务（>30min）硬超时兜底，
    #    防止 Celery worker 被卡死、占住唯一并发位。soft 比 hard 早 5min 触发，
    #    任务侧可以捕获 SoftTimeLimitExceeded 做优雅退出。
    # 3. 升级到 4GiB+ 后，可通过环境变量把 CELERY_WORKER_CONCURRENCY 调到 2~4。
    worker_concurrency = int(os.environ.get("CELERY_WORKER_CONCURRENCY", "1"))
    task_time_limit = int(os.environ.get("CELERY_TASK_TIME_LIMIT", "1800"))
    task_soft_time_limit = int(os.environ.get("CELERY_TASK_SOFT_TIME_LIMIT", "1500"))
    # Whole-book work includes preprocessing, thousands of chunks and packaging.
    # Keep the shorter defaults for housekeeping tasks, not for an entire book.
    book_soft_limit = int(os.environ.get("EPUB_BOOK_SOFT_TIME_LIMIT", "7200"))
    book_hard_limit = int(os.environ.get("EPUB_BOOK_TIME_LIMIT", str(book_soft_limit + 300)))
    if book_soft_limit <= 0 or book_hard_limit <= book_soft_limit:
        raise ValueError("EPUB_BOOK_TIME_LIMIT must exceed a positive EPUB_BOOK_SOFT_TIME_LIMIT")
    housekeeping_soft_limit, housekeeping_hard_limit = _housekeeping_limits()
    visibility_timeout = int(os.environ.get(
        "CELERY_VISIBILITY_TIMEOUT", str(max(10800, book_hard_limit + 1800)),
    ))
    if visibility_timeout <= max(book_hard_limit, task_time_limit, housekeeping_hard_limit):
        raise ValueError("CELERY_VISIBILITY_TIMEOUT must exceed all task hard time limits")

    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="Asia/Shanghai",
        enable_utc=True,
        task_track_started=True,
        task_acks_late=True,
        broker_transport_options={"visibility_timeout": visibility_timeout},
        result_backend_transport_options={"visibility_timeout": visibility_timeout},
        visibility_timeout=visibility_timeout,
        worker_prefetch_multiplier=1,
        worker_concurrency=worker_concurrency,
        epub_queue_isolation=True,
        epub_housekeeping_soft_time_limit=housekeeping_soft_limit,
        epub_housekeeping_time_limit=housekeeping_hard_limit,
        task_default_queue=BOOK_QUEUE,
        task_default_exchange=BOOK_QUEUE,
        task_default_routing_key=BOOK_QUEUE,
        task_create_missing_queues=False,
        task_queues=(
            Queue(BOOK_QUEUE, Exchange(BOOK_QUEUE, type="direct"), routing_key=BOOK_QUEUE),
            Queue(HOUSEKEEPING_QUEUE, Exchange(HOUSEKEEPING_QUEUE, type="direct"), routing_key=HOUSEKEEPING_QUEUE),
        ),
        task_routes={
            **{name: {"queue": BOOK_QUEUE} for name in BOOK_TASKS},
            **{name: {"queue": HOUSEKEEPING_QUEUE} for name in HOUSEKEEPING_TASKS},
        },
        task_time_limit=task_time_limit,
        task_soft_time_limit=task_soft_time_limit,
        task_annotations={
            "jobs.run_conversion": {
                "soft_time_limit": book_soft_limit,
                "time_limit": book_hard_limit,
            },
            **{name: {
                "soft_time_limit": housekeeping_soft_limit,
                "time_limit": housekeeping_hard_limit,
            } for name in HOUSEKEEPING_TASKS},
        },
        beat_schedule={
            "reconcile-payments-daily": {
                "task": "jobs.reconcile_payments",
                "schedule": crontab(hour=reconcile_hour, minute=reconcile_minute),
                "options": {"expires": 3600, "queue": HOUSEKEEPING_QUEUE},
            },
            "check-balance-daily": {
                "task": "infra.check_balance",
                "schedule": crontab(
                    hour=int(os.environ.get("BALANCE_CHECK_HOUR", "8")),
                    minute=int(os.environ.get("BALANCE_CHECK_MINUTE", "0")),
                ),
                "options": {"expires": 3600, "queue": HOUSEKEEPING_QUEUE},
            },
        },
    )
    return app


celery_app = build_celery_app()
worker_init.connect(validate_worker_queues, weak=False, dispatch_uid="epub.worker.queue_isolation")

from .worker_control import register_worker_control_guards
register_worker_control_guards()

# Worker parents load the store while importing task modules; connections from
# that setup must never be reused by prefork children or replacement workers.
from .worker_db_lifecycle import register_worker_db_lifecycle
register_worker_db_lifecycle()
