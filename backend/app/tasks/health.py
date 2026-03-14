from datetime import datetime, timezone

from app.infra.celery_app import celery_app


@celery_app.task(name="infra.health.ping")
def ping() -> dict:
    return {
        "status": "ok",
        "service": "celery",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
