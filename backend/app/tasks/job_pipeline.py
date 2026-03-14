"""
整本转换 Celery 任务：接收 job_id，在 Worker 中执行 run_job。

使用前需配置 CELERY_BROKER_URL 或 REDIS_URL，且任务数据需在持久化 store 中（DATABASE_URL），
否则 Worker 无法通过 job_id 加载任务。
"""

from app.infra.celery_app import celery_app
from app.job_runner import run_job


@celery_app.task(name="jobs.run_conversion")
def run_conversion(job_id: str) -> None:
    """在 Celery Worker 中执行整本转换。"""
    run_job(job_id)
