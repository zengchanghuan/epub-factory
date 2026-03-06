import os
import subprocess
import json
from dotenv import load_dotenv

load_dotenv()

from .unpacker import EpubUnpacker
from .packager import EpubPackager
from .cleaners.css_sanitizer import CssSanitizer
from .cleaners.cjk_normalizer import CjkNormalizer
from .cleaners.semantics_translator import SemanticsTranslator
from .cleaners.device_profile import DeviceProfileCompiler
from .toc_rebuilder import TocRebuilder

EPUBCHECK_JAR = os.environ.get(
    "EPUBCHECK_JAR",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "tools", "epubcheck-5.1.0", "epubcheck.jar")
)


class ExtremeCompiler:
    def __init__(self, input_path: str, output_path: str, output_mode: str = "simplified",
                 enable_translation: bool = False, target_lang: str = "zh-CN",
                 device: str = "generic"):
        self.input_path = input_path
        self.output_path = output_path
        self.book = None
        self.output_mode = output_mode
        self.enable_translation = enable_translation
        
        self.cleaners = [
            CjkNormalizer(output_mode=self.output_mode),
            CssSanitizer(),
            DeviceProfileCompiler(device=device)
        ]
        
        if self.enable_translation:
            self.cleaners.append(SemanticsTranslator(target_lang=target_lang))

    def run(self) -> bool:
        print(f"🚀 [Start] Processing: {self.input_path}")
        
        unpacker = EpubUnpacker(self.input_path)
        self.book = unpacker.load_book()
        
        if not self.book:
            print("❌ [Error] Failed to load EPUB.")
            return False

        if hasattr(self.book, 'direction'):
            self.book.direction = 'ltr'
        elif hasattr(self.book, 'spine'):
            self.book.set_direction('ltr')

        for item in self.book.get_items():
            item_type = item.get_type()
            if item_type == 9 or item_type == 2:
                content = item.get_content()
                for cleaner in self.cleaners:
                    content = cleaner.process(content, item_type)
                item.set_content(content)

        # 3. 目录重建 (TOC Rebuild)
        rebuilder = TocRebuilder()
        self.book = rebuilder.rebuild(self.book)

        packager = EpubPackager(self.book, self.output_path)
        success = packager.save()
        
        if success:
            print(f"✅ [Success] Output saved to: {self.output_path}")
        else:
            print("❌ [Error] Failed to save EPUB.")

        if self.enable_translation:
            for cleaner in self.cleaners:
                if hasattr(cleaner, 'stats'):
                    print(cleaner.stats.summary(cleaner.model))

        if success:
            self._run_epubcheck()
            
        return success

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