"""Bounded broker publication; durable retry belongs to the dispatch outbox."""

from app.domain.dispatch_intent import dispatch_identity


def publish_conversion(job_id: str, expected_attempt_id: str) -> None:
    from app.infra.celery_app import celery_app
    from app.tasks.job_pipeline import run_conversion

    # Use a dedicated connection rather than waiting indefinitely for a pooled
    # producer. These are per-socket engineering limits, not a whole-pass SLA.
    options = {**dict(celery_app.conf.broker_transport_options or {}),
               "socket_connect_timeout": 5, "socket_timeout": 5,
               "retry_on_timeout": False}
    with celery_app.connection_for_write(connect_timeout=5, transport_options=options) as connection:
        connection.ensure_connection(max_retries=0)
        run_conversion.apply_async(
            args=(job_id, expected_attempt_id), connection=connection, retry=False,
            task_id=dispatch_identity(job_id, expected_attempt_id), ignore_result=True,
        )
