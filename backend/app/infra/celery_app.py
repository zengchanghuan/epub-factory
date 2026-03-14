import os

from celery import Celery


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
        include=["app.tasks.health", "app.tasks.job_pipeline", "app.tasks.translate"],
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        task_track_started=True,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
    )
    return app


celery_app = build_celery_app()
