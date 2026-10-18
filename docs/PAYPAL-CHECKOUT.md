# PayPal conversion checkout

PayPal supports ordinary conversion and the separate PDF original-language conversion product at USD 0.99 per book. Batch conversion uses USD 0.99 multiplied by the persisted book count. Translation, precision polish and repairs remain Alipay-only until separately priced. Existing Alipay orders keep their original provider and amount.

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
