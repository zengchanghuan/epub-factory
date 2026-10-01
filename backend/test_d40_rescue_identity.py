"""R2 integration gates for rescue identity and immutable attempt ownership.

The manifest, translation orchestration, rescue queue and reduce-artifact
writer are real. Only model responses/cache hits are replaced; all mutable
state is temporary and network access is forbidden before app imports.
"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ebooklib import epub


ALPHA = "Alpha proposes this approach."
BETA = "Beta supports this approach."
TRANSLATIONS = {ALPHA: "阿尔法提出这种方法。", BETA: "贝塔支持这种方法。"}


class RescueIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epub-r2-rescue-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self._patch(patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + str(self.root / "bootstrap.sqlite3"),
            "EPUB_PERSISTENT_STORE": "0",
            "EPUB_TRANSLATION_CHECKPOINT_DB": str(self.root / "checkpoints.sqlite3"),
            "EPUB_FAILED_CHUNK_DIR": str(self.root / "failed"),
            "REPAIR_UPLOAD_DIR": str(self.root / "repair"),
            "OPENAI_API_KEY": "offline-test-never-use",
            "OPENAI_BASE_URL": "http://offline.invalid/v1",
            "OPENAI_MODEL": "deepseek-flash",
            "EPUB_DEFAULT_TRANSLATION_MODEL": "deepseek-flash",
            "CELERY_BROKER_URL": "", "REDIS_URL": "", "SENTRY_DSN": "",
            "ALIPAY_APP_ID": "", "SMTP_HOST": "",
            "NOTIFY_EMAIL_ENABLED": "0", "OWNER_PAYMENT_EMAIL_ENABLED": "0",
            "EPUB_LLM_RATE_LIMITER_ENABLED": "0",
            "EPUB_LLM_GLOBAL_HEALTH_ENABLED": "0",
            "EPUB_BOOK_PROFILER_ENABLED": "0",
            "EPUB_GLOSSARY_CONCURRENCY": "2",
            "EPUB_TRANSLATION_QUALITY_RETRIES": "1",
            "EPUB_TRANSLATION_TEXT_SEGMENT_RESCUE": "0",
            "EPUB_FAILED_CHUNK_RESCUE": "1",
            "EPUB_CHAPTER_CONCURRENCY_CAP": "1",
        }))
        self._patch(patch("dotenv.load_dotenv", return_value=False))
        self.network = [self._patch(patch(target, side_effect=AssertionError("R2 forbids network")))
                        for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo")]

        from app.domain import book_reduce_service, fast_translation_runner
        from app.domain.manifest_service import build_manifest
        from app.engine.cleaners.semantics_translator import SemanticsTranslator
        from app.engine.unpacker import EpubUnpacker
        from app.models import Job, JobStatus, OutputMode
        from app.storage import JobStore

        self.reducer = book_reduce_service
        self.runner = fast_translation_runner
        self.build_manifest = build_manifest
        self.translator = SemanticsTranslator
        self.unpacker = EpubUnpacker
        self._patch(patch.object(self.reducer, "_REDUCE_WORK_DIR", self.root / "reduce"))
        self._patch(patch("app.engine.cleaners.semantics_translator.TranslationCache", return_value=Mock(
            get=Mock(return_value=None), get_latest_compatible=Mock(return_value=None))))
        self.store = JobStore()
        self._patch(patch.object(self.runner, "job_store", self.store))
        self.job = Job(
            id="r2-rescue-job", trace_id="offline", source_filename="fixture.epub",
            input_path=str(self.root / "fixture.epub"), output_mode=OutputMode.simplified,
            enable_translation=True, status=JobStatus.running,
            translation_stats={"attempt_id": "attempt-1"},
        )
        self.store.add(self.job)

    def _patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def tearDown(self):
        for guard in self.network:
            guard.assert_not_called()

    def fixture(self, paths_and_text=(("part1/chapter.xhtml", ALPHA), ("part2/chapter.xhtml", BETA))):
        book = epub.EpubBook()
        book.set_identifier("r2-rescue-fixture")
        book.set_title("R2 identity")
        book.set_language("en")
        for number, (path, text) in enumerate(paths_and_text):
            chapter = epub.EpubHtml(uid=f"chapter-{number}", title="Chapter", file_name=path)
            chapter.content = f"<html><body><p>{text}</p></body></html>"
            book.add_item(chapter)
            book.spine.append(chapter)
            book.toc += (epub.Link(path, f"Chapter {number + 1}", f"toc-{number}"),)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        source = Path(self.job.input_path)
        epub.write_epub(str(source), book)
        manifest = self.build_manifest(str(source), self.job.id)
        self.assertNotIn("error", manifest)
        loaded = self.unpacker(str(source)).load_book()
        self.assertIsNotNone(loaded)
        contents = {item.get_name(): item.get_content() for item in loaded.get_items() if item.get_type() == 9}
        return manifest, contents

    def run_manifest(self, manifest, contents, **kwargs):
        return asyncio.run(self.runner._translate_manifest_async(
            job=self.job, manifest=manifest, content_by_file=contents,
            glossary={}, progress_callback=lambda _message: None, **kwargs))

    @staticmethod
    def reply(translated):
        return {0: translated}, {"model": "deepseek-flash", "base_url": "http://offline.invalid", "attempts": 1}

    def test_single_failed_chapter_rescue_cannot_overwrite_healthy_same_basename(self):
        manifest, contents = self.fixture()
        chapters = [chapter for chapter in manifest["chapters"] if chapter["chapter_kind"] == "body"]
        self.assertEqual(len(chapters), 2)
        self.assertEqual(len({chapter["chapter_id"] for chapter in chapters}), 2)
        counts = {ALPHA: 0, BETA: 0}

        async def respond(_translator, payload, **_kwargs):
            source = payload[0]["html"]
            text = ALPHA if ALPHA in source else BETA
            counts[text] += 1
            # Exhaust Alpha's normal draft/quality retry before rescue; Beta
            # succeeds normally and must never be inserted into that queue.
            if text == ALPHA and counts[text] <= 2:
                return self.reply(source)
            return self.reply(TRANSLATIONS[text])

        with patch.object(self.translator, "_call_llm_json_batch", new=respond):
            stats, _ = self.run_manifest(manifest, contents)
        self.assertEqual(stats["failed_chunk_rescue_succeeded"], 1)
        self.assertEqual(stats["failed_chunk_rescue_candidates"], 1)
        self.assertEqual(stats["failed_chunks"], 0)
        for path, expected, forbidden in (("part1/chapter.xhtml", ALPHA, BETA),
                                          ("part2/chapter.xhtml", BETA, ALPHA)):
            output = self.reducer.get_chapter_output(self.job.id, path, attempt_id="attempt-1")
            self.assertIsNotNone(output)
            self.assertIn(TRANSLATIONS[expected].encode(), output)
            self.assertNotIn(TRANSLATIONS[forbidden].encode(), output)
        persisted = self.store.list_chunks(self.job.id)
        self.assertEqual(len(persisted), 2)
        self.assertEqual(len({chunk.chunk_id for chunk in persisted}), 2)

    def assert_rejected_before_model(self, manifest, contents, **kwargs):
        with patch.object(self.runner, "SemanticsTranslator") as construct:
            with self.assertRaises(ValueError):
                self.run_manifest(manifest, contents, **kwargs)
            construct.assert_not_called()
        self.assertFalse((self.root / "reduce").exists())

    def test_duplicate_chapter_identity_is_rejected_before_model(self):
        manifest, contents = self.fixture()
        manifest["chapters"][1]["chapter_id"] = manifest["chapters"][0]["chapter_id"]
        self.assert_rejected_before_model(manifest, contents)

    def test_duplicate_chunk_identity_is_rejected_before_model(self):
        manifest, contents = self.fixture()
        manifest["chapters"][1]["chunks"][0]["chunk_id"] = manifest["chapters"][0]["chunks"][0]["chunk_id"]
        self.assert_rejected_before_model(manifest, contents)

    def test_ambiguous_old_chapter_strategy_is_rejected_before_model(self):
        manifest, contents = self.fixture()
        self.assertIn("chapter", manifest["ambiguous_legacy_chapter_ids"])
        self.assert_rejected_before_model(manifest, contents,
                                          chapter_strategy_overrides={"chapter": "academic_rigorous"})

    def test_renamed_long_legacy_strategy_is_rejected_before_model(self):
        old_id = "z" * 90
        manifest, contents = self.fixture(((old_id + ".xhtml", ALPHA),))
        self.assertIn(old_id, manifest["renamed_legacy_chapter_ids"])
        self.assert_rejected_before_model(manifest, contents,
                                          chapter_strategy_overrides={old_id: "academic_rigorous"})

    def test_inflight_old_attempt_writes_cannot_overwrite_new_attempt(self):
        path = "part1/chapter.xhtml"
        manifest, contents = self.fixture(((path, ALPHA),))
        newer = b"<html><body><p>New attempt content must survive.</p></body></html>"

        async def respond(_translator, _payload, **_kwargs):
            # Simulate the shared in-memory Job receiving a newer attempt
            # while the old model request is in flight, without cancelling it.
            self.job.translation_stats = {"attempt_id": "attempt-2", "sentinel": "new"}
            self.reducer.set_chapter_output(self.job.id, path, newer, attempt_id="attempt-2")
            return self.reply(TRANSLATIONS[ALPHA])

        with patch.object(self.translator, "_call_llm_json_batch", new=respond):
            self.run_manifest(manifest, contents)
        old = self.reducer.get_chapter_output(self.job.id, path, attempt_id="attempt-1")
        self.assertIn(TRANSLATIONS[ALPHA].encode(), old)
        self.assertEqual(self.reducer.get_chapter_output(self.job.id, path, attempt_id="attempt-2"), newer)
        self.assertIsNone(self.reducer.get_chapter_output(self.job.id, path))
        self.assertEqual(self.job.translation_stats, {"attempt_id": "attempt-2", "sentinel": "new"})

    def test_prior_same_attempt_artifact_cannot_hide_missing_current_source(self):
        path = "part1/chapter.xhtml"
        manifest, _contents = self.fixture(((path, ALPHA),))
        previous = b"<html><body><p>Earlier artifact, not a completed current rewrite.</p></body></html>"
        self.reducer.set_chapter_output(self.job.id, path, previous, attempt_id="attempt-1")

        async def respond(_translator, _payload, **_kwargs):
            return self.reply(TRANSLATIONS[ALPHA])

        with patch.object(self.translator, "_call_llm_json_batch", new=respond):
            with self.assertRaisesRegex(RuntimeError, "原始内容缺失"):
                self.run_manifest(manifest, {})
        self.assertEqual(self.reducer.get_chapter_output(self.job.id, path, attempt_id="attempt-1"), previous)


if __name__ == "__main__":
    unittest.main(verbosity=2)
