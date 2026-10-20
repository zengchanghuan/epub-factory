# Five-language UI and ebook translation

2026-10-19

The main upload/task page, EPUB repair page and PayPal checkout share a locally
stored interface language (Simplified Chinese, Traditional Chinese, English,
Japanese, Korean) and appearance preference (light, dark, system). Browser language
is used on the first visit. These preferences do not change order amounts or book
translation settings. Legal originals, company registration information and raw
technical diagnostics retain their original language; long Chinese marketing
sections are replaced by a translated product overview outside Simplified Chinese.

`frontend/locales/ui.js` contains authored text, used with `data-i18n` markers and
explicit dynamic control IDs. It never sends files, user text or input values to
a browser translation API. Add translations in all five locales when adding UI.

Translation accepts `source_lang=auto|zh-CN|zh-TW|en|ja|ko` and
`target_lang=zh-CN|zh-TW|en|ja|ko` on the v2 API. Twenty distinct directed pairs are
supported. The source choice is stored with the job and preserved during retries;
source and target participate in upload/cache/checkpoint identity. Translation
preprocessing retains original Han characters instead of running Chinese script
conversion on Japanese/Chinese source text. Draft, review, glossary and literary
prompts follow the requested target. Output EPUB language metadata includes the
target, with translated chapters labeled accordingly.

## Verification

- Isolated backend tests exercise all twenty pairs through upload/quote and a
  mocked model chunk call, invalid language rejection, retry preservation,
  checkpoint invalidation and unchanged-source safeguards.
- Translation scope/corpus, runner/performance, PayPal and EPUB packaging
  regressions run with temporary data and network isolation. Two historical
  performance fixtures are unavailable and skipped.
- All 40 frontend test files pass, including locale normalization, dictionary
  coverage, theme selection and checkout language changes preserving payment.
- Chrome checks cover five interface languages, source/target selection retained
  on UI-language changes, light/dark and persistence across the repair page.
- No live model translation or charge is made for these tests. Offline results
  verify routing and contracts, not translation quality. Existing full EPUB
  linguistic residual auditing is Chinese-specific; other targets retain
  structural/link checks and conservative unchanged-paragraph detection.

## Pricing note (historical multilingual release; superseded by PAYPAL-CHECKOUT.md fee v2)

A CNY quote divided by seven is an exchange conversion, not a profit calculation.
PayPal's published standard mainland-China cross-border commercial fee is 4.4%
plus USD 0.30 for USD receipt. Account-specific terms may differ:
https://www.paypal.com/c2/business/paypal-business-fees?locale.x=en_C2

At that rate USD 0.99 leaves USD 0.64644 before model, server, refund, withdrawal,
FX and tax costs. A fee-adjusted price for a desired USD net amount N is
`(N + 0.30) / 0.956`, rounded up to cents. This is not a margin or tax guarantee.
Actual tax treatment and measured per-service costs are not established here.

## Locale-based payment preference (2026-10-19)

For new uploads, English, Japanese, Korean and Traditional Chinese place PayPal
first and select it by default only after server capabilities confirm availability.
Simplified Chinese prefers Alipay. This applies to ordinary/translation/polish,
text-layer PDF and repair selectors. A manual choice is preserved across locale
changes; unavailable PayPal falls back to Alipay. Disabled selectors for existing
repair orders retain their frozen provider. Changing a selector never reprices or
changes an existing order. All 40 frontend suites pass, with preference tests for
all locales, late capability responses, manual choices and locked selectors.

## Multilingual precision proofreading (2026-10-19)

Single-EPUB proofreading now accepts `polish_language`: zh-CN, zh-TW, en, ja, ko.
The selector describes the book language independently of interface locale.
zh-CN retains the regional-word review; the other four languages review bounded
plain-text slices for spelling, grammar and word choice without translation.
The ordinary converter receives original mode for these four languages, preventing
OpenCC and mainland vocabulary replacement. Chinese conversion controls are
disabled in that mode. Translation plus proofreading in one order remains disabled.

Inspection and execution use the same language-aware planner. Protected nodes,
numbers, URLs, email addresses and placeholders are guarded; model output never
writes HTML. EPUB structure/resources and final validation remain mandatory.
No-change reviews are valid, and incomplete reviews are not delivered.
Language is frozen in both the quote and paid entitlement, including worker retries;
legacy orders without a language retain zh-CN behavior. PayPal pricing and minimum
remain unchanged, with server-generated quotes including the fee allowance.

Validation: 86 isolated backend tests (precision model/guards 30, contract/worker 29,
PayPal checkout 27) and 40 frontend suites passed. Four-language correction fixtures,
protected-content rejection, bounded long text, no-op review, language propagation,
quote/capture consistency and language-tampering rejection are covered. Provider
responses and EPUBCheck in model unit tests are mocked; this is not a full-book
linguistic quality evaluation or evidence of a live customer payment.

Deployment verified on 2026-10-19; backup:
`deploy-backups/20261019T055053Z-741631`. Public API health passed. Live synthetic
English/Japanese/Korean/Traditional Chinese EPUB estimate requests each returned
one review candidate, CNY 3.99 polish and USD 1.95 combined PayPal total (0.99 base,
0.96 add-on). No order or provider charge was created by these checks. Chrome
verified the five-language selector, Chinese conversion disabled for English
proofreading, and PayPal selected in the Korean interface.

## TXT import and precision change review (2026-10-19)

TXT now enters basic conversion, translation and precision proofreading in zh-CN,
zh-TW, English, Japanese and Korean. UTF-8 and BOM-marked UTF-16 are decoded
strictly before billing. Empty, malformed and control-character input is rejected.
Common chapter headings generate EPUB navigation anchors. Basic conversion preserves
the original language/script; proofreading continues to apply its selected language.
Repair remains EPUB-only. Existing pricing and payment entitlement rules are retained.

New precision runs save private pre-proofread EPUB and immutable change evidence in
the attempt directory. Authorized users can page through before/after text, deselect
changes and download a selected EPUB without another model call. Downloads preserve
the original paid artifact, validate revision hashes and reject unknown change IDs.
Partial selections pass EPUB validation. Historical runs without evidence cannot show
an edit comparison. Changes are grouped by text node, rather than individual words.

Validation: 159 isolated backend tests, 41 frontend suites and 6 SEO tests passed.
Coverage includes five-language TXT input, chapter navigation, quote/capture language
freezing, unauthorized review access, stale revisions, selected exports, immutable
originals and stale UI responses. Model/provider calls and EPUBCheck in unit tests
are mocked; these results do not establish full-book linguistic quality or a real
payment. Deployment and browser smoke verification are recorded below.

Final deployment verified on 2026-10-19. Backup:
`deploy-backups/20261019T062654Z-750243`. Service and public API health passed.
Five synthetic TXT estimates (zh-CN, zh-TW, en, ja, ko) returned review candidates
and the expected CNY 3.99 / combined USD 1.95 quote. Public index, locale catalog,
lib and review script hashes match local source. The review endpoint returns the
expected missing-job response. No production order, model call or payment was made.
Chrome confirmed TXT copy, the five-language precision selector, disabled Chinese
conversion for English proofreading, and default PayPal in English. A local synthetic
review modal verified before/after rendering and checkbox selection. Browser file
upload was unavailable because the extension lacks file-URL access; live multipart
TXT uploads were instead verified directly through the estimate API. Full paid
end-to-end proofreading delivery was not run. Frontend 41 suites passed again after
completing the English/Japanese/Korean TXT format hints.

## 2026-10-20: TXT preview and feature discovery

Selecting one TXT in the main uploader now shows an automatic local preview in
all five interface languages. It displays detected encoding, the first 600
characters, recognized chapter count and the first 30 chapter headings with line
numbers. No file is uploaded and no order/model request is made for preview.
Preview is limited to 2 MB; larger files may still use the existing upload flow
(TXT limit 60 MB). UTF-8 and BOM-marked UTF-16 are supported. Empty, invalid or
control-character-containing input shows an error. No recognized heading does not
mean missing body text. Preview is read-only; manual chapter editing is not offered.
Server-side validation and the final quote remain authoritative.

New static discovery pages are `/txt-to-epub.html`, `/ebook-proofreading.html` and
`/proofreading-changes.html`. They describe examples, limits and pricing and link
to the single existing uploader; they never implement a second payment flow.
They are currently Chinese guides, not five-language copies. All five languages
are supported in the interactive TXT preview and existing service interface.

The five-language quality seed covers 20 translation directions and five
same-language proofreading cases. These are synthetic reference samples, not
customer books or native-speaker-certified translations. Automated checks catch
missing outputs, changed numeric literals and source echoes; human assessment of
meaning, fluency, terminology and structure is still required. A passing code
regression is not a passing model-quality evaluation.


## 2026-10-20: DOCX / Markdown proofreading

The single-file proofreading entry accepts EPUB, TXT, DOCX, `.md` and `.markdown`
in all five languages. Select the source language, request a quote and pay through
the existing checkout (including PayPal). This is same-language proofreading, not
translation. DOCX / Markdown is normalized to EPUB before inspection and again
before execution; the delivered original and selected-edit versions are EPUB,
not DOCX / Markdown round trips. Images must be embedded/self-contained; external
or missing resources fail before payment. The same existing pricing, entitlement,
private comparison and authorized download rules apply. Batch proofreading, PDF,
MOBI and AZW3 proofreading remain unsupported.

The format contract covers four import extensions × five languages, real input
normalization, matching quote/worker character counts, authorized downloads and
wrong-token denial, with mocked payment/model boundaries. These tests do not
represent real payments or native-speaker quality acceptance.

Live synthetic evidence: `docs/verification/2026-10-20-language-quality/`.
The 20 translation directions and five proofreading languages were exercised,
including a supplemental Simplified-Chinese risk-word case. There were
25 physical model requests, 24215 tokens and approximately
CNY 0.0671 calculated usage cost (not a provider invoice).
24/25 strict literal checks pass; English → Simplified Chinese translated
`5 mg` / `10 mg` into `5毫克` / `10毫克`, preserving quantity but failing exact
literal comparison. Human review is pending. Raw evidence retains the initial
harness UTF-8 extraction issue and non-candidate seed; corrected extraction and
supplemental execution are explicitly documented in `summary.json`.

Deployment verification: application commit `22409a7` was pushed and deployed;
public health returned 200, seven frontend resources matched source hashes, and
15 live estimate requests (DOCX / MD / MARKDOWN × five languages) all returned
valid quotes. These estimates create no order or model call. PayPal remained
live/enabled and the before/after payment configuration fingerprint matched.
Evidence: `docs/verification/2026-10-20-polish-formats-live.json`.
Offline verification passed 42 frontend suites, 33 precision-contract tests
(including 20 import-format/language combinations and authorized downloads),
31 polisher safety tests, 25 input integrity tests, 25 translation-contract tests,
28 PayPal tests, nine multilingual tests, four TXT tests and five quality-tool tests.
Both SEO generators passed consistency checks. Backup:
`/home/ubuntu/epub-factory/deploy-backups/20261020T035234Z-1057658`.
No new real payment was initiated or claimed as accepted by this release.

Final browser follow-up: the task-link refresh initialization defect is fixed.
All 25 isolated Chrome scenarios passed with zero JavaScript exceptions, including
restoring pending/running/completed tasks, authorized download hashes, terminal
non-delivery, explicit checkout continuation and stale QR clearing. The browser
fixture allows only the read-only PayPal config query and forbids PayPal order
creation/capture. These four synthetic fixture jobs are loopback-only, not live
orders. All 42 frontend suites, seven landing-page tests and 20 catalog tests pass.

## 2026-10-20 — 付费漏斗、公开效果对照与报价说明

- `/examples.html`：简体中文、繁体中文、英语、日语、韩语各有翻译与精校的前后对照，跟随界面语言和 light/dark 设置。样本来自 `docs/verification/2026-10-20-language-quality/outputs.json` 的合成短文真实模型结果；不是客户文件或整本书质量承诺。正确文本未改动时照实显示，不制造修改。
- 首页提交前显示总额/币种、PayPal 已计入手续费、EPUB 交付、任务恢复、失败联系与退款政策说明，并链接公开对照。原有报价、最低金额、支付选择和支付校验不变。
- 管理员从 `/orders-admin.html` 登录后访问 `/funnel-admin.html`。支持五语言、7/30/90 天、首次访问语言、来源类别和设备类别筛选。
- 访问和选文件按 30 分钟浏览器会话去重；报价、付款点击、已核验到账和下载响应按关联订单去重。批量按整批一单，服务分为转换、翻译、精校、修复、PDF、批量。报价到付款与付款到下载使用订单分母；无分母显示 `—`，不会把无数据表示成 0%。
- 仅统计上线后关联成功的订单，不回填历史、不等于全站收入。到账需服务器已核验事件；创建/APPROVED/浏览器上报都不算到账。订单阶段排除已标记测试及 Sandbox；会话数不是独立人数，机器人过滤不保证完整。
- 扫码行为不可直接观测，付款点击缺失不代表未操作。下载表示已授权并准备返回文件，不能证明客户端保存成功；批量响应也不代表每个子文件均完成。跨浏览器、禁用存储、DNT、网络丢报或恢复未关联的旧任务可能缺少归因。
- 不收集书名、正文、完整来源网址或支付凭据；仅保存随机会话、类别、内部订单关联及下载时间。隐私政策已同步说明。不新增第三方追踪，不提供免费模型试跑，不自动发消息或重新付款。

Validation for this release: 43 frontend suites, 31 isolated Chrome scenarios
(zero JavaScript exceptions), seven funnel tests, 28 PayPal tests, 33 precision
contract tests, 22 administrator tests, 18 repair tests and six batch tests passed.
The isolated server run with real EPUBCheck passed 11 D91 tests; one optional
private-original case was skipped. D49's 14 private-history cases were unavailable
and are not claimed as covered. Seven landing-page and 20 catalog tests passed.
See `docs/verification/2026-10-20-growth-funnel.json` for release evidence.
