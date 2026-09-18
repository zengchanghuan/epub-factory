import ebooklib
from ebooklib import epub
from pathlib import Path
import tempfile
import zipfile
from .epub_compat import package_path, normalize_package, normalize_book, normalize_metadata
from .epub_resource_repair import build_resource_repair_plan, MAX_METADATA_BYTES, ResourceRepairError, INVALID


class EpubUnpacker:
    def __init__(self, file_path):
        self.file_path = file_path
        self._last_error = None  # 供调用方展示失败原因
        self.source_warnings = []

    def load_book(self):
        try:
            self._last_error = None
            self.source_warnings = []
            with zipfile.ZipFile(self.file_path) as archive:
                if archive.getinfo('META-INF/container.xml').file_size > MAX_METADATA_BYTES:
                    raise ResourceRepairError(INVALID)
                opf_path = package_path(archive)
                plan = build_resource_repair_plan(archive, opf_path)
                self.source_warnings = list(plan.warnings)
                with tempfile.TemporaryDirectory(prefix='epub_input_compat_') as temp:
                    source_path = self.file_path
                    if plan.replacements:
                        source_path = Path(temp) / 'repaired.epub'
                        with zipfile.ZipFile(source_path, 'w') as output:
                            copied = set()
                            for info in archive.infolist():
                                copied.add(info.filename)
                                output.writestr(info, plan.replacements[info.filename] if info.filename in plan.replacements else archive.read(info.filename))
                            for name, raw in plan.replacements.items():
                                if name not in copied:
                                    output.writestr(name, raw)
                    with zipfile.ZipFile(source_path) as repaired:
                        original = repaired.read(opf_path)
                        normalized, prefer_nav = normalize_package(repaired, opf_path)
                        options = {'ignore_ncx': prefer_nav}
                        normalized_path = source_path
                        if normalized != original:
                            normalized_path = Path(temp) / 'normalized.epub'
                            with zipfile.ZipFile(normalized_path, 'w') as output:
                                for info in repaired.infolist():
                                    output.writestr(info, normalized if info.filename == opf_path else repaired.read(info.filename))
                        book = epub.read_epub(normalized_path, options=options)
            normalize_metadata(book, normalized)
            return normalize_book(book, opf_path)
        except Exception as e:
            self._last_error = e
            print(f"Unpack Error: {e}")
            return None
