# Service funnel (2026-10-26)

The five-language admin dashboard now separates entry exposure, entry clicks, file selection and successful uploads by conversion, translation, proofreading, repair and PDF. Quotes and verified payments retain order-based counting; batch orders remain separate. Prices and payment execution are unchanged.

## Product semantics

Exposure requires at least half the entry to enter the viewport. The first four metrics deduplicate by service and session, while quotes and payments deduplicate by order. Do not divide these different units as a conversion rate. Batch uploads use the chosen service at the session stages and the batch category at the order stages. Upload success means the server accepted the upload, not processing completion. New steps start at deployment; absent historical events do not imply no activity.

## Technical design

POST /api/v2/analytics/events accepts an optional allowlisted service and service_exposed, service_clicked and upload_succeeded events. Corrected clients send metrics_version=2; the optional growth.sqlite database stores their steps in service_steps_v2 with a session/service/event primary key and the existing 90-day retention. Reports tolerate old databases without this table and filter steps through the same language, source, device and time cohort. Legacy service_steps rows and old cached clients are not mixed into corrected entry counts. Old visits, quotes and verified payments remain visible.

The homepage upload dropzone is no longer counted as repair exposure/click. Only the repair page's dropzone has that classification. Async example-page translation CTAs are registered once after rendering. Preflight quote display emits quote_shown before settings confirmation; successful confirmation emits profile_confirmed. Neither browser event proves payment or authorizes processing. Confirmation counts are per order and currently used for translation only.

First-session landing category is stored in visit_landings: home, translation, repair, examples, guide, other or unknown. No URL, search term or query parameters are sent. Landing rows show sessions, quoted orders and verified paid orders separately; multiple orders can belong to one session. Old sessions are unknown, not retrospectively guessed. The same retention, language/device/source cohort and test/sandbox exclusions apply.

IntersectionObserver measures exposure; explicit success branches report accepted uploads. TV, DNT, admin and browser opt-out exclusions remain in place. No filenames, content, emails or raw IPs are included. Verified payment remains restricted to server verified_webhook, verified_query and verified_capture events; test and sandbox orders remain excluded. Telemetry failure cannot grant payment entitlement or start processing.
