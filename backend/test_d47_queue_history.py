"""Opt-in R9: reconcile while a real book worker is blocked, then deliver.

Three SHA-pinned historical books each use separate, real Celery consumers and
an isolated SQLite/filesystem broker. Gateway replies are controlled and no
paid API/production data is accessed. Inherits R8 and older artifact gates.
"""
import hashlib
from contextlib import ExitStack
from pathlib import Path
import unittest
from unittest.mock import patch

import test_d46_write_fence_history as previous
from test_d47_queue_process import SplitWorkerFixture

navigation = previous.navigation


class QueueHistoryTests(previous.WriteFenceHistoryTests):
    def test_reconciliation_precedes_book_release_then_historical_delivery_is_unchanged(self):
        from fastapi.testclient import TestClient
        from app import main
        from app.models import JobStatus
        from app.storage_db import PersistentJobStore
        from app.engine.compiler import EPUBCHECK_JAR
        from app.engine.epub_validation import validate_epub

        for book in navigation.BOOKS:
            with self.subTest(book=book["key"]), ExitStack() as stack:
                root = self.root / ("r9-queue-" + book["key"])
                f = SplitWorkerFixture(root, self.uploads / book["input"])
                stack.callback(f.close)
                f.add_book("blocker", lexicon_domains=[], enable_proper_noun=False)
                customer = f.add_book(book["key"], pending_payment=True,
                                      lexicon_domains=[], enable_proper_noun=False)
                f.pay(customer.id)
                f.start_role("book", "real")
                f.start_role("housekeeping", "real")
                self.assertEqual(f.dispatch()["sent"], 1)
                f.wait_for(lambda: (root / "book-entered.json").is_file())
                self.assertEqual(f.store.get("blocker").status, JobStatus.running)
                # Production-default remote control stays enabled even while
                # real book work is active. It must not defeat fixed roles.
                inspector = f.producer.control.inspect(timeout=5, limit=2)
                before = inspector.active_queues()
                self.assertEqual(len(before or {}), 2)
                queues = {node: [item["name"] for item in items] for node, items in before.items()}
                self.assertEqual(sorted(queues.values()), [["celery"], ["housekeeping"]])
                for command, arguments in (("add_consumer", {"queue": "celery"}),
                                           ("pool_grow", {"n": 2})):
                    replies = f.producer.control.broadcast(command, arguments=arguments,
                                                           reply=True, timeout=5, limit=2)
                    responses = {node: result for reply in replies for node, result in reply.items()}
                    self.assertEqual(set(responses), set(queues))
                    self.assertTrue(all("error" in result for result in responses.values()))
                after = inspector.active_queues()
                self.assertEqual({node: [item["name"] for item in items] for node, items in after.items()}, queues)
                stats = inspector.stats()
                self.assertEqual(set(stats), set(queues))
                self.assertTrue(all(value["pool"]["max-concurrency"] == 1 for value in stats.values()))
                reconciled = f.result(f.short("jobs.reconcile_payments", expires=10))
                self.assertEqual(reconciled["state"], "SUCCESS")
                self.assertEqual(reconciled["routing_key"], "housekeeping")
                self.assertEqual(reconciled["result"]["paid"], 1)
                self.assertEqual(f.result(f.short("infra.health.ping"))["state"], "SUCCESS")
                queued = f.store.get(customer.id)
                self.assertEqual(queued.status, JobStatus.pending)
                self.assertEqual(queued.expected_amount, "0.99")
                self.assertEqual(queued.input_path, str(self.uploads / book["input"]))
                self.assertEqual(queued.translation_stats["attempt_id"], "queue-attempt")
                self.assertEqual(f.store.list_dispatches(customer.id)[0]["status"], "sent")
                self.assertFalse(queued.output_path)
                self.assertFalse((root / "release-book").exists())
                self.assertEqual(f.store.get("blocker").status, JobStatus.running)

                f.release()
                f.wait_for(lambda: f.store.get(customer.id).status in {JobStatus.success, JobStatus.failed}, timeout=60)
                delivered = f.store.get(customer.id)
                self.assertEqual(delivered.status, JobStatus.success, delivered.message)
                self.assertEqual(f.store.get("blocker").status, JobStatus.success)
                self.assertTrue(validate_epub(delivered.output_path, EPUBCHECK_JAR).passed)
                actual = navigation.BookSnapshot(delivered.output_path, self.opencc)
                expected = self.runs[book["key"]][1]
                self.assertEqual(actual.images, expected.images)
                self.assertEqual(set(actual.docs), set(expected.docs))
                for name in expected.docs:
                    self.assertTrue(actual.docs[name]["text"] == expected.docs[name]["text"], name)
                    self.assertLessEqual(expected.docs[name]["ids"], actual.docs[name]["ids"])
                self.assertEqual([(row["label"], row["target"], row["depth"]) for row in actual.toc],
                                 [(row["label"], row["target"], row["depth"]) for row in expected.toc])
                stack.enter_context(patch.object(main, "job_store", PersistentJobStore(f.engine)))
                stack.enter_context(patch.object(main, "OUTPUT_DIR", root / "outputs"))
                client = TestClient(main.app)
                stack.callback(client.close)
                headers = {"X-Job-Token": customer.access_token}
                detail = client.get(f"/api/v2/jobs/{customer.id}", headers=headers)
                self.assertEqual(detail.json()["status"], "completed")
                self.assertEqual(detail.headers["cache-control"], "no-store")
                download = client.get(detail.json()["download_url"], headers=headers)
                self.assertEqual(download.status_code, 200)
                self.assertEqual(hashlib.sha256(download.content).hexdigest(), navigation.sha256(Path(delivered.output_path)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
