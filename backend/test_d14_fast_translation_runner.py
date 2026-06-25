"""
D14 测试：快速翻译主链路

- 预处理后走 MapReduce 翻译
- 所有 chunk 使用同一份 glossary
- 译后术语审计能把仍保留的英文术语修正为指定译名
"""

import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ebooklib import epub

import app.domain.fast_translation_runner as fast_runner
from app.domain.fast_translation_runner import run_fast_translation_job
from app.engine.cleaners.semantics_translator import SemanticsTranslator
from app.models import DeviceProfile, Job, OutputMode
from app.storage import job_store


def _make_epub(path: Path, marker: str) -> None:
    book = epub.EpubBook()
    book.set_identifier(f"d14-{marker}")
    book.set_title("D14 Fast Translation")
    book.set_language("en")
    chapter = epub.EpubHtml(title="Chapter 1", file_name="chap_01.xhtml", lang="en")
    chapter.content = (
        "<html><body>"
        f"<h1>Chapter 1</h1><p>Mr. Smith travels to London {marker}.</p>"
        "</body></html>"
    )
    book.add_item(chapter)
    book.spine.append(chapter)
    book.toc = (epub.Link("chap_01.xhtml", "Chapter 1", "chap1"),)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(path), book, {})


def test_fast_translation_runner_glossary_audit():
    marker = uuid.uuid4().hex[:10]
    old_auto = fast_runner.build_auto_glossary
    old_call = SemanticsTranslator._call_llm_json_batch

    fast_runner.build_auto_glossary = lambda *args, **kwargs: {}

    async def fake_call(self, payload):
        # 故意保留 Smith/London，验证 verify_and_fix 会按 glossary 修正。
        return (
            {item["id"]: item["html"].replace("travels to", "到访") for item in payload},
            {"model": "fake-model", "base_url": "fake://llm", "prompt_tokens": 20, "completion_tokens": 30},
        )

    SemanticsTranslator._call_llm_json_batch = fake_call
    try:
        with tempfile.TemporaryDirectory() as tmp:
            inp = Path(tmp) / "input.epub"
            out = Path(tmp) / "output.epub"
            _make_epub(inp, marker)
            job = Job(
                id=f"d14_{marker}",
                source_filename="input.epub",
                output_mode=OutputMode.simplified,
                trace_id=uuid.uuid4().hex,
                input_path=str(inp),
                enable_translation=True,
                target_lang="zh-CN",
                glossary={"Smith": "史密斯", "London": "伦敦"},
                device=DeviceProfile.generic,
            )
            result = run_fast_translation_job(
                job=job,
                input_path=inp,
                output_path=out,
                progress_callback=lambda _msg: None,
                stage_callback=lambda _stage, _msg, _elapsed=None: None,
            )
            assert out.is_file()
            assert result.translation_stats["translated_chunks"] >= 1
            assert result.translation_stats["glossary_fixed_count"] >= 2
            assert "audit_warn_chunks" in result.translation_stats
            assert "audit_failed_chunks" in result.translation_stats
            assert "audit_flags_count" in result.translation_stats
            chunks = job_store.list_chunks(job.id)
            assert chunks
            assert any(c.source_text for c in chunks)
            assert any(c.translated_text for c in chunks)
            assert all(isinstance(c.audit_json, dict) for c in chunks)
            out_book = epub.read_epub(str(out))
            combined = []
            for item in out_book.get_items():
                if item.get_type() == 9:
                    content = item.get_content()
                    if isinstance(content, bytes):
                        content = content.decode("utf-8", errors="ignore")
                    combined.append(str(content))
            text = "\n".join(combined)
            assert "史密斯" in text
            assert "伦敦" in text
    finally:
        fast_runner.build_auto_glossary = old_auto
        SemanticsTranslator._call_llm_json_batch = old_call


if __name__ == "__main__":
    try:
        test_fast_translation_runner_glossary_audit()
        print("  ✅ test_fast_translation_runner_glossary_audit")
        print("\n📊 1 passed, 0 failed")
    except Exception as exc:
        print(f"  ❌ test_fast_translation_runner_glossary_audit: {exc}")
        raise
