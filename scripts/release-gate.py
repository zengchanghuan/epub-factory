#!/usr/bin/env python3
"""Source-only, offline local release evidence. No install, deploy or live API.

Python auditing is inherited by suite subprocesses; it is not an OS firewall
for Java/Node/Chrome. A successful run is one Mac's local gate, not production,
second-Mac, real-provider or native Redis/prefork acceptance.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parent.parent
SOURCE_SUFFIXES = {".py", ".js", ".cjs", ".html", ".css", ".json", ".md", ".txt", ".yml", ".yaml", ".toml", ".sh", ".svg", ".ico", ".png", ".webp"}
ROOT_FILES = {"README.md", "Dockerfile", "docker-compose.yml", "deploy.sh", "push.sh", "index.html"}
SKIP_ALLOWLIST = {
    "test_d21_real_order_recovery.py": {
        "test_real_original_manifest_and_title_recovery": "EPUB_REGRESSION_BOOK not provided",
        "test_real_saved_chapters_flag_both_unconfirmed_names": "EPUB_REGRESSION_SNAPSHOTS not provided"},
    "test_d26_preview_feedback.py": {"test_real_selected_book_preview_without_modification": "real translated book not provided"},
    "test_d27_targeted_book_repair.py": {"test_real_candidate_keeps_every_image_tag_link_id_and_unchanged_member": "both selected book versions not provided"},
    "test_d28_media_text_translation.py": {"test_real_book_all_sixteen_image_bullets_enter_manifest": "selected source book not provided"},
    "test_d29_translation_performance.py": {
        "test_real_book_resume_fingerprint_is_stable_and_catches_image_bullet_change": "selected real source book not provided",
        "test_real_twenty_block_image_bullet_chapter_resumes_without_requests": "selected real original and formal translated book not provided"},
}


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def included_source(name):
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return False
    if name in {"backend/requirements.lock", "backend/.python-version", "backend/.env.example"}:
        return True
    if any("secret" in part.lower() or part == "__pycache__" for part in path.parts):
        return False
    if any(part.startswith(".") for part in path.parts):
        return (len(path.parts) == 3 and path.parts[:2] == (".github", "workflows")
                and path.suffix in {".yml", ".yaml"} and not path.name.startswith("."))
    if name in ROOT_FILES:
        return True
    if path.suffix not in SOURCE_SUFFIXES or path.name.endswith((".env", ".lock")):
        return False
    return (name in ROOT_FILES or name.startswith(("backend/app/", "backend/data/", "frontend/", "docs/", "scripts/"))
            or name == "backend/scripts/import_llm_bill.py"
            or len(path.parts) == 2 and path.parts[0] == "backend" and (
                path.name.startswith("test_") and path.suffix == ".py"
                or path.name in {"run_regression.py", "requirements.txt"}))


def _git(repo, *args):
    # Git's local metadata only: no remote/config discovery or credential use.
    return subprocess.check_output(["git", "-C", str(repo), *args], timeout=15, env={
        "PATH": os.environ.get("PATH", os.defpath), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})


def collect_sources(repo):
    names = _git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z").decode().split("\0")
    # D23 reads this exact public template. Never treat an untracked file with
    # that name as a reviewed template or broaden this to private .env files.
    if "backend/.env.example" in names:
        tracked = _git(repo, "ls-files", "--cached", "-z").decode().split("\0")
        if "backend/.env.example" not in tracked:
            raise ValueError("The public environment template must be Git-tracked")
    return [Path(name) for name in sorted(set(names)) if name and included_source(name)]


def _regular_source(repo, relative):
    path = repo / relative
    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(repo.resolve()):
        raise ValueError("Source is missing, symbolic or outside repository: " + str(relative))
    for parent in path.parents:
        if parent == repo:
            break
        if parent.is_symlink():
            raise ValueError("Symbolic source directory refused")
    return path


def snapshot_sources(repo, destination):
    manifest = {}
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for relative in collect_sources(repo):
        _regular_source(repo, relative)
        content = _source_bytes(repo, relative)
        before = hashlib.sha256(content).hexdigest()
        target = destination / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o600)
        if digest(target) != before or hashlib.sha256(_source_bytes(repo, relative)).hexdigest() != before:
            raise ValueError("Source changed while snapshotting: " + str(relative))
        manifest[str(relative)] = before
    for folder in destination.rglob("*"):
        if folder.is_dir():
            folder.chmod(0o700)
    return manifest


def _source_bytes(repo, relative):
    """Descriptor traversal prevents a checked source becoming a symlink read."""
    fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in relative.parts[:-1]:
            following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        source = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        with os.fdopen(source, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Nonregular source refused")
            return stream.read()
    finally:
        os.close(fd)


def prepare_evidence(repo, path):
    path = Path(path).expanduser().absolute()
    if path.exists() or path.is_symlink() or path.resolve().is_relative_to(repo.resolve()):
        raise ValueError("Evidence must be a new directory outside the repository")
    for parent in path.parents:
        if parent.is_symlink():
            system_alias = {Path("/tmp"): Path("/private/tmp"), Path("/var"): Path("/private/var")}
            if system_alias.get(parent) != parent.resolve():
                raise ValueError("Symbolic evidence ancestry refused")
    path = path.resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.mkdir(mode=0o700)
    return path


def protected_state(repo):
    result = {}
    for folder in (repo, repo / "backend"):
        for name in ("epub_jobs.db", "rate_limit.db", "translation_cache.db"):
            for suffix in ("", "-wal", "-shm"):
                path = folder / (name + suffix)
                if path.is_symlink():
                    raise ValueError("Protected runtime database is a symlink")
                result[str(path.relative_to(repo))] = digest(path) if path.is_file() else None
    return result


def catalog_scripts(snapshot):
    tree = ast.parse((snapshot / "backend/run_regression.py").read_text())
    groups = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"D_SUITE", "C_SUITE"}:
                    groups[target.id] = ast.literal_eval(node.value)
    if set(groups) != {"D_SUITE", "C_SUITE"}:
        raise ValueError("Both literal regression catalogs are required")
    scripts = groups["D_SUITE"] + groups["C_SUITE"]
    if not scripts or len(set(scripts)) != len(scripts):
        raise ValueError("Empty or duplicate regression catalog")
    for script in scripts:
        if not isinstance(script, str) or not re.fullmatch(r"test_[A-Za-z0-9_]+\.py", script):
            raise ValueError("Invalid regression catalog entry")
        _regular_source(snapshot, Path("backend") / script)
    return scripts


def sanitized_environment(evidence, snapshot, jar):
    for name in ("home", "tmp", "data", "data/repair", "data/uploads", "data/outputs", "logs"):
        (evidence / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    guard = snapshot / "scripts/release_guard"
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(evidence / "home"),
        "TMPDIR": str(evidence / "tmp"), "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "PYTHONPATH": str(guard),
        "RELEASE_GATE_ACTIVE": "1", "RELEASE_GATE_GUARD_DIR": str(guard),
        "RELEASE_GATE_DOTENV_ROOT": str(evidence / "tmp"),
        "DATABASE_URL": "sqlite:///" + str(evidence / "data/jobs.db"), "EPUB_PERSISTENT_STORE": "1",
        "EPUB_TRANSLATION_CHECKPOINT_DB": str(evidence / "data/checkpoints.db"),
        "REPAIR_UPLOAD_DIR": str(evidence / "data/repair"),
        "UPLOAD_DIR": str(evidence / "data/uploads"), "OUTPUT_DIR": str(evidence / "data/outputs"),
        "OPENAI_API_KEY": "dummy", "DEEPSEEK_API_KEY": "dummy", "DASHSCOPE_API_KEY": "dummy", "GEMINI_API_KEY": "dummy",
        "ALIPAY_APP_ID": "", "ALIPAY_PRIVATE_KEY": "", "ALIPAY_PUBLIC_KEY": "",
        "SKIP_PAYMENT_CHECK": "1", "OWNER_PAYMENT_EMAIL_ENABLED": "1", "NOTIFY_EMAIL_ENABLED": "0",
        "SENTRY_DSN": "", "SMTP_HOST": "", "SMTP_USER": "", "SMTP_PASSWORD": "",
        "CELERY_BROKER_URL": "", "REDIS_URL": "", "CELERY_RESULT_BACKEND": "",
        "JOB_DISPATCH_ENABLED": "0", "EPUBCHECK_JAR": str(jar),
    }


def _stop_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # Reap descendants even if the group leader exited on SIGTERM.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_command(command, *, cwd, env, log_path, timeout):
    if command and Path(str(command[0])).name.lower().startswith("python"):
        for option in map(str, command[1:]):
            if option in {"-c", "-m"} or not option.startswith("-"):
                break
            if not option.startswith("--") and any(flag in option[1:] for flag in "IES"):
                raise ValueError("Python isolation flags would disable the release guard")
    started = time.monotonic()
    result = {"command": [str(value) for value in command], "returncode": None,
              "timed_out": False, "interrupted": False, "log": str(log_path)}
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        try:
            result["returncode"] = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            result["timed_out"] = True
            _stop_group(process)
            result["returncode"] = process.returncode
        except KeyboardInterrupt:
            result["interrupted"] = True
            _stop_group(process)
            result["returncode"] = process.returncode
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    return result


def classify_log(script, text, returncode, timed_out=False, profile="offline", test_events=None):
    events = test_events or []
    skips = [event for event in events if event.get("kind") == "skip"]
    results = [event for event in events if event.get("kind") == "unittest_result"]
    allowed = SKIP_ALLOWLIST.get(Path(script).name, {}) if profile == "offline" else {}
    unexpected, seen = [], set()
    for entry in skips:
        method = str(entry.get("test_id", "")).rsplit(".", 1)[-1]
        if method in seen or allowed.get(method) != entry.get("reason"):
            unexpected.append(entry)
        seen.add(method)
    declared = sum(int(value) for value in re.findall(r"^OK \([^\n]*skipped=(\d+)[^\n]*\)$", text, re.M))
    if declared != len(skips):
        unexpected.append({"reason": "unobserved_skip_summary", "count": declared})
    if results and sum(row.get("skipped", 0) for row in results) != len(skips):
        unexpected.append({"reason": "inconsistent_skip_events"})
    manual = [line.strip()[:200] for line in text.splitlines() if re.search(r"\bSKIP(?:PED)?\s*:", line, re.I)]
    failed_results = any(row.get("failures", 0) or row.get("errors", 0) for row in results)
    tests_run = sum(row["tests_run"] for row in results) if results else None
    missing_result = profile == "history" and (tests_run is None or tests_run <= 0)
    if Path(script).suffix == ".js":
        frontend = re.findall(r"Results:\s*(\d+) passed,\s*(\d+) failed", text)
        missing_result = len(frontend) != 1 or sum(map(int, frontend[0])) <= 0
        if len(frontend) == 1:
            tests_run = sum(map(int, frontend[0]))
            failed_results = failed_results or int(frontend[0][1]) > 0
    return {"ok": returncode == 0 and not timed_out and not unexpected and not manual and not failed_results and not missing_result,
            "skipped": len(skips), "unexpected_skips": unexpected, "manual_skips": manual,
            "tests_run": tests_run, "missing_test_result": missing_result}


def load_runtime_helper(repo):
    spec = importlib.util.spec_from_file_location("release_runtime", repo / "scripts/release_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json_file(path, payload):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _read_events(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _synthetic_fixture(snapshot):
    # This old fixture name is ignored by Git and may be a user's actual book.
    # Always synthesize locally; never read or copy its workspace counterpart.
    with zipfile.ZipFile(snapshot / "backend/test_en.epub", "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        archive.writestr("EPUB/package.opf", '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">release-gate-synthetic</dc:identifier><dc:title>Synthetic fixture</dc:title><dc:language>en</dc:language><meta property="dcterms:modified">2026-10-02T00:00:00Z</meta></metadata><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="style" href="style.css" media-type="text/css"/></manifest><spine><itemref idref="chapter"/></spine></package>')
        archive.writestr("EPUB/chapter.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter</title><link rel="stylesheet" href="style.css"/></head><body><h1 id="one">Chapter</h1><p>A short synthetic sentence for offline testing.</p><table><tr><td>One</td></tr></table></body></html>')
        archive.writestr("EPUB/nav.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head><body><nav epub:type="toc"><ol><li><a href="chapter.xhtml#one">Chapter</a></li></ol></nav></body></html>')
        archive.writestr("EPUB/style.css", "p { color: black; }")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--node", default="node")
    parser.add_argument("--epubcheck-jar", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--profile", choices=("offline", "history"), default="offline")
    for name in ("uploads", "outputs", "baselines"):
        parser.add_argument("--" + name, type=Path)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.profile == "history" and not all((args.uploads, args.outputs, args.baselines)):
        print("History requires explicit uploads, outputs and baselines", file=sys.stderr)
        return 2
    report = {"schema_version": 1, "profile": args.profile, "status": "preflight",
              "scope": "one-Mac local offline gate; not second-Mac, production, paid-provider or prefork acceptance",
              "commands": [], "errors": [], "started_at": time.time()}
    evidence = None
    before = None
    try:
        repo = ROOT.resolve()
        evidence = prepare_evidence(repo, args.evidence_dir)
        before = protected_state(repo)
        report["protected_before"] = before
        report["revision"] = _git(repo, "rev-parse", "HEAD").decode().strip()
        report["dirty"] = bool(_git(repo, "status", "--porcelain", "-z"))
        helper = load_runtime_helper(repo)
        report["runtime"] = helper.inspect_runtime(repo, args.python, args.node, args.epubcheck_jar)
        if not report["runtime"].get("ok"):
            raise ValueError("Runtime preflight failed; no test was started")
        python = report["runtime"]["executables"]["python"]["path"]
        node = report["runtime"]["executables"]["node"]["path"]
        jar = args.epubcheck_jar.expanduser()
        jar = (repo / jar if not jar.is_absolute() else jar).resolve()
        if args.profile == "history":
            report["books"] = helper.inspect_books(repo, args.uploads, args.outputs, args.baselines)
            if not report["books"].get("ok"):
                raise ValueError("Historical fixture preflight failed; no test was started")
        snapshot = evidence / "source"
        manifest = snapshot_sources(repo, snapshot)
        report["source_hashes"] = manifest
        scripts = catalog_scripts(snapshot)
        frontend = sorted((snapshot / "frontend/tests").glob("test_*.js"))
        if not frontend or not (snapshot / "frontend/tests/runner.js").is_file():
            raise ValueError("Frontend catalog is empty or runner is missing")
        report["expected_catalog"] = {"backend": len(scripts), "frontend": len(frontend),
                                       "history": int(args.profile == "history")}
        _synthetic_fixture(snapshot)
        env = sanitized_environment(evidence, snapshot, jar)
        java = report["runtime"]["executables"].get("java", {}).get("path")
        if java:
            env["PATH"] = str(Path(java).parent) + os.pathsep + env["PATH"]
        commands = [(name, [python, str(snapshot / "backend" / name)], 120, "offline") for name in scripts]
        commands += [(path.name, [node, str(snapshot / "frontend/tests/runner.js"), path.name], 120, "offline") for path in frontend]
        if args.profile == "history":
            commands.append(("test_d55_order_review_history.py", [python, str(snapshot / "backend/test_d55_order_review_history.py")], 600, "history"))
        report["status"] = "running"
        for index, (name, command, timeout, profile) in enumerate(commands):
            stem = f"{index:03d}-" + name
            events_path, network_path = evidence / "logs" / (stem + ".events.jsonl"), evidence / "logs" / (stem + ".network.jsonl")
            child_env = {**env, "RELEASE_GATE_EVENTS": str(events_path), "RELEASE_GATE_NETWORK_LOG": str(network_path)}
            if profile == "history":
                child_env.update(EPUB_HISTORY_UPLOAD_DIR=str(args.uploads.resolve()), EPUB_HISTORY_OUTPUT_DIR=str(args.outputs.resolve()), EPUB_HISTORY_BASELINE_DIR=str(args.baselines.resolve()))
            result = run_command(command, cwd=snapshot / "backend", env=child_env,
                                 log_path=evidence / "logs" / (stem + ".log"), timeout=timeout)
            events, network = _read_events(events_path), _read_events(network_path)
            result.update(classify_log(name, Path(result["log"]).read_text(errors="replace"), result["returncode"],
                                       result["timed_out"], profile, events))
            result.update(script=name, profile=profile, test_events=events, network_events=network)
            if network or command[0] == python and not any(row.get("kind") == "guard_started" for row in events):
                result["ok"] = False
                result["guard_failure"] = True
            report["commands"].append(result)
            _json_file(evidence / "report.json", report)
            print(f"{'PASS' if result['ok'] else 'FAIL'} {name} (skipped={result['skipped']})", flush=True)
            if result["interrupted"]:
                report["status"] = "interrupted"
                break
        current_sources = {str(path) for path in collect_sources(repo)}
        changed = sorted(current_sources.symmetric_difference(manifest))
        for name in current_sources.intersection(manifest):
            if (hashlib.sha256(_source_bytes(repo, Path(name))).hexdigest() != manifest[name]
                    or hashlib.sha256(_source_bytes(snapshot, Path(name))).hexdigest() != manifest[name]):
                changed.append(name)
        report["source_changed"] = changed
        if changed:
            raise ValueError("Source or frozen snapshot changed during the gate")
        if args.profile == "history":
            report["books_after"] = helper.inspect_books(repo, args.uploads, args.outputs, args.baselines)
            if not report["books_after"].get("ok"):
                raise ValueError("Historical fixtures changed during the gate")
        if report["status"] != "interrupted":
            report["status"] = "passed" if all(row["ok"] for row in report["commands"]) else "failed"
    except KeyboardInterrupt:
        report["status"] = "interrupted"
    except Exception as exc:
        report["status"] = "failed"
        report["errors"].append({"type": type(exc).__name__, "message": str(exc)[:300]})
    finally:
        if evidence is not None:
            try:
                report["protected_after"] = protected_state(ROOT.resolve())
                if before is not None and report["protected_after"] != before:
                    report["status"] = "failed"
                    report["errors"].append({"type": "RuntimeMutation", "message": "Workspace database bytes changed"})
            except Exception as exc:
                report["status"] = "failed"
                report["errors"].append({"type": type(exc).__name__, "message": "Protected-state verification failed"})
            report["finished_at"] = time.time()
            report["summary"] = {"commands": len(report["commands"]), "passed": sum(row["ok"] for row in report["commands"]),
                "skipped_methods": sum(row["skipped"] for row in report["commands"]),
                "reported_tests_run": sum(row["tests_run"] or 0 for row in report["commands"]),
                "timed_out": sum(row["timed_out"] for row in report["commands"]),
                "network_events": sum(len(row["network_events"]) for row in report["commands"]),
                "skips": [{"script": row["script"], **event} for row in report["commands"]
                          for event in row["test_events"] if event.get("kind") == "skip"]}
            _json_file(evidence / "report.json", report)
            print(str(evidence / "report.json"))
    return 0 if report["status"] == "passed" else 130 if report["status"] == "interrupted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
