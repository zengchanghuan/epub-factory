"""Generate static descriptions, never a second upload/payment client."""
from __future__ import annotations

import argparse
from html import escape
import json
from pathlib import Path
from string import Template


ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "tool-landing.html"
PAGES = (
    {
        "filename": "epub-translator.html", "tool": "translate",
        "title": "AI EPUB 电子书翻译与双语对照 – FixEpub",
        "heading": "AI 电子书翻译与双语对照",
        "description": "使用 FixEpub 翻译 EPUB、DOCX 和 Markdown，支持双语对照。前往主页统一上传、确认报价、付款并查看结果；暂不支持 PDF。",
        "intro": "将英文电子书翻译为中文，或生成原文与译文对照版本。上传、翻译选项、当前报价、支付和任务结果均在主页统一管理。",
        "action": "前往主页开始翻译",
        "pricing": "AI 翻译按主页当前报价收费。上传后查看报价与翻译选项，付款前确认最终金额；本页不收款，也不创建任务。",
        "formats": "翻译支持 .epub、.docx、.md 和 .markdown。DOCX 与 Markdown 会先规范化为 EPUB；缺失资源、外部图片或不支持的内容会在检查时提示。MOBI / AZW3 不提供直接翻译，请先准备 EPUB。",
        "features": (
            ("选择翻译方式", "在主页选择目标语言、翻译档位和策略；需要对照阅读时可开启双语版本。"),
            ("术语与结构", "可提供术语对照表。系统会处理目录、正文和可识别的说明文字，并执行成品质检；复杂原书仍建议人工核对。"),
            ("关闭页面后返回", "任务在服务端执行。使用同一浏览器进入主页任务中心，可继续查看状态并下载已完成的文件。"),
        ),
        "faqs": (
            ("图片里的文字也会翻译吗？", "可识别的 HTML 图片说明可进入翻译流程；嵌在图片像素里的文字暂不翻译，OCR 与图文重绘仍是后续计划。"),
            ("翻译费用在哪里确认？", "在主页上传文件并选择翻译选项后查看当前报价，确认后再付款。本页不提供固定每本翻译价。"),
            ("已有任务在哪里找？", "前往主页任务中心，使用原来上传文件的浏览器查看。旧任务链接会由统一入口接续；不要重新上传或重复付款。"),
        ),
    },
    {
        "filename": "vertical-to-horizontal.html", "tool": "horizontal",
        "title": "EPUB 竖排转横排与阅读器排版优化 – FixEpub",
        "heading": "EPUB 竖排转横排",
        "description": "将 EPUB 竖排转换为横排，选择简体或繁体输出及阅读器设置。基础转换 ¥0.99 / 本，AI 精校另计；统一在主页处理，暂不支持 PDF。",
        "intro": "调整电子书的阅读方向，方便在手机、Kindle 或 Apple Books 上阅读。请在主页选择输出文字和目标设备，再确认转换费用。",
        "action": "前往主页转换横排",
        "pricing": "竖排改横排基础转换价为 <strong>¥0.99 / 本</strong>。AI 精校不包含在基础价中，启用时另行计费；最终金额以主页报价为准。",
        "formats": "主页转换支持 .epub、.mobi、.azw3、.docx、.md 和 .markdown；竖排处理主要针对 EPUB 中的文字与样式。不同源格式会先进行检查，源文件损坏或资源缺失可能阻止转换。",
        "features": (
            ("横排阅读", "处理可识别的竖排样式与相关排版设置。扫描图片或固定版面中的文字不会因此重排。"),
            ("选择文字与设备", "可选择横排简体或横排繁体，以及通用、Kindle 或 Apple Books 设置。繁简转换不是日文等语言的翻译。"),
            ("批量处理", "主页支持一次选择多本书或文件夹中的文件，当前每批最多 10 个；批量模式仅用于转换与排版处理。"),
        ),
        "faqs": (
            ("只改成横排会自动翻译吗？", "不会。排版转换与 AI 翻译是不同任务；请在主页确认所选模式和输出文字。"),
            ("复杂目录和脚注一定能修好吗？", "转换会尽量保留并校验目录、链接与脚注结构，但无法从不存在的原始信息中可靠补齐目标。遇到无法安全修复的问题会提示，请核对成品。"),
            ("基础价包含 AI 精校吗？", "不包含。AI 精校仅适用于单个 EPUB 的普通简体转换，属于另行报价的可选服务。"),
        ),
    },
    {
        "filename": "traditional-to-simplified.html", "tool": "simplified",
        "title": "EPUB 繁体转简体与地域词汇转换 – FixEpub",
        "heading": "EPUB 繁体转简体",
        "description": "使用 OpenCC 进行 EPUB 繁简转换，支持选择台湾、港澳或通用繁体来源。基础转换 ¥0.99 / 本，AI 精校另计；统一在主页处理，暂不支持 PDF。",
        "intro": "转换电子书中的繁简文字，并按来源选择地域用语规则。主页也可选择横排繁体输出，统一管理上传、报价和转换结果。",
        "action": "前往主页转换简体",
        "pricing": "繁简基础转换价为 <strong>¥0.99 / 本</strong>。AI 精校不包含在基础价中，启用时另行计费；最终金额以主页报价为准。",
        "formats": "主页转换支持 .epub、.mobi、.azw3、.docx、.md 和 .markdown。繁简处理针对可提取的文字，不会识别或改写图片像素里的文字。",
        "features": (
            ("繁简与地域用语", "基于 OpenCC 及配置的词汇规则进行转换。根据原书来源选择自动 / 通用、台湾或港澳选项。"),
            ("排版与结构检查", "转换同时处理横排输出及适用的样式兼容问题，并检查成品结构。复杂原书仍建议下载后核对目录、脚注与插图。"),
            ("可选 AI 精校", "普通规则转换不调用翻译模型。单个 EPUB 转为简体时可另选 AI 精校，对符合条件的风险用语进行检查，不等同于全书重译。"),
        ),
        "faqs": (
            ("为什么要选择台湾或港澳繁体来源？", "相同文字在不同地区可能有不同常用词。来源选项影响对应规则，应按原书内容选择，不确定时可先使用自动 / 通用。"),
            ("可以一次转换多本书吗？", "可以。主页支持多文件及文件夹选择，每批最多 10 个文件；AI 精校只支持单个 EPUB 的普通简体转换。"),
            ("费用与任务状态在哪里查看？", "基础转换 ¥0.99 / 本，AI 精校另计；最终金额在主页付款前确认。已有任务请使用原浏览器到主页任务中心查看，不要重复付款。"),
        ),
    },
)


def render_page(page, template):
    canonical = "https://fixepub.com/" + page["filename"]
    features = "\n".join(
        f'        <article><h3>{escape(title)}</h3><p>{escape(body)}</p></article>'
        for title, body in page["features"]
    )
    faqs = "\n".join(
        f'      <details><summary>{escape(question)}</summary><p>{escape(answer)}</p></details>'
        for question, answer in page["faqs"]
    )
    schema = {"@context": "https://schema.org", "@type": "WebPage", "name": page["title"],
              "description": page["description"], "url": canonical, "inLanguage": "zh-CN"}
    values = {key: escape(page[key], quote=True) for key in
              ("tool", "title", "heading", "description", "intro", "action", "formats")}
    values.update(canonical=escape(canonical, quote=True), features=features, faqs=faqs,
                  # Only fixed generator configuration contains price markup.
                  pricing=page["pricing"],
                  schema=json.dumps(schema, ensure_ascii=False, indent=2).replace("<", "\\u003c"))
    return Template(template).substitute(values)


def generate_seo_pages(*, check=False, output_dir=None):
    output = Path(output_dir) if output_dir is not None else ROOT / "frontend"
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    mismatches = []
    for page in PAGES:
        rendered = render_page(page, template)
        path = output / page["filename"]
        if check:
            if not path.is_file() or path.read_text(encoding="utf-8") != rendered:
                mismatches.append(page["filename"])
        else:
            output.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
            print(f"Generated {page['filename']}")
    if mismatches:
        print("Static tool pages are out of date: " + ", ".join(mismatches))
        return False
    if check:
        print("Static tool pages match their dedicated template.")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify generated pages without writing files")
    parser.add_argument("--output-dir", type=Path, help="Generate or check in an isolated destination")
    args = parser.parse_args()
    return 0 if generate_seo_pages(check=args.check, output_dir=args.output_dir) else 1


if __name__ == "__main__":
    raise SystemExit(main())
