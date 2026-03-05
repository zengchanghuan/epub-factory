import sqlite3
import hashlib
from pathlib import Path

class TranslationCache:
    def __init__(self, db_path: str = "translation_cache.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS translations (
                    id TEXT PRIMARY KEY,
                    source_html TEXT,
                    translated_html TEXT,
                    target_lang TEXT
                )
            """)

    def _get_hash(self, text: str, target_lang: str) -> str:
        return hashlib.sha256(f"{text}_{target_lang}".encode('utf-8')).hexdigest()

    def get(self, source_html: str, target_lang: str) -> str | None:
        key = self._get_hash(source_html, target_lang)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT translated_html FROM translations WHERE id = ?", (key,))
            row = cursor.fetchone()
            if row:
                return row[0]
        return None

    def set(self, source_html: str, translated_html: str, target_lang: str):
        key = self._get_hash(source_html, target_lang)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO translations (id, source_html, translated_html, target_lang)
                VALUES (?, ?, ?, ?)
            """, (key, source_html, translated_html, target_lang))
