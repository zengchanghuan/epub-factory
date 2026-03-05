import ebooklib
from ebooklib import epub

class EpubPackager:
    def __init__(self, book, output_path):
        self.book = book
        self.output_path = output_path

    def save(self):
        try:
            epub.write_epub(self.output_path, self.book, {})
            return True
        except Exception as e:
            print(f"Package Error: {e}")
            return False