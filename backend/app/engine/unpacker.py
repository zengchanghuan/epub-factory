import ebooklib
from ebooklib import epub
from pathlib import Path
import tempfile
import zipfile
from .epub_compat import package_path, normalize_package, normalize_book, normalize_metadata


class EpubUnpacker:
    def __init__(self, file_path):
        self.file_path = file_path
        self._last_error = None  # 供调用方展示失败原因

    def load_book(self):
        try:
            self._last_error = None
            with zipfile.ZipFile(self.file_path) as archive:
                opf_path = package_path(archive)
                original = archive.read(opf_path)
                normalized, prefer_nav = normalize_package(archive, opf_path)
                options = {'ignore_ncx': prefer_nav}
                if normalized == original:
                    book = epub.read_epub(self.file_path, options=options)
                else:
                    with tempfile.TemporaryDirectory(prefix='epub_input_compat_') as temp:
                        normalized_path = Path(temp) / 'source.epub'
                        with zipfile.ZipFile(normalized_path, 'w') as output:
                            for info in archive.infolist():
                                output.writestr(info, normalized if info.filename == opf_path else archive.read(info.filename))
                        book = epub.read_epub(normalized_path, options=options)
            normalize_metadata(book, normalized)
            return normalize_book(book, opf_path)
        except Exception as e:
            self._last_error = e
            print(f"Unpack Error: {e}")
            return None
