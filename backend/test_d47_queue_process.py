"""R9 real, separate prefork consumers on an isolated filesystem broker.

The production task bodies, routing, launcher, SQL state, publisher and execution
lease run unchanged. Only remote gateways/mail and the synthetic converter are
substituted; the opt-in historical companion uses the actual book converter.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from test_d45_worker_process import transport_options, WorkerFixture


def fixture_worker(root, role, mode):
    root = Path(root)
    with patch("dotenv.load_dotenv", return_value=False):
        from celery import signals
        from app import job_runner
        from app.domain.job_write_fence import current_job_write_fence
        from app.infra import execution_lease
        from app.infra.celery_app import celery_app
        from app.infra.worker import build_worker_argv
        from app.models import ConversionResult
        from app.tasks import reconcile, balance_check, health

    execution_lease.tempfile.gettempdir = lambda: str(root)
    job_runner.OUTPUT_DIR = root / "outputs"
    job_runner.notify_job_completed = lambda *args, **kwargs: None
    job_runner.report_error = lambda *args, **kwargs: None
    real_convert = job_runner.converter.convert_file_to_horizontal

    def convert(source, destination, output_mode, **options):
        fence = current_job_write_fence()
        if fence.job_id == "blocker":
            (root / "book-entered.json").write_text(json.dumps({"pid": os.getpid()}))
            deadline = time.monotonic() + 90
            while not (root / "release-book").exists():
                if time.monotonic() >= deadline:
                    raise AssertionError("Queue fixture did not release blocked book")
                time.sleep(.03)
        if mode == "real":
            return real_convert(source, destination, output_mode, **options)
        shutil.copyfile(source, destination)
        return ConversionResult(message="Controlled queue conversion complete", validation_passed=True)

    job_runner.converter.convert_file_to_horizontal = convert

    def verified_trade(order_no):
        trades = json.loads((root / "trades.json").read_text())
        return trades.get(order_no)

    reconcile.query_verified_trade = verified_trade
    reconcile.close_verified_trade = lambda _order_no: None
    reconcile._queue_paid_email = lambda *args, **kwargs: None

    def balance():
        if (root / "balance-fails").exists():
            raise RuntimeError("Controlled housekeeping failure")
        return 100

    balance_check._fetch_deepseek_balance = balance

    def ready(**_kwargs):
        (root / (role + ".ready")).write_text("ready")

    def completed(task_id=None, task=None, retval=None, state=None, **_kwargs):
        info = {"task": task.name, "state": state, "result": retval,
                "routing_key": task.request.delivery_info.get("routing_key"),
                "pid": os.getpid()}
        temporary = root / "done" / (task_id + ".tmp")
        temporary.write_text(json.dumps(info, default=str))
        temporary.replace(root / "done" / (task_id + ".json"))

    signals.worker_ready.connect(ready, weak=False)
    signals.task_postrun.connect(completed, weak=False)
    celery_app.conf.update(broker_url="filesystem://", result_backend=None,
                          broker_transport_options=transport_options(root),
                          task_ignore_result=True,
                          task_send_sent_event=False, worker_send_task_events=False)
    argv = build_worker_argv(role, loglevel="WARNING") if role in {"book", "housekeeping"} else [
        "worker", "--pool=prefork", "--concurrency=1", "--loglevel=WARNING"]
    if role == "mixed":
        argv += ["--queues=celery,housekeeping"]
    elif role == "house-autoscale":
        argv += ["--queues=housekeeping", "--autoscale=3,1"]
    elif role in {"house-solo", "book-solo"}:
        argv.remove("--pool=prefork")
        argv += ["--pool=solo", "--queues=" + ("housekeeping" if role == "house-solo" else "celery")]
    argv += ["--without-gossip", "--without-mingle", "--without-heartbeat"]
    celery_app.worker_main(argv)


class SplitWorkerFixture(WorkerFixture):
    def __init__(self, root, source):
        super().__init__(root, source)
        from app.infra.celery_app import build_celery_app
        self.producer.close()
        self.producer = build_celery_app()
        self.producer.conf.update(broker_url="filesystem://", result_backend=None,
                                  broker_transport_options=transport_options(self.root), task_ignore_result=True)
        self.roles = {}
        (self.root / "done").mkdir()
        (self.root / "trades.json").write_text("{}")

    def add_book(self, key, *, pending_payment=False, **kwargs):
        from app.models import Job, JobStatus, OutputMode
        job = Job(id=key, trace_id="r9-" + key, source_filename=self.source.name,
                  input_path=str(self.source), output_mode=OutputMode.simplified,
                  status=JobStatus.pending_payment if pending_payment else JobStatus.pending,
                  access_token="queue-owner", expected_amount="0.99",
                  created_at=datetime.now(timezone.utc) - timedelta(hours=1),
                  translation_stats={"attempt_id": "queue-attempt"}, **kwargs)
        self.store.add(job)
        return job

    def start_role(self, role, mode="controlled", *, wait_ready=True):
        environment = dict(os.environ, DATABASE_URL="sqlite:///" + str(self.root / "jobs.db"),
                           EPUB_PERSISTENT_STORE="1", CELERY_BROKER_URL="", REDIS_URL="",
                           CELERY_RESULT_BACKEND="", NOTIFY_EMAIL_ENABLED="0", SENTRY_DSN="",
                           OWNER_PAYMENT_EMAIL_ENABLED="0", OPENAI_API_KEY="dummy",
                           DEEPSEEK_API_KEY="dummy", DASHSCOPE_API_KEY="dummy", GEMINI_API_KEY="dummy",
                           CELERY_WORKER_CONCURRENCY="1",
                           RECONCILE_STALE_MINUTES="30", RECONCILE_TIMEOUT_HOURS="2",
                           PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
        log = (self.root / (role + ".log")).open("wb")
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                    "fixture-worker", str(self.root), role, mode],
                                   cwd=self.root, env=environment, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        self.roles[role] = (process, log)
        if wait_ready:
            self.wait_for(lambda: (self.root / (role + ".ready")).is_file(), timeout=20)
        return process

    def publish(self, job_id, attempt_id):
        self.producer.send_task("jobs.run_conversion", args=[job_id, attempt_id], retry=False, ignore_result=True)

    def short(self, task_name, *, expires=10):
        identity = uuid.uuid4().hex
        self.producer.send_task(task_name, task_id=identity, expires=expires, retry=False, ignore_result=True)
        return identity

    def result(self, identity, timeout=8):
        target = self.root / "done" / (identity + ".json")
        self.wait_for(target.is_file, timeout=timeout)
        return json.loads(target.read_text())

    def pay(self, job_id):
        (self.root / "trades.json").write_text(json.dumps({job_id: {
            "out_trade_no": job_id, "trade_status": "TRADE_SUCCESS", "total_amount": "0.99"}}))

    def release(self):
        (self.root / "release-book").write_text("release")

    def stop_role(self, role):
        process, log = self.roles.pop(role)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        log.close()

    def close(self):
        for role in list(self.roles):
            self.stop_role(role)
        super().close()


class SeparateConsumerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="r9-queues-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        from test_epub_fixture import minimal_epub_bytes
        source = root / "source.epub"
        source.write_bytes(minimal_epub_bytes())
        with patch("dotenv.load_dotenv", return_value=False):
            self.fixture = SplitWorkerFixture(root, source)
        self.addCleanup(self.fixture.close)
        self.root = root

    def test_busy_book_does_not_delay_reconciliation_balance_or_health(self):
        from app.models import JobStatus
        f = self.fixture
        f.add_book("blocker")
        customer = f.add_book("customer", pending_payment=True)
        f.pay(customer.id)
        f.start_role("book")
        f.start_role("housekeeping")
        self.assertEqual(f.dispatch()["sent"], 1)
        f.wait_for(lambda: (self.root / "book-entered.json").is_file())
        self.assertEqual(f.store.get("blocker").status, JobStatus.running)
        # A failed short task must not terminate either consumer or the book.
        (self.root / "balance-fails").write_text("fail")
        failed = f.result(f.short("infra.check_balance"))
        self.assertEqual(failed["state"], "FAILURE")
        self.assertEqual(failed["routing_key"], "housekeeping")
        (self.root / "balance-fails").unlink()
        started = time.monotonic()
        reconciled = f.result(f.short("jobs.reconcile_payments"))
        self.assertEqual(reconciled["state"], "SUCCESS")
        self.assertEqual(reconciled["result"]["paid"], 1)
        self.assertEqual(reconciled["routing_key"], "housekeeping")
        self.assertLess(time.monotonic() - started, 10)
        for name in ("infra.check_balance", "infra.health.ping"):
            result = f.result(f.short(name))
            self.assertEqual(result["state"], "SUCCESS")
            self.assertEqual(result["routing_key"], "housekeeping")
        self.assertFalse((self.root / "release-book").exists())
        self.assertEqual(f.store.get("blocker").status, JobStatus.running)
        self.assertEqual(f.store.get(customer.id).status, JobStatus.pending)
        self.assertEqual(f.store.list_dispatches(customer.id)[0]["status"], "sent")
        self.assertEqual(f.store.get(customer.id).expected_amount, "0.99")
        f.release()
        f.wait_for(lambda: f.store.get(customer.id).status == JobStatus.success)
        self.assertEqual(f.store.get("blocker").status, JobStatus.success)
        self.assertTrue(Path(f.store.get(customer.id).output_path).is_file())

    def test_book_runs_without_housekeeping_consumer_but_never_steals_its_task(self):
        from app.models import JobStatus
        f = self.fixture
        f.add_book("book")
        f.start_role("book")
        ping_id = f.short("infra.health.ping", expires=30)
        self.assertEqual(f.dispatch()["sent"], 1)
        f.wait_for(lambda: f.store.get("book").status == JobStatus.success)
        self.assertFalse((self.root / "done" / (ping_id + ".json")).exists())
        f.start_role("housekeeping")
        self.assertEqual(f.result(ping_id)["routing_key"], "housekeeping")

    def test_real_remote_control_cannot_change_roles_but_inspection_still_works(self):
        f = self.fixture
        f.start_role('book')
        f.start_role('housekeeping')
        self.assertTrue(f.producer.conf.worker_enable_remote_control)
        inspector = f.producer.control.inspect(timeout=5, limit=2)
        initial = inspector.active_queues()
        self.assertEqual(len(initial or {}), 2)
        expected = {node: [entry['name'] for entry in queues] for node, queues in initial.items()}
        self.assertEqual(sorted(expected.values()), [['celery'], ['housekeeping']])
        for command, arguments in (
            ('add_consumer', {'queue': 'celery'}),
            ('add_consumer', {'queue': 'housekeeping'}),
            ('cancel_consumer', {'queue': 'celery'}),
            ('cancel_consumer', {'queue': 'housekeeping'}),
            ('pool_grow', {'n': 2}), ('pool_shrink', {'n': 1}),
            ('autoscale', {'max': 3, 'min': 1}),
        ):
            with self.subTest(command=command, arguments=arguments):
                replies = f.producer.control.broadcast(command, arguments=arguments, reply=True, timeout=5, limit=2)
                responses = {node: value for reply in replies for node, value in reply.items()}
                self.assertEqual(set(responses), set(expected))
                for response in responses.values():
                    self.assertIn('error', response)
                    self.assertIn(command, response['error'])
        final = inspector.active_queues()
        self.assertEqual({node: [entry['name'] for entry in queues] for node, queues in final.items()}, expected)
        stats = inspector.stats()
        self.assertEqual(set(stats), set(expected))
        for value in stats.values():
            self.assertEqual(value['pool']['max-concurrency'], 1)
            self.assertEqual(len(value['pool']['processes']), 1)
        self.assertEqual(inspector.ping(), {node: {'ok': 'pong'} for node in expected})

    def test_bare_or_mixed_worker_refuses_to_consume_any_queued_book(self):
        from app.models import JobStatus
        f = self.fixture
        f.add_book("never-started")
        f.dispatch()
        for role in ("bare", "mixed", "house-autoscale", "house-solo", "book-solo"):
            with self.subTest(role=role):
                process = f.start_role(role, wait_ready=False)
                process.wait(timeout=15)
                self.assertNotEqual(process.returncode, 0)
                self.assertFalse((self.root / (role + ".ready")).exists())
                self.assertEqual(f.store.get("never-started").status, JobStatus.pending)
                self.assertEqual(f.store.get_execution("never-started", "queue-attempt"), None)
                diagnostic = (self.root / (role + ".log")).read_text()
                expected = ('autoscale' if role == 'house-autoscale' else
                            'prefork' if role in {'house-solo', 'book-solo'} else 'exactly one queue')
                self.assertIn(expected, diagnostic)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "fixture-worker":
        fixture_worker(*sys.argv[2:])
    else:
        unittest.main(verbosity=2)
