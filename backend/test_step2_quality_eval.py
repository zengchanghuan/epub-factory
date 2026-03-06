"""
Step 2 翻译质量评估：自动抽样对比原文/译文

从原始英文 EPUB 和翻译后的中文 EPUB 中抽取同位置段落，
生成 Markdown 对比报告，供人工审阅。
"""

import sys
from pathlib import Path
from bs4 import BeautifulSoup
import ebooklib
from ebooklib import epub


def extract_paragraphs(epub_path: str) -> dict[str, list[str]]:
    """从 EPUB 中提取每个 chapter 的段落文本"""
    book = epub.read_epub(epub_path)
    chapters = {}
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            name = item.get_name()
            soup = BeautifulSoup(item.get_content(), 'html.parser')
            paras = []
            for tag in soup.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote']):
                text = tag.get_text(strip=True)
                if text and len(text) > 10:
                    paras.append(text)
            if paras:
                chapters[name] = paras
    return chapters


def generate_report(
    original_path: str,
    translated_path: str,
    output_path: str,
    samples_per_chapter: int = 5,
):
    print(f"📖 Loading original: {original_path}")
    original = extract_paragraphs(original_path)
    print(f"📖 Loading translated: {translated_path}")
    translated = extract_paragraphs(translated_path)

    report_lines = [
        "# 翻译质量评估报告",
        "",
        f"- 原文文件: `{Path(original_path).name}`",
        f"- 译文文件: `{Path(translated_path).name}`",
        f"- 每章抽样段落数: {samples_per_chapter}",
        "",
    ]

    total_pairs = 0
    total_chapters = 0

    common_chapters = set(original.keys()) & set(translated.keys())
    for chapter_name in sorted(common_chapters):
        orig_paras = original[chapter_name]
        trans_paras = translated[chapter_name]
        
        if len(orig_paras) < 3:
            continue

        total_chapters += 1
        report_lines.append(f"## {chapter_name}")
        report_lines.append("")

        # 均匀抽样
        n = min(samples_per_chapter, len(orig_paras), len(trans_paras))
        if n == 0:
            report_lines.append("_（无可对比段落）_\n")
            continue

        step = max(1, len(orig_paras) // n)
        indices = list(range(0, len(orig_paras), step))[:n]

        for idx in indices:
            if idx >= len(orig_paras) or idx >= len(trans_paras):
                break
            total_pairs += 1
            orig_text = orig_paras[idx]
            trans_text = trans_paras[idx]

            report_lines.append(f"### 段落 #{idx + 1}")
            report_lines.append("")
            report_lines.append("**原文：**")
            report_lines.append(f"> {orig_text}")
            report_lines.append("")
            report_lines.append("**译文：**")
            report_lines.append(f"> {trans_text}")
            report_lines.append("")

            # 简单的自动指标
            is_translated = orig_text != trans_text
            has_chinese = any('\u4e00' <= c <= '\u9fff' for c in trans_text)
            length_ratio = len(trans_text) / max(len(orig_text), 1)

            status = "✅" if is_translated and has_chinese else "⚠️"
            report_lines.append(f"{status} 状态: {'已翻译' if is_translated else '未翻译'} | "
                                f"含中文: {'是' if has_chinese else '否'} | "
                                f"长度比: {length_ratio:.2f}")
            report_lines.append("")
            report_lines.append("---")
            report_lines.append("")

    report_lines.append(f"\n## 统计汇总\n")
    report_lines.append(f"- 对比章节数: {total_chapters}")
    report_lines.append(f"- 对比段落对数: {total_pairs}")

    report_content = "\n".join(report_lines)
    Path(output_path).write_text(report_content, encoding="utf-8")
    print(f"📝 Report saved to: {output_path}")
    print(f"📊 {total_chapters} chapters, {total_pairs} paragraph pairs compared")


if __name__ == "__main__":
    workspace = Path("/Users/zengchanghuan/Desktop/workspace/epub-factory")

    original_epub = workspace / "backend" / "test_en.epub"
    translated_epub = workspace / "backend" / "outputs" / "test_en-横排简体-翻译_zh-CN.epub"
    report_path = workspace / "backend" / "translation_quality_report.md"

    if not original_epub.exists():
        print(f"❌ Original not found: {original_epub}")
        sys.exit(1)
    if not translated_epub.exists():
        print(f"❌ Translated not found: {translated_epub}")
        sys.exit(1)

    generate_report(str(original_epub), str(translated_epub), str(report_path))
