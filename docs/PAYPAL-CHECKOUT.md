# PayPal service checkout

PayPal ordinary conversion, text-layer PDF original-language conversion and repair remain USD 0.99 per book. Batch conversion remains USD 0.99 times book count. As of 2026-10-26, new Alipay EPUB repair quotes are CNY 5.99; basic conversion remains CNY 0.99. This repair change does not alter PayPal amounts or reprice existing orders.

New AI orders use `usd-v2-net-div7-fee044-fixed030-min149`:

- Translation: `max(1.49, ceil_cents((CNY_quote / 7 + 0.30) / 0.956))`.
- Combined precision polish: `max(1.49, ceil_cents((0.99 + CNY_polish / 7 + 0.30) / 0.956))` for the whole order. Its displayed conversion component stays USD 0.99; the remaining amount is the polish component including the order's processing fee allowance.
- The USD 0.30 fixed fee is counted once per order, not once per component. Seven CNY/USD is a fixed tariff, not a live FX quotation. This allowance covers a 4.4% + USD 0.30 processing fee assumption, not tax, withdrawal, FX, refunds or guaranteed profit.
- Existing orders retain their frozen amount/provider. Recovery, profile confirmation, approval and capture never apply the new tariff to them.
- Examples: translation CNY 3.99 → USD 1.49; translation CNY 14.01 → USD 2.41; CNY 3.99 polish plus conversion → USD 1.95 total (USD 0.99 + USD 0.96).
- The polish estimate API returns `price_usd`, `total_usd`, `base_usd` and `pricing_rule`, avoiding client-side pricing arithmetic.


## Configuration

Set server environment values (never commit credentials):

- `PAYPAL_CHECKOUT_ENABLED=1`
- `PAYPAL_MODE=live` (default sandbox)
- `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` (legacy `PAYPAL_SECRET` is also supported)
- `PAYPAL_WEBHOOK_ID` for the same app and environment

Register `https://fixepub.com/api/v2/webhooks/paypal` for `PAYMENT.CAPTURE.COMPLETED`. The browser receives only the public client ID. Keep checkout disabled until code, database and callback configuration are ready. Disable through `PAYPAL_CHECKOUT_ENABLED=0`; existing receipts can still be verified.

## Payment boundaries

Upload freezes provider, currency and amount. A durable SQL mapping binds the local order to the PayPal order, merchant, mode and quote. Authenticated creation and capture do not accept a browser-selected amount or provider order ID. Orders v2 operations use stable idempotency keys. Only one final COMPLETED capture with matching merchant, local reference, invoice, amount and USD currency can settle a job. An approval event alone does not unlock work. Callback signatures are checked through PayPal and the order is independently retrieved before fulfillment. Existing settlement/outbox logic deduplicates repeated callbacks. Reconciliation only queries PayPal; it never closes PayPal orders through Alipay. Unpaid checkout expires after at most two hours; completed payments can still recover afterward.

PDF checkout starts only after existing candidate validation and user confirmation. Sandbox payments are marked test orders; live PayPal prices and owner receipts are not affected by Alipay's admin discount. Live admin queries show USD and route to the original provider.

## Verification

Offline backend regression: 156 tests across PayPal, payment hooks, email, reconciliation, PDF persistence/API/checkout and repair pricing. Frontend: 331 tests across 39 files, including USD checkout and original payment recovery. Provider responses are mocked in these tests; they do not prove a real capture. A live order can be created and the approval page inspected without making a payment. Real capture requires a buyer to approve a payment, followed by checking payment settlement and task execution.

## Full-page approval return

The checkout page also handles a full-page return from PayPal. When the return includes `token`, it asks the server to capture the existing bound order without loading the JavaScript SDK again. The URL token and payer ID are not forwarded or trusted as payment evidence. Failed or pending verification exposes the retry button and does not create another order. This complements the SDK `onApprove` path; only server validation of a completed capture releases processing.

Reference: [PayPal Orders integration](https://developer.paypal.com/whats-an-order/) describes the approval return followed by server-side capture. Provider approval alone is not proof of completed payment.

The 2026-10-19 service extension includes translation, repair and precision polish. PDF remains the text-layer-only product; no model OCR is enabled by this change.

### 2026-10-19 local verification

- All 39 frontend suites passed, including seven PayPal tests covering full-page return and verification retry.
- 134 backend tests passed across PayPal checkout, payment hooks, receipts, reconciliation, repair checkout, PDF API and PDF checkout. The reconciliation assertions now explicitly preserve CNY for Alipay and USD for PayPal, and verify that expired PayPal orders never call Alipay close.
- Backend tests ran from a source-only temporary snapshot with isolated SQLite/storage, dotenv blocking and Python network blocking. No workspace order database was used. The temporary Python 3.12 environment used locked package versions except PyCryptodome 3.23.0 in place of legacy PyCrypto; this is not production-runtime parity validation.
- No real PayPal approval, capture, webhook delivery or production deployment was performed in this verification.

## Service extension, 2026-10-19

- Translation keeps the frozen USD quote through profile confirmation, checkout resumption and payment entitlement. The legacy `price_cny` response field is retained for compatibility and must be interpreted with `currency`; `reference_price_cny` preserves the source tariff. Execution validates the frozen quote as before.
- Precision polish supports single EPUBs in zh-CN, zh-TW, en, ja and ko and requires verified payment for the combined USD conversion/add-on amount. Non-zh-CN proofreading preserves the source script and skips Chinese normalization. The frozen quote and entitlement both store the selected language; historical missing language defaults to zh-CN.
- Repair selects and freezes its provider at diagnosis. Existing repair capabilities (random 128-bit job IDs) remain its access mechanism; they are not ordinary authenticated job tokens. PayPal SQL mappings bind `repair_<id>` to provider order/merchant/mode/USD amount. Verified capture, signature-verified webhook plus fresh order retrieval, and scheduled read-only recovery converge on the durable repair confirmation/notification/executor path. Alipay callbacks cannot settle USD repairs. Old repair quotes/providers are never switched.
- All provider creation and capture are server-side, with stable idempotency keys, as described in [PayPal API requests](https://developer.paypal.com/api/make-api-requests/).

Service-extension local verification: 184 backend tests passed (PayPal 22, repair pricing 19, repair checkout 30, translation confirmation 8, reconciliation 14, hooks 12, email 23, entitlements 15, polish 26, PDF checkout 15). All 39 frontend suites passed after updating currency assertions; new cases cover USD translation confirmation rendering, repair routing, service descriptions and polish estimates. Same isolated source-snapshot environment described above; gateway responses mocked, no real capture proven.

Live verification after deployment: synthetic uploads for translation, precision polish and repair returned USD 0.99, USD 1.98 and USD 0.99 respectively. Each created a real PayPal order and a repeated create returned the same provider ID. No approval or capture was performed. Production health checks passed; original service-extension backup: `deploy-backups/20261019T031635Z-704974`. Browser verification confirmed the repair PayPal selector is available before upload. A subsequent label fix ensures the translation profile confirmation button says PayPal for USD orders; its rendering regression passed.

## Actual sandbox end-to-end verification — 2026-10-19

An isolated source copy, SQLite database and loopback-only service completed an
actual PayPal Sandbox buyer approval and capture for USD 1.95. Production remained
in live mode; no production configuration, order database or worker queue was used.
The synthetic English TXT job `1c039a7711dd` reached completed with 4 real model
calls, 4 reviewed spans and 2 changed paragraphs. Provider receipt validation used
completed-capture checks (amount, currency, merchant/order bindings and final capture).

Original, all-kept, all-reverted and partial-selection EPUB downloads passed real
EPUBCheck 5.1.0. Paragraph contents matched each selection, all-kept matched the
original bytes, and selection exports did not overwrite the original artifact.
Unauthorized review access returned 403. Repeated capture returned COMPLETED without
re-executing proofreading. The actual review component also downloaded a partial
selection through the browser. Structured evidence: `verification/2026-10-19-paypal-sandbox-e2e.json`.

The first sandbox order failed before model calls because the isolated launcher
omitted UTF-8 locale: Java could not resolve Chinese characters in the output path.
A direct reproduction failed with LANG unset and passed with C.UTF-8. The normal
manual-review safeguard prevented automatic retry, so its evidence was retained
and a second virtual-money order ran successfully after correcting the launcher.
No production code change or payment bypass was used to obtain success.

This verifies server-side capture confirmation, not public webhook delivery.
The temporary sandbox webhook used a reserved .invalid hostname to avoid sending
test callbacks to production; it was removed after testing. The isolated service
and temporary credential copies were cleaned up. This small English sample is not
a full-book or five-language model-quality evaluation.

## Public Sandbox webhook verification — 2026-10-20

A real Sandbox USD 1.95 payment (order `0SH165921B452830B`, capture
`04L28688J5203421F`) delivered `PAYMENT.CAPTURE.COMPLETED` to an isolated public
HTTPS listener. The application capture endpoint was not used: direct Sandbox
capture left local settlement to the signature-verified callback and independent
provider order validation. Job `0407a83a2c23` recorded `verified_webhook`, completed
precision polish with four real model calls and two changes, and produced four
EPUBCheck-valid downloads whose text matched their review selections.

An exact signed-event replay through the public URL returned 200 without repeating
polish. Changing the amount in that event returned 400 Invalid signature. This was
an HTTP replay test, not a test of PayPal's resend API. Temporary webhook, local and
server credential copies, listener and tunnel were removed/stopped. Production
configuration was unchanged and health remained ok. Intermittent connectivity
caused the initial 503; the same credentials subsequently passed OAuth with HTTP
200. Evidence: `verification/2026-10-20-paypal-sandbox-webhook.json`.

This is Sandbox evidence, not a real-money production transaction. The first
natural live precision-polish payment is monitored separately without initiating
payments or capturing customer orders as part of monitoring.
