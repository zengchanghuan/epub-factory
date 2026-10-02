"""Release preflight contract tests: local temporary files, mocked tool boundary."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("release_runtime", Path(__file__).with_name("release_runtime.py"))
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "backend").mkdir()
        self.lock = self.root / "backend/requirements.lock"
        self.lock.write_text("# local pins\nlxml==5.1.0\nSQLAlchemy==2.0.49\nuvicorn[standard]==0.35.0\n")
        self.jar = self.root / "epubcheck.jar"
        self.jar.write_bytes(b"local test jar, never executed")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("python", "node", "java"):
            path = self.bin / name
            path.write_text("#!/bin/false\n")
            path.chmod(0o700)
        self.python = self.bin / "python"
        self.node = self.bin / "node"
        self.probe = {"python": "3.10.12", "python_full": "3.10.12 (local test)",
            "sqlite": "3.37.2", "lxml": "5.1.0.0", "libxml_compiled": "2.12.3",
            "libxml_runtime": "2.12.3", "libxslt_compiled": "1.1.39", "libxslt_runtime": "1.1.39",
            "packages": {"lxml": ["5.1.0"], "sqlalchemy": ["2.0.49"], "uvicorn": ["0.35.0"],
                         "pytest": ["8.3.2"]}}
        self.answers = {"node": "v22.16.0\n", "java": 'openjdk version "17.0.15" 2025-04-15\n',
                        "epubcheck": "EPUBCheck v5.1.0\n"}
        self.calls = []
        environment = patch.dict(os.environ, {"PATH": str(self.bin), "PYTHONPATH": "secret-import-path",
            "PYTHONHOME": "secret-home", "DYLD_LIBRARY_PATH": "secret-library",
            "JAVA_TOOL_OPTIONS": "secret-java", "JDK_JAVA_OPTIONS": "secret-jdk",
            "OPENAI_API_KEY": "secret-token", "HTTP_PROXY": "https://secret.invalid"})
        environment.start()
        self.addCleanup(environment.stop)
        for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo"):
            guard = patch(target, side_effect=AssertionError("No networking in preflight tests"))
            guard.start(); self.addCleanup(guard.stop)
        boundary = patch.object(runtime.subprocess, "run", side_effect=self.fake_run)
        self.run = boundary.start(); self.addCleanup(boundary.stop)

    def fake_run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if "-c" in argv:
            text = json.dumps(self.probe)
        elif "-jar" in argv:
            text = self.answers["epubcheck"]
        elif "-version" in argv:
            return subprocess.CompletedProcess(argv, 0, "", self.answers["java"])
        else:
            text = self.answers["node"]
        return subprocess.CompletedProcess(argv, 0, text, "")

    def inspect(self):
        return runtime.inspect_runtime(self.root, str(self.python), str(self.node), self.jar)

    def test_exact_runtime_lock_and_local_hashes_with_extra_dev_packages(self):
        result = self.inspect()
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["requirements_drift"], [])
        self.assertEqual(result["extra_packages"], {"pytest": "8.3.2"})
        self.assertEqual(result["key_packages"]["lxml"], "5.1.0")
        self.assertEqual(result["versions"]["libxml_runtime"], "2.12.3")
        self.assertEqual(result["lock_sha256"], hashlib.sha256(self.lock.read_bytes()).hexdigest())
        self.assertEqual(result["jar_sha256"], hashlib.sha256(self.jar.read_bytes()).hexdigest())
        self.assertRegex(result["packages_sha256"], r"^[0-9a-f]{64}$")

    def test_probe_isolated_environment_timeouts_and_no_app_import(self):
        self.inspect()
        self.assertEqual(len(self.calls), 4)
        homes = set()
        for argv, options in self.calls:
            self.assertEqual(options["timeout"], 20)
            self.assertFalse(options["check"])
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
            env = options["env"]
            self.assertEqual(set(env), {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ"})
            self.assertEqual(options["cwd"], env["HOME"])
            homes.add(env["HOME"])
            self.assertNotIn("secret", json.dumps(env))
        self.assertEqual(len(homes), 1)
        self.assertFalse(Path(next(iter(homes))).exists())
        command = self.calls[0][0]
        self.assertEqual(command[1:4], ["-I", "-B", "-c"])
        self.assertIn("importlib.metadata", command[4])
        self.assertNotIn("import app", command[4])
        self.assertNotIn("dotenv", command[4])

    def test_explicit_virtualenv_symlink_entry_is_not_replaced_by_base_python(self):
        venv = self.root / "venv/bin"
        venv.mkdir(parents=True)
        link = venv / "python"
        link.symlink_to(self.python)
        self.python = link
        result = self.inspect()
        self.assertTrue(result["ok"])
        self.assertEqual(self.calls[0][0][0], str(link))
        self.assertEqual(result["executables"]["python"]["path"], str(link))
        self.assertEqual(result["executables"]["python"]["resolved_path"], str(link.resolve()))

    def test_bare_tools_search_path_and_relative_tools_use_repo_root(self):
        result = runtime.inspect_runtime(self.root, "bin/python", "node", "epubcheck.jar")
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(self.calls[0][0][0], str(self.python))
        self.assertEqual(result["executables"]["java"]["path"], str(self.bin / "java"))

    def test_package_drift_and_missing_pin_fail_not_extra_dev_packages(self):
        self.probe["packages"]["lxml"] = ["5.2.0"]
        del self.probe["packages"]["sqlalchemy"]
        result = self.inspect()
        self.assertFalse(result["ok"])
        self.assertEqual(result["requirements_drift"], [
            {"package": "lxml", "expected": "5.1.0", "installed": "5.2.0"},
            {"package": "sqlalchemy", "expected": "2.0.49", "installed": None}])

    def test_normalized_names_and_extras_are_distribution_pins(self):
        self.lock.write_text("LXML==5.1.0\nSQLAlchemy==2.0.49\nuvicorn[standard,watch]==0.35.0\nfoo_bar.baz==1.2.0\n")
        self.probe["packages"]["foo-bar-baz"] = ["1.2.0"]
        self.assertTrue(self.inspect()["ok"])

    def test_invalid_lock_syntax_never_executes_requirements(self):
        for line in ("-r secret.txt", "foo>=1.0", "foo==1.*", "foo @ https://private.invalid/token",
                     "foo==1.0;python_version>'3'", "foo==1.0 #comment", "foo==1.0\nFOO==1.0", ""):
            with self.subTest(line=line):
                self.lock.write_text(line)
                result = self.inspect()
                self.assertFalse(result["ok"])
                self.assertIn("requirements_lock_invalid", [error["code"] for error in result["errors"]])
                self.assertNotIn("private.invalid", json.dumps(result))

    def test_wrong_pinned_tool_versions_all_fail_explicitly(self):
        self.probe["python"] = "3.10.13"
        self.answers.update(node="v23.1.0", java='java version "21.0.1"', epubcheck="EPUBCheck v5.2.0")
        result = self.inspect()
        self.assertFalse(result["ok"])
        self.assertEqual({error["code"] for error in result["errors"]},
                         {"python_version", "node_version", "java_version", "epubcheck_version"})

    def test_timeout_or_failure_never_exposes_stderr_or_exception_environment(self):
        self.run.side_effect = subprocess.TimeoutExpired("secret-token", 20, output="secret-token", stderr="secret-token")
        result = self.inspect()
        self.assertEqual(len(result["errors"]), 4)
        self.assertTrue(all(error["code"].endswith("_timeout") for error in result["errors"]))
        self.assertNotIn("secret-token", json.dumps(result))
        self.run.side_effect = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 2, "secret-token", "secret-token")
        self.assertNotIn("secret-token", json.dumps(self.inspect()))

    def test_untrusted_version_strings_or_ambiguous_metadata_fail_closed(self):
        original = copy.deepcopy(self.probe)
        for field, value in (("python", "https://secret.invalid/token"), ("sqlite", "secret-token"),
                             ("packages", {"lxml": ["5.1.0", "5.2.0"]}),
                             ("packages", {"lxml": ["https://secret.invalid/token"]})):
            self.probe = {**original, field: value}
            result = self.inspect()
            self.assertFalse(result["ok"])
            self.assertIn("python_probe_failed", [error["code"] for error in result["errors"]])
            self.assertNotIn("secret.invalid", json.dumps(result))
        self.probe = original
        self.answers["epubcheck"] = "secret-token"
        self.assertNotIn("secret-token", json.dumps(self.inspect()))

    def test_missing_executables_or_jar_fail_without_installation(self):
        self.python.unlink()
        (self.bin / "java").unlink()
        self.jar.unlink()
        result = self.inspect()
        self.assertFalse(result["ok"])
        self.assertEqual({error["code"] for error in result["errors"]},
                         {"python_unavailable", "java_unavailable", "epubcheck_jar_invalid"})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0][1:], ["--version"])

    def test_real_epubcheck_version_summary_format_is_strictly_recognized(self):
        self.answers["epubcheck"] = ("EPUBCheck v5.1.0\n"
            "Messages: 0 fatals / 0 errors / 0 warnings / 0 infos\n\nEPUBCheck completed\n")
        self.assertTrue(self.inspect()["ok"])
        for text in ("EPUBCheck v5.1.0\nEPUBCheck v5.1.0", "EPUBCheck v5.1.0\narbitrary text",
                     "EPUBCheck v5.1.0\nMessages: 0 fatals / 1 errors / 0 warnings / 0 infos\nEPUBCheck completed",
                     "prefix EPUBCheck v5.1.0", "EPUBCheck v5.1.0\nEPUBCheck completed"):
            self.answers["epubcheck"] = text
            result = self.inspect()
            self.assertFalse(result["ok"])
            self.assertIn("epubcheck_probe_failed", [error["code"] for error in result["errors"]])


class BookTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "backend").mkdir()
        self.uploads, self.outputs, self.baselines = [self.root / name for name in ("uploads", "outputs", "baselines")]
        for path in (self.uploads, self.outputs, self.baselines): path.mkdir()
        self.books, self.baseline_hashes = [], {}
        for index in range(3):
            key = f"book-{index}"
            source = f"opaque input {index}".encode()
            output = f"opaque prior output {index}".encode()
            baseline = f"opaque baseline {index}".encode()
            digest = hashlib.sha256(source).hexdigest()
            self.books.append({"key": key, "input": f"input-{index}.epub", "input_sha256": digest,
                "output": f"output-{index}.epub", "output_sha256": hashlib.sha256(output).hexdigest()})
            (self.uploads / self.books[-1]["input"]).write_bytes(source)
            (self.outputs / self.books[-1]["output"]).write_bytes(output)
            directory = self.baselines / digest[:12]
            directory.mkdir()
            (directory / "converted.epub").write_bytes(baseline)
            self.baseline_hashes[key] = hashlib.sha256(baseline).hexdigest()
        self.write_manifests()
        for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo"):
            guard = patch(target, side_effect=AssertionError("Networking forbidden"))
            guard.start(); self.addCleanup(guard.stop)

    def write_manifests(self):
        self.book_manifest = self.root / "backend/test_d37_entitlement_history.py"
        self.baseline_manifest = self.root / "backend/test_d54_infra_history.py"
        self.book_manifest.write_text("raise RuntimeError('MUST NOT IMPORT')\nBOOKS = " + repr(tuple(self.books)))
        self.baseline_manifest.write_text("raise RuntimeError('MUST NOT IMPORT')\nBASELINE_SHA256 = " + repr(self.baseline_hashes))

    def inspect(self):
        return runtime.inspect_books(self.root, self.uploads, self.outputs, self.baselines)

    def test_nine_fixed_hashes_literal_read_does_not_import_tests(self):
        before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.root.rglob("*") if path.is_file()}
        with patch.object(runtime.subprocess, "run", side_effect=AssertionError("Book check is read-only")):
            result = self.inspect()
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(len(result["files"]), 9)
        self.assertEqual({row["role"] for row in result["files"]}, {"input", "output", "baseline"})
        self.assertTrue(all(row["sha256"] == row["expected_sha256"] for row in result["files"]))
        self.assertEqual({str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.root.rglob("*") if path.is_file()}, before)

    def test_missing_fixture_is_failure_not_skip(self):
        (self.uploads / self.books[0]["input"]).unlink()
        result = self.inspect()
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["files"]), 9)
        self.assertEqual(result["errors"][0]["code"], "history_file_missing")
        self.assertIsNone(result["files"][0]["sha256"])

    def test_changed_old_artifact_and_baseline_both_fail(self):
        (self.outputs / self.books[0]["output"]).write_bytes(b"changed")
        (self.baselines / self.books[1]["input_sha256"][:12] / "converted.epub").write_bytes(b"changed")
        result = self.inspect()
        self.assertFalse(result["ok"])
        self.assertEqual([error["code"] for error in result["errors"]], ["history_hash_mismatch"] * 2)

    def test_symlink_leaf_and_directory_never_followed(self):
        leaf = self.uploads / self.books[0]["input"]
        copied = self.root / "outside.epub"
        copied.write_bytes(leaf.read_bytes())
        leaf.unlink(); leaf.symlink_to(copied)
        directory = self.baselines / self.books[1]["input_sha256"][:12]
        target = self.root / "outside-baseline"
        directory.rename(target); directory.symlink_to(target, target_is_directory=True)
        result = self.inspect()
        self.assertFalse(result["ok"])
        self.assertEqual([error["code"] for error in result["errors"]], ["history_file_unsafe"] * 2)

    def test_traversal_absolute_and_backslash_manifest_paths_fail(self):
        for value in ("../outside.epub", str(self.uploads / "input-0.epub"), "a\\file.epub", "a/../input-0.epub"):
            self.books[0]["input"] = value
            self.write_manifests()
            result = self.inspect()
            self.assertFalse(result["ok"])
            self.assertIn("history_file_unsafe", [error["code"] for error in result["errors"]])

    def test_fifo_is_rejected_without_blocking(self):
        path = self.uploads / self.books[0]["input"]
        path.unlink(); os.mkfifo(path)
        result = self.inspect()
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][0]["code"], "history_file_unsafe")

    def test_manifest_must_be_unique_literal_exactly_three_matching_books(self):
        for text in ("BOOKS = make_books()", "BOOKS = ()", "BOOKS = ()\nBOOKS = ()", "BOOKS = [broken syntax"):
            self.book_manifest.write_text(text)
            result = self.inspect()
            self.assertFalse(result["ok"])
            self.assertEqual(result["errors"][0]["code"], "history_manifest_invalid")
        self.write_manifests()
        self.baseline_manifest.write_text("BASELINE_SHA256 = {}")
        self.assertFalse(self.inspect()["ok"])

    def test_missing_fixture_root_is_failure_and_does_not_create_it(self):
        missing = self.root / "missing"
        result = runtime.inspect_books(self.root, missing, self.outputs, self.baselines)
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"][0]["code"], "input_directory_invalid")
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
