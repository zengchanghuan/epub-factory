"""Explicit single-role worker entry point. Import never starts a consumer."""
from __future__ import annotations

import argparse
import os


def build_worker_argv(role: str, *, config=None, loglevel: str = "INFO", pid: int | None = None) -> list[str]:
    if role not in {"book", "housekeeping"}:
        raise ValueError("Worker role must be book or housekeeping")
    if config is None:
        from .celery_app import celery_app
        config = celery_app.conf
    if loglevel.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("Unsupported worker log level")
    if role == "book":
        queue = "celery"
        concurrency = int(config.worker_concurrency)
        soft, hard = int(config.task_soft_time_limit), int(config.task_time_limit)
    else:
        queue = "housekeeping"
        concurrency = 1
        soft, hard = int(config.epub_housekeeping_soft_time_limit), int(config.epub_housekeeping_time_limit)
    if concurrency < 1 or soft <= 0 or hard <= soft:
        raise ValueError("Worker requires positive concurrency and soft time limit below hard limit")
    return [
        "worker", f"--queues={queue}", "--pool=prefork", "--prefetch-multiplier=1",
        f"--concurrency={concurrency}", f"--soft-time-limit={soft}", f"--time-limit={hard}",
        f"--hostname=fixepub-{role}-{os.getpid() if pid is None else pid}@%h", f"--loglevel={loglevel.upper()}",
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run one isolated FixEpub Celery worker role.")
    parser.add_argument("role", choices=("book", "housekeeping"))
    parser.add_argument("--loglevel", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
    args = parser.parse_args(argv)
    from .celery_app import celery_app
    worker_argv = build_worker_argv(args.role, config=celery_app.conf, loglevel=args.loglevel)
    celery_app.conf.epub_worker_role = args.role
    result = celery_app.worker_main(worker_argv)
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
