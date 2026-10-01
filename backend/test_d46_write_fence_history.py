"""R8 opt-in historical late-writer/cancel/retry/artifact protection gate.

Three SHA-pinned original books take real conversion twice. A real thread with
the old write scope remains paused while cancellation and the newer attempt
commit. Old state/chapter/chunk/stage/clear writes must all be rejected, and
the new EPUB must still pass validation and refresh/download byte comparison.
No paid model, payment, production database or existing Worker is used.
"""
import hashlib
import io
import threading
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import test_d45_execution_history as previous

navigation = previous.navigation


class WriteFenceHistoryTests(previous.ExecutionHistoryTests):
    def test_old_executor_cannot_touch_restarted_historical_job_or_delivered_epub(self):
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from app import main, job_runner
        from app.models import Job, JobStatus, OutputMode, JobChapter, JobChunk, JobStage
        from app.storage_db import Base, PersistentJobStore
        from app.domain.job_write_fence import JobWriteConflict
        from app.engine.compiler import EPUBCHECK_JAR
        from app.engine.epub_validation import validate_epub

        for book in navigation.BOOKS:
            with self.subTest(book=book["key"]), ExitStack() as stack:
                root = self.root / ("r8-fence-" + book["key"])
                outputs = root / "outputs"
                outputs.mkdir(parents=True)
                engine = create_engine("sqlite:///" + str(root / "jobs.db"))
                stack.callback(engine.dispose)
                Base.metadata.create_all(engine)
                store = PersistentJobStore(engine)
                job = Job(id=book["key"], trace_id="r8-history", source_filename=book["input"],
                          input_path=str(self.uploads / book["input"]), access_token="r8-owner",
                          output_mode=OutputMode.simplified, status=JobStatus.pending,
                          lexicon_domains=[], enable_proper_noun=False,
                          translation_stats={"attempt_id": "old-attempt"})
                store.add(job)
                for module, name, value in ((main, "job_store", store), (job_runner, "job_store", store),
                                            (main, "OUTPUT_DIR", outputs), (job_runner, "OUTPUT_DIR", outputs)):
                    stack.enter_context(patch.object(module, name, value))
                stack.enter_context(patch("app.infra.execution_lease.tempfile.gettempdir", return_value=str(root)))
                notify = stack.enter_context(patch.object(job_runner, "notify_job_completed"))
                stack.enter_context(patch.object(job_runner, "report_error"))
                client = TestClient(main.app)
                stack.callback(client.close)
                headers = {"X-Job-Token": "r8-owner"}
                ready, resume = threading.Event(), threading.Event()
                errors, rejected, old_destinations = [], [], []
                real_convert = job_runner.converter.convert_file_to_horizontal
                winner = {}

                def converted_then_late_writes(source, destination, mode, **options):
                    result = real_convert(source, destination, mode, **options)
                    if threading.current_thread().name != "r8-old-executor":
                        return result
                    old_destinations.append(Path(destination))
                    ready.set()
                    if not resume.wait(60):
                        raise AssertionError("Historical newer executor did not finish")
                    actions = {
                        "status": lambda: store.update_status(job.id, JobStatus.success, "STALE WRITER",
                                                              output_path=winner["path"], translation_stats={"stale": True}),
                        "chapter": lambda: store.upsert_chapter(JobChapter(job.id, "marker", "old.xhtml")),
                        "chunk": lambda: store.upsert_chunk(JobChunk(job.id, "marker", "marker", 0, "old", "old")),
                        "stage": lambda: store.add_stage(JobStage(job.id, "stale_writer")),
                        "clear": lambda: store.clear_translation_progress(job.id),
                    }
                    for name, action in actions.items():
                        try:
                            action()
                        except JobWriteConflict:
                            rejected.append(name)
                        else:
                            raise AssertionError("Historical stale write was accepted: " + name)
                    return result

                stack.enter_context(patch.object(job_runner.converter, "convert_file_to_horizontal",
                                                 side_effect=converted_then_late_writes))
                def run_old():
                    try:
                        job_runner.run_job(job.id, "old-attempt")
                    except BaseException as exc:
                        errors.append(exc)
                old = threading.Thread(target=run_old, name="r8-old-executor")
                def stop_old():
                    resume.set()
                    old.join(15)
                    self.assertFalse(old.is_alive(), "Old executor must stop before fixture cleanup")
                stack.callback(stop_old)
                with redirect_stdout(io.StringIO()):
                    old.start()
                    self.assertTrue(ready.wait(40), errors)
                    cancelled = client.post(f"/api/v2/jobs/{job.id}/cancel", headers=headers)
                    self.assertEqual(cancelled.status_code, 200, cancelled.text)
                    restarted, reason = store.restart_translation_attempt(
                        job.id, attempt_id="new-attempt", action_label="Historical retry", max_free_retries=2,
                        started_at=datetime.now(timezone.utc))
                    self.assertEqual(reason, "ok")
                    store.upsert_chapter(JobChapter(job.id, "marker", "new.xhtml"), expected_attempt_id="new-attempt")
                    store.upsert_chunk(JobChunk(job.id, "marker", "marker", 0, "new", "new"), expected_attempt_id="new-attempt")
                    job_runner.run_job(job.id, "new-attempt")
                    delivered = store.get(job.id)
                    self.assertEqual(delivered.status, JobStatus.success, delivered.message)
                    winner["path"] = delivered.output_path
                    before = navigation.sha256(Path(delivered.output_path))
                    resume.set()
                    old.join(15)
                self.assertFalse(old.is_alive())
                self.assertEqual(errors, [])
                self.assertCountEqual(rejected, ["status", "chapter", "chunk", "stage", "clear"])
                current = store.get(job.id)
                self.assertEqual(current.status, JobStatus.success)
                self.assertEqual(current.translation_stats["attempt_id"], "new-attempt")
                self.assertNotIn("stale", current.translation_stats)
                self.assertEqual(current.output_path, winner["path"])
                self.assertEqual(navigation.sha256(Path(current.output_path)), before)
                self.assertEqual(store.list_chapters(job.id)[0].file_path, "new.xhtml")
                self.assertEqual(store.list_chunks(job.id)[0].source_hash, "new")
                self.assertNotIn("stale_writer", [stage.stage_name for stage in store.list_stages(job.id)])
                self.assertTrue(all(not path.exists() for path in old_destinations))
                self.assertEqual(notify.call_count, 1, "Only the committed newer artifact may notify")
                self.assertTrue(validate_epub(current.output_path, EPUBCHECK_JAR).passed)
                snapshot = navigation.BookSnapshot(current.output_path, self.opencc)
                reference = self.runs[book["key"]][1]
                self.assertEqual(snapshot.images, reference.images)
                self.assertEqual(set(snapshot.docs), set(reference.docs))
                for name in reference.docs:
                    self.assertTrue(snapshot.docs[name]["text"] == reference.docs[name]["text"], name)
                    self.assertLessEqual(reference.docs[name]["ids"], snapshot.docs[name]["ids"])
                self.assertEqual([(row["label"], row["target"], row["depth"]) for row in snapshot.toc],
                                 [(row["label"], row["target"], row["depth"]) for row in reference.toc])
                with patch.object(main, "job_store", PersistentJobStore(engine)):
                    detail = client.get(f"/api/v2/jobs/{job.id}", headers=headers)
                    self.assertEqual(detail.json()["status"], "completed")
                    self.assertEqual(detail.headers["cache-control"], "no-store")
                    download = client.get(detail.json()["download_url"], headers=headers)
                self.assertEqual(download.status_code, 200)
                self.assertEqual(hashlib.sha256(download.content).hexdigest(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
