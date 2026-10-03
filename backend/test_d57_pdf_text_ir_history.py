"""B1 opt-in real-book IR integrity, not semantic/paragraph/rendering proof.

The user-selected encrypted Chinese PDF is read-only and SHA-pinned. A separate
pure-pypdf child independently produces only hashes/counts/object references;
the actual IR must preserve each raw-text value and encoded image stream. No
full text, image data, source path, payment or model request is logged.
"""
import base64
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from app.domain.pdf_text_ir import extract_pdf_text_ir


SOURCE_ENV = "EPUB_PDF_HISTORY_FILE"
EXPECTED_SHA256 = "af21894c7542c1a7e3c286a762cc15c1d1aeaa81fb9ca8e028d85ff5d22b5023"
EXPECTED_PAGES = 346
EXPECTED_PAGE_IMAGE_REFS = 111
EXPECTED_UNIQUE_IMAGES = 110
EXPECTED_ENCODED_BYTES = 5325528


REFERENCE_PROBE = r'''
import hashlib, json, socket, sys
def deny_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo", "socket.sendto", "socket.sendmsg"}:
        raise PermissionError("reference probe network disabled")
sys.addaudithook(deny_network)
from pypdf import PdfReader
reader = PdfReader(sys.argv[1], strict=True)
if reader.is_encrypted:
    assert reader.decrypt("") in (1, 2)
    assert reader.user_access_permissions is not None and int(reader.user_access_permissions) & 16
resources, pages = {}, []
def digest(data): return hashlib.sha256(data).hexdigest()
for number, page in enumerate(reader.pages, 1):
    fragments = []
    def visitor(text, cm, tm, font, size):
        if text: fragments.append(digest(text.encode("utf-8")))
    raw = page.extract_text(visitor_text=visitor)
    seen, image_refs = set(), []
    def images(container, depth=0):
        assert depth < 20
        if not container: return
        container = container.get_object()
        entries = container.get("/XObject", {})
        entries = entries.get_object() if hasattr(entries, "get_object") else entries
        for ref in entries.values():
            identity = (ref.idnum, ref.generation)
            if identity in seen: continue
            seen.add(identity)
            obj = ref.get_object()
            if obj.get("/Subtype") == "/Image":
                image_refs.append(list(identity))
                encoded = obj._data
                row = {"sha256": digest(encoded), "size": len(encoded)}
                key = "%s:%s" % identity
                assert key not in resources or resources[key] == row
                resources[key] = row
            elif obj.get("/Subtype") == "/Form":
                images(obj.get("/Resources", {}), depth + 1)
    images(page.get("/Resources", {}))
    pages.append({"page_number": number, "raw_text_sha256": digest(raw.encode("utf-8")),
                  "char_count": len(raw), "nonspace_count": sum(not c.isspace() for c in raw),
                  "zero_width_count": raw.count("\u200b"), "fragment_text_sha256": fragments,
                  "image_object_ids": image_refs})
print(json.dumps({"pages": pages, "resources": resources}, separators=(",", ":")))
'''


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def identity(path):
    value = path.stat()
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_mode)


class PdfTextIrHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured = os.environ.get(SOURCE_ENV, "").strip()
        if not configured:
            raise unittest.SkipTest("EPUB_PDF_HISTORY_FILE not provided; real PDF IR gate not executed")
        cls.path = Path(configured).absolute()
        if not cls.path.is_file() or sha256(cls.path) != EXPECTED_SHA256:
            raise AssertionError("Real PDF IR gate requires the exact user-selected SHA-pinned source")
        cls.before_identity = identity(cls.path)
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        cls.guards = [cls.stack.enter_context(patch.object(socket.socket, name, side_effect=AssertionError("network forbidden")))
                      for name in ("connect", "connect_ex")]
        cls.guards.append(cls.stack.enter_context(patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden"))))
        cls.addClassCleanup(lambda: [guard.assert_not_called() for guard in cls.guards])
        cls.root = Path(cls.stack.enter_context(tempfile.TemporaryDirectory(prefix="d57-real-ir-"))).resolve()
        cls.stack.enter_context(patch.object(tempfile, "tempdir", str(cls.root)))
        cls.stack.enter_context(patch.dict(os.environ, {"TMPDIR": str(cls.root), "TMP": str(cls.root), "TEMP": str(cls.root)}))
        cls.result = extract_pdf_text_ir(cls.path)
        probe = subprocess.run([sys.executable, "-I", "-B", "-c", REFERENCE_PROBE, str(cls.path)],
                               cwd=cls.root, capture_output=True, text=True, timeout=60,
                               env={"PATH": os.defpath, "HOME": str(cls.root), "TMPDIR": str(cls.root), "LANG": "C"})
        if probe.returncode != 0:
            raise AssertionError("Independent real PDF reference probe failed; parser details not logged")
        cls.reference = json.loads(probe.stdout)

    def test_all_pages_and_six_image_only_pages_remain_source_locatable(self):
        result = self.result
        self.assertEqual(result["source_sha256"], EXPECTED_SHA256)
        self.assertEqual(result["page_count"], EXPECTED_PAGES)
        self.assertEqual([p["page_number"] for p in result["pages"]], list(range(1, EXPECTED_PAGES + 1)))
        self.assertEqual(len({p["page_id"] for p in result["pages"]}), EXPECTED_PAGES)
        image_only = [p for p in result["pages"] if not p["nonspace_count"]]
        self.assertEqual(len(image_only), 6)
        for page in image_only:
            self.assertTrue(page["image_resource_ids"])
            self.assertTrue(page["page_id"])
            self.assertEqual(len(page["raw_text"]), 0)
        self.assertEqual(result["status"], "review_required")
        self.assertIs(result["eligible_for_payment"], False)
        self.assertIn("reading_order_unverified", result["flags"])
        self.assertIn("encoded_streams_only", result["flags"])

    def test_every_raw_text_and_fragment_hash_matches_independent_pypdf_without_normalization(self):
        self.assertEqual(len(self.reference["pages"]), EXPECTED_PAGES)
        zero_width_count = 0
        for page, original in zip(self.result["pages"], self.reference["pages"]):
            with self.subTest(page=page["page_number"]):
                actual_hash = hashlib.sha256(page["raw_text"].encode("utf-8")).hexdigest()
                self.assertEqual(actual_hash, original["raw_text_sha256"])
                self.assertEqual(page["char_count"], original["char_count"])
                self.assertEqual(page["nonspace_count"], original["nonspace_count"])
                self.assertEqual(page["raw_text"].count("\u200b"), original["zero_width_count"])
                actual_fragments = [hashlib.sha256(f["text"].encode("utf-8")).hexdigest() for f in page["fragments"]]
                self.assertEqual(actual_fragments, original["fragment_text_sha256"])
                zero_width_count += original["zero_width_count"]
        self.assertGreater(zero_width_count, 0)

    def test_all_encoded_image_bytes_match_original_decrypted_streams_and_page_references(self):
        resources = self.result["resources"]
        self.assertEqual(len(resources), EXPECTED_UNIQUE_IMAGES)
        self.assertEqual(len(self.reference["resources"]), EXPECTED_UNIQUE_IMAGES)
        self.assertEqual(sum(len(p["image_resource_ids"]) for p in self.result["pages"]), EXPECTED_PAGE_IMAGE_REFS)
        resource_objects = {}
        for resource in resources:
            encoded = base64.b64decode(resource["encoded_stream_base64"], validate=True)
            object_id = resource["object_id"]
            key = "%s:%s" % tuple(object_id)
            original = self.reference["resources"][key]
            self.assertEqual(hashlib.sha256(encoded).hexdigest(), original["sha256"])
            self.assertEqual(resource["encoded_stream_sha256"], original["sha256"])
            self.assertEqual(resource["encoded_size"], original["size"])
            self.assertEqual(len(encoded), original["size"])
            self.assertIn("encoded_stream_only", resource["flags"])
            self.assertEqual(resource["filters"], ["/DCTDecode"])
            resource_objects[resource["resource_id"]] = object_id
        self.assertEqual(sum(r["encoded_size"] for r in resources), EXPECTED_ENCODED_BYTES)
        self.assertEqual(self.result["total_encoded_resource_bytes"], EXPECTED_ENCODED_BYTES)
        for page, original in zip(self.result["pages"], self.reference["pages"]):
            self.assertEqual([resource_objects[rid] for rid in page["image_resource_ids"]], original["image_object_ids"])

    def test_second_real_execution_preserves_all_page_event_and_resource_identities(self):
        again = extract_pdf_text_ir(self.path)
        self.assertEqual([p["page_id"] for p in again["pages"]], [p["page_id"] for p in self.result["pages"]])
        for first, second in zip(self.result["pages"], again["pages"]):
            self.assertEqual([f["fragment_id"] for f in first["fragments"]], [f["fragment_id"] for f in second["fragments"]])
            self.assertEqual(first["image_resource_ids"], second["image_resource_ids"])
        self.assertEqual([(r["resource_id"], r["encoded_stream_sha256"]) for r in again["resources"]],
                         [(r["resource_id"], r["encoded_stream_sha256"]) for r in self.result["resources"]])

    def test_original_and_private_snapshot_cleanup_stay_unchanged(self):
        self.assertEqual(sha256(self.path), EXPECTED_SHA256)
        self.assertEqual(identity(self.path), self.before_identity)
        self.assertEqual(list(self.root.iterdir()), [])
        for guard in self.guards: guard.assert_not_called()


def main():
    configured = os.environ.get(SOURCE_ENV, "").strip()
    if not configured or not Path(configured).is_file():
        print("Real PDF IR gate requires EPUB_PDF_HISTORY_FILE pointing to the selected original.", file=sys.stderr)
        return 2
    program = unittest.main(verbosity=2, exit=False)
    result = program.result
    if not result.wasSuccessful() or not result.testsRun or result.skipped:
        return 1
    report = PdfTextIrHistoryTests.result
    print(json.dumps({"verification": "real_pdf_raw_ir_integrity_only", "source_sha256": EXPECTED_SHA256,
                      "page_count": report["page_count"], "total_char_count": report["total_char_count"],
                      "total_fragment_count": report["total_fragment_count"], "unique_image_resources": len(report["resources"]),
                      "page_image_resource_references": sum(len(p["image_resource_ids"]) for p in report["pages"]),
                      "total_encoded_resource_bytes": report["total_encoded_resource_bytes"],
                      "status": report["status"], "eligible_for_payment": False, "source_unchanged": True,
                      "parent_network_calls": sum(g.call_count for g in PdfTextIrHistoryTests.guards),
                      "child_network_policy": "deny", "independent_rendering_proven": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
