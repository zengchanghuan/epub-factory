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
