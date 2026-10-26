# EPUB translation search pages — 2026-10-26

## User-facing changes

Dedicated English, Japanese, Korean and Traditional Chinese EPUB translation pages complement the existing Simplified Chinese page. Each explains supported language directions, bilingual output, glossaries, file formats, pricing and recovery. PayPal's US$1.49 AI minimum is explicitly not a whole-book fixed price. No unsupported OCR, PDF translation or guaranteed translation-quality claims are made.

The localized language guides link to the matching translation page. Every translation guide links to every language variant, uses a self canonical and reciprocal hreflang annotations, and is included in the sitemap. Static HTML makes the substantive copy available without JavaScript.

## Implementation and verification

Generate new pages with scripts/generate_translation_pages.py, Chinese tool pages with scripts/generate_seo_pages.py, and guides/sitemap with scripts/generate_language_pages.py. Each supports --check. Run scripts/test_translation_seo.py and scripts/test_tool_landing.py plus frontend tool-entry, pricing and growth tests.

All upload/payment/recovery actions remain on index.html. Localized CTA links set tool=translate and an allowlisted language; restoration retains the existing route. Anonymous translation CTA exposures/clicks use the existing optional funnel with TV/DNT/self exclusion. No payment configuration or pricing changes.

## Translation flow review fixes

- Fresh translations require an explicit target. Same source/target is rejected before quoting; auto-detect source remains available. Existing task recovery does not reset the recorded direction or amount.
- Links from translation to examples and back retain mode, UI language and allowlisted source/target values, never task tokens, email or arbitrary query fields. Explicit entry language is remembered on this browser.
- Bilingual output remains prominent. Optional strategy, quality/model, glossary and chapter overrides are collapsed; preflight direction, saved quote and confirmation remain visible. Email notification is below upload.
- Authored preflight, quote and primary translation status/error guidance covers five languages. Model evidence, book titles and raw technical details retain their original language and are labeled accordingly. Display localization never recalculates a price.
- All five static translator pages embed published synthetic translation output via scripts/translation_sample.py and frontend/examples-data.json. The chapter link/layout is explicitly illustrative, not evidence of whole-book quality. Example pages include a dedicated translation CTA.
- Funnel v2 separates preflight quote, settings confirmation and verified payment, and adds first landing-page category. See SERVICE-FUNNEL.md for historical-data boundaries.

2026-10-26 browser verification: English entry requires a target; choosing English to Korean, opening samples and returning retains translation mode and both languages. Offline tests cover all 20 directions, unchanged historical USD quotes and payment isolation; no real payment or new paid-model evaluation was run for this UI release.

Search Console setup completed on 2026-10-26 after the user signed into DNSPod. Added the user-supplied Google verification TXT at @ with TTL 600; all five existing A records remain unchanged. Authoritative DNS and Google public DNS returned the record, and Search Console confirmed ownership. The https://fixepub.com/ URL-prefix property is also verified: use this property for main-site reporting so tv/latex/other subdomains are excluded. The domain property covers all subdomains and must not be used as a main-site-only total. Re-submitted https://fixepub.com/sitemap.xml successfully; it includes all five translation landing pages.

## Measurement limits

The main-site Search Console 3-month Web report read on 2026-10-26 showed 45 clicks, 470 impressions, 9.6% CTR and average position 15.4. The repair page had 27 clicks and 218 impressions; the homepage had 18 clicks and 254 impressions. No dedicated translation page appeared in the seven-row page table. These are historical search-report metrics, not today's visits or payments; page impression sums may differ from property totals. The newly verified domain property is still processing data, while the URL-prefix property exposes historical data. Lack of recorded referrals does not establish whether a page is unindexed, low ranking or receiving impressions without clicks. Use Search Console's page/query impressions, clicks, CTR, position and indexing reports to assess these new URLs after recrawling. Do not treat a site: search or this deployment as indexing confirmation. Rankings and traffic are not guaranteed.

References: https://developers.google.com/search/docs/appearance/title-link and https://developers.google.com/search/docs/specialty/international/localized-versions
