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


class TranslationPipelineError(RuntimeError):
    """翻译链路失败，不应回退到 SafeMode 伪装成功。"""



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
                 glossary: dict | None = None, temperature: float | None = None,
                 traditional_variant: str = "auto",
                 progress_callback=None, stage_callback=None):
        self.input_path = input_path
        self.output_path = output_path
        self.book = None
        self.output_mode = output_mode
        self.traditional_variant = traditional_variant or "auto"
        self.enable_translation = enable_translation
        self.bilingual = bilingual
        self.glossary: dict = glossary or {}
        self.progress_callback = progress_callback or (lambda msg: None)
        self.stage_callback = stage_callback or (lambda name, msg, elapsed_ms=None: None)
        self.final_message = ""
        self.error_code = None
        self.validation_passed = True  # EpubCheck 通过则为 True，否则为 False，用于杜绝假成功
        
        # 依赖 models.QualityStats，我们先局部引入避免循环依赖
        from app.models import QualityStats, ErrorCode  # noqa: F401 – ErrorCode 供下方使用
        self.job_stats = QualityStats()
        self._ErrorCode = ErrorCode
        
        self.cleaners = [
            CjkNormalizer(output_mode=self.output_mode, traditional_variant=self.traditional_variant),
            CssSanitizer(),
            TypographyEnhancer(),
            StemGuard(),
            DeviceProfileCompiler(device=device),
        ]

        if self.enable_translation:
            t = SemanticsTranslator(
                target_lang=target_lang,
                bilingual=bilingual,
                glossary=glossary,
                temperature=temperature,
            )
            self.cleaners.append(t)

        self.metrics = PipelineMetrics()

    @staticmethod
    def _should_skip_translation_for_file(file_name: str) -> bool:
        """仅跳过纯导航/元数据文件，正文性质的辅助章节（目录、附录、索引等）仍翻译。"""
        lower = (file_name or "").lower()
        skip_keywords = (
            "nav",
            "copyright",
            "license",
            "colophon",
            "titlepage",
            "cover",
        )
        return any(k in lower for k in skip_keywords)

    # ─── 公共入口：带两级降级兜底 ───────────────────────────────────────

    def run(self) -> bool:
        print(f"🚀 [Start] Processing: {self.input_path}")
        t0 = time.monotonic()
        try:
            result = self._run_full_pipeline()
        except TranslationPipelineError as exc:
            self.metrics.total_ms = (time.monotonic() - t0) * 1000
            self.final_message = str(exc)
            self.error_code = self._ErrorCode.TRANSLATION_FAILED
            print(f"❌ [Translation] {exc}")
            print(self.metrics.summary())
            return False
        except Exception as exc:
            print(f"⚠️ [Fallback] Full pipeline failed: {exc}")
            print("🔄 [Fallback] Retrying in safe mode (direction-only)…")
            self.metrics.mode = "safe"
            try:
                result = self._run_safe_mode()
            except Exception as exc2:
                print(f"❌ [Fallback] Safe mode also failed: {exc2}")
                self.final_message = str(exc2)
                self.error_code = self._ErrorCode.CONVERT_FAILED
                return False
            if result and self.enable_translation:
                self.final_message = (
                    "翻译流程未完成，仅做了版式转换；请检查网络与 API 配置后重试。"
                )
                self.error_code = self._ErrorCode.TRANSLATION_FAILED
                print("❌ [SafeMode] 用户请求了翻译，但仅完成安全模式，不视为成功")
                return False
        self.metrics.total_ms = (time.monotonic() - t0) * 1000
        print(self.metrics.summary())
        if not self.final_message:
            self.final_message = "转换成功"
        return result

    # ─── 完整流水线 ──────────────────────────────────────────────────────

    def _run_full_pipeline(self) -> bool:
        self.stage_callback("preprocessing", "开始解包 EPUB")
        t = time.monotonic()
        self._unpacker = EpubUnpacker(self.input_path)
        self.book = self._unpacker.load_book()
        unpack_ms = (time.monotonic() - t) * 1000
        self.metrics.record("Unpack", unpack_ms)
        self.stage_callback("preprocessing", "解包完成", int(unpack_ms))

        if not self.book:
            err = getattr(self._unpacker, "_last_error", None)
            msg = "Failed to load EPUB"
            if err:
                msg = f"{msg}: {err!s}"
            raise RuntimeError(msg)

        if hasattr(self.book, 'direction'):
            self.book.direction = 'ltr'
        elif hasattr(self.book, 'spine'):
            self.book.set_direction('ltr')

        # 每个 Cleaner 单独计时
        cleaner_totals: dict[str, float] = {}
        items = list(self.book.get_items())
        docs = [item for item in items if item.get_type() in (9, 2)]
        total_docs = len(docs)
        if self.enable_translation:
            self.stage_callback("translating", "开始翻译")

        for i, item in enumerate(docs):
            item_type = item.get_type()
            file_name = item.get_name()
            
            # 报告当前处理文件进度
            self.progress_callback(f"正在处理 {file_name} ({i+1}/{total_docs})")
            
            content = item.get_content()
            if content is None:
                # ebooklib 某些 item（如空白占位资源）内容为 None，跳过处理避免 set_content 崩溃
                continue
            for cleaner in self.cleaners:
                if isinstance(cleaner, SemanticsTranslator):
                    if self._should_skip_translation_for_file(file_name):
                        self.progress_callback(f"跳过非正文翻译: {file_name} ({i+1}/{total_docs})")
                        continue
                    # 替换回调以带上文件名上下文
                    cleaner.progress_callback = lambda p_msg, fn=file_name, idx=i, total=total_docs: self.progress_callback(
                        f"翻译 {fn} ({p_msg}) - {idx+1}/{total}"
                    )
                
                ct = time.monotonic()
                try:
                    result_content = cleaner.process(content, item_type)
                    if result_content is not None:
                        content = result_content
                except Exception as exc:
                    print(f"⚠️ [{cleaner.__class__.__name__}] skipped on "
                          f"{file_name!r}: {exc}")
                cleaner_totals[cleaner.__class__.__name__] = (
                    cleaner_totals.get(cleaner.__class__.__name__, 0)
                    + (time.monotonic() - ct) * 1000
                )
            item.set_content(content)

        # 聚合质量统计（cleaner.stats 可能是 dict 或 dataclass）
        for cleaner in self.cleaners:
            if hasattr(cleaner, "stats"):
                st = cleaner.stats
                pairs = st.items() if isinstance(st, dict) else (
                    (f.name, getattr(st, f.name))
                    for f in getattr(st, "__dataclass_fields__", {}).values()
                )
                for k, v in pairs:
                    if isinstance(v, (int, float)) and hasattr(self.job_stats, k):
                        setattr(self.job_stats, k, getattr(self.job_stats, k) + v)

        translation_stats = self.get_translation_stats()
        if self.enable_translation and translation_stats.get("all_failed"):
            last_error = translation_stats.get("last_error") or "上游模型连接失败"
            raise TranslationPipelineError(
                f"AI 翻译失败：未成功写入任何译文。请检查模型服务连接。最后错误：{last_error}"
            )
        if self.enable_translation and translation_stats.get("failed_chunks"):
            self.error_code = self._ErrorCode.PARTIAL_TRANSLATION
            failed = translation_stats["failed_chunks"]
            done = translation_stats["translated_chunks"] + translation_stats["cached_chunks"]
            self.final_message = f"转换成功，但有 {failed} 个段落翻译失败，成功写入 {done} 个段落。"

        for name, ms in cleaner_totals.items():
            self.metrics.record(f"  Cleaner:{name}", ms)
        if self.enable_translation:
            translate_ms = sum(cleaner_totals.values())
            self.stage_callback("translating", "翻译完成", int(translate_ms))

        self.stage_callback("packaging", "开始打包")
        t = time.monotonic()
        rebuilder = TocRebuilder()
        self.book = rebuilder.rebuild(self.book)
        if hasattr(rebuilder, "stats"):
            self.job_stats.toc_generated += rebuilder.stats.get("toc_generated", 0)
        self.metrics.record("TocRebuilder", (time.monotonic() - t) * 1000)

        t = time.monotonic()
        packager = EpubPackager(self.book, self.output_path)
        success = packager.save()
        pack_ms = (time.monotonic() - t) * 1000
        self.metrics.record("Packager+PostFix", pack_ms)
        self.stage_callback("packaging", "打包完成", int(pack_ms))

        if success:
            print(f"✅ [Success] Output saved to: {self.output_path}")
            self._print_translation_stats()
            self.stage_callback("validating", "开始校验")
            t = time.monotonic()
            self.validation_passed = self._run_epubcheck()
            check_ms = (time.monotonic() - t) * 1000
            self.metrics.record("EpubCheck", check_ms)
            self.stage_callback("validating", "校验完成", int(check_ms))
            if not self.validation_passed:
                self.error_code = self._ErrorCode.EPUB_VALIDATION_FAILED
                self.final_message = "打包成功但 EPUB 校验未通过，结果不可交付"
        else:
            print("❌ [Error] Failed to save EPUB.")

        return success

    # ─── 安全模式（兜底）：仅做排版方向转换 ──────────────────────────────

    def _run_safe_mode(self) -> bool:
        book = None
        if getattr(self, "book", None) is not None:
            book = self.book
            self.metrics.record("SafeMode:ReuseBook", 0)
        if book is None:
            t = time.monotonic()
            unpacker = getattr(self, "_unpacker", None) or EpubUnpacker(self.input_path)
            book = unpacker.load_book()
            self.metrics.record("SafeMode:Unpack", (time.monotonic() - t) * 1000)
            if not book:
                err = getattr(unpacker, "_last_error", None)
                msg = "Failed to load EPUB in safe mode"
                if err:
                    msg = f"{msg}: {err!s}"
                raise RuntimeError(msg)

        t = time.monotonic()
        safe_cleaner = CjkNormalizer(output_mode=self.output_mode, traditional_variant=getattr(self, "traditional_variant", "auto"))
        for item in book.get_items():
            item_type = item.get_type()
            if item_type == 9 or item_type == 2:
                raw = item.get_content()
                if raw is None:
                    continue
                result_content = safe_cleaner.process(raw, item_type)
                if result_content is not None:
                    item.set_content(result_content)
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
            if isinstance(cleaner, SemanticsTranslator) and hasattr(cleaner, 'stats'):
                print(cleaner.stats.summary(cleaner.model))

    def get_translation_stats(self) -> dict:
        if not self.enable_translation:
            return {}
        for cleaner in self.cleaners:
            if isinstance(cleaner, SemanticsTranslator):
                return cleaner.stats.to_dict(cleaner.model)
        return {}

    def _run_epubcheck(self) -> bool:
        """
        执行 EpubCheck 校验，仅当 0 个 FATAL 且 0 个 ERROR 时视为通过。
        :return: True 通过或跳过（无 JAR/Java），False 有致命或错误级别问题
        """
        jar = os.path.abspath(EPUBCHECK_JAR)
        if not os.path.exists(jar):
            print("⚠️ [EpubCheck] JAR not found, skipping validation")
            return True  # 跳过时不影响交付

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
                return False  # 无法解析结果时保守视为未通过
            finally:
                if os.path.exists(json_out):
                    os.unlink(json_out)

            messages = data.get("messages", [])
            fatals = sum(1 for m in messages if m.get("severity") == "FATAL")
            errors = sum(1 for m in messages if m.get("severity") == "ERROR")
            warnings = sum(1 for m in messages if m.get("severity") == "WARNING")

            if fatals == 0 and errors == 0:
                print(f"✅ [EpubCheck] PASSED — 0 errors, {warnings} warnings")
                return True
            print(f"⚠️ [EpubCheck] {fatals} fatals / {errors} errors / {warnings} warnings")
            for m in messages:
                if m.get("severity") in ("FATAL", "ERROR"):
                    loc = m.get("locations", [{}])[0]
                    path = loc.get("path", "?")
                    line = loc.get("line", "?")
                    print(f"  {m.get('severity')} {m.get('id','')}: {path}:{line} — {m.get('message','')}")
            return False
        except FileNotFoundError:
            print("⚠️ [EpubCheck] Java not installed, skipping validation")
            return True
        except subprocess.TimeoutExpired:
            print("⚠️ [EpubCheck] Timed out after 60s")
            return False
        except Exception as e:
            print(f"⚠️ [EpubCheck] Unexpected error: {e}")
            return False