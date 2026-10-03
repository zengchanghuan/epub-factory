"""Pure catalog CLI tests using temporary miniature repositories without Git.

The real catalog is read as AST only. No application module, historical book,
database or real regression script is imported or executed.
"""
import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile


ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "backend" / "run_regression.py"


def catalog_literals(source):
    result = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"D_SUITE", "C_SUITE"}:
                    result[target.id] = ast.literal_eval(node.value)
    return result


def replace_catalog(source, d_suite, c_suite):
    lines = source.splitlines(keepends=True)
    replacements = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in {"D_SUITE", "C_SUITE"}:
                value = d_suite if name == "D_SUITE" else c_suite
                replacements.append((node.lineno - 1, node.end_lineno, f"{name} = {value!r}\n"))
    if len(replacements) != 2:
        raise AssertionError("catalog fixture could not locate both literal suites")
    for start, end, text in sorted(replacements, reverse=True):
        lines[start:end] = [text]
    return "".join(lines)


class RegressionCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="regression-catalog-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.backend = self.root / "backend"
        self.backend.mkdir()
        self.source = RUNNER.read_text(encoding="utf-8")
        app = self.backend / "app"
        app.mkdir()
        (app / "__init__.py").write_text(
            "from pathlib import Path\nPath(__file__).parents[2].joinpath('APP_IMPORTED').touch()\n"
            "raise AssertionError('application import is forbidden in catalog listing')\n", encoding="utf-8")
        self.configure(["test_d_fixture.py"], ["test_c_fixture.py"])

    def configure(self, d_suite, c_suite, *, create=True):
        (self.backend / "run_regression.py").write_text(replace_catalog(self.source, d_suite, c_suite), encoding="utf-8")
        if create:
            for suite in (d_suite, c_suite):
                if type(suite) is list:
                    for name in suite:
                        if type(name) is str and name.startswith("test_") and name.endswith(".py") and Path(name).name == name and "\\" not in name:
                            path = self.backend / name
                            if not path.exists() and not path.is_symlink():
                                path.write_text("from pathlib import Path\nPath(__file__).parents[1].joinpath('TEST_EXECUTED').touch()\n"
                                                "raise AssertionError('listing must not execute tests')\n", encoding="utf-8")

    def run_cli(self, *arguments):
        return subprocess.run([sys.executable, "-B", str(self.backend / "run_regression.py"), *arguments],
                              cwd=self.root, capture_output=True, text=True, timeout=10,
                              env={"PATH": os.defpath, "HOME": str(self.root), "LANG": "C"})

    def assert_refused(self, *arguments):
        result = self.run_cli(*(arguments or ("--list",)))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.strip())
        self.assertNotIn(str(self.root), result.stderr)
        self.assertFalse((self.root / "APP_IMPORTED").exists())
        self.assertFalse((self.root / "TEST_EXECUTED").exists())
        return result

    def test_current_110_catalog_including_d17_and_d59_matches_ast_without_running_real_scripts(self):
        suites = catalog_literals(self.source)
        expected = suites["D_SUITE"] + suites["C_SUITE"]
        self.assertEqual(len(expected), 110)
        self.assertEqual({name for name in expected if name.startswith("test_d59_")}, {
            "test_d59_pdf_product.py", "test_d59_pdf_execution.py", "test_d59_pdf_store.py",
            "test_d59_pdf_checkout.py", "test_d59_pdf_api.py",
        })
        self.assertEqual(len(set(expected)), len(expected))
        d17 = [name for name in expected if name.startswith("test_d17_")]
        self.assertEqual(len(d17), 1)
        self.configure(suites["D_SUITE"], suites["C_SUITE"])
        result = self.run_cli("--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), expected)
        self.assertEqual(result.stderr, "")
        self.assertFalse((self.root / ".git").exists())
        self.assertFalse((self.root / "APP_IMPORTED").exists())
        self.assertFalse((self.root / "TEST_EXECUTED").exists())
        for name in expected:
            self.assertTrue((ROOT / "backend" / name).is_file())
            self.assertFalse((ROOT / "backend" / name).is_symlink())

    def test_list_preserves_d_then_c_order_and_has_no_header_or_application_import(self):
        d_suite = ["test_d_z.py", "test_d_a.py"]
        c_suite = ["test_c_b.py", "test_c_a.py"]
        self.configure(d_suite, c_suite)
        result = self.run_cli("--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "\n".join(d_suite + c_suite) + "\n")
        self.assertEqual(result.stderr, "")
        self.assertFalse((self.root / "APP_IMPORTED").exists())
        self.assertFalse((self.root / "TEST_EXECUTED").exists())

    def test_suite_container_requires_lists(self):
        for bad in (None, (), {}, "test_d_fixture.py", True):
            for category in ("D_SUITE", "C_SUITE"):
                with self.subTest(category=category, bad_type=type(bad).__name__):
                    self.configure(bad if category == "D_SUITE" else ["test_d_fixture.py"],
                                   bad if category == "C_SUITE" else ["test_c_fixture.py"])
                    self.assert_refused()

    def test_one_group_may_be_empty_but_empty_combined_catalog_is_rejected(self):
        for d_suite, c_suite in (([], ["test_c_fixture.py"]), (["test_d_fixture.py"], [])):
            with self.subTest(d_suite=d_suite):
                self.configure(d_suite, c_suite)
                result = self.run_cli("--list")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), d_suite + c_suite)
        self.configure([], [])
        self.assert_refused()

    def test_entries_require_exact_strings(self):
        for bad in (None, True, 17, ["test_nested.py"], {"name": "test_nested.py"}):
            with self.subTest(bad_type=type(bad).__name__):
                self.configure(["test_d_fixture.py", bad], ["test_c_fixture.py"])
                self.assert_refused()

    def test_duplicate_within_or_across_suites_refuses_before_any_stdout(self):
        for d_suite, c_suite in ((["test_d_fixture.py"] * 2, ["test_c_fixture.py"]),
                                 (["test_d_fixture.py"], ["test_c_fixture.py", "test_d_fixture.py"])):
            with self.subTest(d_count=len(d_suite), c_count=len(c_suite)):
                self.configure(d_suite, c_suite)
                self.assert_refused()

    def test_path_traversal_absolute_nested_and_non_python_names_are_rejected(self):
        for bad in ("../private.py", "/tmp/private.py", "nested/test_nested.py", "nested\\test_nested.py",
                    "test_a.py/..", "test_a.py\nPRIVATE_SECRET", "test_a;PRIVATE_SECRET.py", "test_a.txt", "private.py"):
            with self.subTest(name=bad):
                self.configure(["test_d_fixture.py", bad], ["test_c_fixture.py"])
                result = self.assert_refused()
                self.assertNotIn("PRIVATE_SECRET", result.stderr)

    def test_missing_late_file_does_not_print_earlier_valid_entries(self):
        self.configure(["test_d_fixture.py", "test_missing.py"], ["test_c_fixture.py"], create=False)
        self.assert_refused()

    def test_directory_named_like_script_is_not_a_regular_file(self):
        (self.backend / "test_directory.py").mkdir()
        self.configure(["test_d_fixture.py", "test_directory.py"], ["test_c_fixture.py"], create=False)
        self.assert_refused()

    def test_symlink_to_inside_or_outside_file_is_rejected_without_following(self):
        outside = self.root / "outside-private.py"
        outside.write_text("raise AssertionError('do not execute or follow')\n", encoding="utf-8")
        alias = self.backend / "test_alias.py"
        for target in (self.backend / "test_d_fixture.py", outside):
            with self.subTest(inside=target.parent == self.backend):
                alias.symlink_to(target)
                self.configure(["test_d_fixture.py", "test_alias.py"], ["test_c_fixture.py"], create=False)
                self.assert_refused()
                alias.unlink()

    def test_invalid_arguments_do_not_fall_through_to_test_execution(self):
        self.assert_refused("--unknown")
        self.assert_refused("--lis")
        self.assert_refused("--list", "unexpected")

    def test_default_run_validates_complete_catalog_before_first_test(self):
        self.configure(["test_d_fixture.py", "test_missing.py"], ["test_c_fixture.py"], create=False)
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.root / "TEST_EXECUTED").exists())


class CiFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ci-fixture-contract-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.scripts = self.root / "tools" / "scripts"
        self.scripts.mkdir(parents=True)
        for name in ("prepare-ci-fixture.py", "release-gate.py"):
            (self.scripts / name).write_bytes((ROOT / "scripts" / name).read_bytes())
        self.tree = self.make_tree("disposable")

    def make_tree(self, name):
        tree = self.root / name
        backend = tree / "backend"
        backend.mkdir(parents=True)
        (backend / "run_regression.py").write_text("raise AssertionError('catalog must not execute during fixture creation')\n", encoding="utf-8")
        app = backend / "app"
        app.mkdir()
        (app / "__init__.py").write_text("raise AssertionError('app must not import during fixture creation')\n", encoding="utf-8")
        return tree

    def command(self, *arguments):
        return [sys.executable, "-B", str(self.scripts / "prepare-ci-fixture.py"), *map(str, arguments)]

    def run_cli(self, *arguments):
        return subprocess.run(self.command(*arguments), cwd=self.root, capture_output=True, text=True, timeout=10,
                              env={"PATH": os.defpath, "HOME": str(self.root), "LANG": "C"})

    def assert_rejected(self, *arguments):
        result = self.run_cli(*arguments)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.strip())
        self.assertNotIn(str(self.root), result.stderr)
        return result

    def assert_real_synthetic_zip(self, target):
        with zipfile.ZipFile(target) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(set(archive.namelist()), {"mimetype", "META-INF/container.xml", "EPUB/package.opf",
                                                       "EPUB/chapter.xhtml", "EPUB/nav.xhtml", "EPUB/style.css"})
            self.assertEqual(archive.namelist()[0], "mimetype")
            self.assertEqual(archive.read("mimetype"), b"application/epub+zip")
            self.assertEqual(archive.getinfo("mimetype").compress_type, zipfile.ZIP_STORED)
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            rootfile = container.find("{*}rootfiles/{*}rootfile")
            self.assertEqual(rootfile.attrib["full-path"], "EPUB/package.opf")
            package = ET.fromstring(archive.read(rootfile.attrib["full-path"]))
            self.assertEqual(package.find("{*}metadata/{http://purl.org/dc/elements/1.1/}title").text, "Synthetic fixture")
            self.assertEqual(package.find("{*}metadata/{http://purl.org/dc/elements/1.1/}identifier").text, "release-gate-synthetic")
            ET.fromstring(archive.read("EPUB/chapter.xhtml"))
            ET.fromstring(archive.read("EPUB/nav.xhtml"))
            self.assertIn(b"A short synthetic sentence", archive.read("EPUB/chapter.xhtml"))

    def test_explicit_disposable_root_creates_real_zip_without_importing_catalog_or_app(self):
        before = {str(p.relative_to(self.tree)): p.read_bytes() for p in self.tree.rglob("*") if p.is_file()}
        result = self.run_cli("--root", self.tree)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not a historical customer book", result.stdout)
        self.assert_real_synthetic_zip(self.tree / "backend" / "test_en.epub")
        for relative, value in before.items():
            self.assertEqual((self.tree / relative).read_bytes(), value)
        after = {str(p.relative_to(self.tree)) for p in self.tree.rglob("*") if p.is_file()}
        self.assertEqual(after, set(before) | {"backend/test_en.epub"})
        self.assertFalse((self.tree / ".git").exists())

    def test_cli_requires_explicit_existing_complete_root(self):
        self.assert_rejected()
        self.assert_rejected("--root", self.root / "not-existing")
        self.assert_rejected("--root", self.tree / "backend" / "..")
        empty = self.root / "empty"
        empty.mkdir()
        self.assert_rejected("--root", empty)
        missing_catalog = self.make_tree("missing-catalog")
        (missing_catalog / "backend" / "run_regression.py").unlink()
        self.assert_rejected("--root", missing_catalog)
        self.assertFalse((self.tree / "backend" / "test_en.epub").exists())

    def test_existing_file_directory_and_broken_link_targets_are_never_overwritten(self):
        original = b"this may be a user's actual book; preserve exactly"
        for kind in ("file", "directory", "broken-link"):
            tree = self.make_tree(kind)
            target = tree / "backend" / "test_en.epub"
            if kind == "file": target.write_bytes(original)
            elif kind == "directory": target.mkdir()
            else: target.symlink_to(self.root / "missing-real-book")
            with self.subTest(kind=kind):
                self.assert_rejected("--root", tree)
                if kind == "file": self.assertEqual(target.read_bytes(), original)
                elif kind == "directory": self.assertTrue(target.is_dir())
                else:
                    self.assertTrue(target.is_symlink())
                    self.assertFalse((self.root / "missing-real-book").exists())

    def test_existing_symlink_target_does_not_modify_its_destination(self):
        destination = self.root / "private-original.epub"
        original = b"private original bytes"
        destination.write_bytes(original)
        target = self.tree / "backend" / "test_en.epub"
        target.symlink_to(destination)
        self.assert_rejected("--root", self.tree)
        self.assertTrue(target.is_symlink())
        self.assertEqual(destination.read_bytes(), original)

    def test_root_and_intermediate_directory_symlinks_are_rejected(self):
        direct = self.root / "root-alias"
        direct.symlink_to(self.tree, target_is_directory=True)
        ancestor = self.root / "ancestor-alias"
        ancestor.symlink_to(self.root, target_is_directory=True)
        for path in (direct, ancestor / self.tree.name):
            with self.subTest(root=path.name):
                self.assert_rejected("--root", path)
        self.assertFalse((self.tree / "backend" / "test_en.epub").exists())

    def test_backend_and_catalog_symlinks_are_rejected(self):
        external = self.make_tree("external")
        linked_backend = self.root / "linked-backend"
        linked_backend.mkdir()
        (linked_backend / "backend").symlink_to(external / "backend", target_is_directory=True)
        self.assert_rejected("--root", linked_backend)
        catalog = self.tree / "backend" / "run_regression.py"
        catalog.unlink()
        catalog.symlink_to(external / "backend" / "run_regression.py")
        self.assert_rejected("--root", self.tree)
        self.assertFalse((external / "backend" / "test_en.epub").exists())

    def test_git_checkout_or_git_ancestor_markers_are_rejected_without_git_commands(self):
        for kind in ("directory", "file", "broken-link"):
            tree = self.make_tree("git-" + kind)
            marker = tree / ".git"
            if kind == "directory": marker.mkdir()
            elif kind == "file": marker.write_text("gitdir: private-metadata\n", encoding="utf-8")
            else: marker.symlink_to(self.root / "missing-git-metadata")
            with self.subTest(kind=kind):
                self.assert_rejected("--root", tree)
                self.assertFalse((tree / "backend" / "test_en.epub").exists())
        ancestor = self.root / "ancestor-checkout"
        ancestor.mkdir()
        (ancestor / ".git").mkdir()
        tree = self.make_tree("ancestor-checkout/nested")
        self.assert_rejected("--root", tree)

    def test_concurrent_preparers_create_once_and_leave_a_complete_synthetic_zip(self):
        env = {"PATH": os.defpath, "HOME": str(self.root), "LANG": "C"}
        processes = [subprocess.Popen(self.command("--root", self.tree), cwd=self.root, env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        try:
            for process in processes:
                process.communicate(timeout=10)
            self.assertEqual(sorted(p.returncode for p in processes), [0, 2])
            self.assert_real_synthetic_zip(self.tree / "backend" / "test_en.epub")
        finally:
            for process in processes:
                if process.poll() is None: process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
