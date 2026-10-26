# EPUB translation search pages — 2026-10-26

## User-facing changes

Dedicated English, Japanese, Korean and Traditional Chinese EPUB translation pages complement the existing Simplified Chinese page. Each explains supported language directions, bilingual output, glossaries, file formats, pricing and recovery. PayPal's US$1.49 AI minimum is explicitly not a whole-book fixed price. No unsupported OCR, PDF translation or guaranteed translation-quality claims are made.

The localized language guides link to the matching translation page. Every translation guide links to every language variant, uses a self canonical and reciprocal hreflang annotations, and is included in the sitemap. Static HTML makes the substantive copy available without JavaScript.

## Implementation and verification

Generate new pages with scripts/generate_translation_pages.py, Chinese tool pages with scripts/generate_seo_pages.py, and guides/sitemap with scripts/generate_language_pages.py. Each supports --check. Run scripts/test_translation_seo.py and scripts/test_tool_landing.py plus frontend tool-entry, pricing and growth tests.

All upload/payment/recovery actions remain on index.html. Localized CTA links set tool=translate and an allowlisted language; restoration retains the existing route. Anonymous translation CTA exposures/clicks use the existing optional funnel with TV/DNT/self exclusion. No payment configuration or pricing changes.

## Measurement limits

No Search Console property or query-level data was accessed. Lack of recorded referrals does not establish whether a page is unindexed, low ranking or receiving impressions without clicks. Use Search Console's page/query impressions, clicks, CTR, position and indexing reports to assess these new URLs after recrawling. Do not treat a site: search or this deployment as indexing confirmation. Rankings and traffic are not guaranteed.

References: https://developers.google.com/search/docs/appearance/title-link and https://developers.google.com/search/docs/specialty/international/localized-versions
