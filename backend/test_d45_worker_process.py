"""Actual Celery prefork loss/limits using an isolated filesystem broker.

No Redis/Postgres/provider is contacted. The runner, SQL store, OS processes,
Celery time-limit signals and file execution lease are real. The book converter
is a controlled boundary here; the separate opt-in history test uses real books.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


def transport_options(root):
    return {"data_folder_in": str(root / "queue"), "data_folder_out": str(root / "queue"),
            "control_folder": str(root / "control"), "polling_interval": .1}


def fixture_worker(root, mode):
    root = Path(root)
    with patch("dotenv.load_dotenv", return_value=False):
        from app import job_runner
        from app.infra import execution_lease
        from app.infra.celery_app import celery_app
        from app.tasks.job_pipeline import run_conversion
        from app.tasks import job_pipeline
        from app.models import ConversionResult
    execution_lease.tempfile.gettempdir = lambda: str(root)
    job_runner.OUTPUT_DIR = root / "outputs"
    job_runner.notify_job_completed = lambda *a, **k: None
    job_runner.report_error = lambda *a, **k: None
    original_converter = job_runner.converter.convert_file_to_horizontal
    if mode == "before":
        def before_admission(*_args, **_kwargs):
            (root / "entered.json").write_text(json.dumps({"pid": os.getpid(), "mode": mode}))
            while True:
                time.sleep(.05)
        job_pipeline.run_job = before_admission

    def convert(source, destination, mode_arg, **options):
        if mode == "hard":
            signal.signal(signal.SIGUSR1, signal.SIG_IGN)
        (root / "entered.json").write_text(json.dumps({"pid": os.getpid(), "mode": mode}))
        if mode in {"kill", "soft", "hard"}:
            while True:
                time.sleep(.05)
        if mode == "real":
            return original_converter(source, destination, mode_arg, **options)
        import shutil
        shutil.copyfile(source, destination)
        return ConversionResult(message="Controlled conversion complete", validation_passed=True)

    job_runner.converter.convert_file_to_horizontal = convert
    celery_app.conf.update(
        broker_url="filesystem://", result_backend=None,
        broker_transport_options=transport_options(root),
        worker_enable_remote_control=False, task_ignore_result=True,
        task_send_sent_event=False, worker_send_task_events=False,
        task_annotations={"jobs.run_conversion": {
            "soft_time_limit": 2 if mode in {"soft", "hard"} else 60,
            "time_limit": 4 if mode in {"soft", "hard"} else 70,
        }},
    )
    celery_app.worker_main(["worker", "--queues=celery", "--pool=prefork", "--concurrency=1", "--loglevel=WARNING",
                            "--without-gossip", "--without-mingle", "--without-heartbeat"])


class WorkerFixture:
    def __init__(self, root, source):
        from sqlalchemy import create_engine
        from app.storage_db import Base, PersistentJobStore
        from celery import Celery
        self.root, self.source = Path(root), Path(source)
        for name in ("queue", "control", "outputs"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.engine = create_engine("sqlite:///" + str(self.root / "jobs.db"))
        Base.metadata.create_all(self.engine)
        self.store = PersistentJobStore(self.engine)
        self.producer = Celery("r7-producer", broker="filesystem://")
        self.producer.conf.update(broker_transport_options=transport_options(self.root), task_ignore_result=True)
        self.process = None
        self.log = None
        self.dispatch_time = None

    def add(self, key="process-book", **kwargs):
        from app.models import Job, JobStatus, OutputMode
        job = Job(id=key, trace_id=key, source_filename=self.source.name,
                  input_path=str(self.source), output_mode=OutputMode.simplified,
                  status=JobStatus.pending, access_token="offline-token",
                  translation_stats={"attempt_id": "same-attempt", "api_calls": 7,
                                     "free_retry_count": 1, "total_tokens": 123},
                  **kwargs)
        self.store.add(job)
        return job

    def start(self, mode):
        self.stop()
        (self.root / "entered.json").unlink(missing_ok=True)
        environment = dict(os.environ, DATABASE_URL="sqlite:///" + str(self.root / "jobs.db"),
                           EPUB_PERSISTENT_STORE="1", CELERY_BROKER_URL="", REDIS_URL="",
                           CELERY_RESULT_BACKEND="", NOTIFY_EMAIL_ENABLED="0", SENTRY_DSN="",
                           OWNER_PAYMENT_EMAIL_ENABLED="0", OPENAI_API_KEY="dummy",
                           DEEPSEEK_API_KEY="dummy", DASHSCOPE_API_KEY="dummy", GEMINI_API_KEY="dummy",
                           PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
        self.log = (self.root / (mode + ".log")).open("wb")
        self.process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "fixture-worker", str(self.root), mode],
                                        cwd=self.root, env=environment, stdout=self.log, stderr=subprocess.STDOUT,
                                        start_new_session=True)

    def publish(self, job_id, attempt_id):
        self.producer.send_task("jobs.run_conversion", args=[job_id, attempt_id], retry=False, ignore_result=True)

    def dispatch(self):
        from app.domain.job_dispatch_service import dispatch_pending
        return dispatch_pending(self.store, self.publish, now=self.dispatch_time)

    def wait_for(self, predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.05)
        logs = "\n".join(path.read_text(errors="replace")[-5000:] for path in self.root.glob("*.log"))
        raise AssertionError("Isolated worker condition timed out: " + str(self.root) + "\n" + logs)

    def entered(self):
        self.wait_for(lambda: (self.root / "entered.json").is_file())
        return json.loads((self.root / "entered.json").read_text())["pid"]

    def recover(self, advance=601):
        from app.domain.job_recovery_service import recover_lost_executions
        with patch("app.infra.execution_lease.tempfile.gettempdir", return_value=str(self.root)), \
                patch.dict(os.environ, {"CELERY_BROKER_URL": "", "REDIS_URL": ""}):
            self.dispatch_time = time.time() + advance
            return recover_lost_executions(self.store, now=self.dispatch_time, stale_seconds=600)

    def stop(self):
        if self.process is not None:
            # This session/group was created by this fixture; never touch an
            # existing development/production worker or shared Redis service.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=5)
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None

    def close(self):
        self.stop()
        self.producer.close()
        self.engine.dispose()


class CeleryLossTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="r7-prefork-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with patch("dotenv.load_dotenv", return_value=False):
            from app.engine.adapters.html_to_epub_builder import build
        self.source = self.root / "original.epub"
        build("<p>Process loss regression fixture.</p>", {"title": "Original", "language": "en"}, self.source)
        self.fixture = WorkerFixture(self.root, self.source)
        self.addCleanup(self.fixture.close)
        self.job = self.fixture.add()

    def roundtrip(self, mode):
        from app.models import JobStatus
        fixture = self.fixture
        fixture.start(mode)
        self.assertEqual(fixture.dispatch()["sent"], 1)
        child_pid = fixture.entered()
        original = fixture.store.get(self.job.id)
        self.assertEqual(original.status, JobStatus.running)
        if mode == "kill":
            self.assertEqual(fixture.recover()["busy"], 1, "Live executor must not be stolen")
            os.kill(child_pid, signal.SIGKILL)
        else:
            needle = "SoftTimeLimitExceeded" if mode == "soft" else "Hard time limit"
            fixture.wait_for(lambda: needle in (self.root / (mode + ".log")).read_text(errors="replace"))
        # The worker's ACK policy may drop a lost message. Recovery is durable
        # in SQL, not dependent on a surviving parent or broker requeue.
        fixture.stop()
        from app.storage_db import PersistentJobStore
        fixture.store = PersistentJobStore(fixture.engine)
        self.assertEqual(fixture.store.get(self.job.id).status, JobStatus.running)
        self.assertEqual(fixture.recover()["recovered"], 1)
        pending = fixture.store.get(self.job.id)
        self.assertEqual(pending.translation_stats, original.translation_stats)
        self.assertEqual(fixture.store.get_execution(self.job.id, "same-attempt")["recoveries"], 1)
        fixture.start("complete")
        self.assertEqual(fixture.dispatch()["sent"], 1)
        fixture.wait_for(lambda: fixture.store.get(self.job.id).status == JobStatus.success
                         and fixture.store.get_execution(self.job.id, "same-attempt")["state"] == "finished")
        done = fixture.store.get(self.job.id)
        self.assertEqual(done.translation_stats["free_retry_count"], 1)
        self.assertEqual(Path(done.output_path).read_bytes(), self.source.read_bytes())
        self.assertEqual(fixture.store.get_execution(self.job.id, "same-attempt")["state"], "finished")
        self.assertEqual(fixture.recover()["scanned"], 0)

    def test_prefork_child_sigkill_and_parent_restart(self):
        self.roundtrip("kill")

    def test_actual_celery_soft_time_limit(self):
        self.roundtrip("soft")

    def test_actual_celery_hard_time_limit(self):
        self.roundtrip("hard")

    def test_child_lost_before_admission_is_redelivered_without_execution_penalty(self):
        from app.models import JobStatus
        fixture = self.fixture
        fixture.start("before")
        self.assertEqual(fixture.dispatch()["sent"], 1)
        os.kill(fixture.entered(), signal.SIGKILL)
        fixture.stop()
        self.assertEqual(fixture.store.get(self.job.id).status, JobStatus.pending)
        self.assertIsNone(fixture.store.get_execution(self.job.id, "same-attempt"))
        self.assertEqual(fixture.recover(advance=3601)["queued_rearmed"], 1)
        fixture.start("complete")
        self.assertEqual(fixture.dispatch()["sent"], 1)
        fixture.wait_for(lambda: fixture.store.get(self.job.id).status == JobStatus.success)
        self.assertEqual(fixture.store.get_execution(self.job.id, "same-attempt")["recoveries"], 0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "fixture-worker":
        fixture_worker(sys.argv[2], sys.argv[3])
    else:
        unittest.main(verbosity=2)
