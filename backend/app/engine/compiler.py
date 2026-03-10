import os
import subprocess
import json
import time
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv

load_dotenv()

from .unpacker import EpubUnpacker
from .packager import EpubPackager
from .cleaners import CssSanitizer, CjkNormalizer, SemanticsTranslator, DeviceProfileCompiler, TypographyEnhancer, StemGuard
from .toc_rebuilder import TocRebuilder

@dataclass
class StageTimer:
    """记录单个流水线阶段的耗时"""
    name: str
    elapsed_ms: float = 0.0
    status: str = "ok"  # ok | skipped | error


@dataclass
class PipelineMetrics:
    """整个 Pipeline 运行的可观测性指标"""
    stages: List[StageTimer] = field(default_factory=list)
    total_ms: float = 0.0
    mode: str = "full"  # full | safe

    def record(self, name: str, elapsed_ms: float, status: str = "ok") -> None:
        self.stages.append(StageTimer(name=name, elapsed_ms=elapsed_ms, status=status))

    def summary(self) -> str:
        lines = [
            f"\n{'─' * 52}",
            f"⏱  Pipeline [{self.mode}] — 总耗时 {self.total_ms:.0f} ms",
            f"{'─' * 52}",
        ]
        for s in self.stages:
            icon = "✅" if s.status == "ok" else ("⚠️" if s.status == "skipped" else "❌")
            lines.append(f"  {icon} {s.name:<28} {s.elapsed_ms:>7.1f} ms")
        lines.append(f"{'─' * 52}")
        return "\n".join(lines)


EPUBCHECK_JAR = os.environ.get(
    "EPUBCHECK_JAR",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "tools", "epubcheck-5.1.0", "epubcheck.jar")
)


class ExtremeCompiler:
    def __init__(self, input_path: str, output_path: str, output_mode: str = "simplified",
                 enable_translation: bool = False, target_lang: str = "zh-CN",
                 device: str = "generic", bilingual: bool = False,
                 glossary: dict | None = None):
        self.input_path = input_path
        self.output_path = output_path
        self.book = None
        self.output_mode = output_mode
        self.enable_translation = enable_translation
        self.bilingual = bilingual
        self.glossary: dict = glossary or {}
        
        self.cleaners = [
            CjkNormalizer(output_mode=self.output_mode),
            CssSanitizer(),
            TypographyEnhancer(),
            StemGuard(),
            DeviceProfileCompiler(device=device),
        ]

        if self.enable_translation:
            self.cleaners.append(SemanticsTranslator(
                target_lang=target_lang,
                bilingual=bilingual,
                glossary=glossary,
            ))

        self.metrics = PipelineMetrics()

    # ─── 公共入口：带两级降级兜底 ───────────────────────────────────────

    def run(self) -> bool:
        print(f"🚀 [Start] Processing: {self.input_path}")
        t0 = time.monotonic()
        try:
            result = self._run_full_pipeline()
        except Exception as exc:
            print(f"⚠️ [Fallback] Full pipeline failed: {exc}")
            print("🔄 [Fallback] Retrying in safe mode (direction-only)…")
            self.metrics.mode = "safe"
            try:
                result = self._run_safe_mode()
            except Exception as exc2:
                print(f"❌ [Fallback] Safe mode also failed: {exc2}")
                return False
        self.metrics.total_ms = (time.monotonic() - t0) * 1000
        print(self.metrics.summary())
        return result

    # ─── 完整流水线 ──────────────────────────────────────────────────────

    def _run_full_pipeline(self) -> bool:
        t = time.monotonic()
        unpacker = EpubUnpacker(self.input_path)
        self.book = unpacker.load_book()
        self.metrics.record("Unpack", (time.monotonic() - t) * 1000)

        if not self.book:
            raise RuntimeError("Failed to load EPUB")

        if hasattr(self.book, 'direction'):
            self.book.direction = 'ltr'
        elif hasattr(self.book, 'spine'):
            self.book.set_direction('ltr')

        # 每个 Cleaner 单独计时
        cleaner_totals: dict[str, float] = {}
        for item in self.book.get_items():
            item_type = item.get_type()
            if item_type == 9 or item_type == 2:
                content = item.get_content()
                for cleaner in self.cleaners:
                    ct = time.monotonic()
                    try:
                        content = cleaner.process(content, item_type)
                    except Exception as exc:
                        print(f"⚠️ [{cleaner.__class__.__name__}] skipped on "
                              f"{item.get_name()!r}: {exc}")
                    cleaner_totals[cleaner.__class__.__name__] = (
                        cleaner_totals.get(cleaner.__class__.__name__, 0)
                        + (time.monotonic() - ct) * 1000
                    )
                item.set_content(content)

        for name, ms in cleaner_totals.items():
            self.metrics.record(f"  Cleaner:{name}", ms)

        t = time.monotonic()
        rebuilder = TocRebuilder()
        self.book = rebuilder.rebuild(self.book)
        self.metrics.record("TocRebuilder", (time.monotonic() - t) * 1000)

        t = time.monotonic()
        packager = EpubPackager(self.book, self.output_path)
        success = packager.save()
        self.metrics.record("Packager+PostFix", (time.monotonic() - t) * 1000)

        if success:
            print(f"✅ [Success] Output saved to: {self.output_path}")
            self._print_translation_stats()
            t = time.monotonic()
            self._run_epubcheck()
            self.metrics.record("EpubCheck", (time.monotonic() - t) * 1000)
        else:
            print("❌ [Error] Failed to save EPUB.")

        return success

    # ─── 安全模式（兜底）：仅做排版方向转换 ──────────────────────────────

    def _run_safe_mode(self) -> bool:
        t = time.monotonic()
        unpacker = EpubUnpacker(self.input_path)
        book = unpacker.load_book()
        self.metrics.record("SafeMode:Unpack", (time.monotonic() - t) * 1000)

        if not book:
            raise RuntimeError("Failed to load EPUB in safe mode")

        t = time.monotonic()
        safe_cleaner = CjkNormalizer(output_mode=self.output_mode)
        for item in book.get_items():
            item_type = item.get_type()
            if item_type == 9 or item_type == 2:
                content = safe_cleaner.process(item.get_content(), item_type)
                item.set_content(content)
        self.metrics.record("SafeMode:CjkNormalizer", (time.monotonic() - t) * 1000)

        t = time.monotonic()
        packager = EpubPackager(book, self.output_path)
        success = packager.save()
        self.metrics.record("SafeMode:Packager", (time.monotonic() - t) * 1000)

        if success:
            print(f"✅ [SafeMode] Direction-only output saved to: {self.output_path}")
        return success

    # ─── 辅助方法 ────────────────────────────────────────────────────────

    def _print_translation_stats(self) -> None:
        if not self.enable_translation:
            return
        for cleaner in self.cleaners:
            if hasattr(cleaner, 'stats'):
                print(cleaner.stats.summary(cleaner.model))

    def _run_epubcheck(self):
        jar = os.path.abspath(EPUBCHECK_JAR)
        if not os.path.exists(jar):
            print("⚠️ [EpubCheck] JAR not found, skipping validation")
            return

        try:
            import tempfile as _tmpfile
            json_out = _tmpfile.mktemp(suffix=".json")
            result = subprocess.run(
                ["java", "-jar", jar, self.output_path, "--json", json_out],
                capture_output=True, text=True, timeout=60
            )
            try:
                with open(json_out, "r") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                lines = result.stderr.strip().split('\n')
                for line in lines[-5:]:
                    print(f"  {line}")
                return
            finally:
                if os.path.exists(json_out):
                    os.unlink(json_out)

            messages = data.get("messages", [])
            fatals = sum(1 for m in messages if m.get("severity") == "FATAL")
            errors = sum(1 for m in messages if m.get("severity") == "ERROR")
            warnings = sum(1 for m in messages if m.get("severity") == "WARNING")

            if fatals == 0 and errors == 0:
                print(f"✅ [EpubCheck] PASSED — 0 errors, {warnings} warnings")
            else:
                print(f"⚠️ [EpubCheck] {fatals} fatals / {errors} errors / {warnings} warnings")
                for m in messages:
                    if m.get("severity") in ("FATAL", "ERROR"):
                        loc = m.get("locations", [{}])[0]
                        path = loc.get("path", "?")
                        line = loc.get("line", "?")
                        print(f"  {m.get('severity')} {m.get('id','')}: {path}:{line} — {m.get('message','')}")
        except FileNotFoundError:
            print("⚠️ [EpubCheck] Java not installed, skipping validation")
        except subprocess.TimeoutExpired:
            print("⚠️ [EpubCheck] Timed out after 60s")
        except Exception as e:
            print(f"⚠️ [EpubCheck] Unexpected error: {e}")