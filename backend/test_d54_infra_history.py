"""Opt-in actual Redis / dual-prefork / three historical EPUB release gate.

Requires EPUB_INFRA_REDIS_SERVER (an existing redis-server executable), an
explicit unused redis://127.0.0.1:<high-port>/0 EPUB_INFRA_REDIS_URL, and the
three EPUB_HISTORY_* directories. This test owns the only Redis process it
stops/restarts. It never installs Redis or controls an existing service.

Run WITHOUT the older all-network-denying sitecustomize. This file installs an
audit guard in the parent and every Python worker that permits only the exact
Redis loopback address. Model, Alipay and SMTP boundaries remain forbidden;
only reconciliation's verified-response boundary is controlled. Java runs the
local EPUBCheck JAR; this is not OS-level packet capture or paid translation QA.
"""
from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlsplit
import uuid
from unittest.mock import patch

import test_d38_navigation_history as navigation

REQUIRED_ENV = ("EPUB_INFRA_REDIS_SERVER", "EPUB_INFRA_REDIS_URL", "EPUB_HISTORY_UPLOAD_DIR",
                "EPUB_HISTORY_OUTPUT_DIR", "EPUB_HISTORY_BASELINE_DIR")
BASELINE_SHA256 = {
    "double-helix": "af8f94eaede12901df64b798a19f53428dcde6da29401f805055da93cc8b458b",
    "die-with-zero": "c37126d5f5460d0470865b7d9819799e87b17d27a269df747eef3448afaf1e87",
    "responsibility-and-judgement": "74a71af452cadd670e6c4e0f30da1ac205efc88c2cb266deaa5c78bb9abcb60a",
}


def redis_address(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "redis" or parsed.hostname != "127.0.0.1"
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path != "/0" or not parsed.port or parsed.port < 1024 or parsed.port == 6379):
        raise ValueError("Use an explicit unused redis://127.0.0.1:<non-default-high-port>/0")
    return parsed.hostname, parsed.port


def install_network_guard(url, path):
    """Audit before any app import; allow one loopback endpoint, not all localhost."""
    host, port = redis_address(url)
    path = Path(path)

    def audit(event, args):
        if event not in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
            return
        if event == "socket.getaddrinfo":
            address = args[:2]
        else:
            address = args[1] if event == "socket.connect" else args[-1]
        allowed = (isinstance(address, (tuple, list)) and len(address) >= 2
                   and address[0] == host and str(address[1]) == str(port))
        # Record event class only, never arbitrary addresses or credentials.
        with path.open("a") as stream:
            stream.write(json.dumps({"event": event, "allowed": allowed, "pid": os.getpid()}) + "\n")
        if not allowed:
            raise RuntimeError("D54 blocks every network destination except its own Redis endpoint")

    sys.addaudithook(audit)


def isolated_environment(root, url):
    environment = dict(os.environ)
    environment.update({
        "DATABASE_URL": "sqlite:///" + str(root / "jobs.db"), "EPUB_PERSISTENT_STORE": "1",
        "EPUB_TRANSLATION_CHECKPOINT_DB": str(root / "checkpoints.db"),
        "REPAIR_UPLOAD_DIR": str(root / "repair"), "UPLOAD_DIR": str(root / "uploads"),
        "OUTPUT_DIR": str(root / "outputs"), "CELERY_BROKER_URL": url, "REDIS_URL": url,
        "CELERY_RESULT_BACKEND": url, "CELERY_WORKER_CONCURRENCY": "1",
        "OPENAI_API_KEY": "", "DEEPSEEK_API_KEY": "", "DASHSCOPE_API_KEY": "",
        "GEMINI_API_KEY": "", "OPENAI_BASE_URL": "https://model.invalid/v1",
        "ALIPAY_APP_ID": "", "ALIPAY_SELLER_ID": "", "SENTRY_DSN": "", "SMTP_HOST": "",
        "NOTIFY_EMAIL_ENABLED": "0", "OWNER_PAYMENT_EMAIL_ENABLED": "0",
        "JOB_DISPATCH_ENABLED": "0", "EPUB_LLM_RATE_LIMITER_ENABLED": "0",
        "RECONCILE_STALE_MINUTES": "30", "RECONCILE_TIMEOUT_HOURS": "2",
        "DOWNLOAD_SIGN_SECRET": "d54-only-local-signature", "SKIP_PAYMENT_CHECK": "0",
        "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        # Match the existing R9 service templates, preserving real prefork.
        "NOSETPS": "1", "PYTHONFAULTHANDLER": "1",
        "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "", "NO_PROXY": "127.0.0.1",
    })
    return environment


def forbid_external_boundaries(stack):
    blocked = []
    for target in ("requests.sessions.Session.request", "smtplib.SMTP", "smtplib.SMTP_SSL",
                   "openai.OpenAI", "openai.AsyncOpenAI",
                   "app.engine.cleaners.semantics_translator.SemanticsTranslator.__init__"):
        blocked.append(stack.enter_context(patch(target, side_effect=AssertionError("D54 forbids paid/external calls"))))
    return blocked


def isolate_legacy_rate_limiter(stack, root):
    """Keep its real schema/code but route only its fixed workspace DB path."""
    workspace_database = Path(__file__).resolve().parent / "rate_limit.db"
    connect = sqlite3.connect

    def isolated(database, *args, **kwargs):
        if isinstance(database, (str, os.PathLike)) and Path(database).resolve() == workspace_database:
            database = str(root / "rate_limit.db")
        return connect(database, *args, **kwargs)

    stack.enter_context(patch("sqlite3.connect", side_effect=isolated))


def worker_main(root, role):
    root = Path(root)
    url = os.environ["EPUB_INFRA_REDIS_URL"]
    install_network_guard(url, root / "network.jsonl")
    with ExitStack() as stack:
        stack.enter_context(patch("dotenv.load_dotenv", return_value=False))
        isolate_legacy_rate_limiter(stack, root)
        from celery import signals
        from app import job_runner
        from app.domain.job_write_fence import current_job_write_fence
        from app.infra.celery_app import celery_app
        from app.infra.worker import build_worker_argv
        from app.models import ConversionResult
        from app.tasks import reconcile, balance_check

        forbid_external_boundaries(stack)
        job_runner.OUTPUT_DIR = root / "outputs"
        job_runner.notify_job_completed = lambda *a, **kw: None
        job_runner.report_error = lambda *a, **kw: None
        convert = job_runner.converter.convert_file_to_horizontal

        def controlled_gate(source, destination, output_mode, **options):
            identity = current_job_write_fence().job_id
            with (root / "executions.jsonl").open("a") as stream:
                stream.write(json.dumps({"job_id": identity, "pid": os.getpid()}) + "\n")
            if identity == "blocker":
                (root / "book-entered").write_text("entered")
                deadline = time.monotonic() + 120
                while not (root / "release-book").is_file():
                    if time.monotonic() > deadline:
                        raise AssertionError("D54 blocker was not released")
                    time.sleep(.03)
                shutil.copyfile(source, destination)
                return ConversionResult(message="Synthetic admission blocker only", validation_passed=True)
            if identity == "worker-loss":
                (root / "loss-entered").write_text("entered before actual conversion")
                deadline = time.monotonic() + 120
                while not (root / "release-loss").is_file():
                    if time.monotonic() > deadline:
                        raise AssertionError("D54 loss fixture was not released")
                    time.sleep(.03)
            return convert(source, destination, output_mode, **options)

        job_runner.converter.convert_file_to_horizontal = controlled_gate
        reconcile.query_verified_trade = lambda order: json.loads((root / "trades.json").read_text()).get(order)
        reconcile.close_verified_trade = lambda _order: None
        reconcile._queue_paid_email = lambda *a, **kw: None
        balance_check._fetch_deepseek_balance = lambda: 100

        def ready(**_kw):
            (root / (role + ".ready")).write_text(json.dumps({
                "pid": os.getpid(), "sqlite_version": sqlite3.sqlite_version, "role": role}))

        def completed(task_id=None, task=None, retval=None, state=None, **_kw):
            payload = {"task": task.name, "state": state, "result": retval,
                       "routing_key": task.request.delivery_info.get("routing_key"), "pid": os.getpid()}
            temporary = root / "done" / (task_id + ".tmp")
            temporary.write_text(json.dumps(payload, default=str))
            temporary.replace(root / "done" / (task_id + ".json"))

        signals.worker_ready.connect(ready, weak=False)
        signals.task_postrun.connect(completed, weak=False)
        celery_app.conf.update(result_backend=None, task_ignore_result=True,
                              task_send_sent_event=False, worker_send_task_events=False)
        celery_app.worker_main(build_worker_argv(role, loglevel="WARNING") + [
            "--without-gossip", "--without-mingle", "--without-heartbeat"])


class OwnedRedis:
    """Lifecycle authority comes only from our own Popen handle, never Redis INFO PID."""
    def __init__(self, root, executable, url):
        self.root, self.executable, self.url = root, executable, url
        self.host, self.port = redis_address(url)
        self.process = self.log = None
        self.config = root / "redis.conf"
        self.config.write_text("\n".join([
            "bind 127.0.0.1", f"port {self.port}", "protected-mode yes", "daemonize no",
            f'dir "{root}"', "save \"\"", "appendonly no", "loglevel warning", "databases 1", "",
        ]))

    def client(self):
        import redis
        return redis.Redis.from_url(self.url, socket_connect_timeout=.3, socket_timeout=.3)

    def start(self):
        if self.process is not None:
            raise AssertionError("Our Redis is already running")
        # Never stop, flush or attach to somebody else's loopback service.
        with socket.socket() as check:
            if check.connect_ex((self.host, self.port)) == 0:
                raise AssertionError("Redis test address is occupied; refusing to touch it")
        self.log = (self.root / "redis.log").open("ab")
        self.process = subprocess.Popen([str(self.executable), str(self.config)],
                                        stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 10
        with self.client() as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise AssertionError("Test Redis exited: " + (self.root / "redis.log").read_text())
                try:
                    if client.ping():
                        info = client.info("server")
                        if info["process_id"] != self.process.pid:
                            raise AssertionError("Redis endpoint does not belong to our Popen process")
                        self.version = info["redis_version"]
                        return info["run_id"]
                except ConnectionError:
                    pass
                except Exception as exc:
                    import redis
                    if not isinstance(exc, redis.exceptions.ConnectionError):
                        raise
                time.sleep(.05)
        raise AssertionError("Test Redis did not become ready")

    def stop(self):
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None
        self.log.close()
        self.log = None


@unittest.skipUnless(all(os.environ.get(name) for name in REQUIRED_ENV),
    "Explicit self-owned Redis executable/address and all three historical directories required")
class RealInfrastructureHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = os.environ["EPUB_INFRA_REDIS_URL"]
        redis_address(cls.url)
        executable = Path(os.environ["EPUB_INFRA_REDIS_SERVER"]).resolve(strict=True)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise AssertionError("redis-server executable is unavailable")
        artifact = os.environ.get("EPUB_INFRA_ARTIFACT_DIR")
        if artifact:
            cls.root = Path(artifact).resolve()
            cls.root.mkdir(parents=True, exist_ok=False)
        else:
            temporary = tempfile.TemporaryDirectory(prefix="epub-d54-infra-")
            cls.addClassCleanup(temporary.cleanup)
            cls.root = Path(temporary.name)
        for name in ("outputs", "uploads", "done"):
            (cls.root / name).mkdir()
        cls.uploads = Path(os.environ["EPUB_HISTORY_UPLOAD_DIR"]).resolve()
        cls.deliveries = Path(os.environ["EPUB_HISTORY_OUTPUT_DIR"]).resolve()
        cls.baseline = Path(os.environ["EPUB_HISTORY_BASELINE_DIR"]).resolve()
        cls.hashes = {}
        for book in navigation.BOOKS:
            for kind, directory in (("input", cls.uploads), ("output", cls.deliveries)):
                path = directory / book[kind]
                actual = navigation.sha256(path)
                if actual != book[kind + "_sha256"]:
                    raise AssertionError("Historical SHA mismatch: " + str(path))
                cls.hashes[path] = actual
            path = cls.baseline / book["input_sha256"][:12] / "converted.epub"
            cls.hashes[path] = navigation.sha256(path)
            if cls.hashes[path] != BASELINE_SHA256[book["key"]]:
                raise AssertionError("Historical baseline SHA mismatch: " + str(path))
        install_network_guard(cls.url, cls.root / "network.jsonl")
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        cls.stack.enter_context(patch.dict(os.environ, isolated_environment(cls.root, cls.url)))
        cls.stack.enter_context(patch("dotenv.load_dotenv", return_value=False))
        cls.workspace_databases = {}
        for suffix in ("", "-wal", "-shm"):
            path = Path(__file__).resolve().parent / ("rate_limit.db" + suffix)
            cls.workspace_databases[path] = navigation.sha256(path) if path.exists() else None
        isolate_legacy_rate_limiter(cls.stack, cls.root)
        from app import main
        from app.storage_db import _make_engine, PersistentJobStore
        from opencc import OpenCC
        cls.blocked = forbid_external_boundaries(cls.stack)
        cls.main = main
        cls.engine = _make_engine()
        cls.addClassCleanup(cls.engine.dispose)
        cls.store = PersistentJobStore(cls.engine)
        for name, value in (("job_store", cls.store), ("OUTPUT_DIR", cls.root / "outputs"),
                            ("UPLOAD_DIR", cls.root / "uploads")):
            cls.stack.enter_context(patch.object(main, name, value))
        cls.opencc = OpenCC("t2s")
        cls.redis = OwnedRedis(cls.root, executable, cls.url)
        cls.addClassCleanup(cls.redis.stop)
        cls.roles = {}
        cls.addClassCleanup(cls.stop_workers)
        (cls.root / "trades.json").write_text("{}")

    @classmethod
    def stop_workers(cls):
        for process, log in cls.roles.values():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
            log.close()
        cls.roles.clear()

    @classmethod
    def tearDownClass(cls):
        cls.stop_workers()
        for path, expected in cls.hashes.items():
            if navigation.sha256(path) != expected:
                raise AssertionError("Read-only original/baseline changed: " + str(path))
        for blocked in cls.blocked:
            blocked.assert_not_called()
        for path, before in cls.workspace_databases.items():
            after = navigation.sha256(path) if path.exists() else None
            if after != before:
                raise AssertionError("Workspace rate limiter database changed: " + str(path))
        rows = [json.loads(row) for path in cls.root.rglob("network.jsonl") for row in path.read_text().splitlines()]
        if not rows or not all(row["allowed"] for row in rows):
            raise AssertionError("External networking attempted or real Redis was not exercised")
        print(json.dumps({"runtime": str(cls.root), "network_allowed_events": len(rows),
                          "external_events": 0, "historical_sha_unchanged": len(cls.hashes),
                          "workspace_rate_db_unchanged": True, "sqlite_version": sqlite3.sqlite_version,
                          "redis_version": getattr(cls.redis, "version", None)}))

    def wait_for(self, predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.05)
        logs = "\n".join(p.read_text(errors="replace")[-7000:] for p in self.root.glob("*.log"))
        self.fail("Infrastructure gate timed out: " + str(self.root) + "\n" + logs)

    def start_worker(self, role):
        (self.root / (role + ".ready")).unlink(missing_ok=True)
        log = (self.root / (role + ".log")).open("wb")
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "fixture-worker",
                                    str(self.root), role], cwd=self.root,
                                   env=isolated_environment(self.root, self.url), stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        self.roles[role] = (process, log)
        self.wait_for(lambda: (self.root / (role + ".ready")).is_file())
        ready = json.loads((self.root / (role + ".ready")).read_text())
        self.assertEqual(ready["sqlite_version"], sqlite3.sqlite_version)
        self.assertEqual(ready["pid"], process.pid)

    def add_job(self, key, source, *, waiting=True):
        from app.models import Job, JobStatus, OutputMode
        job = Job(id=key, trace_id="d54-" + key, source_filename=source.name, input_path=str(source),
                  output_mode=OutputMode.simplified, expected_amount="0.99",
                  status=JobStatus.pending_payment if waiting else JobStatus.pending,
                  created_at=datetime.now(timezone.utc) - timedelta(hours=1),
                  access_token=uuid.uuid4().hex, token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                  lexicon_domains=[], enable_proper_noun=False)
        self.store.add(job)
        return job

    def task_result(self, task_name):
        from app.infra.celery_app import celery_app
        identity = uuid.uuid4().hex
        celery_app.send_task(task_name, task_id=identity, expires=15, retry=False, ignore_result=True)
        target = self.root / "done" / (identity + ".json")
        self.wait_for(target.is_file, timeout=15)
        result = json.loads(target.read_text())
        self.assertEqual(result["state"], "SUCCESS", result)
        self.assertEqual(result["routing_key"], "housekeeping")
        return result

    def assert_artifact(self, book, job):
        from fastapi.testclient import TestClient
        from app.engine.compiler import EPUBCHECK_JAR
        from app.engine.epub_validation import validate_epub
        from app.models import JobStatus
        from app.storage_db import PersistentJobStore
        self.assertEqual(job.status, JobStatus.success, job.message)
        validation = validate_epub(job.output_path, EPUBCHECK_JAR)
        self.assertTrue(validation.passed, validation.message)
        before = navigation.BookSnapshot(self.uploads / book["input"], self.opencc)
        after = navigation.BookSnapshot(job.output_path, self.opencc)
        self.assertEqual(before.images, after.images)
        for name, source in before.docs.items():
            self.assertIn(name, after.docs)
            target = after.docs[name]
            if name in before.nav_docs:
                cursor = iter(target["text"])
                self.assertTrue(all(any(candidate == char for candidate in cursor) for char in source["text"]))
            else:
                self.assertEqual(source["text"], target["text"], name)
            self.assertLessEqual(source["ids"], target["ids"])
            old = Counter((r["label"], r["target"]) for r in source["links"] if before.valid(r["target"]))
            new = Counter((r["label"], r["target"]) for r in target["links"] if after.valid(r["target"]) and not r["disabled"])
            self.assertFalse(old - new, name)
        original_toc = [(r["label"], r["target"], r["depth"]) for r in before.toc if before.valid(r["target"])]
        actual_toc = iter((r["label"], r["target"], r["depth"]) for r in after.toc if after.valid(r["target"]))
        self.assertTrue(original_toc)
        self.assertTrue(all(any(row == expected for row in actual_toc) for expected in original_toc))
        digest = navigation.sha256(Path(job.output_path))
        with ExitStack() as stack:
            stack.enter_context(patch.object(self.main, "job_store", PersistentJobStore(self.engine)))
            client = TestClient(self.main.app)
            stack.callback(client.close)
            headers = {"X-Job-Token": job.access_token}
            detail = client.get(f"/api/v2/jobs/{job.id}", headers=headers)
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(detail.json()["status"], "completed")
            self.assertEqual(detail.headers["cache-control"], "no-store")
            download = client.get(detail.json()["download_url"], headers=headers)
            self.assertEqual(download.status_code, 200)
            self.assertEqual(hashlib.sha256(download.content).hexdigest(), digest)
            # A signed URL is itself a valid capability. Strip its signature
            # as well as the header before asserting the anonymous denial.
            signed_only = client.get(detail.json()["download_url"])
            self.assertEqual(signed_only.status_code, 200)
            self.assertEqual(hashlib.sha256(signed_only.content).hexdigest(), digest)
            bare_path = urlsplit(detail.json()["download_url"]).path
            self.assertEqual(client.get(bare_path).status_code, 403)
            self.assertEqual(client.get(bare_path, params={"exp": int(time.time()) + 60, "sig": "incorrect"}).status_code, 403)
            with patch.object(self.main, "DOWNLOAD_SIGN_TTL_SECONDS", -1):
                expired = self.main._attach_download_sig(job.id, bare_path)
            self.assertEqual(client.get(expired).status_code, 403)
        print(json.dumps({"book": book["key"], "job_id": job.id, "output_sha256": digest,
                          "output_path": job.output_path,
                          "epubcheck_passed": True, "images": sum(after.images.values()),
                          "documents": len(after.docs), "download_sha_matches": True}))

    def test_redis_restart_outbox_dual_workers_and_three_real_deliveries(self):
        from app.domain.job_dispatch_service import dispatch_pending
        from app.infra.job_dispatch_publisher import publish_conversion
        from app.infra.celery_app import celery_app
        from app.models import JobStatus
        from app.storage_db import PersistentJobStore
        from test_epub_fixture import minimal_epub_bytes

        first_run = self.redis.start()
        with self.redis.client() as client:
            self.assertEqual(client.dbsize(), 0)
        jobs = [self.add_job(book["key"], self.uploads / book["input"]) for book in navigation.BOOKS]
        self.redis.stop()
        for job in jobs:
            released = self.store.settle_verified_payment(job.id, source="verified_query", amount=job.expected_amount)
            self.assertEqual(released["released"], [job.id])
        failed = dispatch_pending(self.store, publish_conversion)
        self.assertEqual(failed["retry"], 3)
        self.assertEqual(failed["published"], 0)
        due = 0
        for job in jobs:
            rows = self.store.list_dispatches(job.id)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "pending")
            self.assertEqual(rows[0]["attempts"], 1)
            self.assertEqual(self.store.get(job.id).status, JobStatus.pending)
            self.assertIsNone(self.store.get_execution(job.id, ""))
            due = max(due, rows[0]["next_attempt_at"])
        self.store = PersistentJobStore(self.engine)
        self.assertNotEqual(first_run, self.redis.start(), "An actual Redis process restart is required")
        synthetic = self.root / "blocker.epub"
        synthetic.write_bytes(minimal_epub_bytes())
        self.add_job("blocker", synthetic, waiting=False)
        self.start_worker("book")
        self.start_worker("housekeeping")
        self.assertEqual(dispatch_pending(self.store, publish_conversion, job_id="blocker")["sent"], 1)
        self.wait_for(lambda: (self.root / "book-entered").is_file())
        self.assertEqual(self.store.get("blocker").status, JobStatus.running)
        self.assertEqual(dispatch_pending(self.store, publish_conversion, now=due + .1)["sent"], 3)
        for job in jobs:
            self.assertEqual(self.store.get(job.id).status, JobStatus.pending)
            self.assertIsNone(self.store.get_execution(job.id, ""))
        inspector = celery_app.control.inspect(timeout=5, limit=2)
        queues = inspector.active_queues()
        self.assertEqual(sorted([[q["name"] for q in names] for names in (queues or {}).values()]),
                         [["celery"], ["housekeeping"]])
        probe = self.add_job("maintenance-probe", synthetic)
        (self.root / "trades.json").write_text(json.dumps({probe.id: {
            "out_trade_no": probe.id, "trade_status": "TRADE_SUCCESS", "total_amount": probe.expected_amount}}))
        began = time.monotonic()
        paid = self.task_result("jobs.reconcile_payments")
        self.assertEqual(paid["result"]["paid"], 1)
        self.assertLess(time.monotonic() - began, 15)
        self.assertEqual(self.store.get(probe.id).status, JobStatus.pending)
        self.assertEqual(self.store.list_dispatches(probe.id)[0]["status"], "sent")
        self.store.update_status(probe.id, JobStatus.cancelled, message="Controlled probe cleanup",
                                 expected_attempt_id="", expected_statuses={JobStatus.pending})
        self.task_result("infra.health.ping")
        self.task_result("infra.check_balance")
        self.assertEqual(self.store.get("blocker").status, JobStatus.running)
        self.assertFalse((self.root / "release-book").exists())
        (self.root / "release-book").write_text("release")
        self.wait_for(lambda: all(self.store.get(job.id).status in {JobStatus.success, JobStatus.failed} for job in jobs), timeout=180)
        for book, original in zip(navigation.BOOKS, jobs):
            with self.subTest(book=book["key"]):
                job = self.store.get(original.id)
                self.assertEqual(job.expected_amount, original.expected_amount)
                self.assertEqual(job.input_path, original.input_path)
                self.assertEqual(self.store.get_execution(job.id, "")["state"], "finished")
                self.assertEqual(self.store.get_execution(job.id, "")["recoveries"], 0)
                self.assertEqual(len(self.store.list_dispatches(job.id)), 1)
                self.assert_artifact(book, job)
        self.assertEqual(dispatch_pending(self.store, publish_conversion)["published"], 0)
        # At-least-once broker delivery is safe even after a successful result.
        duplicates = []
        for job in jobs:
            identity = uuid.uuid4().hex
            celery_app.send_task("jobs.run_conversion", args=[job.id, ""], task_id=identity,
                                 retry=False, ignore_result=True)
            duplicates.append(identity)
        self.wait_for(lambda: all((self.root / "done" / (key + ".json")).is_file() for key in duplicates))
        counts = Counter(json.loads(row)["job_id"] for row in (self.root / "executions.jsonl").read_text().splitlines())
        self.assertEqual(counts, Counter({"blocker": 1, **{job.id: 1 for job in jobs}}))
        self.assertEqual(self.store.get(probe.id).status, JobStatus.cancelled)
        self.assertIsNone(self.store.get_execution(probe.id, ""))
        # Production uses SQLite WAL. Exercise its online backup API locally,
        # not a potentially torn raw copy, then reload every delivery pointer.
        from sqlalchemy import create_engine
        backup_path = self.root / "jobs-backup.db"
        with sqlite3.connect(str(self.root / "jobs.db")) as source, sqlite3.connect(str(backup_path)) as destination:
            source.backup(destination)
            self.assertEqual(destination.execute("PRAGMA quick_check").fetchone()[0], "ok")
        backup_engine = create_engine("sqlite:///" + str(backup_path))
        try:
            backup = PersistentJobStore(backup_engine)
            for original in jobs:
                current, saved = self.store.get(original.id), backup.get(original.id)
                self.assertEqual(saved.status, JobStatus.success)
                self.assertEqual(saved.expected_amount, original.expected_amount)
                self.assertEqual(saved.translation_stats, current.translation_stats)
                self.assertEqual(saved.output_path, current.output_path)
                self.assertEqual(backup.get_execution(saved.id, "")["state"], "finished")
                self.assertEqual(backup.list_dispatches(saved.id), self.store.list_dispatches(saved.id))
        finally:
            backup_engine.dispose()
        print(json.dumps({"real_redis_restarted": True, "failed_outbox": 3, "recovered_outbox": 3,
                          "actual_historical_conversions": 3, "duplicate_conversions": 0,
                          "maintenance_completed_while_book_blocked": True,
                          "sqlite_online_backup_verified": True}))
        self.stop_workers()
        self.redis.stop()

    def test_worker_loss_uses_actual_redis_lease_and_recovers_one_real_book(self):
        from app.domain.job_dispatch_service import dispatch_pending
        from app.domain.job_recovery_service import recover_lost_executions
        from app.infra.job_dispatch_publisher import publish_conversion
        from app.infra.celery_app import celery_app
        from app.models import JobStatus
        from app.storage_db import _make_engine, PersistentJobStore

        self.stop_workers()
        self.redis.stop()
        self.root = self.root / "worker-loss-case"
        self.root.mkdir()
        for name in ("outputs", "uploads", "done"):
            (self.root / name).mkdir()
        (self.root / "trades.json").write_text("{}")
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(os.environ, isolated_environment(self.root, self.url)))
        self.engine = _make_engine()
        self.addCleanup(self.engine.dispose)
        self.store = PersistentJobStore(self.engine)
        stack.enter_context(patch.object(self.main, "OUTPUT_DIR", self.root / "outputs"))
        self.redis = OwnedRedis(self.root, Path(os.environ["EPUB_INFRA_REDIS_SERVER"]).resolve(), self.url)
        self.addCleanup(self.redis.stop)
        self.addCleanup(self.stop_workers)
        self.redis.start()
        book = navigation.BOOKS[2]
        original = self.add_job("worker-loss", self.uploads / book["input"], waiting=False)
        self.start_worker("book")
        self.assertEqual(dispatch_pending(self.store, publish_conversion, job_id=original.id)["sent"], 1)
        self.wait_for(lambda: (self.root / "loss-entered").exists())
        running = self.store.get(original.id)
        record = self.store.get_execution(original.id, "")
        self.assertEqual(running.status, JobStatus.running)
        self.assertFalse(running.output_path)
        at = time.time() + 601  # Select stale heartbeat without a ten-minute sleep.
        self.assertEqual(recover_lost_executions(self.store, now=at)["busy"], 1)
        process, log = self.roles.pop("book")
        os.killpg(process.pid, signal.SIGKILL)  # Exactly the group created by start_worker.
        process.wait(timeout=10)
        log.close()
        self.assertEqual(self.store.get(original.id).status, JobStatus.running)
        self.assertEqual(recover_lost_executions(self.store, now=at)["busy"], 1,
                         "A killed worker's still-valid Redis lease must not be stolen")
        key = "epub:execution:" + hashlib.sha256(f"{original.id}:conversion".encode()).hexdigest()
        with self.redis.client() as client:
            self.assertEqual(client.get(key).decode(), record["owner"])
            # Only after SIGKILL and owner verification, accelerate this test
            # lease's actual Redis expiry. No DEL, mocked lease or file fallback.
            self.assertTrue(client.pexpire(key, 100))
            self.wait_for(lambda: client.get(key) is None)
        self.store = PersistentJobStore(self.engine)
        recovery = recover_lost_executions(self.store, now=at)
        self.assertEqual(recovery["recovered"], 1, recovery)
        queued = self.store.get(original.id)
        self.assertEqual(queued.status, JobStatus.pending)
        self.assertEqual(queued.translation_stats, running.translation_stats)
        self.assertEqual(queued.expected_amount, original.expected_amount)
        self.assertEqual(self.store.get_execution(original.id, "")["recoveries"], 1)
        self.assertEqual(len(self.store.list_dispatches(original.id)), 1)
        (self.root / "release-loss").write_text("release new owner")
        self.start_worker("book")
        self.assertEqual(dispatch_pending(self.store, publish_conversion, job_id=original.id, now=at + 1)["sent"], 1)
        self.wait_for(lambda: self.store.get(original.id).status in {JobStatus.success, JobStatus.failed}, timeout=90)
        delivered = self.store.get(original.id)
        self.assert_artifact(book, delivered)
        record_after = self.store.get_execution(original.id, "")
        self.assertEqual(record_after["recoveries"], 1)
        self.assertEqual(record_after["state"], "finished")
        identity = uuid.uuid4().hex
        celery_app.send_task("jobs.run_conversion", args=[original.id, ""], task_id=identity, retry=False, ignore_result=True)
        self.wait_for(lambda: (self.root / "done" / (identity + ".json")).is_file())
        attempts = [json.loads(row) for row in (self.root / "executions.jsonl").read_text().splitlines()]
        self.assertEqual(len(attempts), 2, "Killed pre-conversion owner + one actual conversion, no duplicate completion")
        self.assertNotEqual(attempts[0]["pid"], attempts[1]["pid"])
        self.assertEqual(recover_lost_executions(self.store, now=at + 2)["scanned"], 0)
        print(json.dumps({"book": book["key"], "actual_worker_group_sigkill": True,
                          "stale_live_lease_busy": True, "actual_redis_expiry_accelerated_ms": 100,
                          "same_attempt_recoveries": 1, "actual_conversions_after_loss": 1}))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "fixture-worker":
        worker_main(*sys.argv[2:])
    else:
        missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
        if missing:
            print("D54 direct execution requires actual infrastructure/history: " + ", ".join(missing), file=sys.stderr)
            raise SystemExit(2)
        unittest.main(verbosity=2)
