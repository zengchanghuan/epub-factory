"""Offline release-gate safety contracts; temporary miniature repositories only."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("release_gate_test_subject", SCRIPTS / "release-gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="epub-release-gate-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.tracked = set()
        # No git writes: emulate only local tracked-name/revision reads against
        # this synthetic tree, while all file/copy/process logic remains real.
        self.git = patch.object(gate, "_git", side_effect=self.git_read)
        self.git.start()
        self.addCleanup(self.git.stop)

    def git_read(self, repo, *arguments):
        if arguments == ("ls-files", "--cached", "-z"):
            return ("\0".join(sorted(self.tracked)) + "\0").encode()
        if arguments[0] == "ls-files":
            return ("\0".join(str(path.relative_to(repo)) for path in sorted(repo.rglob("*"))
                              if path.is_file() or path.is_symlink()) + "\0").encode()
        if arguments[0] == "rev-parse":
            return b"0000000000000000000000000000000000000000\n"
        if arguments[0] == "status":
            return b""
        raise AssertionError("Unexpected git read")

    def file(self, name, content="source fixture\n", *, root=None):
        path = (root or self.repo) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def miniature(self, script=None):
        self.file("backend/run_regression.py", "D_SUITE = ['test_tiny.py']\nC_SUITE = []\n")
        self.file("backend/test_tiny.py", script or (
            "import unittest\nclass Tiny(unittest.TestCase):\n"
            " def test_pass(self): self.assertTrue(True)\n"
            "if __name__ == '__main__': unittest.main()\n"))
        self.file("backend/app/__init__.py", "")
        self.file("frontend/tests/runner.js", "// Synthetic runner for fake node only\n")
        self.file("frontend/tests/test_tiny.js", "// tiny fixture only\n")
        self.file("README.md", "# Synthetic source checkout\n")
        shutil.copytree(SCRIPTS / "release_guard", self.repo / "scripts/release_guard")
        return self.repo

    def run_main(self, *, name="main-evidence", profile="offline", book_result=None, book_paths=True):
        evidence = self.root / name
        jar = self.file("fixture.jar", "not executed", root=self.root)
        # CI need not have Node installed: this executable is only a controlled
        # successful transport boundary, never a frontend-behavior claim.
        node = self.file("fake-node", f"#!{sys.executable}\nprint('Results: 1 passed, 0 failed')\n", root=self.root)
        node.chmod(0o700)
        helper = SimpleNamespace(
            inspect_runtime=lambda *args: {"ok": True, "versions": {"fixture": True}, "errors": [],
                                          "executables": {"python": {"path": sys.executable},
                                                          "node": {"path": str(node)}}},
            inspect_books=lambda *args: book_result or {"ok": True, "errors": [], "files": []})
        arguments = ["--python", sys.executable, "--node", str(node), "--epubcheck-jar", str(jar),
                     "--evidence-dir", str(evidence), "--profile", profile]
        if profile == "history" and book_paths:
            for option in ("uploads", "outputs", "baselines"):
                folder = self.root / option
                folder.mkdir(exist_ok=True)
                arguments.extend(["--" + option, str(folder)])
        with patch.object(gate, "ROOT", self.repo), patch.object(gate, "load_runtime_helper", return_value=helper), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = gate.main(arguments)
        report_path = evidence / "report.json"
        return code, json.loads(report_path.read_text()) if report_path.exists() else None

    def environment(self, name="command"):
        evidence = self.root / name
        evidence.mkdir()
        snapshot = evidence / "current"
        snapshot.mkdir()
        shutil.copytree(SCRIPTS / "release_guard", snapshot / "scripts/release_guard")
        jar = self.file("fixture.jar", "not executed", root=self.root)
        env = gate.sanitized_environment(evidence, snapshot, jar)
        env.update(RELEASE_GATE_EVENTS=str(evidence / "events.jsonl"),
                   RELEASE_GATE_NETWORK_LOG=str(evidence / "network.jsonl"))
        return evidence, snapshot, env

    def dotenv_environment(self):
        evidence, snapshot, env = self.environment("dotenv-command")
        permitted = Path(env["RELEASE_GATE_DOTENV_ROOT"])
        self.assertEqual(permitted, (evidence / "tmp").resolve())
        return evidence, snapshot, env, permitted

    def run_python_fixture(self, evidence, snapshot, env, source, *, cwd=None):
        script = self.file("dotenv-probe.py", textwrap.dedent(source), root=snapshot)
        result = gate.run_command([sys.executable, str(script)], cwd=cwd or snapshot, env=env,
                                  log_path=evidence / "dotenv.log", timeout=15)
        self.assertEqual(result["returncode"], 0, Path(result["log"]).read_text())
        self.assertFalse(result["timed_out"])
        return result

    def test_source_policy_includes_code_but_excludes_credentials_books_and_runtime(self):
        for name in ("backend/app/main.py", "backend/test_example.py", "frontend/index.html",
                     "frontend/app.js", "scripts/check.sh", "README.md", "backend/.env.example"):
            with self.subTest(name=name):
                self.assertTrue(gate.included_source(name))
        for name in (".env", "backend/.env", "backend/.env.production", "frontend/.env.example",
                     "backend/.env.example.local", "backend/secrets.pem",
                     "keys/private.key", "backend/uploads/book.epub", "backend/outputs/result.epub",
                     "backend/book.pdf", "backend/orders.db", "backend/orders.db-wal",
                     "backend/orders.db-shm", "backend/cache.sqlite", "epub_jobs.db.schema-init.lock",
                     ".mcp-qq.lock", "backend/__pycache__/private.pyc", "backend/.venv/private.py"):
            with self.subTest(name=name):
                self.assertFalse(gate.included_source(name))

    def test_exact_tracked_public_environment_template_is_snapshotted_unchanged(self):
        template = self.file("backend/.env.example", "# Public placeholder template\nALIPAY_PRIVATE_KEY=\n")
        self.tracked.add("backend/.env.example")
        self.file("backend/.env", "PRIVATE_CANARY")
        self.file("frontend/.env.example", "PRIVATE_CANARY")
        self.file("backend/.env.example.local", "PRIVATE_CANARY")
        destination = self.root / "snapshot"
        manifest = gate.snapshot_sources(self.repo, destination)
        self.assertEqual(set(manifest), {"backend/.env.example"})
        self.assertEqual(manifest["backend/.env.example"], digest(template))
        self.assertEqual((destination / "backend/.env.example").read_bytes(), template.read_bytes())

    def test_untracked_file_named_public_environment_template_is_rejected(self):
        template = self.file("backend/.env.example", "PRIVATE_UNTRACKED_CANARY")
        before = digest(template)
        destination = self.root / "snapshot"
        with self.assertRaises((ValueError, RuntimeError)):
            gate.snapshot_sources(self.repo, destination)
        self.assertFalse((destination / "backend/.env.example").exists())
        self.assertEqual(digest(template), before)

    def test_snapshot_contains_only_selected_files_and_matches_bytes(self):
        self.file("backend/app/main.py", "SYNTHETIC = True\n")
        self.file("frontend/index.html", "<h1>fixture</h1>\n")
        self.file("backend/.env", "PRIVATE_CANARY=must-not-copy\n")
        self.file("backend/uploads/book.epub", "private book")
        self.file("backend/runtime.db", "runtime")
        before = {str(path.relative_to(self.repo)): path.read_bytes() for path in self.repo.rglob("*") if path.is_file()}
        destination = self.root / "snapshot"
        manifest = gate.snapshot_sources(self.repo, destination)
        self.assertEqual(set(manifest), {"backend/app/main.py", "frontend/index.html"})
        for name, expected in manifest.items():
            self.assertEqual(digest(destination / name), expected)
            self.assertEqual((destination / name).read_bytes(), before[name])
        self.assertEqual(before, {str(path.relative_to(self.repo)): path.read_bytes() for path in self.repo.rglob("*") if path.is_file()})

    def test_source_symlink_cannot_smuggle_private_bytes_under_a_python_name(self):
        secret = self.file("outside.env", "PRIVATE_CANARY", root=self.root)
        target = self.repo / "backend/app/main.py"
        target.parent.mkdir(parents=True)
        target.symlink_to(secret)
        with self.assertRaises((ValueError, RuntimeError)):
            gate.snapshot_sources(self.repo, self.root / "snapshot")
        self.assertEqual(secret.read_text(), "PRIVATE_CANARY")

    def test_source_directory_symlink_is_rejected_without_traversal(self):
        outside = self.root / "private"
        outside.mkdir()
        self.file("main.py", "SECRET", root=outside)
        (self.repo / "backend").mkdir()
        (self.repo / "backend/app").symlink_to(outside, target_is_directory=True)
        with patch.object(gate, "_git", return_value=b"backend/app/main.py\0"):
            with self.assertRaises((ValueError, RuntimeError)):
                gate.snapshot_sources(self.repo, self.root / "snapshot")

    def test_snapshot_refuses_reusing_existing_destination(self):
        self.file("backend/app/main.py", "fresh source")
        destination = self.root / "previous-snapshot"
        destination.mkdir()
        marker = self.file("existing.py", "must remain", root=destination)
        with self.assertRaises((ValueError, RuntimeError, FileExistsError)):
            gate.snapshot_sources(self.repo, destination)
        self.assertEqual(marker.read_text(), "must remain")

    def test_evidence_is_new_external_directory_never_repo_or_existing_data(self):
        inside = self.repo / "reports/new"
        existing = self.root / "existing"
        existing.mkdir()
        marker = self.file("keep.txt", "do not overwrite", root=existing)
        for candidate in (self.repo, inside, existing):
            with self.subTest(candidate=candidate), self.assertRaises((ValueError, RuntimeError)):
                gate.prepare_evidence(self.repo, candidate)
        self.assertFalse(inside.exists())
        self.assertEqual(marker.read_text(), "do not overwrite")
        new = self.root / "new-evidence"
        self.assertEqual(gate.prepare_evidence(self.repo, new), new)
        self.assertTrue(new.is_dir())

    def test_evidence_symlink_ancestor_is_rejected_even_when_target_is_external(self):
        outside = self.root / "outside"
        outside.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        with self.assertRaises((ValueError, RuntimeError)):
            gate.prepare_evidence(self.repo, alias / "evidence")
        self.assertFalse((outside / "evidence").exists())

    def test_environment_never_inherits_provider_payment_email_or_proxy_secrets(self):
        private = {key: "PRIVATE_ENV_CANARY" for key in (
            "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ALIPAY_PRIVATE_KEY", "ALIPAY_APP_ID",
            "SMTP_PASSWORD", "DATABASE_URL", "REDIS_URL", "CELERY_BROKER_URL",
            "HTTPS_PROXY", "AWS_SECRET_ACCESS_KEY", "PYTHONPATH", "DYLD_LIBRARY_PATH")}
        with patch.dict(os.environ, private):
            evidence, snapshot, env = self.environment()
        self.assertNotIn("PRIVATE_ENV_CANARY", json.dumps(env))
        self.assertEqual(env["SKIP_PAYMENT_CHECK"], "1")
        self.assertEqual(env["OWNER_PAYMENT_EMAIL_ENABLED"], "1")
        self.assertIn(str(evidence), env["DATABASE_URL"])
        self.assertIn(str(evidence), env["HOME"])
        self.assertTrue(Path(env["RELEASE_GATE_GUARD_DIR"]).is_dir())
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep)[0], env["RELEASE_GATE_GUARD_DIR"])

    def test_explicit_regular_dotenv_under_fixed_tmp_root_loads_and_honors_override(self):
        evidence, snapshot, env, permitted = self.dotenv_environment()
        fixture = self.file("case/config.env", "D55_FIXTURE_FRESH=中文\nD55_FIXTURE_EXISTING=file\n", root=permitted)
        env["D55_FIXTURE_EXISTING"] = "exported"
        self.run_python_fixture(evidence, snapshot, env, f"""
            import os
            from pathlib import Path
            from dotenv import load_dotenv
            path = Path({str(fixture)!r})
            assert load_dotenv(path, override=False, encoding='utf-8') is True
            assert os.environ['D55_FIXTURE_FRESH'] == '中文'
            assert os.environ['D55_FIXTURE_EXISTING'] == 'exported'
            assert load_dotenv(dotenv_path=path, override=True) is True
            assert os.environ['D55_FIXTURE_EXISTING'] == 'file'
        """)

    def test_dotenv_missing_default_empty_path_and_stream_do_not_load_anything(self):
        evidence, snapshot, env, permitted = self.dotenv_environment()
        fixture = self.file(".env", "D55_DENIED_DISCOVERY=secret-canary\n", root=permitted)
        self.run_python_fixture(evidence, snapshot, env, f"""
            import io, os
            from pathlib import Path
            from dotenv import load_dotenv
            assert load_dotenv() is False
            assert load_dotenv(None) is False
            assert load_dotenv('') is False
            assert load_dotenv(Path({str(permitted / 'missing.env')!r})) is False
            assert load_dotenv(stream=io.StringIO('D55_DENIED_STREAM=private')) is False
            assert load_dotenv(Path({str(fixture)!r}), stream=io.StringIO('D55_DENIED_STREAM=private')) is False
            assert 'D55_DENIED_STREAM' not in os.environ
            assert 'D55_DENIED_DISCOVERY' not in os.environ
        """, cwd=permitted)

    def test_dotenv_outside_repo_and_other_tmp_files_are_rejected_before_open(self):
        evidence, snapshot, env, permitted = self.dotenv_environment()
        outside = self.file("outside-private.env", "D55_OUTSIDE_PRIVATE=private-canary\n")
        other = self.file("other-temp/other-private.env", "D55_OTHER_PRIVATE=private-canary\n", root=self.root)
        self.run_python_fixture(evidence, snapshot, env, f"""
            import os, sys
            from dotenv import load_dotenv
            forbidden = {{{str(outside)!r}, {str(other)!r}, 'outside-private.env', 'other-private.env'}}
            opened = []
            def audit(event, args):
                if event == 'open' and args[0] in forbidden:
                    opened.append(args[0])
                    raise AssertionError('Outside dotenv must not be opened')
            sys.addaudithook(audit)
            for path in ({str(outside)!r}, {str(other)!r}):
                assert load_dotenv(path, override=True) is False
            assert not opened
            assert 'D55_OUTSIDE_PRIVATE' not in os.environ
            assert 'D55_OTHER_PRIVATE' not in os.environ
        """)

    def test_dotenv_leaf_and_parent_symlinks_cannot_enter_allowlist(self):
        evidence, snapshot, env, permitted = self.dotenv_environment()
        inside = self.file("regular/allowed.env", "D55_LINKED_PRIVATE=private-canary\n", root=permitted)
        outside = self.file("outside/blocked.env", "D55_LINKED_PRIVATE=private-canary\n", root=self.root)
        leaf_inside = permitted / "leaf-inside.env"
        leaf_inside.symlink_to(inside)
        leaf_outside = permitted / "leaf-outside.env"
        leaf_outside.symlink_to(outside)
        parent_inside = permitted / "parent-inside"
        parent_inside.symlink_to(inside.parent, target_is_directory=True)
        parent_outside = permitted / "parent-outside"
        parent_outside.symlink_to(outside.parent, target_is_directory=True)
        paths = [str(leaf_inside), str(leaf_outside), str(parent_inside / inside.name), str(parent_outside / outside.name)]
        self.run_python_fixture(evidence, snapshot, env, f"""
            import os
            from dotenv import load_dotenv
            for path in {paths!r}:
                assert load_dotenv(path, override=True) is False, 'Symlink dotenv must be rejected'
            assert 'D55_LINKED_PRIVATE' not in os.environ
        """)

    def test_dotenv_root_is_captured_before_child_changes_tmpdir_or_guard_environment(self):
        evidence, snapshot, env, permitted = self.dotenv_environment()
        inside = self.file("allowed.env", "D55_FIXED_ROOT=allowed\n", root=permitted)
        outside = self.file("other-temp/outside.env", "D55_OUTSIDE_PRIVATE=private-canary\n", root=self.root)
        self.run_python_fixture(evidence, snapshot, env, f"""
            import os
            from dotenv import load_dotenv
            os.environ['TMPDIR'] = {str(outside.parent)!r}
            os.environ['RELEASE_GATE_DOTENV_ROOT'] = {str(outside.parent)!r}
            os.chdir({str(outside.parent)!r})
            assert load_dotenv({str(outside)!r}, override=True) is False
            assert 'D55_OUTSIDE_PRIVATE' not in os.environ
            assert load_dotenv({str(inside)!r}, override=True) is True
            assert os.environ['D55_FIXED_ROOT'] == 'allowed'
        """)

    def test_dotenv_fixed_root_and_network_guard_survive_empty_and_forged_grandchild_env(self):
        evidence, snapshot, env, permitted = self.dotenv_environment()
        inside = self.file("allowed.env", "D55_FIXED_ROOT=allowed\n", root=permitted)
        outside = self.file("other-temp/outside.env", "D55_OUTSIDE_PRIVATE=private-canary\n", root=self.root)
        child = self.file("dotenv-grandchild.py", textwrap.dedent(f"""
            import os, socket
            from dotenv import load_dotenv
            assert os.environ['RELEASE_GATE_DOTENV_ROOT'] == {str(permitted)!r}
            assert load_dotenv({str(outside)!r}, override=True) is False
            assert 'D55_OUTSIDE_PRIVATE' not in os.environ
            assert load_dotenv({str(inside)!r}, override=True) is True
            assert os.environ['D55_FIXED_ROOT'] == 'allowed'
            try:
                socket.getaddrinfo('127.0.0.1', 9)
            except RuntimeError:
                pass
            else:
                raise AssertionError('Inherited network guard is missing')
        """), root=snapshot)
        self.run_python_fixture(evidence, snapshot, env, f"""
            import os, subprocess, sys
            os.environ['TMPDIR'] = {str(outside.parent)!r}
            os.environ['RELEASE_GATE_DOTENV_ROOT'] = {str(outside.parent)!r}
            minimal = {{'PATH': os.environ.get('PATH', ''), 'TMPDIR': {str(outside.parent)!r},
                        'RELEASE_GATE_DOTENV_ROOT': {str(outside.parent)!r}}}
            for environment in (minimal, dict(os.environ)):
                result = subprocess.run([sys.executable, {str(child)!r}], env=environment,
                                        capture_output=True, text=True, timeout=10)
                assert result.returncode == 0, result.stderr
        """)
        events = [json.loads(line) for line in Path(env["RELEASE_GATE_EVENTS"]).read_text().splitlines()]
        self.assertGreaterEqual(sum(event.get("kind") == "guard_started" for event in events), 3)
        network = Path(env["RELEASE_GATE_NETWORK_LOG"]).read_text()
        self.assertEqual(len(network.splitlines()), 2)
        self.assertNotIn("private-canary", network)
        self.assertNotIn("127.0.0.1", network)

    def test_catalog_is_read_as_ast_never_executed_and_reports_actual_count(self):
        self.miniature()
        marker = self.root / "must-not-execute"
        self.file("backend/run_regression.py", (
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('forbidden')\n"
            "D_SUITE = ['test_tiny.py']\nC_SUITE = []\n"))
        scripts = gate.catalog_scripts(self.repo)
        self.assertEqual(scripts, ["test_tiny.py"])
        self.assertFalse(marker.exists())
        self.assertNotEqual(len(scripts), 104)

    def test_catalog_rejects_missing_duplicate_escaping_or_non_literal_entries(self):
        self.miniature()
        for expression in ("['missing.py']", "['test_tiny.py', 'test_tiny.py']",
                           "['../private.py']", "list(['test_tiny.py'])"):
            self.file("backend/run_regression.py", f"D_SUITE = {expression}\nC_SUITE = []\n")
            with self.subTest(expression=expression), self.assertRaises((ValueError, RuntimeError)):
                gate.catalog_scripts(self.repo)

    @staticmethod
    def outcome(*, failures=0, errors=0, skipped=0, tests_run=1):
        return {"kind": "unittest_result", "tests_run": tests_run,
                "failures": failures, "errors": errors, "skipped": skipped}

    def test_zero_exit_cannot_hide_recorded_test_failure_or_timeout(self):
        event = self.outcome()
        self.assertTrue(gate.classify_log("test_tiny.py", "Ran 1 test\nOK\n", 0, test_events=[event])["ok"])
        for events, code, timed_out in (([self.outcome(failures=1)], 0, False),
                                        ([event], 1, False), ([event], 0, True)):
            self.assertFalse(gate.classify_log("test_tiny.py", "OK\n", code,
                                             timed_out=timed_out, test_events=events)["ok"])

    def test_only_exact_allowlisted_skip_identity_and_reason_are_accepted_offline(self):
        script = "test_d29_translation_performance.py"
        skip = {"kind": "skip", "test_id": "__main__.PerformanceTests.test_real_book_resume_fingerprint_is_stable_and_catches_image_bullet_change",
                "reason": "selected real source book not provided"}
        events = [skip, self.outcome(skipped=1)]
        allowed = gate.classify_log(script, "OK (skipped=1)\n", 0, test_events=events)
        self.assertTrue(allowed["ok"])
        self.assertEqual(allowed["skipped"], 1)
        self.assertEqual(allowed["unexpected_skips"], [])
        for name, event in (("test_tiny.py", skip), (script, {**skip, "reason": "different reason"}),
                            (script, {**skip, "test_id": "__main__.PerformanceTests.test_new_uncovered_case"})):
            self.assertFalse(gate.classify_log(name, "OK (skipped=1)\n", 0,
                                             test_events=[event, self.outcome(skipped=1)])["ok"])
        self.assertFalse(gate.classify_log(script, "OK (skipped=1)\n", 0,
                                         profile="history", test_events=events)["ok"])

    def test_manual_skip_or_unverified_skip_count_is_not_a_pass(self):
        for text, events in (("⏭️ SKIP: missing fixture\nResults: 1 passed, 0 failed\n", []),
                             ("OK (skipped=1)\n", [self.outcome(skipped=1)])):
            self.assertFalse(gate.classify_log("test_tiny.py", text, 0, test_events=events)["ok"])

    def test_history_requires_positive_observed_test_count_not_just_zero_exit(self):
        for text, events in (("OK\n", []), ("Ran 0 tests\nOK\n", [self.outcome(tests_run=0)])):
            self.assertFalse(gate.classify_log("test_history.py", text, 0,
                                             profile="history", test_events=events)["ok"])

    def test_frontend_requires_one_nonempty_successful_results_summary(self):
        good = gate.classify_log("test_front.js", "Results: 2 passed, 0 failed\n", 0)
        self.assertTrue(good["ok"])
        self.assertEqual(good["tests_run"], 2)
        for text in ("nothing ran\n", "Results: 0 passed, 0 failed\n", "Results: 2 passed, 1 failed\n",
                     "Results: 2 passed, 0 failed\nResults: 3 passed, 0 failed\n"):
            with self.subTest(text=text):
                self.assertFalse(gate.classify_log("test_front.js", text, 0)["ok"])

    def test_main_tiny_catalog_passes_real_child_guard_and_reports_two_not_104(self):
        self.miniature()
        code, report = self.run_main()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["expected_catalog"], {"backend": 1, "frontend": 1, "history": 0})
        self.assertEqual(report["summary"]["commands"], 2)
        self.assertEqual(report["summary"]["passed"], 2)
        self.assertEqual(report["summary"]["skipped_methods"], 0)
        self.assertTrue(any(event.get("kind") == "guard_started" for event in report["commands"][0]["test_events"]))

    def test_main_failed_test_is_not_masked_by_successful_frontend(self):
        self.miniature("import unittest\nclass Tiny(unittest.TestCase):\n def test_fail(self): self.fail('expected')\nunittest.main()\n")
        code, report = self.run_main()
        self.assertNotEqual(code, 0)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["summary"]["commands"], 2)
        self.assertEqual(report["summary"]["passed"], 1)
        self.assertFalse(report["commands"][0]["ok"])

    def test_main_history_requires_explicit_paths_and_validated_pins_before_tests(self):
        self.miniature()
        code, report = self.run_main(name="no-pins", profile="history", book_paths=False)
        self.assertEqual(code, 2)
        self.assertIsNone(report)
        code, report = self.run_main(name="bad-pins", profile="history", book_result={
            "ok": False, "errors": [{"code": "book_missing", "message": "Synthetic missing fixture"}], "files": []})
        self.assertNotEqual(code, 0)
        self.assertEqual(report["commands"], [])
        self.assertEqual(report["books"]["errors"][0]["code"], "book_missing")

    def test_main_history_does_not_accept_any_skipped_method(self):
        self.miniature()
        self.file("backend/test_d55_order_review_history.py", (
            "import unittest\nclass History(unittest.TestCase):\n"
            " @unittest.skip('fixture missing')\n def test_real(self): pass\nunittest.main()\n"))
        code, report = self.run_main(profile="history")
        self.assertNotEqual(code, 0)
        self.assertEqual(report["expected_catalog"]["history"], 1)
        result = next(item for item in report["commands"] if item["profile"] == "history")
        self.assertEqual(result["skipped"], 1)
        self.assertFalse(result["ok"])

    def test_main_history_reports_success_only_with_positive_zero_skip_result(self):
        self.miniature()
        self.file("backend/test_d55_order_review_history.py", (
            "import unittest\nclass History(unittest.TestCase):\n"
            " def test_synthetic_contract(self): self.assertTrue(True)\nunittest.main()\n"))
        code, report = self.run_main(profile="history")
        self.assertEqual(code, 0, report)
        history = next(item for item in report["commands"] if item["profile"] == "history")
        self.assertEqual(history["tests_run"], 1)
        self.assertEqual(history["skipped"], 0)
        self.assertEqual(report["summary"]["commands"], 3)

    def test_main_rejects_original_source_mutation_after_snapshot(self):
        original = self.repo / "README.md"
        self.miniature(f"from pathlib import Path\nPath({str(original)!r}).write_text('changed during test')\n")
        code, report = self.run_main()
        self.assertNotEqual(code, 0)
        self.assertEqual(report["status"], "failed")
        self.assertIn("README.md", report["source_changed"])

    def test_main_rejects_mutation_of_frozen_source_even_when_original_is_unchanged(self):
        self.miniature("from pathlib import Path\n(Path(__file__).parent.parent/'README.md').write_text('snapshot changed')\n")
        original_hash = digest(self.repo / "README.md")
        code, report = self.run_main()
        self.assertNotEqual(code, 0)
        self.assertIn("README.md", report["source_changed"])
        self.assertEqual(digest(self.repo / "README.md"), original_hash)

    def test_main_source_membership_change_is_a_failure_even_when_existing_hashes_match(self):
        new_source = self.repo / "backend/app/new.py"
        self.miniature(f"from pathlib import Path\nPath({str(new_source)!r}).write_text('NEW = True')\n")
        code, report = self.run_main()
        self.assertNotEqual(code, 0, report)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(new_source.exists())

    def test_main_deleted_original_source_is_not_reported_as_passed(self):
        deleted = self.repo / "README.md"
        self.miniature(f"from pathlib import Path\nPath({str(deleted)!r}).unlink()\n")
        code, report = self.run_main()
        self.assertNotEqual(code, 0, report)
        self.assertEqual(report["status"], "failed")

    def test_main_reports_workspace_database_creation_as_failure_without_deleting_it(self):
        database = self.repo / "epub_jobs.db"
        self.miniature(f"from pathlib import Path\nPath({str(database)!r}).write_bytes(b'synthetic private runtime')\n")
        code, report = self.run_main()
        self.assertNotEqual(code, 0)
        self.assertEqual(report["status"], "failed")
        self.assertNotEqual(report["protected_before"], report["protected_after"])
        self.assertEqual(database.read_bytes(), b"synthetic private runtime")

    def test_main_cannot_report_success_if_a_test_catches_a_network_rejection(self):
        self.miniature("import socket\ntry: socket.getaddrinfo('127.0.0.1', 9)\nexcept BaseException: pass\n")
        code, report = self.run_main()
        self.assertNotEqual(code, 0)
        self.assertTrue(report["commands"][0]["guard_failure"])
        self.assertTrue(report["commands"][0]["network_events"])

    def test_real_child_guard_blocks_socket_and_records_no_destination_secret(self):
        evidence, snapshot, env = self.environment()
        script = self.file("child.py", (
            "import socket,sys\n"
            "assert 'sitecustomize' in sys.modules\n"
            "blocked = 0\n"
            "for action in (lambda: socket.socket().connect(('127.0.0.1', 9)), "
            "lambda: socket.getaddrinfo('127.0.0.1', 9), "
            "lambda: socket.gethostbyname('localhost'), "
            "lambda: socket.gethostbyaddr('127.0.0.1'), "
            "lambda: socket.getnameinfo(('127.0.0.1', 9), 0), "
            "lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendmsg([b'PRIVATE_NETWORK_CANARY'], [], 0, ('127.0.0.1', 9)), "
            "lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'PRIVATE_NETWORK_CANARY', ('127.0.0.1', 9))):\n"
            " try: action()\n"
            " except BaseException: blocked += 1\n"
            "assert blocked == 7\nprint('network blocked')\n"), root=snapshot)
        result = gate.run_command([sys.executable, str(script)], cwd=snapshot, env=env,
                                  log_path=evidence / "guard.log", timeout=10)
        self.assertEqual(result["returncode"], 0, Path(result["log"]).read_text())
        network = Path(env["RELEASE_GATE_NETWORK_LOG"]).read_text()
        self.assertNotIn("PRIVATE_NETWORK_CANARY", network)
        self.assertNotIn("127.0.0.1", network)
        self.assertGreaterEqual(len(network.splitlines()), 7)

    def test_guard_and_skip_audit_are_inherited_by_real_python_grandchild(self):
        evidence, snapshot, env = self.environment()
        child = self.file("nested.py", (
            "import socket,sys,unittest\n"
            "assert 'sitecustomize' in sys.modules\n"
            "class Example(unittest.TestCase):\n"
            " @unittest.skip('synthetic unexpected skip')\n"
            " def test_skipped(self): pass\n"
            "unittest.main()\n"), root=snapshot)
        script = self.file("parent.py", (
            "import subprocess,sys\n"
            f"raise SystemExit(subprocess.run([sys.executable, {str(child)!r}], env={{'PATH':'/usr/bin:/bin'}}).returncode)\n"), root=snapshot)
        result = gate.run_command([sys.executable, str(script)], cwd=snapshot, env=env,
                                  log_path=evidence / "nested.log", timeout=10)
        self.assertEqual(result["returncode"], 0, Path(result["log"]).read_text())
        events = [json.loads(line) for line in Path(env["RELEASE_GATE_EVENTS"]).read_text().splitlines()]
        self.assertTrue(any(item.get("kind") == "skip" and item.get("test_id", "").endswith("test_skipped") for item in events))

    def test_child_cannot_disable_guard_with_python_startup_flags(self):
        evidence, snapshot, env = self.environment()
        for flag in ("-I", "-E", "-S"):
            with self.subTest(flag=flag):
                marker = evidence / (flag + ".executed")
                try:
                    result = gate.run_command([sys.executable, flag, "-c",
                        f"from pathlib import Path; Path({str(marker)!r}).write_text('unguarded')"],
                        cwd=snapshot, env=env, log_path=evidence / (flag + ".log"), timeout=10)
                except (ValueError, RuntimeError):
                    result = None
                if result is not None:
                    self.assertNotEqual(result["returncode"], 0)
                self.assertFalse(marker.exists())

    def test_timeout_stops_only_owned_process_group_and_preserves_diagnostics(self):
        evidence, snapshot, env = self.environment()
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        self.addCleanup(lambda: (unrelated.terminate(), unrelated.wait(timeout=5)) if unrelated.poll() is None else None)
        child_pid = evidence / "child.pid"
        script = self.file("hang.py", (
            "import signal,subprocess,sys,time\nfrom pathlib import Path\n"
            "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "def stop(*args):\n child.wait(timeout=1)\n raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            f"Path({str(child_pid)!r}).write_text(str(child.pid))\n"
            "print('owned child started', flush=True)\ntime.sleep(30)\n"), root=snapshot)
        result = gate.run_command([sys.executable, str(script)], cwd=snapshot, env=env,
                                  log_path=evidence / "timeout.log", timeout=2)
        self.assertTrue(result["timed_out"])
        self.assertIsNone(unrelated.poll())
        self.assertIn("owned child started", Path(result["log"]).read_text())
        pid = int(child_pid.read_text())
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("timed-out owned grandchild is still running")


if __name__ == "__main__":
    unittest.main(verbosity=2)
