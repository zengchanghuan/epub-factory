"""Static localized EPUB translation guides; no upload or payment implementation."""
import argparse
from html import escape
import json
from pathlib import Path
from translation_sample import render_sample

ROOT = Path(__file__).resolve().parents[1]
PAGES = {'zh-CN': 'epub-translator.html', 'en': 'epub-translator-en.html',
         'ja': 'epub-translator-ja.html', 'ko': 'epub-translator-ko.html', 'zh-TW': 'epub-translator-zh-tw.html'}
NAMES = {'zh-CN':'简体中文','en':'English','ja':'日本語','ko':'한국어','zh-TW':'繁體中文'}
CONTENT = {
 'en': {
  'title':'EPUB Translator – Translate Ebooks Online | FixEpub',
  'heading':'Translate EPUB ebooks into English, Japanese, Korean or Chinese',
  'description':'Translate EPUB ebooks between English, Japanese, Korean, Simplified and Traditional Chinese. Optional bilingual output and glossary. Review your quote before paying.',
  'action':'Upload an ebook for a translation quote',
  'sections':[
   ('How to translate an EPUB file','Upload an EPUB you have permission to process, select the source and target languages, and review the translation settings. Confirm the file-specific quote before payment. Processing runs on the server; return to the task center in the same browser to download the EPUB.'),
   ('English, Japanese, Korean and Chinese ebook translation','Translate in either direction between English, Japanese, Korean, Simplified Chinese and Traditional Chinese: 20 distinct language pairs. For example, translate a Japanese EPUB into English or an English EPUB into Korean. Chinese character conversion alone is a separate basic service, not a full AI translation.'),
   ('Bilingual EPUB and terminology','Choose bilingual output to read the original alongside the translation, or supply a glossary for names and technical terms. The workflow processes chapters and the table of contents and checks the output. Review important passages, numbers and specialist terminology against the original; AI translation can make mistakes.'),
   ('Translation price and PayPal','Translation has a file-specific quote; the basic US$0.99 conversion price does not cover AI translation. PayPal AI orders have a US$1.49 minimum, not a fixed whole-book price. The displayed USD total includes payment fees. Review the final amount before deciding to pay.'),
   ('Which ebook formats can I translate?','EPUB, TXT, DOCX, MD and Markdown are supported, with EPUB output. Convert MOBI or AZW3 to a supported format first. PDF translation, scanned-page OCR and text embedded in image pixels are not supported. Protected or unparseable files cannot be processed.'),
   ('Can I close the page after paying?','Yes. Processing continues on the server. Use the original browser and task center to check progress and download the result. Do not pay again to recover an existing task; contact support with the task ID if needed.')],
  'examples':'See translation samples','tasks':'Task center','contact':'Contact and support'},
 'ja': {
  'title':'EPUB 翻訳ツール – 電子書籍を英語・日本語などに翻訳 | FixEpub',
  'heading':'EPUB 電子書籍を日本語・英語・韓国語・中国語に翻訳',
  'description':'EPUB を日本語・英語・韓国語・簡体字・繁体字の間で翻訳。原文対訳と用語集に対応。ファイル別の見積もりを確認してから支払えます。',
  'action':'電子書籍をアップロードして見積もりを確認',
  'sections':[
   ('EPUB を翻訳する手順','処理する権利のある EPUB をアップロードし、原文と翻訳先の言語を選択します。翻訳設定とファイル別の見積もりを確認してから支払ってください。完了後は同じブラウザのタスク一覧から EPUB をダウンロードできます。'),
   ('5言語・20方向の相互翻訳','日本語、英語、韓国語、簡体字中国語、繁体字中国語の間で双方向に翻訳できます。英語の EPUB を日本語に、日本語の EPUB を英語に翻訳する場合などに利用できます。中国語の繁簡変換だけを行う基本サービスとは別です。'),
   ('原文対訳と用語集','原文と訳文を併記する形式を選べます。人名や専門用語の対応表も指定できます。章や目次を処理し、出力を検査しますが、重要な文章や数値は原文と照合してください。AI 翻訳には誤りが生じる場合があります。'),
   ('翻訳料金と PayPal','翻訳料金はファイルごとに見積もります。基本変換の US$0.99 は翻訳料金ではありません。PayPal の AI 注文は最低 US$1.49 で、全書籍の固定料金ではありません。表示される米ドル総額には決済手数料が含まれます。'),
   ('対応ファイルと制限','EPUB、TXT、DOCX、MD、Markdown を入力し、EPUB を出力します。MOBI と AZW3 は事前に対応形式へ変換してください。PDF 翻訳、スキャン画像の OCR、画像内文字の翻訳には対応していません。保護されたファイルや解析できないファイルは処理できません。'),
   ('ページを閉じた後のダウンロード','処理はサーバーで続行されます。元のブラウザのタスク一覧で進捗を確認し、結果を保存してください。既存タスクを探すために再度支払う必要はありません。')],
  'examples':'翻訳の例を見る','tasks':'タスク一覧','contact':'お問い合わせ'},
 'ko': {
  'title':'EPUB 번역기 – 전자책 영어·한국어·일본어 번역 | FixEpub',
  'heading':'EPUB 전자책을 한국어·영어·일본어·중국어로 번역',
  'description':'EPUB 전자책을 한국어, 영어, 일본어, 중국어 간체·번체 사이에서 번역하세요. 원문 대조와 용어집을 지원하며 결제 전에 견적을 확인합니다.',
  'action':'전자책 업로드 및 번역 견적 확인',
  'sections':[
   ('EPUB 파일 번역 방법','처리 권한이 있는 EPUB을 업로드하고 원문 및 대상 언어를 선택하세요. 번역 설정과 파일별 견적을 확인한 다음 결제합니다. 완료 후 같은 브라우저의 작업 목록에서 EPUB을 다운로드할 수 있습니다.'),
   ('5개 언어와 20개 번역 방향','한국어, 영어, 일본어, 중국어 간체와 번체 사이의 양방향 번역을 지원합니다. 영어 EPUB을 한국어로, 한국어 EPUB을 일본어로 번역할 수 있습니다. 중국어 간체·번체 문자 변환만 하는 기본 서비스와 AI 번역은 별개입니다.'),
   ('원문 대조와 용어집','원문과 번역문을 함께 읽는 대조 형식을 선택하거나 인명 및 전문 용어의 대응표를 제공할 수 있습니다. 장과 목차를 처리하고 결과를 검사하지만 중요한 문장, 수치 및 전문 용어는 원문과 대조해 주세요. AI 번역에는 오류가 있을 수 있습니다.'),
   ('번역 요금 및 PayPal','번역은 파일별 견적을 제공합니다. 기본 변환 가격 US$0.99에 AI 번역은 포함되지 않습니다. PayPal AI 주문의 최소 금액은 US$1.49이며 모든 책의 고정 요금이 아닙니다. 표시된 달러 총액에는 결제 수수료가 포함됩니다.'),
   ('지원 형식과 제한','EPUB, TXT, DOCX, MD, Markdown을 입력하고 EPUB으로 받습니다. MOBI와 AZW3는 지원 형식으로 먼저 변환해야 합니다. PDF 번역, 스캔 OCR, 이미지 내부 텍스트 번역은 지원하지 않습니다. 보호되었거나 해석할 수 없는 파일은 처리할 수 없습니다.'),
   ('페이지를 닫아도 되나요?','서버에서 처리가 계속됩니다. 원래 브라우저의 작업 목록에서 진행 상황과 결과를 확인하세요. 기존 작업을 찾기 위해 다시 결제하지 마세요.')],
  'examples':'번역 예시 보기','tasks':'작업 목록','contact':'문의 및 지원'},
 'zh-TW': {
  'title':'EPUB 翻譯工具 – 電子書中英日韓互譯與雙語對照 | FixEpub',
  'heading':'EPUB 電子書翻譯：繁中、簡中、英語、日語與韓語互譯',
  'description':'將 EPUB 翻譯成繁體中文、簡體中文、英語、日語或韓語，支援雙語對照與術語表。上傳後確認檔案報價，再決定是否付款。',
  'action':'上傳電子書並查看翻譯報價',
  'sections':[
   ('如何翻譯 EPUB 電子書','上傳您有權處理的 EPUB，選擇原文與目標語言，檢查翻譯設定與檔案報價，再決定是否付款。完成後回到原瀏覽器的任務中心下載 EPUB。'),
   ('五種語言、20 個翻譯方向','繁體中文、簡體中文、英語、日語與韓語支援兩兩互譯。例如將日文 EPUB 翻成繁體中文，或將繁體中文書稿翻成英語。只做繁簡字形轉換的基礎服務與 AI 全文翻譯不同。'),
   ('雙語對照與術語表','可選擇原文與譯文對照閱讀，也可提供人名及專業詞彙的術語表。系統處理章節與目錄並檢查輸出；AI 翻譯仍可能出錯，重要段落、數字及專業用語請與原文核對。'),
   ('翻譯費用與 PayPal','翻譯依檔案報價，不包含在 US$0.99 的基礎轉換費內。PayPal AI 訂單最低 US$1.49，並非所有書籍的固定翻譯價。顯示的美元總額已含付款手續費，付款前請確認最終金額。'),
   ('支援格式與限制','可上傳 EPUB、TXT、DOCX、MD 或 Markdown，輸出 EPUB。MOBI 與 AZW3 請先轉成支援的格式。尚不支援 PDF 翻譯、掃描 OCR 或圖片像素內文字翻譯。受保護或無法解析的檔案不能處理。'),
   ('關閉頁面後如何取回結果','處理會在伺服器繼續執行。請用原瀏覽器進入任務中心查看進度並下載，不要為了找回既有任務再次付款。')],
  'examples':'查看翻譯效果示例','tasks':'任務中心','contact':'聯絡客服'}
}

def alternates():
    return '\n'.join(f'<link rel="alternate" hreflang="{lang}" href="https://fixepub.com/{name}" />' for lang,name in PAGES.items())+'\n<link rel="alternate" hreflang="x-default" href="https://fixepub.com/epub-translator-en.html" />'

def navigation():
    return ' · '.join(f'<a href="/{name}" lang="{lang}" hreflang="{lang}">{NAMES[lang]}</a>' for lang,name in PAGES.items())

def render(lang):
    d=CONTENT[lang]; url='https://fixepub.com/'+PAGES[lang]
    css=(ROOT/'scripts/templates/tool-landing.html').read_text().split('<style>')[1].split('</style>')[0]
    css += '.topbar{flex-wrap:wrap}.topbar nav{flex-wrap:wrap} @media(prefers-color-scheme:dark){:root{background:#141b24;color:#e8edf5}.panel,section.panel{background:#1c2633;border-color:#3b485d}.brand,a{color:#9abfff}.lead,.features p,.notice{color:#becbdd}}'
    sections=''.join(f'<section class="panel"><h2>{escape(h)}</h2><p>{escape(p)}</p></section>' for h,p in d['sections'])
    action=f'<a class="button primary" data-entry-link="upload" data-growth-service="translation" href="/?tool=translate&amp;lang={lang}">{escape(d["action"])}</a>'
    schema=json.dumps({'@context':'https://schema.org','@type':'WebPage','name':d['title'],'description':d['description'],'url':url,'inLanguage':lang},ensure_ascii=False)
    return f'''<!doctype html>
<!-- Generated by scripts/generate_translation_pages.py -->
<html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(d['title'])}</title><meta name="description" content="{escape(d['description'],quote=True)}">
<link rel="canonical" href="{url}">{alternates()}
<meta property="og:type" content="website"><meta property="og:title" content="{escape(d['title'],quote=True)}"><meta property="og:description" content="{escape(d['description'],quote=True)}"><meta property="og:url" content="{url}">
<script type="application/ld+json">{schema}</script><style>{css}</style>
<script src="/tool-entry.js?v=20261026-seo" defer></script><script src="/growth-analytics.js?v=20261026-r2" defer></script></head>
<body data-tool-entry="translate"><header class="topbar"><a class="brand" href="/?lang={lang}">FixEpub</a><nav aria-label="Language">{navigation()}</nav></header>
<main><section class="hero"><h1>{escape(d['heading'])}</h1><p class="lead">{escape(d['description'])}</p><div class="actions">{action}<a class="button" href="/examples.html?tool=translate&amp;lang={lang}">{escape(d['examples'])}</a></div></section>
{render_sample(lang)}{sections}<div class="actions">{action}<a href="/?view=tasks&amp;lang={lang}" data-entry-link="tasks">{escape(d['tasks'])}</a></div></main>
<footer><a href="/contact-en.html">{escape(d['contact'])}</a> · <a href="/privacy-en.html">Privacy</a> · <a href="/terms-en.html">Terms</a> · © 2026 FixEpub</footer></body></html>\n'''

def generate(check=False):
    for lang in CONTENT:
        p=ROOT/'frontend'/PAGES[lang];content=render(lang)
        if check:
            assert p.read_text()==content, str(p)
        else:p.write_text(content)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');generate(parser.parse_args().check)
