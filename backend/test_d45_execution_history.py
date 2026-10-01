"""Opt-in R7: kill a real Celery child, reload, recover and deliver three books.

Inherits R6's original/artifact SHA, navigation, images, payment and delivery
checks. Filesystem broker / SQLite / local lease are real but not production
Redis/Postgres. No payment or LLM call. No online translation-quality claim.
"""
import hashlib
import os
from pathlib import Path
import signal
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import test_d44_payment_lifecycle_history as previous
from test_d45_worker_process import WorkerFixture

navigation = previous.navigation


class ExecutionHistoryTests(previous.PaymentLifecycleHistoryTests):
    def test_lost_prefork_executor_recovers_each_original_and_refresh_download(self):
        from fastapi.testclient import TestClient
        from app import main
        from app.models import JobStatus
        from app.storage_db import PersistentJobStore
        from app.engine.compiler import EPUBCHECK_JAR
        from app.engine.epub_validation import validate_epub
        from app.domain.translation_checkpoints import TranslationCheckpoints, book_resume_key

        for book in navigation.BOOKS:
            with self.subTest(book=book["key"]), ExitStack() as stack:
                root = self.root / ("r7-loss-" + book["key"])
                fixture = WorkerFixture(root, self.uploads / book["input"])
                stack.callback(fixture.close)
                job = fixture.add(book["key"], lexicon_domains=[], enable_proper_noun=False)
                # Preserve existing checkpoint bytes and scope, including fresh
                # attempt identity. Actual partial translation resume is covered
                # separately with controlled model replies, never a paid call.
                manifest = {"chapters": [{"file_path": "historical-checkpoint"}]}
                scope = book_resume_key(job, manifest)
                checkpoint_path = str(root / "checkpoints.db")
                checkpoints = TranslationCheckpoints(job.id, scope, checkpoint_path)
                checkpoints.put("chunk:completed", {"source_sha256": book["input_sha256"], "validated": True})
                before = navigation.sha256(Path(checkpoint_path))
                fixture.start("kill")
                self.assertEqual(fixture.dispatch()["sent"], 1)
                pid = fixture.entered()
                self.assertEqual(fixture.recover()["busy"], 1)
                os.kill(pid, signal.SIGKILL)
                fixture.stop()
                fixture.store = PersistentJobStore(fixture.engine)
                self.assertEqual(fixture.store.get(job.id).status, JobStatus.running)
                self.assertEqual(fixture.recover()["recovered"], 1)
                pending = fixture.store.get(job.id)
                self.assertEqual(pending.translation_stats["attempt_id"], "same-attempt")
                self.assertEqual(pending.translation_stats["api_calls"], 7)
                self.assertEqual(book_resume_key(pending, manifest), scope)
                self.assertEqual(navigation.sha256(Path(checkpoint_path)), before)
                self.assertTrue(TranslationCheckpoints(job.id, scope, checkpoint_path).get("chunk:completed")["validated"])
                fixture.start("real")
                self.assertEqual(fixture.dispatch()["sent"], 1)
                fixture.wait_for(lambda: fixture.store.get(job.id).status in {JobStatus.success, JobStatus.failed}, timeout=50)
                delivered = fixture.store.get(job.id)
                self.assertEqual(delivered.status, JobStatus.success, delivered.message)
                self.assertEqual(fixture.store.get_execution(job.id, "same-attempt")["recoveries"], 1)
                self.assertTrue(validate_epub(delivered.output_path, EPUBCHECK_JAR).passed)
                snapshot = navigation.BookSnapshot(delivered.output_path, self.opencc)
                reference = self.runs[book["key"]][1]
                self.assertEqual(snapshot.images, reference.images)
                self.assertEqual(set(snapshot.docs), set(reference.docs))
                for name in reference.docs:
                    self.assertTrue(snapshot.docs[name]["text"] == reference.docs[name]["text"],
                                    "Historical text changed: " + book["key"] + " " + name)
                    self.assertLessEqual(reference.docs[name]["ids"], snapshot.docs[name]["ids"])
                self.assertEqual([(row["label"], row["target"], row["depth"]) for row in snapshot.toc],
                                 [(row["label"], row["target"], row["depth"]) for row in reference.toc])
                stack.enter_context(patch.object(main, "job_store", PersistentJobStore(fixture.engine)))
                stack.enter_context(patch.object(main, "OUTPUT_DIR", root / "outputs"))
                client = TestClient(main.app)
                stack.callback(client.close)
                headers = {"X-Job-Token": job.access_token}
                detail = client.get(f"/api/v2/jobs/{job.id}", headers=headers)
                self.assertEqual(detail.json()["status"], "completed")
                self.assertEqual(detail.headers["cache-control"], "no-store")
                download = client.get(detail.json()["download_url"], headers=headers)
                self.assertEqual(download.status_code, 200)
                self.assertEqual(hashlib.sha256(download.content).hexdigest(), navigation.sha256(Path(delivered.output_path)))
                self.assertEqual(fixture.recover()["scanned"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
