"""R2 history: actual manifest/reduce/package replay, never new translation.

Set EPUB_HISTORY_UPLOAD_DIR and EPUB_HISTORY_OUTPUT_DIR to the three pinned
historical originals/deliveries. B2's real, non-AI preprocessing is followed by
actual per-chapter set/get and reduce_and_package, then real EPUBCheck. The
replay is byte-identity input to the reducer, not a claimed translation test.

Additional storage collision/late-attempt tests reuse two distinct, untouched
chapter byte strings from each original. Their Volume-A/Volume-B namespaces
are explicit test inputs; we do not claim these three source books contain
that collision themselves. No original or existing delivery is modified.
"""
from __future__ import annotations

import hashlib
import io
import json
import threading
import unittest
import zipfile
from collections import Counter
from contextlib import redirect_stdout
from unittest.mock import patch

import test_d39_table_history as tables


class ReduceHistoryTests(tables.TableHistoryTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from app.domain import book_reduce_service as reduce_api
        from app.domain.manifest_service import build_manifest
        from app.engine.compiler import EPUBCHECK_JAR
        from app.engine.epub_validation import validate_epub
        from app.engine.unpacker import EpubUnpacker

        cls.reduce_api = reduce_api
        cls.stack.enter_context(patch.object(reduce_api, "_REDUCE_WORK_DIR", cls.root / "reduce-work"))
        cls.replay_records = {}
        cls.original_chapter_samples = {}
        cls.replay_validation = {}
        for key, (original_snapshot, prepared_snapshot, compiler) in list(cls.runs.items()):
            prepared = cls.root / (key + "-converted.epub")
            replayed = cls.root / (key + "-replayed.epub")
            source = cls.root / (key + "-source.epub")
            job_id, attempt_id = "d40-history-" + key, "replay-attempt-2"
            with redirect_stdout(io.StringIO()):
                manifest = build_manifest(str(prepared), job_id)
                original_manifest = build_manifest(str(source), job_id + "-original")
                book = EpubUnpacker(str(prepared)).load_book()
            if manifest.get("error") or original_manifest.get("error") or book is None:
                raise AssertionError(f"Actual historical manifest failed: {key}")
            body_paths = [chapter["file_path"] for chapter in manifest["chapters"]
                          if chapter["chapter_kind"] == "body"]
            if not body_paths or len(set(body_paths)) != len(body_paths):
                raise AssertionError(f"Missing or ambiguous real body paths: {key}")
            items = {item.get_name(): item for item in book.get_items() if item is not None}
            payloads = {}
            for name in body_paths:
                payload = items[name].get_content()
                if isinstance(payload, str):
                    payload = payload.encode("utf-8")
                payloads[name] = payload
                reduce_api.set_chapter_output(job_id, name, payload, attempt_id=attempt_id)
                if reduce_api.get_chapter_output(job_id, name, attempt_id=attempt_id) != payload:
                    raise AssertionError(f"Actual chapter bytes changed in intermediate storage: {key} {name}")

            reader = reduce_api.make_get_chapter_content(job_id, attempt_id=attempt_id)
            reads = Counter()
            def replay_content(name):
                content = reader(name)
                if name not in payloads or content != payloads[name]:
                    raise AssertionError(f"Reducer fetched wrong real chapter/attempt: {key} {name}")
                reads[name] += 1
                return content

            with redirect_stdout(io.StringIO()):
                packaged = reduce_api.reduce_and_package(str(prepared), str(replayed), replay_content)
            if not packaged or not replayed.is_file():
                raise AssertionError(f"Real identity reduce/package failed: {key}")
            cls.replay_validation[key] = validate_epub(replayed, EPUBCHECK_JAR)
            final_snapshot = tables.navigation.BookSnapshot(replayed, cls.opencc)
            cls.runs[key] = (original_snapshot, final_snapshot, compiler)
            source_tables, _, source_css, _ = cls.table_runs[key]
            table_classes = {token for group in source_tables.values() for table in group
                             for node in [table, *table.find_all(True)] for token in node.get("class", [])}
            cls.table_runs[key] = (source_tables,
                tables.tables_from_zip(replayed, final_snapshot.docs), source_css,
                tables.class_declarations_from_zip(replayed, table_classes))

            original_samples = []
            with zipfile.ZipFile(source) as archive:
                names = archive.namelist()
                for chapter in original_manifest["chapters"]:
                    if chapter["chapter_kind"] != "body":
                        continue
                    name = chapter["file_path"]
                    members = [member for member in names if member == name or member.endswith("/" + name)]
                    if len(members) != 1:
                        raise AssertionError(f"Ambiguous original chapter ZIP member: {key} {name}")
                    raw = archive.read(members[0])
                    if all(raw != existing for _, existing in original_samples):
                        original_samples.append((name, raw))
                    if len(original_samples) == 2:
                        break
            if len(original_samples) != 2:
                raise AssertionError(f"Need two different actual chapter byte strings: {key}")
            cls.original_chapter_samples[key] = original_samples
            cls.replay_records[key] = {
                "manifest": manifest, "body_paths": body_paths, "reads": reads,
                "prepared_snapshot": prepared_snapshot,
                "payload_hashes": {name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()},
            }

        print("R2 historical identity replay counts: " + json.dumps({key: {
            "manifest_chapters": len(value["manifest"]["chapters"]),
            "body_chapters_stored": len(value["body_paths"]),
            "body_chapters_read": sum(value["reads"].values()),
            "chunks": sum(len(chapter["chunks"]) for chapter in value["manifest"]["chapters"]),
        } for key, value in cls.replay_records.items()}, sort_keys=True))

    def test_actual_epubcheck_fixes_target_without_new_errors_elsewhere(self):
        for key, (_, _, compiler) in self.runs.items():
            with self.subTest(book=key):
                self.assertEqual(compiler.metrics.mode, "full", "Preprocessing must not fall back")
                self.assertTrue(compiler.validation_passed, compiler.final_message)
                self.assertTrue(self.replay_validation[key].passed, self.replay_validation[key].message)
                report = self.validation_reports[key + "-replayed.epub"]
                self.assertEqual(report["checker"]["nError"], 0)
                self.assertEqual(report["checker"]["nFatal"], 0)
                self.assertFalse([message for message in report["messages"]
                                  if message["severity"] in {"ERROR", "FATAL"}])

    def test_actual_manifest_chapters_replayed_once_with_unique_ids(self):
        for key, record in self.replay_records.items():
            with self.subTest(book=key):
                self.assertEqual(record["reads"], Counter(record["body_paths"]),
                                 "Every real body chapter must pass through the intermediate reader exactly once")
                chapters = record["manifest"]["chapters"]
                ids = [chapter["chapter_id"] for chapter in chapters]
                chunk_ids = [chunk["chunk_id"] for chapter in chapters for chunk in chapter["chunks"]]
                self.assertEqual(len(ids), len(set(ids)), "Real manifest chapter IDs collide")
                self.assertEqual(len(chunk_ids), len(set(chunk_ids)), "Real manifest chunk IDs collide")
                prepared, final = record["prepared_snapshot"], self.runs[key][1]
                self.assertEqual([(entry["label"], entry["target"], entry["depth"]) for entry in prepared.toc],
                                 [(entry["label"], entry["target"], entry["depth"]) for entry in final.toc],
                                 "Identity replay must not pretend to translate original TOC labels")

    def test_same_basename_storage_with_distinct_real_source_chapters(self):
        for key, samples in self.original_chapter_samples.items():
            with self.subTest(book=key):
                first, second = samples[0][1], samples[1][1]
                self.assertNotEqual(first, second)
                job, attempt = "collision-" + key, "same-attempt"
                path_a, path_b = "Volume-A/chapter.xhtml", "Volume-B/chapter.xhtml"
                stored_a = self.reduce_api.set_chapter_output(job, path_a, first, attempt_id=attempt)
                stored_b = self.reduce_api.set_chapter_output(job, path_b, second, attempt_id=attempt)
                self.assertNotEqual(stored_a, stored_b, "Different full chapter paths share an intermediate file")
                reader = self.reduce_api.make_get_chapter_content(job, attempt_id=attempt)
                self.assertEqual(reader(path_a), first)
                self.assertEqual(reader(path_b), second)
                self.assertIsNone(reader("Volume-C/chapter.xhtml"))

    def test_late_previous_attempt_cannot_change_current_reader(self):
        for key, samples in self.original_chapter_samples.items():
            with self.subTest(book=key):
                original_a, original_b = samples[0][1], samples[1][1]
                job, name = "late-attempt-" + key, "text/chapter.xhtml"
                self.reduce_api.set_chapter_output(job, name, original_a, attempt_id="attempt-1")
                ready, release = threading.Event(), threading.Event()
                errors = []
                def stale_writer():
                    try:
                        ready.set()
                        if not release.wait(5):
                            raise AssertionError("Late-attempt test handoff timed out")
                        self.reduce_api.set_chapter_output(job, name, original_a, attempt_id="attempt-1")
                    except BaseException as error:
                        errors.append(error)
                thread = threading.Thread(target=stale_writer)
                thread.start()
                try:
                    self.assertTrue(ready.wait(5))
                    self.reduce_api.set_chapter_output(job, name, original_b, attempt_id="attempt-2")
                    reader = self.reduce_api.make_get_chapter_content(job, attempt_id="attempt-2")
                    self.assertEqual(reader(name), original_b)
                finally:
                    release.set()
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(reader(name), original_b, "Stale attempt changed the captured current-attempt reader")
                self.assertEqual(self.reduce_api.get_chapter_output(job, name, attempt_id="attempt-1"), original_a)

    def test_old_unscoped_basename_bytes_are_not_implicitly_reused(self):
        for key, samples in self.original_chapter_samples.items():
            with self.subTest(book=key):
                old_bytes, current_bytes = samples[0][1], samples[1][1]
                job, name = "legacy-cache-" + key, "Volume-B/chapter.xhtml"
                legacy = self.root / "reduce-work" / job / "reduced" / "chapter.xhtml"
                legacy.parent.mkdir(parents=True, exist_ok=True)
                legacy.write_bytes(old_bytes)
                self.assertIsNone(self.reduce_api.get_chapter_output(job, name, attempt_id="attempt-2"))
                self.assertIsNone(self.reduce_api.get_chapter_output(job, name),
                                  "Default compatibility mode must not revive old basename-only cache")
                self.reduce_api.set_chapter_output(job, name, current_bytes, attempt_id="attempt-2")
                self.assertEqual(self.reduce_api.get_chapter_output(job, name, attempt_id="attempt-2"), current_bytes)
                self.assertEqual(legacy.read_bytes(), old_bytes, "Legacy cache must not be silently rewritten/deleted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
