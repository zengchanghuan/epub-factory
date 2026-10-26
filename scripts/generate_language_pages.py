"""Static, crawlable language guides. No account data, uploads or payment client."""
from html import escape
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LANGUAGES = {'zh-CN': ('ebook-tools.html', '简体中文'), 'zh-TW': ('zh-tw.html', '繁體中文'),
             'en': ('en.html', 'English'), 'ja': ('ja.html', '日本語'), 'ko': ('ko.html', '한국어')}
CONTENT = {
 'zh-CN': {
  'title':'电子书五语翻译、精校与 EPUB 修复 | FixEpub',
  'description':'FixEpub 支持简中、繁中、英、日、韩电子书互译与精校，EPUB 格式修复、繁简转换和文字型 PDF 转 EPUB。确认报价后支付，支持 PayPal。',
  'heading':'翻译、精校与修复你的电子书', 'intro':'把自己的书稿处理成便于阅读的 EPUB。按文件选择服务，先查看报价，再决定是否付款。',
  'action':'打开工具并查看报价', 'contact':'联系客服',
  'sections':[
   ('五种语言互译','简体中文、繁体中文、英语、日语和韩语支持 20 个不同语言方向。翻译可上传 EPUB、TXT、DOCX、MD 或 Markdown，输出 EPUB。可选择双语对照、翻译策略和术语表；重要译文建议与原文核对。'),
   ('同语言精校','单本 EPUB、TXT、DOCX 或 Markdown 支持五种语言精校。简中检查两岸风险用词；繁中、英语、日语和韩语检查拼写、语法及用词，保留原语言。精校与翻译分开处理，不会自动执行另一项服务。'),
   ('格式修复与排版','EPUB 修复先诊断包结构和资源引用。繁简转换支持台湾、港澳和通用来源；排版可选择 Kindle、Apple Books 或通用设置。普通转换支持 TXT、EPUB、MOBI、AZW3、DOCX 和 Markdown，输出 EPUB；无法解析或受保护的文件不能处理。'),
   ('PDF 的支持范围','独立 PDF → EPUB 入口支持文字型 PDF，保留原文与图片，先检查并提示版面风险。扫描件 OCR、PDF 翻译和 PDF 精校尚未开放；复杂版式可能被拒绝。'),
   ('费用与支付','基础转换与 EPUB 修复为人民币 ¥0.99 / 本或 PayPal US$0.99 / 本。翻译和精校另行报价，PayPal AI 订单最低 US$1.49，显示的美元总额含支付手续费。最终费用取决于文件与服务，付款前确认。'),
   ('如何开始和取回结果','进入工具页，上传有权处理的文件，选择语言与服务，检查报价后付款。处理在服务器运行；回到原浏览器的任务中心查看进度并下载。请及时保存结果，不要为找回同一任务重复付款。')],
  'links':['翻译与双语对照','EPUB 修复','繁简转换','竖排转横排']},
 'zh-TW': {
  'title':'電子書五語翻譯、校對與 EPUB 修復 | FixEpub',
  'description':'簡中、繁中、英、日、韓電子書互譯與校對，EPUB 格式修復、繁簡轉換及文字型 PDF 轉 EPUB。先確認報價，支援 PayPal。',
  'heading':'翻譯、校對與修復你的電子書','intro':'將自己的書稿處理成便於閱讀的 EPUB。依檔案選擇服務，先查看報價，再決定是否付款。',
  'action':'開啟工具並查看報價','contact':'聯絡客服',
  'sections':[
   ('五種語言互譯','簡體中文、繁體中文、英語、日語與韓語支援 20 個不同語言方向。翻譯可上傳 EPUB、TXT、DOCX、MD 或 Markdown，輸出 EPUB。可選雙語對照、翻譯策略與術語表；重要譯文建議與原文核對。'),
   ('同語言校對','單本 EPUB、TXT、DOCX 或 Markdown 支援五種語言校對。簡中檢查兩岸風險用詞；繁中、英語、日語與韓語檢查拼字、文法及用詞，保留原語言。校對與翻譯分開處理，不會自動執行另一項服務。'),
   ('格式修復與排版','EPUB 修復先診斷套件結構與資源引用。繁簡轉換支援臺灣、港澳及通用來源；排版可選 Kindle、Apple Books 或通用設定。一般轉換支援 TXT、EPUB、MOBI、AZW3、DOCX 和 Markdown，輸出 EPUB；無法解析或受保護的檔案不能處理。'),
   ('PDF 支援範圍','獨立 PDF → EPUB 入口支援文字型 PDF，保留原文與圖片，先檢查並提示版面風險。掃描件 OCR、PDF 翻譯與校對尚未開放；複雜版式可能被拒絕。'),
   ('費用與付款','基礎轉換及 EPUB 修復為人民幣 ¥0.99 / 本或 PayPal US$0.99 / 本。翻譯及校對另行報價，PayPal AI 訂單最低 US$1.49，顯示的美元總額含付款手續費。最終費用依檔案與服務而定，付款前確認。'),
   ('開始使用與取回結果','開啟工具，上傳有權處理的檔案，選擇語言與服務，檢查報價後付款。處理於伺服器執行；回到原瀏覽器的任務中心查看進度並下載。請及時保存結果，不要為找回同一任務重複付款。')],
  'links':['翻譯與雙語對照','EPUB 修復','繁簡轉換','直排轉橫排']},
 'en': {
  'title':'EPUB Translation, Proofreading & Repair | FixEpub',
  'description':'Translate ebooks between English, Japanese, Korean, Simplified and Traditional Chinese. Proofread EPUBs, repair formatting, and convert text-based PDFs to EPUB. PayPal supported.',
  'heading':'Translate, proofread and repair your ebooks',
  'intro':'Prepare your own manuscripts for reading as EPUBs. Choose a service for your file and review the quote before paying.',
  'action':'Open the tools and get a quote','contact':'Contact support',
  'sections':[
   ('Translate between five languages','English, Japanese, Korean, Simplified Chinese and Traditional Chinese support 20 distinct translation directions. Upload EPUB, TXT, DOCX, MD or Markdown and receive an EPUB. Choose bilingual output, a translation strategy and a glossary. Review important passages against the original.'),
   ('Proofread in the original language','Proofreading supports one EPUB, TXT, DOCX or Markdown at a time in all five languages. English, Japanese, Korean and Traditional Chinese review spelling, grammar and word choice without translation. Simplified Chinese reviews regional wording. Translation and proofreading are separate jobs.'),
   ('Repair and format EPUBs','Repair begins with a diagnosis of EPUB package structure and resource references. Chinese script conversion supports Taiwan, Hong Kong/Macau and general profiles. Choose Kindle, Apple Books or generic layout settings. Basic conversion accepts TXT, EPUB, MOBI, AZW3, DOCX and Markdown and outputs EPUB; protected or unreadable files cannot be processed.'),
   ('What PDF conversion supports','The separate PDF-to-EPUB tool handles text-based PDFs, preserving original text and images after a layout-risk check. Scanned PDF OCR, PDF translation and PDF proofreading are not available. Complex layouts may be rejected.'),
   ('Pricing and PayPal','Basic conversion and EPUB repair cost CNY 0.99 per book with Alipay or US$0.99 with PayPal. Translation and proofreading are quoted separately. PayPal AI orders have a US$1.49 minimum; the displayed USD total includes payment processing fees. Review the final file-specific quote before paying.'),
   ('Start a job and return later','Open the tools, upload a file you have permission to process, select the language and service, and confirm the quote before paying. Processing runs on the server. Return to the task centre in the same browser to check progress and download. Save your result promptly; do not pay again to recover an existing job.')],
  'links':['Translation and bilingual output (Chinese guide)','EPUB repair','Chinese script conversion (Chinese guide)','Vertical to horizontal text (Chinese guide)']},
 'ja': {
  'title':'EPUB 翻訳・校正・修復ツール | FixEpub',
  'description':'日本語・英語・韓国語・簡体字・繁体字の電子書籍を相互翻訳。EPUB 校正、形式修復、文字型 PDF から EPUB への変換。料金確認後に PayPal で支払い。',
  'heading':'電子書籍を翻訳・校正・修復',
  'intro':'自分の原稿を読みやすい EPUB に。ファイルに合うサービスを選び、見積もりを確認してから支払えます。',
  'action':'ツールを開いて見積もりを確認','contact':'サポートに連絡',
  'sections':[
   ('5言語の相互翻訳','日本語・英語・韓国語・簡体字中国語・繁体字中国語の20方向に対応します。EPUB、TXT、DOCX、MD、Markdown を入力し、EPUB を出力します。原文対訳、翻訳方針、用語集を選択できます。重要な箇所は原文と照合してください。'),
   ('原文と同じ言語で校正','1冊の EPUB・TXT・DOCX・Markdown を5言語で校正できます。日本語・英語・韓国語・繁体字では綴り、文法、語句を確認し、翻訳はしません。簡体字は地域による用語の違いを確認します。翻訳と校正は別の処理です。'),
   ('EPUB の修復とレイアウト','修復ではパッケージ構造やリソース参照を先に診断します。中国語の繁簡変換は台湾・香港／マカオ・一般設定に対応。Kindle、Apple Books、汎用のレイアウトを選べます。基本変換は TXT、EPUB、MOBI、AZW3、DOCX、Markdown を受け付け、EPUB を出力します。保護されたファイルや解析できないファイルは処理できません。'),
   ('PDF 対応範囲','専用の PDF → EPUB ツールはテキストを含む PDF を扱い、レイアウト上の問題を確認したうえで原文と画像を保持します。スキャン PDF の OCR、PDF 翻訳、PDF 校正には未対応です。複雑なレイアウトは拒否される場合があります。'),
   ('料金と PayPal','基本変換と EPUB 修復は1冊につき Alipay で人民元0.99元、PayPal で US$0.99。翻訳と校正は別途見積もりとなります。PayPal の AI 注文は最低 US$1.49 で、表示される米ドル合計には決済手数料が含まれます。支払い前に最終金額を確認してください。'),
   ('開始と結果の受け取り','ツールを開き、処理する権利のあるファイルをアップロードし、言語とサービスを選択します。見積もり確認後に支払い、サーバーで処理します。同じブラウザーのタスク一覧に戻り、進捗とダウンロードを確認できます。結果は早めに保存し、既存のタスクを探すために再度支払わないでください。')],
  'links':['翻訳と対訳（中国語ガイド）','EPUB 修復','繁簡変換（中国語ガイド）','縦書きから横書きへ（中国語ガイド）']},
 'ko': {
  'title':'EPUB 번역·교정·복구 도구 | FixEpub',
  'description':'한국어·영어·일본어·중국어 간체·번체 전자책 상호 번역, EPUB 교정과 형식 복구, 텍스트 PDF의 EPUB 변환. 견적 확인 후 PayPal 결제.',
  'heading':'전자책 번역, 교정 및 복구',
  'intro':'내 원고를 읽기 편한 EPUB으로 준비하세요. 파일에 맞는 서비스를 선택하고 견적을 확인한 후 결제합니다.',
  'action':'도구 열기 및 견적 확인','contact':'고객 지원',
  'sections':[
   ('5개 언어 상호 번역','한국어, 영어, 일본어, 중국어 간체와 번체 사이의 20개 방향을 지원합니다. EPUB, TXT, DOCX, MD 또는 Markdown을 업로드하면 EPUB으로 출력합니다. 원문 대조, 번역 방식, 용어집을 선택할 수 있습니다. 중요한 내용은 원문과 함께 확인하세요.'),
   ('원래 언어로 교정','EPUB, TXT, DOCX 또는 Markdown 한 권씩 5개 언어 교정을 지원합니다. 한국어·영어·일본어·번체는 철자, 문법, 어휘를 검토하며 번역하지 않습니다. 간체 중국어는 지역 용어 차이를 검토합니다. 번역과 교정은 별도 작업입니다.'),
   ('EPUB 복구와 서식','복구는 EPUB 패키지 구조와 리소스 참조 진단부터 시작합니다. 중국어 간체·번체 변환은 대만, 홍콩·마카오, 일반 설정을 지원합니다. Kindle, Apple Books 또는 일반 서식을 선택할 수 있습니다. 기본 변환 입력은 TXT, EPUB, MOBI, AZW3, DOCX, Markdown이며 출력은 EPUB입니다. 보호되었거나 해석할 수 없는 파일은 처리할 수 없습니다.'),
   ('PDF 지원 범위','별도 PDF → EPUB 도구는 텍스트가 포함된 PDF를 처리하며, 레이아웃 위험을 확인한 후 원문과 이미지를 유지합니다. 스캔 PDF OCR, PDF 번역 및 PDF 교정은 아직 지원하지 않습니다. 복잡한 레이아웃은 거부될 수 있습니다.'),
   ('요금 및 PayPal','기본 변환과 EPUB 복구는 권당 Alipay CNY 0.99 또는 PayPal US$0.99입니다. 번역과 교정은 별도 견적을 제공합니다. PayPal AI 주문은 최소 US$1.49이며 표시된 달러 총액에 결제 수수료가 포함됩니다. 결제 전에 파일별 최종 금액을 확인하세요.'),
   ('시작 및 결과 받기','도구에서 처리 권한이 있는 파일을 업로드하고 언어와 서비스를 선택하세요. 견적 확인 후 결제하면 서버에서 처리됩니다. 같은 브라우저의 작업 목록으로 돌아와 진행 상황을 확인하고 다운로드하세요. 결과를 바로 저장하고 기존 작업을 찾기 위해 다시 결제하지 마세요.')],
  'links':['번역 및 원문 대조 (중국어 안내)','EPUB 복구','중국어 간체·번체 변환 (중국어 안내)','세로쓰기·가로쓰기 변환 (중국어 안내)']}
}


TXT_REVIEW = {
 'zh-CN': ('TXT 导入与精校修改对照', 'TXT 支持 UTF-8 或带 BOM 的 UTF-16，识别常见中英日韩章标题并生成 EPUB 目录。精校完成后可逐项查看修改前后文字，勾选保留或取消修改，下载所选版本；不会覆盖原成品或再次调用模型。旧任务可能没有对照记录。'),
 'zh-TW': ('TXT 匯入與精校修改對照', 'TXT 支援 UTF-8 或帶 BOM 的 UTF-16，辨識常見中英日韓章標題並產生 EPUB 目錄。精校完成後可逐項查看修改前後文字，勾選保留或取消修改，下載所選版本；不覆蓋原成品或再次呼叫模型。舊任務可能沒有對照記錄。'),
 'en': ('TXT import and proofreading changes', 'Import UTF-8 or BOM-marked UTF-16 text. Common Chinese, English, Japanese and Korean chapter headings become EPUB contents entries. After proofreading, compare the original and revised passages, keep or reject changes, and download your selection. This does not overwrite the original result or call the model again. Older jobs may not have change records.'),
 'ja': ('TXT 入力と校正の変更確認', 'UTF-8 または BOM 付き UTF-16 のテキストを入力できます。一般的な中英日韓の章見出しから EPUB の目次を作成します。校正後は変更前後を比較し、変更を採用または取り消して選択した版をダウンロードできます。元の成果物を上書きせず、モデルを再実行しません。古いタスクには変更記録がない場合があります。'),
 'ko': ('TXT 가져오기 및 교정 변경 비교', 'UTF-8 또는 BOM이 있는 UTF-16 텍스트를 지원합니다. 일반적인 중국어·영어·일본어·한국어 장 제목으로 EPUB 목차를 만듭니다. 교정 후 변경 전후를 비교하고 수정 사항을 유지하거나 취소하여 선택한 버전을 다운로드할 수 있습니다. 원본 결과를 덮어쓰거나 모델을 다시 호출하지 않습니다. 이전 작업에는 변경 기록이 없을 수 있습니다.')
}
for language, section in TXT_REVIEW.items():
    CONTENT[language]['sections'].insert(2, section)


def render(language):
    data = CONTENT[language]
    url = 'https://fixepub.com/' + LANGUAGES[language][0]
    alternates = ''.join(f'<link rel="alternate" hreflang="{lang}" href="https://fixepub.com/{name}" />\n' for lang,(name,_) in LANGUAGES.items())
    alternates += '<link rel="alternate" hreflang="x-default" href="https://fixepub.com/en.html" />'
    navigation = ' · '.join(f'<a href="/{name}" lang="{lang}" hreflang="{lang}">{label}</a>' for lang,(name,label) in LANGUAGES.items())
    sections = ''.join(f'<section><h2>{escape(title)}</h2><p>{escape(body)}</p></section>' for title,body in data['sections'])
    from generate_translation_pages import PAGES as translation_pages
    destinations = [translation_pages[language],'epub-repair.html','traditional-to-simplified.html','vertical-to-horizontal.html']
    labels = list(data['links'])
    labels[0] = {'zh-CN':'EPUB 电子书翻译','zh-TW':'EPUB 電子書翻譯','en':'EPUB translator','ja':'EPUB 翻訳','ko':'EPUB 번역기'}[language]
    links = ' · '.join(f'<a href="/{name}">{escape(label)}</a>' for name,label in zip(destinations,labels))
    schema = json.dumps({'@context':'https://schema.org','@type':'WebPage','name':data['title'],'description':data['description'],'url':url,'inLanguage':language},ensure_ascii=False).replace('<','\\u003c')
    return f'''<!doctype html>
<!-- Generated by scripts/generate_language_pages.py -->
<html lang="{language}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(data['title'])}</title><meta name="description" content="{escape(data['description'],quote=True)}">
<link rel="canonical" href="{url}">{alternates}
<meta property="og:type" content="website"><meta property="og:url" content="{url}"><meta property="og:title" content="{escape(data['title'],quote=True)}"><meta property="og:description" content="{escape(data['description'],quote=True)}">
<meta name="twitter:card" content="summary"><script type="application/ld+json">{schema}</script>
<style>
:root{{color-scheme:light dark;font:17px/1.8 system-ui,sans-serif;background:#f5f7fb;color:#203047}}*{{box-sizing:border-box}}body{{margin:0}}header,main,footer{{max-width:940px;margin:auto;padding:24px}}header{{display:flex;gap:24px;justify-content:space-between;flex-wrap:wrap}}a{{color:#1957b6;text-underline-offset:4px}}.brand{{font-size:24px;font-weight:750;text-decoration:none}}h1{{font-size:clamp(30px,5vw,46px);line-height:1.3;margin:28px 0 20px}}h2{{font-size:23px;margin:0 0 12px}}section{{background:#fff;padding:26px;border:1px solid #dce3ee;border-radius:14px;margin:20px 0}}p{{margin:0 0 18px}}.intro{{font-size:20px;color:#50617b}}.action{{display:inline-block;padding:12px 22px;background:#1957b6;color:#fff;border-radius:9px;text-decoration:none;margin:12px 0 24px}}nav,footer{{font-size:14px}}a:focus-visible{{outline:3px solid #4285ea;outline-offset:4px}}@media(prefers-color-scheme:dark){{:root{{background:#141b24;color:#e8edf5}}section{{background:#1c2633;border-color:#3b485d}}a{{color:#8ab5ff}}.intro{{color:#b5c3d8}}.action{{color:white}}}}@media(max-width:600px){{header,main,footer{{padding:18px}}section{{padding:20px}}}}
</style><script src="/growth-analytics.js?v=20261026-r2" defer></script></head><body><header><a class="brand" href="/?lang={language}">FixEpub</a><nav aria-label="Language">{navigation}</nav></header>
<main><h1>{escape(data['heading'])}</h1><p class="intro">{escape(data['intro'])}</p><a class="action" href="/?lang={language}">{escape(data['action'])}</a>{sections}<nav>{links}</nav><a class="action" href="/?lang={language}">{escape(data['action'])}</a></main>
<footer><a href="/contact{'-en' if language=='en' else ''}.html">{escape(data['contact'])}</a> · © 2026 FixEpub</footer></body></html>
'''


def generate(check=False):
    output = ROOT / 'frontend'
    expected = {name:render(lang) for lang,(name,_) in LANGUAGES.items()}
    pages = ['examples.html','txt-to-epub.html','ebook-proofreading.html','proofreading-changes.html', '', 'epub-translator.html','epub-repair.html','traditional-to-simplified.html','vertical-to-horizontal.html',*expected,
             'about.html','contact.html','privacy.html','terms.html','refund.html','contact-en.html','privacy-en.html','terms-en.html','refund-en.html']
    from generate_translation_pages import PAGES as translation_pages
    pages += [name for name in translation_pages.values() if name not in pages]
    expected['sitemap.xml'] = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + ''.join(f'  <url><loc>https://fixepub.com/{name}</loc></url>\n' for name in pages) + '</urlset>\n'
    for name,content in expected.items():
        if check:
            if not (output/name).exists() or (output/name).read_text()!=content:
                raise SystemExit('Out of date: '+name)
        else:
            (output/name).write_text(content)
    print(('Checked' if check else 'Generated')+' language guides and canonical sitemap')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--check',action='store_true')
    generate(parser.parse_args().check)
