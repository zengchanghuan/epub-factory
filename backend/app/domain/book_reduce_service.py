"""
全书 Reduce 与打包：将各章回写后的 HTML 合并进内存 Book，执行 TOC 重建与打包。
"""

import ebooklib
from pathlib import Path
from typing import Callable, Optional

from app.domain.manifest_service import build_manifest
from app.engine.unpacker import EpubUnpacker
from app.engine.toc_rebuilder import TocRebuilder
from app.engine.packager import EpubPackager

# 章节回写结果存放目录（job_id 下按 file_path 存文件）
_REDUCE_WORK_DIR = Path(__file__).resolve().parent.parent.parent / "reduce_work"


def _safe_key(file_path: str) -> str:
    """将 EPUB 内路径转为单文件名，避免路径遍历。"""
    return Path(file_path).name or "index.xhtml"


def set_chapter_output(job_id: str, file_path: str, content: bytes) -> Path:
    """写入某章回写后的 HTML，供 reduce_and_package 读取。返回写入路径。"""
    d = _REDUCE_WORK_DIR / job_id / "reduced"
    d.mkdir(parents=True, exist_ok=True)
    out = d / _safe_key(file_path)
    out.write_bytes(content)
    return out


def get_chapter_output(job_id: str, file_path: str) -> Optional[bytes]:
    """读取某章回写后的 HTML；不存在则返回 None。"""
    p = _REDUCE_WORK_DIR / job_id / "reduced" / _safe_key(file_path)
    if not p.is_file():
        return None
    return p.read_bytes()


def make_get_chapter_content(job_id: str) -> Callable[[str], Optional[bytes]]:
    """返回供 reduce_and_package 使用的 get_chapter_content 回调。"""
    return lambda file_path: get_chapter_output(job_id, file_path)


def reduce_and_package(
    input_epub_path: str,
    output_epub_path: str,
    get_chapter_content: Callable[[str], Optional[bytes]],
    direction: str = "ltr",
) -> bool:
    """
    加载 EPUB，用回调提供的章节内容覆盖对应文档，再 TOC 重建并打包。

    :param input_epub_path: 原始 EPUB 路径
    :param output_epub_path: 输出 EPUB 路径
    :param get_chapter_content: (file_path) -> 回写后的章节 HTML 字节，若为 None 则保留原文
    :param direction: 书籍方向，默认 ltr
    :return: 打包是否成功
    """
    unpacker = EpubUnpacker(input_epub_path)
    book = unpacker.load_book()
    if not book:
        return False
    if hasattr(book, "direction"):
        book.direction = direction
    elif hasattr(book, "set_direction"):
        book.set_direction(direction)

    manifest = build_manifest(input_epub_path, "reduce")
    if manifest.get("error"):
        return False
    file_path_to_content: dict[str, bytes] = {}
    for ch in manifest.get("chapters", []):
        if ch.get("chapter_kind") != "body":
            continue
        fp = ch.get("file_path")
        if not fp:
            continue
        content = get_chapter_content(fp)
        if content is not None:
            file_path_to_content[fp] = content

    for item in book.get_items():
        if item is None:
            continue
        if item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        name = item.get_name() if hasattr(item, "get_name") else None
        if not name:
            continue
        if name in file_path_to_content:
            item.set_content(file_path_to_content[name])

    rebuilder = TocRebuilder()
    book = rebuilder.rebuild(book)
    packager = EpubPackager(book, output_epub_path)
    return packager.save()
