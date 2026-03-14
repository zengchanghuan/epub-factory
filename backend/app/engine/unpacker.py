import ebooklib
from ebooklib import epub


class EpubUnpacker:
    def __init__(self, file_path):
        self.file_path = file_path
        self._last_error = None  # 供调用方展示失败原因

    def load_book(self):
        try:
            self._last_error = None
            return epub.read_epub(self.file_path)
        except Exception as e:
            self._last_error = e
            print(f"Unpack Error: {e}")
            return None