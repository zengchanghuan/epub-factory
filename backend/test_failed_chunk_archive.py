import json
import os
import tempfile
import uuid
from pathlib import Path

from app.domain.chapter_translation_service import ChunkResult
from app.domain.failed_chunk_archive import archive_failed_chunk
from app.models import ChunkStatus


def test_archive_failed_chunk_writes_html_and_tag_delta():
    old_dir = os.environ.get("EPUB_FAILED_CHUNK_DIR")
    old_enabled = os.environ.get("EPUB_FAILED_CHUNK_ARCHIVE")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["EPUB_FAILED_CHUNK_DIR"] = tmp
        os.environ["EPUB_FAILED_CHUNK_ARCHIVE"] = "1"
        job_id = f"archive_{uuid.uuid4().hex[:8]}"
        chunk = ChunkResult(
            chunk_id="c1_001",
            sequence=1,
            locator="chap.xhtml#1",
            original_html="This is <em>important</em> and <a href='n.xhtml'>linked</a>.",
            translated_html="这是重要的，并且有链接。",
            cached=False,
            error="html tag mismatch; retry still invalid",
            retry_count=2,
            latency_ms=1200,
            audit_json={"risk_level": "fail", "flags": ["translation_error"]},
        )

        path = archive_failed_chunk(
            job_id=job_id,
            chapter_id="chap",
            chunk=chunk,
            status=ChunkStatus.failed,
        )

        assert path is not None
        assert path.is_file()
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert data["job_id"] == job_id
        assert data["retry_count"] == 2
        assert "original_html" in data
        assert data["tag_delta"]["em"] == {"source": 1, "translated": 0}
        assert data["tag_delta"]["a"] == {"source": 1, "translated": 0}

    _restore_env("EPUB_FAILED_CHUNK_DIR", old_dir)
    _restore_env("EPUB_FAILED_CHUNK_ARCHIVE", old_enabled)


def test_archive_failed_chunk_skips_nonfailed_warning():
    old_dir = os.environ.get("EPUB_FAILED_CHUNK_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["EPUB_FAILED_CHUNK_DIR"] = tmp
        chunk = ChunkResult(
            chunk_id="c1_warn",
            sequence=2,
            locator="chap.xhtml#2",
            original_html="A warning only.",
            translated_html="只是一个警告。",
            cached=False,
            audit_json={"risk_level": "warn", "flags": ["glossary_review"]},
        )

        path = archive_failed_chunk(
            job_id="archive_warn",
            chapter_id="chap",
            chunk=chunk,
            status=ChunkStatus.translated,
        )

        assert path is None
        assert not list(Path(tmp).glob("**/*.json"))

    _restore_env("EPUB_FAILED_CHUNK_DIR", old_dir)


def _restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


if __name__ == "__main__":
    test_archive_failed_chunk_writes_html_and_tag_delta()
    test_archive_failed_chunk_skips_nonfailed_warning()
