"""Synthetic PDF preflight contracts, NOT real-book or translation quality proof.

PDF bytes are generated solely as test fixtures in private TemporaryDirectories.
This suite imports no app startup, dotenv, database, gateway or translation code.
Run separately from release_guard: the inspected parser deliberately uses -I -B.
"""
from contextlib import ExitStack
from dataclasses import replace
import copy
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.constants import UserAccessPermissions
from pypdf.generic import BooleanObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject, TextStringObject

from app.domain import pdf_text_preflight as pdf
from app.domain.input_formats import (
    PDF_DISABLED_MESSAGE, SUPPORTED_EXTENSIONS, is_pdf_header, validate_filename,
)


SECRET_TEXT = "SYNTHETIC_PRIVATE_BODY_NOT_FOR_REPORT_" * 3


def fixture_pdf(pages=("text",), *, encrypted=None, permissions=None, permission_entry=None):
    """Create in-memory testing bytes; no manuscript/document artifact is authored."""
    writer = PdfWriter()
    for kind in pages:
        page = writer.add_blank_page(width=612, height=792)
        if kind == "blank":
            continue
        resources = DictionaryObject()
        operations = []
        if kind in {"text", "text_image", "sparse", "replacement", "control", "private_use"}:
            font = DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            })
            text = "Tiny" if kind == "sparse" else SECRET_TEXT
            if kind in {"replacement", "control", "private_use"}:
                codepoint = {"replacement": "FFFD", "control": "0001", "private_use": "E000"}[kind]
                cmap = DecodedStreamObject()
                cmap.set_data((
                    "/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
                    "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def "
                    "/CMapName /SyntheticUnicode def /CMapType 2 def "
                    "1 begincodespacerange <00> <FF> endcodespacerange "
                    "1 beginbfchar <41> <" + codepoint + "> endbfchar "
                    "endcmap CMapName currentdict /CMap defineresource pop end end"
                ).encode("ascii"))
                font[NameObject("/ToUnicode")] = writer._add_object(cmap)
                text = "A" * 60
            resources[NameObject("/Font")] = DictionaryObject({NameObject("/F1"): writer._add_object(font)})
            operations.append("BT /F1 12 Tf 72 720 Td (" + text + ") Tj ET")
        if kind in {"image", "text_image"}:
            image = DecodedStreamObject()
            image.set_data(b"\xff\x00\x00")
            image.update({
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Image"),
                NameObject("/Width"): NumberObject(1),
                NameObject("/Height"): NumberObject(1),
                NameObject("/BitsPerComponent"): NumberObject(8),
                NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
            })
            resources[NameObject("/XObject")] = DictionaryObject({NameObject("/I1"): writer._add_object(image)})
            operations.append("q 200 0 0 200 72 400 cm /I1 Do Q")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(operations).encode("ascii"))
        page[NameObject("/Resources")] = resources
        page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted is not None:
        options = {} if permissions is None else {"permissions_flag": permissions}
        writer.encrypt(encrypted, owner_password="synthetic-owner-password", **options)
        if permission_entry == "missing":
            del writer._encrypt_entry[NameObject("/P")]
        elif permission_entry is not None:
            writer._encrypt_entry[NameObject("/P")] = permission_entry
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class CompletedWorker:
    returncode = 0
    pid = -1

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class PdfTextPreflightTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="d56-synthetic-"))).resolve()
        self.guards = [
            self.stack.enter_context(patch.object(socket.socket, name, side_effect=AssertionError("network forbidden")))
            for name in ("connect", "connect_ex")
        ]
        self.guards.append(self.stack.enter_context(patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden"))))
        self.addCleanup(lambda: [guard.assert_not_called() for guard in self.guards])
        self.path = self.write("synthetic-private-input.pdf", fixture_pdf())

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def reject(self, path=None, reason=None, **kwargs):
        with self.assertRaises(pdf.PdfPreflightError) as caught:
            pdf.inspect_text_pdf(path or self.path, **kwargs)
        if reason is not None:
            self.assertEqual(caught.exception.reason, reason)
        self.assertNotIn(SECRET_TEXT, str(caught.exception))
        self.assertNotIn(str(self.root), str(caught.exception))
        return caught.exception

    def inject_result(self, result, *, raw=None, exit_code=0):
        def launch(snapshot, result_path, limits):
            Path(result_path).write_bytes(raw if raw is not None else json.dumps(result).encode("utf-8"))
            process = CompletedWorker()
            process.returncode = exit_code
            return process
        return patch.object(pdf, "_launch_worker", side_effect=launch)

    def assert_review(self, result):
        self.assertEqual(result["status"], "review_required")
        self.assertIs(result["eligible_for_payment"], False)

    def test_real_text_parser_reports_only_statistics_and_preserves_input(self):
        before = self.path.read_bytes()
        before_names = sorted(p.name for p in self.root.iterdir())
        result = pdf.inspect_text_pdf(self.path)
        self.assertEqual(result["schema_version"], "pdf-text-preflight-v1")
        self.assertEqual(result["parser_version"], "6.0.0")
        self.assertEqual(result["source_sha256"], hashlib.sha256(before).hexdigest())
        expected_status = "review_required" if result["flags"] else "text_candidate"
        self.assertEqual(result["status"], expected_status)
        self.assertTrue(set(result["flags"]) <= {"parser_warnings", "memory_limit_unavailable"})
        if "memory_limit_unavailable" in result["flags"]:
            self.assertEqual(sys.platform, "darwin")
        self.assertIs(result["eligible_for_payment"], False)
        self.assertEqual(result["page_count"], 1)
        self.assertEqual(result["total_char_count"], len(SECRET_TEXT))
        self.assertEqual(result["total_nonspace_count"], len(SECRET_TEXT))
        self.assertEqual(result["pages"][0]["page_number"], 1)
        self.assertEqual(result["pages"][0]["kind"], "text")
        self.assertEqual(result["pages"][0]["status"], "text_candidate")
        self.assertNotIn(SECRET_TEXT, json.dumps(result))
        self.assertNotIn(str(self.path), json.dumps(result))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before_names)

    def test_blank_and_image_only_are_review_not_claimed_scan_or_eligible(self):
        for kind in ("blank", "image"):
            with self.subTest(kind=kind):
                result = pdf.inspect_text_pdf(self.write(kind + ".pdf", fixture_pdf((kind,))))
                self.assert_review(result)
                page = result["pages"][0]
                self.assertEqual(page["kind"], "empty_or_graphic")
                self.assertEqual(page["char_count"], 0)
                self.assertIn("no_extractable_text", page["flags"])
                self.assertEqual(page["image_xobject_count"], int(kind == "image"))
                self.assertNotIn("scan", json.dumps(result).lower())

    def test_text_with_image_counts_image_without_claiming_visual_fidelity(self):
        result = pdf.inspect_text_pdf(self.write("with-image.pdf", fixture_pdf(("text_image",))))
        self.assertEqual(result["pages"][0]["kind"], "text")
        self.assertEqual(result["pages"][0]["image_xobject_count"], 1)
        # pypdf 6 inserts one line separator before this fixture's image Do.
        self.assertEqual(result["pages"][0]["char_count"], len(SECRET_TEXT) + 1)
        self.assertEqual(result["pages"][0]["nonspace_count"], len(SECRET_TEXT))
        self.assertIs(result["eligible_for_payment"], False)

    def test_mixed_pages_are_all_reported_in_order_without_hiding_missing_layer(self):
        result = pdf.inspect_text_pdf(self.write("mixed.pdf", fixture_pdf(("text", "image", "blank", "text"))))
        self.assert_review(result)
        self.assertEqual(result["page_count"], 4)
        self.assertEqual([page["page_number"] for page in result["pages"]], [1, 2, 3, 4])
        self.assertEqual([page["kind"] for page in result["pages"]], ["text", "empty_or_graphic", "empty_or_graphic", "text"])
        self.assertEqual(result["total_char_count"], 2 * len(SECRET_TEXT))

    def test_sparse_text_needs_review_and_cannot_become_billable(self):
        result = pdf.inspect_text_pdf(self.write("sparse.pdf", fixture_pdf(("sparse",))))
        self.assert_review(result)
        self.assertIn("sparse_text", result["pages"][0]["flags"])

    def test_actual_unicode_anomaly_signals_are_counts_not_raw_text(self):
        for kind, field, flag in (
            ("replacement", "replacement_count", "replacement_characters"),
            ("control", "control_count", "control_characters"),
            ("private_use", "private_use_count", "private_use_characters"),
        ):
            with self.subTest(kind=kind):
                result = pdf.inspect_text_pdf(self.write(kind + ".pdf", fixture_pdf((kind,))))
                self.assert_review(result)
                self.assertEqual(result["pages"][0][field], 60)
                self.assertIn(flag, result["pages"][0]["flags"])

    def test_password_required_encryption_is_rejected_without_guessing(self):
        self.reject(self.write("encrypted.pdf", fixture_pdf(encrypted="synthetic-password")), "encrypted_pdf")

    def test_empty_password_with_explicit_extract_permission_is_review_only(self):
        path = self.write("empty-password.pdf", fixture_pdf(encrypted="", permissions=UserAccessPermissions.EXTRACT))
        original = path.read_bytes()
        result = pdf.inspect_text_pdf(path)
        self.assert_review(result)
        self.assertIn("empty_password_encryption", result["flags"])
        self.assertEqual(result["pages"][0]["char_count"], len(SECRET_TEXT))
        self.assertEqual(result["pages"][0]["status"], "text_candidate")
        self.assertEqual(path.read_bytes(), original)

    def test_empty_password_without_copy_permission_cannot_extract_even_if_accessibility_allowed(self):
        for permissions in (UserAccessPermissions(0), UserAccessPermissions.PRINT,
                            UserAccessPermissions.EXTRACT_TEXT_AND_GRAPHICS):
            with self.subTest(permissions=int(permissions)):
                path = self.write("restricted.pdf", fixture_pdf(encrypted="", permissions=permissions))
                self.reject(path, "extraction_forbidden")

    def test_encryption_missing_or_malformed_permission_is_never_defaulted_to_allowed(self):
        for entry in ("missing", BooleanObject(True), TextStringObject("4294967292"), NumberObject(1 << 40)):
            with self.subTest(entry_type=type(entry).__name__):
                path = self.write("unknown-permissions.pdf", fixture_pdf(encrypted="", permission_entry=entry))
                # pypdf may reject the malformed encryption dictionary before
                # the permission gate; either way it must not return statistics.
                error = self.reject(path)
                self.assertIn(error.reason, {"extraction_forbidden", "corrupt_pdf", "encrypted_pdf"})

    def test_empty_document_and_malformed_or_truncated_bytes_fail_closed(self):
        self.reject(self.write("zero-pages.pdf", fixture_pdf(())), "empty_pdf")
        for data in (b"not-a-pdf", b"%PDF-1.7\n", fixture_pdf()[:80]):
            with self.subTest(size=len(data)):
                self.reject(self.write("corrupt.pdf", data), "corrupt_pdf")

    def test_nonregular_missing_symlink_and_symlink_ancestor_sources_are_rejected(self):
        missing = self.root / "missing.pdf"
        leaf = self.root / "alias.pdf"
        leaf.symlink_to(self.path)
        parent = self.root / "alias-directory"
        parent.symlink_to(self.root, target_is_directory=True)
        for path in (missing, self.root, leaf, parent / self.path.name):
            with self.subTest(name=path.name):
                self.reject(path, "invalid_source")

    def test_standard_platform_temp_aliases_accept_original_without_general_symlink_bypass(self):
        if sys.platform != "darwin":
            # Other platforms keep the normal no-symlink path contract.
            self.assertEqual(pdf.inspect_text_pdf(self.path)["page_count"], 1)
            return
        for public_root in ("/tmp", "/var"):
            canonical_root = Path("/private" + public_root)
            self.assertTrue(Path(public_root).is_symlink())
            self.assertEqual(Path(public_root).resolve(), canonical_root)
            directory = "/private/tmp" if public_root == "/tmp" else str(self.root.parent)
            with self.subTest(alias=public_root), tempfile.TemporaryDirectory(prefix="d56-platform-", dir=directory) as name:
                canonical = Path(name).resolve() / "synthetic.pdf"
                canonical.write_bytes(fixture_pdf())
                alias = Path(str(canonical).removeprefix("/private"))
                result = pdf.inspect_text_pdf(alias)
                self.assertEqual(result["source_sha256"], hashlib.sha256(canonical.read_bytes()).hexdigest())
                self.assertIs(result["eligible_for_payment"], False)

    def test_invalid_limits_are_rejected_before_launch(self):
        cases = (
            {"max_file_bytes": 0}, {"max_file_bytes": True}, {"max_pages": -1},
            {"max_pages": 1.5}, {"timeout_seconds": 0}, {"timeout_seconds": True},
            {"timeout_seconds": float("nan")}, {"timeout_seconds": float("inf")},
            {"timeout_seconds": 301}, {"max_page_chars": 0},
            {"max_total_chars": -1}, {"max_result_bytes": 0}, {"min_text_chars": 0},
        )
        with patch.object(pdf, "_launch_worker") as launch:
            for changes in cases:
                with self.subTest(changes=changes):
                    with self.assertRaises(pdf.PdfPreflightError) as caught:
                        limits = replace(pdf.PdfPreflightLimits(), **changes)
                        pdf.inspect_text_pdf(self.path, limits=limits)
                    self.assertEqual(caught.exception.reason, "invalid_limits")
            launch.assert_not_called()

    def test_file_limit_rejects_before_worker_and_source_stays_unchanged(self):
        before = self.path.read_bytes()
        with patch.object(pdf, "_launch_worker") as launch:
            self.reject(reason="file_limit", limits=replace(pdf.PdfPreflightLimits(), max_file_bytes=len(before) - 1))
            launch.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_source_mutation_during_snapshot_rejects_without_partial_parser_run(self):
        calls = 0
        original = self.path.read_bytes()
        def change_fixture_only():
            nonlocal calls
            calls += 1
            if calls == 2:
                # Simulate another writer touching this test's own source.
                self.path.write_bytes(original + b"\n")
            return False
        with patch.object(pdf, "_launch_worker") as launch:
            self.reject(reason="source_changed", cancel_check=change_fixture_only)
            launch.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original + b"\n")

    def test_page_and_text_limits_do_not_return_partial_success(self):
        path = self.write("multi.pdf", fixture_pdf(("text", "text")))
        self.reject(path, "page_limit", limits=replace(pdf.PdfPreflightLimits(), max_pages=1))
        self.reject(path, "text_limit", limits=replace(pdf.PdfPreflightLimits(), max_page_chars=40))
        self.reject(path, "text_limit", limits=replace(pdf.PdfPreflightLimits(), max_total_chars=len(SECRET_TEXT)))

    def test_cancellation_before_work_does_not_start_parser(self):
        with patch.object(pdf, "_launch_worker") as launch:
            self.reject(reason="cancelled", cancel_check=lambda: True)
            launch.assert_not_called()

    def sleeping_worker(self, snapshot, result_path, limits):
        process = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(60)"],
                                   start_new_session=True, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        self.stack.callback(self.stop_process, process)
        return process

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)

    def test_timeout_ends_owned_worker_and_does_not_kill_unrelated_process(self):
        self.processes = []
        sibling = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(60)"],
                                   start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.stack.callback(self.stop_process, sibling)
        started = time.monotonic()
        with patch.object(pdf, "_launch_worker", side_effect=self.sleeping_worker):
            self.reject(reason="timeout", limits=replace(pdf.PdfPreflightLimits(), timeout_seconds=0.5))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(len(self.processes), 1)
        self.assertIsNotNone(self.processes[0].poll())
        self.assertIsNone(sibling.poll())

    def test_midflight_cancel_and_callback_exception_cleanup_worker(self):
        for raises in (False, True):
            self.processes = []
            def check():
                if self.processes:
                    if raises:
                        raise LookupError("synthetic caller cancellation")
                    return True
                return False
            with self.subTest(raises=raises), patch.object(pdf, "_launch_worker", side_effect=self.sleeping_worker):
                if raises:
                    with self.assertRaises(LookupError):
                        pdf.inspect_text_pdf(self.path, cancel_check=check)
                else:
                    self.reject(reason="cancelled", cancel_check=check)
            self.assertEqual(len(self.processes), 1)
            self.assertIsNotNone(self.processes[0].poll())

    def test_actual_worker_uses_isolated_python_private_snapshot_and_sanitized_env(self):
        real_popen = subprocess.Popen
        calls = []
        def capture(command, *args, **kwargs):
            calls.append((command, kwargs))
            self.assertIn("-I", command)
            self.assertIn("-B", command)
            self.assertNotIn(str(self.path), command)
            self.assertTrue(kwargs["start_new_session"])
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            self.assertNotIn("ALIPAY_PRIVATE_KEY", kwargs["env"])
            self.assertNotIn("PYTHONPATH", kwargs["env"])
            return real_popen(command, *args, **kwargs)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "not-real-private", "ALIPAY_PRIVATE_KEY": "not-real-private"}), \
                patch.object(pdf.subprocess, "Popen", side_effect=capture):
            result = pdf.inspect_text_pdf(self.path)
        self.assertEqual(result["page_count"], 1)
        self.assertEqual(len(calls), 1)

    def test_parent_rejects_non_json_truncated_or_oversized_worker_results(self):
        for raw in (b"not-json", b"{", b"[]", b"null", b'{"error":"corrupt_pdf","error":"timeout"}',
                    b'{"error":"private-unknown-error"}', b'{"error":"corrupt_pdf","text":"private"}'):
            with self.subTest(raw=raw), self.inject_result(None, raw=raw):
                self.reject(reason="invalid_result")
        with self.inject_result(None, raw=b" " * 2048):
            self.reject(reason="result_limit", limits=replace(pdf.PdfPreflightLimits(), max_result_bytes=1024))

    def test_child_specific_network_fence_rejects_before_socket_connect(self):
        script = (
            "import importlib.util, pathlib, socket\n"
            "spec=importlib.util.spec_from_file_location('private_preflight', " + repr(str(Path(pdf.__file__).resolve())) + ")\n"
            "module=importlib.util.module_from_spec(spec)\n"
            "import sys; sys.modules[spec.name]=module; spec.loader.exec_module(module)\n"
            "module._child_sandbox(module.PdfPreflightLimits())\n"
            "try: socket.getaddrinfo('127.0.0.1',9)\n"
            "except PermissionError: print('DNS_REJECTED')\n"
            "else: raise AssertionError('unguarded resolver')\n"
            "connection=socket.socket()\n"
            "try: connection.connect(('127.0.0.1',9))\n"
            "except PermissionError: print('CONNECT_REJECTED')\n"
            "else: raise AssertionError('unguarded connect')\n"
            "finally: connection.close()\n"
        )
        process = subprocess.run([sys.executable, "-I", "-B", "-c", script], cwd=self.root,
                                 capture_output=True, text=True, timeout=10,
                                 env={"PATH": os.defpath, "HOME": str(self.root), "TMPDIR": str(self.root)})
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.splitlines(), ["DNS_REJECTED", "CONNECT_REJECTED"])
        self.assertEqual(process.stderr, "")

    def test_parent_rejects_malicious_schema_types_identity_and_raw_text_fields(self):
        good = pdf.inspect_text_pdf(self.path)
        cases = []
        for key, value in (
            ("schema_version", "unknown"), ("source_sha256", "0" * 64),
            ("parser_version", "0.0"), ("eligible_for_payment", True),
            ("eligible_for_payment", 0), ("page_count", True),
            ("total_char_count", -1), ("total_nonspace_count", "1"),
            ("text", SECRET_TEXT), ("status", "approved"), ("flags", ["unknown"]),
        ):
            value_result = copy.deepcopy(good)
            value_result[key] = value
            cases.append(value_result)
        for key, value in (("text", SECRET_TEXT), ("char_count", True), ("image_xobject_count", -1),
                           ("has_content_stream", 1), ("flags", ["unknown"]), ("kind", "scan")):
            value_result = copy.deepcopy(good)
            value_result["pages"][0][key] = value
            cases.append(value_result)
        for index, result in enumerate(cases):
            with self.subTest(case=index), self.inject_result(result):
                self.reject(reason="invalid_result")

    def test_parent_rejects_missing_duplicate_out_of_order_pages_and_wrong_totals(self):
        path = self.write("two.pdf", fixture_pdf(("text", "text")))
        good = pdf.inspect_text_pdf(path)
        cases = []
        for mode in ("missing", "duplicate", "order", "total", "nonspace", "status"):
            result = copy.deepcopy(good)
            if mode == "missing": result["pages"].pop()
            elif mode == "duplicate": result["pages"][1]["page_number"] = 1
            elif mode == "order": result["pages"].reverse()
            elif mode == "total": result["total_char_count"] += 1
            elif mode == "nonspace": result["pages"][0]["nonspace_count"] = result["pages"][0]["char_count"] + 1
            else: result["pages"][0]["status"] = "review_required"
            cases.append(result)
        for index, result in enumerate(cases):
            with self.subTest(case=index), self.inject_result(result):
                self.reject(path, "invalid_result")

    def test_worker_exit_failure_never_accepts_plausible_partial_result(self):
        good = pdf.inspect_text_pdf(self.path)
        with self.inject_result(good, exit_code=7):
            self.reject(reason="worker_failed")

    def test_result_symlink_is_not_followed_or_exposed(self):
        outside = self.write("private-result.json", json.dumps({"text": SECRET_TEXT}).encode())
        before = outside.read_bytes()
        def launch(snapshot, result_path, limits):
            Path(result_path).symlink_to(outside)
            return CompletedWorker()
        with patch.object(pdf, "_launch_worker", side_effect=launch):
            self.reject(reason="invalid_result")
        self.assertEqual(outside.read_bytes(), before)

    def test_repeated_preflight_does_not_create_public_artifacts_or_change_source(self):
        before = self.path.read_bytes()
        first = pdf.inspect_text_pdf(self.path)
        second = pdf.inspect_text_pdf(self.path)
        self.assertEqual(first, second)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.root.iterdir()], [self.path.name])

    def test_existing_public_input_policy_still_refuses_pdf(self):
        self.assertNotIn(".pdf", SUPPORTED_EXTENSIONS)
        for name in ("book.pdf", "book.PDF"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, PDF_DISABLED_MESSAGE):
                validate_filename(name)
        self.assertTrue(is_pdf_header(b"%PDF-1.7\n"))
        self.assertTrue(is_pdf_header(b"\xef\xbb\xbf\n%PDF-1.7\n"))
        self.assertFalse(is_pdf_header(b"PK\x03\x04"))

    def run_cli(self, source, *extra):
        command = [sys.executable, "-I", "-B", str(Path(__file__).resolve().parents[1] / "scripts" / "inspect-text-pdf.py"),
                   str(source), *map(str, extra)]
        return subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=15,
                              env={"PATH": os.defpath, "HOME": str(self.root), "TMPDIR": str(self.root), "LANG": "C"})

    def test_cli_creates_only_new_private_statistics_report_and_preserves_source_sha(self):
        before = self.path.read_bytes()
        report_path = self.root / "report.json"
        process = self.run_cli(self.path, "--report", report_path)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout, "")
        report = json.loads(report_path.read_text())
        self.assertEqual(report["source_sha256"], hashlib.sha256(before).hexdigest())
        self.assertIs(report["eligible_for_payment"], False)
        self.assertEqual(report_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn(SECRET_TEXT, report_path.read_text() + process.stdout + process.stderr)
        self.assertEqual({p.name for p in self.root.iterdir()}, {self.path.name, report_path.name})

    def test_cli_refuses_existing_report_without_overwrite_or_private_body_leak(self):
        original = b"private existing report must stay unchanged"
        report_path = self.write("existing.json", original)
        process = self.run_cli(self.path, "--report", report_path)
        self.assertEqual(process.returncode, 3)
        self.assertEqual(report_path.read_bytes(), original)
        self.assertNotIn(SECRET_TEXT, process.stdout + process.stderr)
        self.assertNotIn(original.decode(), process.stdout + process.stderr)

    def test_cli_malformed_and_symlink_source_fail_safely_without_report(self):
        bad = self.write("malformed-private-source.pdf", b"%PDF-1.7\n" + SECRET_TEXT.encode())
        link = self.root / "source-alias.pdf"
        link.symlink_to(self.path)
        for source, reason in ((bad, "corrupt_pdf"), (link, "invalid_source")):
            with self.subTest(reason=reason):
                report_path = self.root / (reason + ".json")
                process = self.run_cli(source, "--report", report_path)
                self.assertEqual(process.returncode, 2)
                result = json.loads(process.stdout)
                self.assertEqual(result["reason"], reason)
                self.assertIs(result["eligible_for_payment"], False)
                self.assertFalse(report_path.exists())
                self.assertNotIn(SECRET_TEXT, process.stdout + process.stderr)
                self.assertNotIn(str(self.root), process.stdout + process.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
