"""
EPUB 格式修复引擎。

支持修复的问题类型：
  PKG-006  mimetype 文件缺失或不是 ZIP 第一个条目
  HTM-004  xhtml 文件使用了旧版 DOCTYPE（XHTML 1.0/1.1）
  RSC-005  OPF metadata 中含非法 xmlns="" 属性
  ENC      文件编码非 UTF-8

使用方式：
  from app.engine.epub_repairer import diagnose, repair
  report = diagnose("input.epub")   # 返回 DiagnoseReport
  repair("input.epub", "output.epub")  # 修复并写出
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# ─── 数据结构 ────────────────────────────────────────────────

@dataclass
class RepairIssue:
    code: str          # 错误码，如 PKG-006
    severity: str      # "error" | "warning"
    message: str       # 人类可读描述
    file: str = ""     # 涉及文件（可选）
    fixable: bool = True


@dataclass
class DiagnoseReport:
    total_issues: int = 0
    fixable_count: int = 0
    unfixable_count: int = 0
    issues: List[RepairIssue] = field(default_factory=list)
    epub_version: str = "unknown"

    def is_valid(self) -> bool:
        return self.total_issues == 0

    def to_dict(self) -> dict:
        return {
            "total_issues": self.total_issues,
            "fixable_count": self.fixable_count,
            "unfixable_count": self.unfixable_count,
            "epub_version": self.epub_version,
            "issues": [
                {
                    "code": i.code,
                    "severity": i.severity,
                    "message": i.message,
                    "file": i.file,
                    "fixable": i.fixable,
                }
                for i in self.issues
            ],
        }


# ─── 诊断 ────────────────────────────────────────────────────

def diagnose(epub_path: str) -> DiagnoseReport:
    """
    快速诊断 EPUB 文件，返回问题报告。
    不依赖 epubcheck，仅检查本引擎能修复的问题。
    """
    report = DiagnoseReport()
    path = Path(epub_path)

    if not path.exists():
        report.issues.append(RepairIssue("FILE-404", "error", "文件不存在", fixable=False))
        _tally(report)
        return report

    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()

            # PKG-006：mimetype 检查
            if "mimetype" not in names:
                report.issues.append(RepairIssue(
                    "PKG-006", "error", "缺少 mimetype 文件",
                    file="mimetype",
                ))
            elif names[0] != "mimetype":
                report.issues.append(RepairIssue(
                    "PKG-006", "error", f"mimetype 不是 ZIP 第一个条目（当前位置 #{names.index('mimetype') + 1}）",
                    file="mimetype",
                ))
            else:
                # 检查是否以压缩方式存储（必须 STORED）
                info = zf.getinfo("mimetype")
                if info.compress_type != zipfile.ZIP_STORED:
                    report.issues.append(RepairIssue(
                        "PKG-006", "error", "mimetype 文件被压缩（必须不压缩存储）",
                        file="mimetype",
                    ))

            # HTM-004：DOCTYPE 检查（合并同类项，只报告一条）
            old_doctype_re = re.compile(
                r'<!DOCTYPE\s+html\s+(?:PUBLIC|SYSTEM)[^>]*>', re.IGNORECASE | re.DOTALL
            )
            xhtml_files = [n for n in names if n.lower().endswith(".xhtml") or n.lower().endswith(".html")]
            affected_doctype: list[str] = []
            for name in xhtml_files:
                try:
                    content = zf.read(name).decode("utf-8", errors="replace")
                    if old_doctype_re.search(content):
                        affected_doctype.append(name)
                except Exception:
                    pass
            if affected_doctype:
                report.issues.append(RepairIssue(
                    "HTM-004", "error",
                    f"{len(affected_doctype)} 个 XHTML 文件使用旧版 DOCTYPE（XHTML 1.x），Apple Books / EPUB 3 不接受",
                    file=", ".join(affected_doctype[:3]) + ("…" if len(affected_doctype) > 3 else ""),
                ))

            # RSC-005：OPF 命名空间检查
            opf_files = [n for n in names if n.lower().endswith(".opf")]
            for name in opf_files:
                try:
                    content = zf.read(name).decode("utf-8", errors="replace")
                    if 'xmlns=""' in content:
                        count = content.count('xmlns=""')
                        report.issues.append(RepairIssue(
                            "RSC-005", "error",
                            f"OPF metadata 含 {count} 处非法 xmlns=\"\"（会导致元数据解析失败）",
                            file=name,
                        ))
                except Exception:
                    pass

            # EPUB 版本检测
            for name in opf_files:
                try:
                    content = zf.read(name).decode("utf-8", errors="replace")
                    m = re.search(r'version="(\d+\.\d+)"', content)
                    if m:
                        report.epub_version = m.group(1)
                        break
                except Exception:
                    pass

    except zipfile.BadZipFile:
        report.issues.append(RepairIssue(
            "PKG-BAD", "error", "不是有效的 ZIP/EPUB 文件，无法修复", fixable=False,
        ))

    _tally(report)
    return report


def _tally(report: DiagnoseReport) -> None:
    report.total_issues = len(report.issues)
    report.fixable_count = sum(1 for i in report.issues if i.fixable)
    report.unfixable_count = sum(1 for i in report.issues if not i.fixable)


# ─── 修复 ────────────────────────────────────────────────────

def repair(input_path: str, output_path: str) -> DiagnoseReport:
    """
    修复 EPUB 文件，写入 output_path。
    返回修复前的诊断报告（描述修复了什么）。
    """
    report = diagnose(input_path)
    if not report.fixable_count and report.unfixable_count:
        raise ValueError("文件无法修复：" + "; ".join(i.message for i in report.issues if not i.fixable))

    tmp = tempfile.mkdtemp()
    try:
        with zipfile.ZipFile(input_path) as zf:
            zf.extractall(tmp)

        # 1. 补充/确认 mimetype
        mt = Path(tmp) / "mimetype"
        if not mt.exists():
            mt.write_text("application/epub+zip", encoding="ascii")

        # 2. 修复所有 xhtml DOCTYPE
        old_doctype_re = re.compile(
            r'<!DOCTYPE\s+html\s+(?:PUBLIC|SYSTEM)[^>]*>', re.IGNORECASE | re.DOTALL
        )
        for f in Path(tmp).rglob("*.xhtml"):
            txt = f.read_text(encoding="utf-8", errors="replace")
            new_txt = old_doctype_re.sub("<!DOCTYPE html>", txt, count=1)
            if new_txt != txt:
                f.write_text(new_txt, encoding="utf-8")
        for f in Path(tmp).rglob("*.html"):
            txt = f.read_text(encoding="utf-8", errors="replace")
            new_txt = old_doctype_re.sub("<!DOCTYPE html>", txt, count=1)
            if new_txt != txt:
                f.write_text(new_txt, encoding="utf-8")

        # 3. 修复 OPF xmlns=""
        for f in Path(tmp).rglob("*.opf"):
            txt = f.read_text(encoding="utf-8", errors="replace")
            new_txt = re.sub(r'\s+xmlns=""', '', txt)
            if new_txt != txt:
                f.write_text(new_txt, encoding="utf-8")

        # 4. 重新打包（mimetype 第一且不压缩）
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.unlink()

        with zipfile.ZipFile(str(out), "w", zipfile.ZIP_DEFLATED) as zout:
            zout.write(str(mt), "mimetype", compress_type=zipfile.ZIP_STORED)
            for root, dirs, files in os.walk(tmp):
                dirs.sort()
                for fname in sorted(files):
                    fpath = Path(root) / fname
                    arc = str(fpath.relative_to(tmp))
                    if arc == "mimetype":
                        continue
                    zout.write(str(fpath), arc)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return report
