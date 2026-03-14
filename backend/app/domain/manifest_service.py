"""
EPUB Manifest 服务：解包后按文件识别正文/非正文，生成标准 Chunk Manifest。

供后续章节级翻译任务与 Reduce 回写使用；不依赖 Celery，可单独被 ingest 任务或现有 pipeline 调用。
"""

from pathlib import Path
from typing import Any, Dict, List

from app.models import ChapterKind
from app.engine.unpacker import EpubUnpacker
from app.engine.chunk_extractor import extract_chunks, ChunkItem


# 文件名关键词 → 章节类型（与 compiler._should_skip_translation_for_file 对齐并扩展为 kind）
def classify_chapter_kind(file_name: str) -> ChapterKind:
    """
    根据 EPUB 内文件名/路径识别章节类型。
    """
    lower = (file_name or "").lower()
    if any(k in lower for k in ("nav", "toc", "contents", "ncx")):
        return ChapterKind.nav
    if any(k in lower for k in ("copyright", "license", "colophon", "titlepage")):
        return ChapterKind.copyright
    if any(k in lower for k in ("appendix", "appendices")):
        return ChapterKind.appendix
    if any(k in lower for k in ("index", "glossary", "bibliograph")):
        return ChapterKind.index
    if any(k in lower for k in ("cover", "acknowledg", "about", "footnote", "endnote")):
        return ChapterKind.other
    # 其余视为正文
    return ChapterKind.body


def _chapter_id_from_path(file_path: str) -> str:
    """从 item 路径生成稳定 chapter_id，用于 manifest 与 chunk_id 前缀。"""
    stem = Path(file_path).stem
    # 保留字母数字与下划线，避免空格和特殊字符
    safe = "".join(c if c.isalnum() or c == "_" else "_" for c in stem)
    return safe or "chapter"


def build_manifest(epub_path: str, job_id: str) -> Dict[str, Any]:
    """
    解包 EPUB，识别每章类型，提取正文（及可选非正文）的 chunk，生成标准 manifest。

    :param epub_path: 本地 EPUB 文件路径
    :param job_id: 任务 ID，写入 manifest.job_id
    :return: 符合 11.6 Chunk Manifest 标准格式的 dict
    """
    unpacker = EpubUnpacker(epub_path)
    book = unpacker.load_book()
    if not book:
        return {"job_id": job_id, "chapters": [], "error": "Failed to load EPUB"}

    chapters: List[Dict[str, Any]] = []
    items = list(book.get_items())
    # 仅处理文档类型（9 = ITEM_DOCUMENT），不处理 CSS 等
    for item in items:
        if item is None:
            continue
        if item.get_type() != 9:
            continue
        file_name = item.get_name()
        if not file_name:
            continue
        kind = classify_chapter_kind(file_name)
        try:
            content = item.get_content()
            if content is None:
                content = b""
            if isinstance(content, str):
                content = content.encode("utf-8", errors="replace")
        except Exception:
            chapters.append({
                "chapter_id": _chapter_id_from_path(file_name),
                "file_path": file_name,
                "chapter_kind": kind.value,
                "chunks": [],
            })
            continue

        chapter_id = _chapter_id_from_path(file_name)
        # 仅正文提取 chunk，非正文保留空 chunks 便于 Reduce 按 file_path 回写
        chunk_list = extract_chunks(content, chapter_id)
        chunks_payload = [
            {
                "chunk_id": c.chunk_id,
                "sequence": c.sequence,
                "locator": c.locator,
                "html": c.html,
                "text": c.text,
                "word_count": c.word_count,
                "char_count": c.char_count,
            }
            for c in chunk_list
        ]
        chapters.append({
            "chapter_id": chapter_id,
            "file_path": file_name,
            "chapter_kind": kind.value,
            "chunks": chunks_payload,
        })

    return {"job_id": job_id, "chapters": chapters}
