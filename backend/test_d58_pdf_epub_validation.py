"""D58 isolated EPUB validation transport, not a real Java EPUBCheck run.

Real -I Python subprocesses execute a private fake Java transport. The shared
validator still owns every JSON/exit-code rule. No app configuration is loaded.
"""
from contextlib import ExitStack
from dataclasses import asdict
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

from app.domain import pdf_epub_validation as validation
from app.domain import pdf_text_preflight as guard


class PdfEpubValidationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="d58-validator-test-"))).resolve()
        self.epub = self.root / "synthetic.epub"
        self.jar = self.root / "synthetic.jar"
        self.epub.write_bytes(b"synthetic transport fixture; not a validated EPUB")
        self.jar.write_bytes(b"synthetic Java transport placeholder")
        self.stack.enter_context(patch.object(tempfile, "tempdir", str(self.root)))
        self.guards = [self.stack.enter_context(patch.object(socket.socket, method, side_effect=AssertionError("network forbidden")))
                       for method in ("connect", "connect_ex", "sendto", "sendmsg")]
        self.guards.append(self.stack.enter_context(patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden"))))
        self.addCleanup(lambda: [mock.assert_not_called() for mock in self.guards])
        self.counter = 0

    def fake_java(self, report=None, *, code=0, prelude="", tail=""):
        self.counter += 1
        directory = self.root / ("bin-" + str(self.counter))
        directory.mkdir()
        path = directory / "java"
        script = f"#!{sys.executable}\nimport json,os,sys,time,subprocess\nfrom pathlib import Path\n"
        script += prelude + "\n"
        if report is not None:
            script += f"Path(sys.argv[-1]).write_text({json.dumps(json.dumps(report))})\n"
        script += tail + f"\nraise SystemExit({code})\n"
        path.write_text(script)
        path.chmod(0o700)
        return path

    def call(self, java, *, seconds=4, cancel_check=None):
        with patch.object(validation.shutil, "which", return_value=str(java) if java else None):
            return validation.validate_pdf_epub(self.epub, self.jar, deadline=time.monotonic() + seconds,
                                                 cancel_check=cancel_check)

    def good_report(self, warnings=0):
        return {"messages": [{"severity": "WARNING", "message": "synthetic"} for _ in range(warnings)],
                "checker": {"nFatal": 0, "nError": 0, "nWarning": warnings}}

    def assert_no_worker_directories(self):
        self.assertEqual(list(self.root.glob("pdf_epubcheck_*")), [])

    def assert_gone(self, pid):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(.025)
        self.fail("owned validation descendant still exists")

    def test_real_isolated_child_uses_shared_success_and_warning_semantics(self):
        result = self.call(self.fake_java(self.good_report(2)))
        self.assertEqual(asdict(result), {"passed": True, "message": "EPUB 校验通过，2 个警告",
                                         "error_code": None, "warnings": 2})
        self.assert_no_worker_directories()

    def test_shared_failure_invalid_report_and_exit_rules_are_reused(self):
        cases = [({"messages": [{"severity": "ERROR"}], "checker": {"nFatal": 0, "nError": 1, "nWarning": 0}}, 1, "EPUB_VALIDATION_FAILED"),
                 ({"messages": [], "checker": {"nFatal": 0, "nError": 1, "nWarning": 0}}, 0, "EPUB_VALIDATION_UNAVAILABLE"),
                 (self.good_report(), 3, "EPUB_VALIDATION_UNAVAILABLE"),
                 ({"messages": [{"severity": "PRIVATE_SOURCE_UNKNOWN"}], "checker": {"nFatal": 0, "nError": 0, "nWarning": 0}}, 0, "EPUB_VALIDATION_UNAVAILABLE"),
                 (None, 0, "EPUB_VALIDATION_UNAVAILABLE")]
        for report, code, expected in cases:
            with self.subTest(expected=expected):
                result = self.call(self.fake_java(report, code=code))
                self.assertFalse(result.passed)
                self.assertEqual(result.error_code, expected)
                self.assertNotIn("PRIVATE_SOURCE", result.message)
        self.assert_no_worker_directories()

    def test_missing_java_is_checked_in_real_child_not_parent_environment_fallback(self):
        result = self.call(None)
        self.assertFalse(result.passed)
        self.assertEqual(result.error_code, "EPUB_VALIDATION_UNAVAILABLE")
        self.assertIn("Java", result.message)
        self.assert_no_worker_directories()

    def test_missing_jar_and_missing_epub_keep_shared_error_precedence(self):
        self.jar.unlink()
        result = self.call(None)
        self.assertEqual(result.error_code, "EPUB_VALIDATION_UNAVAILABLE")
        self.assertIn("EPUBCheck", result.message)
        self.epub.unlink()
        result = self.call(None)
        self.assertEqual(result.error_code, "EPUB_VALIDATION_FAILED")
        self.assert_no_worker_directories()

    def test_child_environment_contains_no_credentials_configs_or_java_options(self):
        observed = self.root / "observed.json"
        java = self.fake_java(self.good_report(), prelude=f"Path({str(observed)!r}).write_text(json.dumps(dict(os.environ)))")
        secrets = {"OPENAI_API_KEY": "PRIVATE_SOURCE_SECRET", "DATABASE_URL": "PRIVATE_SOURCE_DB",
                   "SMTP_PASSWORD": "PRIVATE_SOURCE_MAIL", "JAVA_TOOL_OPTIONS": "PRIVATE_SOURCE_JAVA",
                   "_JAVA_OPTIONS": "PRIVATE_SOURCE_JAVA", "JDK_JAVA_OPTIONS": "PRIVATE_SOURCE_JAVA",
                   "PYTHONPATH": "PRIVATE_SOURCE_PATH", "PYTHONSTARTUP": "PRIVATE_SOURCE_STARTUP",
                   "HTTP_PROXY": "PRIVATE_SOURCE_PROXY", "HOME": "PRIVATE_SOURCE_HOME"}
        with patch.dict(os.environ, secrets):
            self.assertTrue(self.call(java).passed)
        environment = json.loads(observed.read_text())
        self.assertFalse(set(secrets) - {"HOME"} & set(environment))
        self.assertTrue(environment["HOME"].startswith(str(self.root / "pdf_epubcheck_")))
        self.assertTrue(environment["PATH"].startswith(str(java.parent)))
        self.assertNotIn("PRIVATE_SOURCE", str(environment))
        self.assert_no_worker_directories()

    def test_parent_launcher_selects_isolated_python_and_owned_process_group(self):
        report = self.root / "result.json"
        java = self.fake_java()
        with patch.object(validation.subprocess, "Popen") as launch:
            validation._launch(self.epub, self.jar, report, str(java))
        args, kwargs = launch.call_args
        self.assertEqual(args[0][:3], [sys.executable, "-I", "-B"])
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(set(kwargs["env"]), {"PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL"})
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)

    def test_cancel_before_launch_does_not_start_any_process(self):
        with patch.object(validation, "_launch") as launch, self.assertRaises(guard.PdfPreflightError) as caught:
            validation.validate_pdf_epub(self.epub, self.jar, deadline=time.monotonic() + 10, cancel_check=lambda: True)
        self.assertEqual(caught.exception.reason, "cancelled")
        launch.assert_not_called()

    def test_actual_running_java_cancel_is_prompt_and_removes_owned_group(self):
        pidfile = self.root / "java.pid"
        java = self.fake_java(prelude=f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\ntime.sleep(30)")
        start = time.monotonic()
        with self.assertRaises(guard.PdfPreflightError) as caught:
            self.call(java, cancel_check=pidfile.exists)
        self.assertEqual(caught.exception.reason, "cancelled")
        self.assertLess(time.monotonic() - start, 2.5)
        self.assert_gone(int(pidfile.read_text()))
        self.assert_no_worker_directories()

    def test_actual_running_java_obeys_total_deadline_before_shared_60_seconds(self):
        pidfile = self.root / "java.pid"
        java = self.fake_java(prelude=f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\ntime.sleep(30)")
        start = time.monotonic()
        with self.assertRaises(guard.PdfPreflightError) as caught:
            self.call(java, seconds=.8)
        self.assertEqual(caught.exception.reason, "timeout")
        self.assertLess(time.monotonic() - start, 2.5)
        self.assert_gone(int(pidfile.read_text()))
        self.assert_no_worker_directories()

    def test_successful_python_exit_still_cleans_up_leftover_java_descendant(self):
        pidfile = self.root / "descendant.pid"
        prelude = "child=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(30)'])\n"
        prelude += f"Path({str(pidfile)!r}).write_text(str(child.pid))"
        java = self.fake_java(self.good_report(), prelude=prelude)
        self.assertTrue(self.call(java).passed)
        self.assert_gone(int(pidfile.read_text()))
        self.assert_no_worker_directories()

    def test_cancel_after_result_read_is_not_returned_as_success(self):
        cancelled = [False]
        original = validation._read_result
        def read(path):
            value = original(path)
            cancelled[0] = True
            return value
        with patch.object(validation, "_read_result", side_effect=read), self.assertRaises(guard.PdfPreflightError) as caught:
            self.call(self.fake_java(self.good_report()), cancel_check=lambda: cancelled[0])
        self.assertEqual(caught.exception.reason, "cancelled")
        self.assert_no_worker_directories()

    def test_deadline_after_result_read_is_not_returned_as_success(self):
        original = validation._read_result
        def read(path):
            value = original(path)
            time.sleep(.6)
            return value
        with patch.object(validation, "_read_result", side_effect=read), self.assertRaises(guard.PdfPreflightError) as caught:
            self.call(self.fake_java(self.good_report()), seconds=.5)
        self.assertEqual(caught.exception.reason, "timeout")
        self.assert_no_worker_directories()

    def test_ipc_result_rejects_unknown_fields_duplicate_keys_bad_types_and_size(self):
        good = {"passed": True, "message": "valid", "error_code": None, "warnings": 0}
        bad = [{**good, "manuscript": "PRIVATE_SOURCE"}, {**good, "passed": 1},
               {**good, "warnings": True}, {**good, "warnings": -1},
               {**good, "message": "x\nPRIVATE_SOURCE"}, {**good, "error_code": "EPUB_VALIDATION_FAILED"},
               {**good, "passed": False}, {**good, "message": "x" * 513}]
        path = self.root / "envelope.json"
        for value in bad:
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                validation._read_result(path)
        for raw in (b'{"passed":true,"passed":true,"message":"ok","error_code":null,"warnings":0}',
                    b" " * (validation._RESULT_LIMIT + 1), b'{"passed":NaN}'):
            path.write_bytes(raw)
            with self.assertRaises(ValueError):
                validation._read_result(path)
        link = self.root / "envelope-link.json"
        link.symlink_to(path)
        with self.assertRaises(OSError):
            validation._read_result(link)

    def test_large_java_report_and_stdout_are_bounded_without_leaking_output(self):
        prelude = "sys.stdout.write('PRIVATE_SOURCE'*100000);sys.stderr.write('PRIVATE_SOURCE'*100000)\n"
        prelude += f"Path(sys.argv[-1]).write_bytes(b'X'*({validation._JAVA_REPORT_LIMIT}+1))"
        result = self.call(self.fake_java(prelude=prelude))
        self.assertFalse(result.passed)
        self.assertEqual(result.error_code, "EPUB_VALIDATION_UNAVAILABLE")
        self.assertNotIn("PRIVATE_SOURCE", result.message)
        self.assert_no_worker_directories()

    def test_invalid_and_already_elapsed_deadlines_fail_before_launch(self):
        for value, reason in ((True, "invalid_limits"), (float("inf"), "invalid_limits"),
                              (float("nan"), "invalid_limits"), ("30", "invalid_limits"),
                              (time.monotonic() - 1, "timeout")):
            with self.subTest(reason=reason), patch.object(validation, "_launch") as launch:
                with self.assertRaises(guard.PdfPreflightError) as caught:
                    validation.validate_pdf_epub(self.epub, self.jar, deadline=value)
                self.assertEqual(caught.exception.reason, reason)
                launch.assert_not_called()

    def test_loading_shared_validator_does_not_import_engine_or_configuration(self):
        before = set(sys.modules)
        shared = validation._load_shared_validator()
        self.assertTrue(callable(shared.validate_epub))
        self.assertFalse({"app.engine", "app.engine.compiler", "app.main", "app.storage", "dotenv"} & (set(sys.modules) - before))


if __name__ == "__main__":
    unittest.main()
