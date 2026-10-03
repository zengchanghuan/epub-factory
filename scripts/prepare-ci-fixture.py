#!/usr/bin/env python3
"""Create only the synthetic regression EPUB in an explicit disposable source tree.

Not a real-book test, source exporter or runtime isolation layer. Refuses Git
checkouts, symbolic directories and existing targets (including broken links).
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys


def prepare(root):
    root = Path(root).absolute()
    if ".." in root.parts:
        raise ValueError("Parent traversal is not allowed")
    aliases = {Path("/tmp"): Path("/private/tmp"), Path("/var"): Path("/private/var")}
    for folder in (root, *root.parents):
        if folder.is_symlink() and aliases.get(folder) != folder.resolve():
            raise ValueError("Symbolic fixture directories are not allowed")
        if (folder / ".git").exists() or (folder / ".git").is_symlink():
            raise ValueError("Use an isolated disposable tree, not a Git checkout")
    root = root.resolve(strict=True)
    backend = root / "backend"
    if not backend.is_dir() or backend.is_symlink():
        raise ValueError("A real backend directory is required")
    catalog = backend / "run_regression.py"
    if not catalog.is_file() or catalog.is_symlink():
        raise ValueError("The isolated source catalog is required")
    spec = importlib.util.spec_from_file_location("ci_fixture_gate", Path(__file__).with_name("release-gate.py"))
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    # The generator uses exclusive creation: never overwrite a user's real book
    # named test_en.epub or another concurrent preparer's target.
    gate._synthetic_fixture(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True,
                        help="Explicit disposable source tree (e.g. /app inside the CI container)")
    args = parser.parse_args(argv)
    try:
        prepare(args.root)
    except (OSError, ValueError):
        print("Cannot create a fresh synthetic CI fixture; no existing file was overwritten.", file=sys.stderr)
        return 2
    print("Created synthetic regression fixture (not a historical customer book).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
