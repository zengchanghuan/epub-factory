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
        "title": "EPUB 电子书翻译与双语对照 – FixEpub",
        "heading": "电子书翻译与双语对照",
        "description": "EPUB、TXT、DOCX、Markdown 支持简中、繁中、英、日、韩五语互译与双语对照。前往主页统一上传、确认报价、付款并查看结果；暂不支持 PDF。",
        "intro": "简体中文、繁体中文、英语、日语、韩语两两互译，可生成原文与译文对照版本。上传、翻译选项、当前报价、支付和任务结果均在主页统一管理。",
        "action": "前往主页开始翻译",
        "pricing": "电子书翻译按主页当前报价收费。上传后查看报价与翻译选项，付款前确认最终金额；本页不收款，也不创建任务。",
        "formats": "翻译支持 .epub、.docx、.txt、.md 和 .markdown。DOCX 与 Markdown 会先规范化为 EPUB；缺失资源、外部图片或不支持的内容会在检查时提示。MOBI / AZW3 不提供直接翻译，请先准备 EPUB。",
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
        "description": "将 EPUB 竖排转换为横排，选择简体或繁体输出及阅读器设置。基础转换 ¥0.99 / 本，用词精校另计；PDF 使用独立的原文转换入口。",
        "intro": "调整电子书的阅读方向，方便在手机、Kindle 或 Apple Books 上阅读。请在主页选择输出文字和目标设备，再确认转换费用。",
        "action": "前往主页转换横排",
        "pricing": "竖排改横排基础转换价为 <strong>¥0.99 / 本</strong>。用词精校不包含在基础价中，启用时另行计费；最终金额以主页报价为准。",
        "formats": "主页转换支持 .epub、.mobi、.azw3、.docx、.txt、.md 和 .markdown；竖排处理主要针对 EPUB 中的文字与样式。不同源格式会先进行检查，源文件损坏或资源缺失可能阻止转换。",
        "features": (
            ("横排阅读", "处理可识别的竖排样式与相关排版设置。扫描图片或固定版面中的文字不会因此重排。"),
            ("选择文字与设备", "可选择横排简体或横排繁体，以及通用、Kindle 或 Apple Books 设置。繁简转换不是日文等语言的翻译。"),
            ("批量处理", "主页支持一次选择多本书或文件夹中的文件，当前每批最多 10 个；批量模式仅用于转换与排版处理。"),
        ),
        "faqs": (
            ("只改成横排会自动翻译吗？", "不会。排版转换与 电子书翻译是不同任务；请在主页确认所选模式和输出文字。"),
            ("复杂目录和脚注一定能修好吗？", "转换会尽量保留并校验目录、链接与脚注结构，但无法从不存在的原始信息中可靠补齐目标。遇到无法安全修复的问题会提示，请核对成品。"),
            ("基础价包含 用词精校吗？", "不包含。用词精校支持单本 EPUB、TXT、DOCX 或 Markdown 的简中、繁中、英语、日语或韩语，属于另行报价的可选服务；非简中精校保留原语言。"),
        ),
    },
    {
        "filename": "traditional-to-simplified.html", "tool": "simplified",
        "title": "EPUB 繁体转简体与地域词汇转换 – FixEpub",
        "heading": "EPUB 繁体转简体",
        "description": "使用 OpenCC 进行 EPUB 繁简转换，支持选择台湾、港澳或通用繁体来源。基础转换 ¥0.99 / 本，用词精校另计；PDF 使用独立的原文转换入口。",
        "intro": "转换电子书中的繁简文字，并按来源选择地域用语规则。主页也可选择横排繁体输出，统一管理上传、报价和转换结果。",
        "action": "前往主页转换简体",
        "pricing": "繁简基础转换价为 <strong>¥0.99 / 本</strong>。用词精校不包含在基础价中，启用时另行计费；最终金额以主页报价为准。",
        "formats": "主页转换支持 .epub、.mobi、.azw3、.docx、.txt、.md 和 .markdown。繁简处理针对可提取的文字，不会识别或改写图片像素里的文字。",
        "features": (
            ("繁简与地域用语", "基于 OpenCC 及配置的词汇规则进行转换。根据原书来源选择自动 / 通用、台湾或港澳选项。"),
            ("排版与结构检查", "转换同时处理横排输出及适用的样式兼容问题，并检查成品结构。复杂原书仍建议下载后核对目录、脚注与插图。"),
            ("可选 用词精校", "普通规则转换不调用翻译模型。单个 EPUB、TXT、DOCX 或 Markdown 转为简体时可另选 用词精校，对符合条件的风险用语进行检查，不等同于全书重译。"),
        ),
        "faqs": (
            ("为什么要选择台湾或港澳繁体来源？", "相同文字在不同地区可能有不同常用词。来源选项影响对应规则，应按原书内容选择，不确定时可先使用自动 / 通用。"),
            ("可以一次转换多本书吗？", "可以。主页支持多文件及文件夹选择，每批最多 10 个文件；用词精校支持单个 EPUB、TXT、DOCX 或 Markdown，不能与批量转换合用。"),
            ("费用与任务状态在哪里查看？", "基础转换 ¥0.99 / 本，用词精校另计；最终金额在主页付款前确认。已有任务请使用原浏览器到主页任务中心查看，不要重复付款。"),
        ),
    },
)


PAGES += ({'filename': 'txt-to-epub.html',
  'tool': 'simplified',
  'title': 'TXT 转 EPUB：编码与章节预览 | FixEpub',
  'heading': 'TXT 转 EPUB：编码与章节预览',
  'description': '把纯文本整理成可阅读的 EPUB。先在本机查看编码、正文片段和自动识别的章标题，再确认处理选项与报价。',
  'intro': '把纯文本整理成可阅读的 EPUB。先在本机查看编码、正文片段和自动识别的章标题，再确认处理选项与报价。',
  'action': '前往主页选择文件与服务',
  'pricing': '基础转换为人民币 ¥0.99 / 本或 PayPal US$0.99 / 本。翻译与精校另行报价，PayPal AI 订单最低 '
             'US$1.49；组合订单总额以付款前报价为准。修改对照导出不另收精校费。',
  'formats': 'TXT 转换接受 UTF-8 或带 BOM 的 UTF-16；精校支持单个 EPUB、TXT、DOCX 或 Markdown。输出 EPUB，不支持扫描 PDF OCR 或 PDF 精校。',
  'features': (('本机预览',
                '选择单个 TXT 后自动预览，不上传、不调用模型、不收费。预览限 2 MB，显示前 600 字、前 30 个章标题；正式 TXT 上传上限为 60 MB。'),
               ('章节与编码', '接受 UTF-8 或带 BOM 的 UTF-16，识别常见中、英、日、韩章标题。没有识别到章标题也会保留正文；当前预览不支持手动修改目录。'),
               ('示例',
                'Chapter 1: Library 或 第一章 图书馆 可识别为目录项，正文段落保留。纯文本中的 HTML 和 Markdown '
                '标记不会执行或当作富文本解析。')),
  'faqs': (('预览会创建支付订单吗？', '不会。本机 TXT 预览不上传文件。只有按主页正常流程提交并确认报价后才进入付款。'),
           ('能保证 AI 修改都正确吗？', '不能。请复核数字、人名、否定关系和专业术语，不需要的修改可在对照中撤销。'))},
 {'filename': 'ebook-proofreading.html',
  'tool': 'simplified',
  'title': '电子书 AI 精校：五语言原文校对 | FixEpub',
  'heading': '电子书 AI 精校：五语言原文校对',
  'description': '为单个 EPUB、TXT、DOCX 或 Markdown 检查用词、语法与拼写，精校完成后逐条核对修改。支持简中、繁中、英语、日语和韩语。',
  'intro': '为单个 EPUB、TXT、DOCX 或 Markdown 检查用词、语法与拼写，精校完成后逐条核对修改。支持简中、繁中、英语、日语和韩语。',
  'action': '前往主页选择文件与服务',
  'pricing': '基础转换为人民币 ¥0.99 / 本或 PayPal US$0.99 / 本。翻译与精校另行报价，PayPal AI 订单最低 '
             'US$1.49；组合订单总额以付款前报价为准。修改对照导出不另收精校费。',
  'formats': 'TXT 转换接受 UTF-8 或带 BOM 的 UTF-16；精校支持单个 EPUB、TXT、DOCX 或 Markdown。输出 EPUB，不支持扫描 PDF OCR 或 PDF 精校。',
  'features': (('按语言精校', '简中主要检查地域风险用词；繁中、英语、日语、韩语检查原语言表达。精校与翻译分开处理，不能替代专业编辑。'),
               ('修改示例',
                '英语 This are a short test. 可校正为 This is a short test. 实际修改取决于上下文，重要表述请人工复核。'),
               ('如何启用', '在主页选择单个 EPUB、TXT、DOCX 或 Markdown，选择精校语言并开启精校，检查组合报价再付款。模型可能不建议修改，不保证每段都产生改动。')),
  'faqs': (('预览会创建支付订单吗？', '不会。本机 TXT 预览不上传文件。只有按主页正常流程提交并确认报价后才进入付款。'),
           ('能保证 AI 修改都正确吗？', '不能。请复核数字、人名、否定关系和专业术语，不需要的修改可在对照中撤销。'))},
 {'filename': 'proofreading-changes.html',
  'tool': 'simplified',
  'title': '精校修改对照：保留、撤销与下载 | FixEpub',
  'heading': '精校修改对照：保留、撤销与下载',
  'description': '对已完成的精校任务比较原文与修改后文本，选择保留或撤销建议，再下载自己的版本。',
  'intro': '对已完成的精校任务比较原文与修改后文本，选择保留或撤销建议，再下载自己的版本。',
  'action': '前往主页选择文件与服务',
  'pricing': '基础转换为人民币 ¥0.99 / 本或 PayPal US$0.99 / 本。翻译与精校另行报价，PayPal AI 订单最低 '
             'US$1.49；组合订单总额以付款前报价为准。修改对照导出不另收精校费。',
  'formats': 'TXT 转换接受 UTF-8 或带 BOM 的 UTF-16；精校支持单个 EPUB、TXT、DOCX 或 Markdown。输出 EPUB，不支持扫描 PDF OCR 或 PDF 精校。',
  'features': (('逐条对照', '从任务结果打开修改对照，可翻页查看并选择撤销某条修改；切换任务不会显示上一任务的正文。'),
               ('下载选择版本', '保留全部、撤销全部或部分撤销均生成对应 EPUB，不覆盖最初成品，也不会再次调用精校模型。'),
               ('适用范围', '需要已完成并带修改记录的精校任务及原任务访问权限。旧任务可能没有对照记录；此功能不是任意文本编辑器。')),
  'faqs': (('预览会创建支付订单吗？', '不会。本机 TXT 预览不上传文件。只有按主页正常流程提交并确认报价后才进入付款。'),
           ('能保证 AI 修改都正确吗？', '不能。请复核数字、人名、否定关系和专业术语，不需要的修改可在对照中撤销。'))})

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
