import ebooklib
from ebooklib import epub

class EpubUnpacker:
    def __init__(self, file_path):
        self.file_path = file_path

    def load_book(self):
        try:
            return epub.read_epub(self.file_path)
        except Exception as e:
            print(f"Unpack Error: {e}")
            return None