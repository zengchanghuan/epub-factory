"""One fail-closed EPUBCheck gate for conversion and translation outputs."""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.models import ErrorCode


@dataclass(frozen=True)
class EpubValidationResult:
    passed: bool
    message: str
    error_code: str | None = None
    warnings: int = 0


def _unavailable(reason: str) -> EpubValidationResult:
    return EpubValidationResult(
        False, f"无法完成 EPUB 校验：{reason}，结果不可交付",
        ErrorCode.EPUB_VALIDATION_UNAVAILABLE.value,
    )


def validate_epub(output_path: str | Path, jar_path: str | Path) -> EpubValidationResult:
    """Require an actual successful process and a complete, consistent JSON report.

    Missing tools, timeouts and malformed reports are infrastructure failures;
    actual ERROR/FATAL findings are publication failures. Neither is deliverable.
    The private temporary directory is removed even if execution/parsing fails.
    """
    output = Path(output_path).absolute()
    jar = Path(jar_path).absolute()
    if not output.is_file():
        return EpubValidationResult(
            False, "EPUB 输出文件不存在，结果不可交付", ErrorCode.EPUB_VALIDATION_FAILED.value,
        )
    if not jar.is_file():
        return _unavailable("服务器缺少 EPUBCheck 校验工具")

    try:
        with tempfile.TemporaryDirectory(prefix="epubcheck_") as temporary:
            report_path = Path(temporary) / "report.json"
            process = subprocess.run(
                ["java", "-jar", str(jar), str(output), "--json", str(report_path)],
                capture_output=True, text=True, timeout=60,
            )
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                return _unavailable("EPUBCheck 未生成有效 JSON 报告")

            if not isinstance(report, dict) or not isinstance(report.get("messages"), list):
                return _unavailable("EPUBCheck 报告结构不完整")
            messages = report["messages"]
            if any(
                not isinstance(message, dict)
                or message.get("severity") not in {"FATAL", "ERROR", "WARNING", "USAGE", "INFO", "SUPPRESSED"}
                for message in messages
            ):
                return _unavailable("EPUBCheck 报告包含无效校验记录")
            counts = {
                severity: sum(message["severity"] == severity for message in messages)
                for severity in ("FATAL", "ERROR", "WARNING")
            }
            checker = report.get("checker")
            if not isinstance(checker, dict) or any(
                type(checker.get(key)) is not int or checker[key] < 0
                for key in ("nFatal", "nError", "nWarning")
            ):
                return _unavailable("EPUBCheck 报告缺少完整校验计数")
            if any(checker[key] != counts[severity] for key, severity in (
                ("nFatal", "FATAL"), ("nError", "ERROR"), ("nWarning", "WARNING"),
            )):
                return _unavailable("EPUBCheck 报告计数与记录不一致")
            if counts["FATAL"] or counts["ERROR"]:
                return EpubValidationResult(
                    False,
                    f"EPUB 校验未通过：{counts['FATAL']} 个致命错误、{counts['ERROR']} 个错误，结果不可交付",
                    ErrorCode.EPUB_VALIDATION_FAILED.value,
                    counts["WARNING"],
                )
            if process.returncode != 0:
                return _unavailable(f"EPUBCheck 异常退出（退出码 {process.returncode}）")
            return EpubValidationResult(True, f"EPUB 校验通过，{counts['WARNING']} 个警告", warnings=counts["WARNING"])
    except FileNotFoundError:
        return _unavailable("服务器缺少 Java 运行环境")
    except subprocess.TimeoutExpired:
        return _unavailable("EPUBCheck 执行超过 60 秒")
    except OSError:
        return _unavailable("EPUBCheck 运行或报告文件访问失败")
