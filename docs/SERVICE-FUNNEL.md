# Service funnel (2026-10-26)

The five-language admin dashboard now separates entry exposure, entry clicks, file selection and successful uploads by conversion, translation, proofreading, repair and PDF. Quotes and verified payments retain order-based counting; batch orders remain separate. Prices and payment execution are unchanged.

## Product semantics

Exposure requires at least half the entry to enter the viewport. The first four metrics deduplicate by service and session, while quotes and payments deduplicate by order. Do not divide these different units as a conversion rate. Batch uploads use the chosen service at the session stages and the batch category at the order stages. Upload success means the server accepted the upload, not processing completion. New steps start at deployment; absent historical events do not imply no activity.

## Technical design

POST /api/v2/analytics/events accepts an optional allowlisted service and service_exposed, service_clicked and upload_succeeded events. The optional growth.sqlite database stores service_steps with a session/service/event primary key and the existing 90-day retention. Reports tolerate old databases without the new table and filter steps through the same language, source, device and time cohort.

IntersectionObserver measures exposure; explicit success branches report accepted uploads. TV, DNT, admin and browser opt-out exclusions remain in place. No filenames, content, emails or raw IPs are included. Verified payment remains restricted to server verified_webhook, verified_query and verified_capture events; test and sandbox orders remain excluded. Telemetry failure cannot grant payment entitlement or start processing.
