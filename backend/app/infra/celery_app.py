import os

from celery import Celery
from celery.schedules import crontab


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

    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="Asia/Shanghai",
        enable_utc=True,
        task_track_started=True,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        beat_schedule={
            "reconcile-payments-daily": {
                "task": "jobs.reconcile_payments",
                "schedule": crontab(hour=reconcile_hour, minute=reconcile_minute),
                "options": {"expires": 3600},
            },
            "check-balance-daily": {
                "task": "infra.check_balance",
                "schedule": crontab(
                    hour=int(os.environ.get("BALANCE_CHECK_HOUR", "8")),
                    minute=int(os.environ.get("BALANCE_CHECK_MINUTE", "0")),
                ),
                "options": {"expires": 3600},
            },
        },
    )
    return app


celery_app = build_celery_app()
