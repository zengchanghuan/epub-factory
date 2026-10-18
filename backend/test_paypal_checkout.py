"""Offline PayPal boundaries using a real temporary SQLite and public endpoints."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest.mock import patch

from app import main
from app.infra import paypal
from app.domain import paypal_checkout as checkout
from app.domain import payment_email_service as mail
from app.models import JobStatus
import test_d59_pdf_api as pdf_fixtures
from test_epub_fixture import minimal_epub_bytes


def provider_order(local_id, amount="0.99", status="CREATED"):
    unit = {"custom_id": local_id, "invoice_id": "fixepub-" + local_id,
            "amount": {"currency_code": "USD", "value": amount}, "payee": {"merchant_id": "MERCHANT12345"}}
    if status == "COMPLETED":
        unit["payments"] = {"captures": [{"id": "CAPTURE123456", "status": "COMPLETED", "final_capture": True,
            "amount": {"currency_code": "USD", "value": amount}}]}
    return {"id": "ORDER123456789", "status": status, "purchase_units": [unit]}


class PayPalTests(unittest.TestCase):
    prepared = pdf_fixtures.PdfApiTests.prepared
    confirm = pdf_fixtures.PdfApiTests.confirm

    def setUp(self):
        pdf_fixtures.PdfApiTests.setUp(self)
        self.stack.enter_context(patch.dict(os.environ, {
            "PAYPAL_CLIENT_ID": "offline-client", "PAYPAL_SECRET": "offline-secret",
            "PAYPAL_MODE": "sandbox", "PAYPAL_CHECKOUT_ENABLED": "1", "PAYPAL_WEBHOOK_ID": "offline-hook",
        }))
        self.receipts = self.stack.enter_context(patch.object(mail, "queue_paid_order_email", return_value=True))
        self.create_provider = self.stack.enter_context(patch.object(paypal, "create_order",
            side_effect=lambda local_id, amount, **kw: provider_order(local_id, amount)))
        self.get_provider = self.stack.enter_context(patch.object(paypal, "get_order"))
        self.capture_provider = self.stack.enter_context(patch.object(paypal, "capture_order"))
        self.verify = self.stack.enter_context(patch.object(paypal, "verify_webhook", return_value=True))

    def create(self, *, pdf=False, batch=False, **data):
        fields = {"payment_method": "paypal", "output_mode": "simplified", **data}
        if pdf:
            fields.pop("output_mode")
            response = self.client.post("/api/v2/pdf-jobs", files={"file": ("example.pdf", b"%PDF-1.4\nSYNTHETIC")}, data=fields)
        elif batch:
            response = self.client.post("/api/v2/batches", files=[("files", (f"{i}.epub", minimal_epub_bytes())) for i in range(2)], data=fields)
        else:
            response = self.client.post("/api/v2/jobs", files={"file": ("example.epub", minimal_epub_bytes())}, data=fields)
        return response

    def created(self, **options):
        response = self.create(**options)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.client.headers["X-Job-Token"] = data["access_token"]
        self.local_id = "batch_" + data["batch_id"] if options.get("batch") else data["job_id"]
        self.job_id = data["jobs"][0]["job_id"] if options.get("batch") else data["job_id"]
        return self.store.get(self.job_id)

    def order(self):
        response = self.client.post(f"/api/v2/paypal/orders/{self.local_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def capture(self):
        return self.client.post(f"/api/v2/paypal/orders/{self.local_id}/capture")

    def test_single_frozen_usd_quote_never_calls_alipay_or_provider_before_checkout(self):
        job = self.created()
        self.assertEqual(job.expected_amount, "0.99")
        self.assertEqual(job.status, JobStatus.pending_payment)
        self.assertEqual(job.translation_stats["payment_checkout"]["currency"], "USD")
        self.pay.assert_not_called(); self.create_provider.assert_not_called(); self.enqueue.assert_not_called()
        data = self.client.get(f"/api/v2/jobs/{job.id}").json()
        self.assertEqual((data["currency"], data["payment_method"]), ("USD", "paypal"))
        self.assertNotIn("payment_checkout", data.get("translation_stats") or {})

    def test_live_price_and_receipt_are_not_changed_by_admin_discount(self):
        with patch.dict(os.environ, {"PAYPAL_MODE": "live", "ADMIN_SECRET": "test-admin"}):
            job = self.created(admin_key="test-admin")
            self.assertEqual(job.expected_amount, "0.99")
            self.assertFalse(job.is_test_order)

    def test_unpriced_services_and_unknown_method_fail_before_gateway(self):
        for fields in ({"enable_translation": "true"}, {"enable_precision_polish": "true"}, {"payment_method": "unknown"}):
            self.assertEqual(self.create(**fields).status_code, 400)
        self.pay.assert_not_called(); self.create_provider.assert_not_called()

    def test_capability_disabled_and_memory_store_cannot_open_paypal(self):
        with patch.dict(os.environ, {"PAYPAL_CHECKOUT_ENABLED": "0"}):
            self.assertEqual(self.create().status_code, 503)
            self.assertFalse(self.client.get('/api/v2/paypal/config').json()["enabled"])
        with patch.object(main, 'job_store', object()):
            self.assertFalse(self.client.get('/api/v2/paypal/config').json()["enabled"])

    def test_batch_aggregates_two_books_and_rejects_child_checkout(self):
        job = self.created(batch=True)
        self.assertEqual(job.expected_amount, "1.98")
        self.assertEqual(len(self.store.list_jobs_by_batch_id(job.batch_id)), 2)
        self.order()
        self.assertEqual(self.create_provider.call_args.args, (self.local_id, "1.98"))
        self.assertEqual(self.client.post(f"/api/v2/paypal/orders/{job.id}").status_code, 409)

    def test_pdf_preserves_review_gate_and_usd_quote_through_confirmation(self):
        job = self.created(pdf=True)
        self.assertEqual(job.translation_stats["pdf_conversion"]["amount"], "0.99")
        self.assertEqual(self.client.post(f"/api/v2/paypal/orders/{job.id}").status_code, 409)
        job = self.prepared(job)
        response = self.confirm(job)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["currency"], "USD")
        self.assertTrue(response.json()["pay_url"].startswith("https://fixepub.com/paypal-checkout.html"))
        self.order(); self.pay.assert_not_called()

    def test_capture_requires_ownership_and_matching_persisted_order(self):
        self.created(); self.order()
        self.client.headers["X-Job-Token"] = "wrong"
        self.assertEqual(self.capture().status_code, 403)
        self.capture_provider.assert_not_called()

    def test_capture_is_idempotent_and_releases_only_after_verified_completed_payment(self):
        job = self.created(); self.order()
        self.get_provider.return_value = provider_order(self.local_id, status="APPROVED")
        self.capture_provider.return_value = provider_order(self.local_id, status="COMPLETED")
        self.assertEqual(self.capture().status_code, 200)
        self.assertEqual(self.store.get(job.id).status, JobStatus.pending)
        self.assertEqual(self.receipts.call_args.kwargs["currency"], "USD")
        self.get_provider.return_value = provider_order(self.local_id, status="COMPLETED")
        self.assertEqual(self.capture().status_code, 200)
        self.capture_provider.assert_called_once()
        self.assertEqual(len(self.store.list_dispatches(job.id)), 1)

    def test_pending_capture_or_wrong_currency_amount_merchant_does_not_release(self):
        job = self.created(); self.order()
        variants=[]
        for field,value in (("currency_code", "CNY"), ("value", "0.01")):
            order=provider_order(self.local_id, status="COMPLETED")
            order["purchase_units"][0]["payments"]["captures"][0]["amount"][field]=value
            variants.append(order)
        order=provider_order(self.local_id, status="COMPLETED"); order["purchase_units"][0]["payee"]["merchant_id"]="WRONGMERCHANT"; variants.append(order)
        order=provider_order(self.local_id, status="COMPLETED"); order["purchase_units"][0]["payments"]["captures"][0]["status"]="PENDING"; variants.append(order)
        for order in variants:
            self.get_provider.return_value=order
            self.assertEqual(self.capture().status_code, 503)
            self.assertEqual(self.store.get(job.id).status, JobStatus.pending_payment)
        self.receipts.assert_not_called(); self.enqueue.assert_not_called()

    def test_expired_checkout_cannot_recreate_or_capture_but_completed_payment_recovers(self):
        job=self.created(); self.order()
        with self.store._Session() as session:
            from app.storage_db import JobRecord
            record=session.get(JobRecord,job.id); record.created_at=datetime.now(timezone.utc)-timedelta(hours=7); session.commit()
        self.get_provider.return_value=provider_order(self.local_id,status="APPROVED")
        self.assertEqual(self.capture().status_code,409)
        self.capture_provider.assert_not_called()
        self.get_provider.return_value=provider_order(self.local_id,status="COMPLETED")
        self.assertEqual(self.capture().status_code,200)
        self.assertEqual(self.store.get(job.id).status,JobStatus.pending)

    def test_browser_origin_or_unsigned_webhook_cannot_release(self):
        job=self.created(); self.order()
        self.assertEqual(self.client.post(f"/api/v2/paypal/orders/{self.local_id}/capture",headers={"Origin":"https://evil.invalid"}).status_code,403)
        self.verify.return_value=False
        response=self.client.post('/api/v2/webhooks/paypal',json={"event_type":"PAYMENT.CAPTURE.COMPLETED"})
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.store.get(job.id).status,JobStatus.pending_payment)

    def test_signed_webhook_fetches_order_and_recovers_after_browser_closed(self):
        job=self.created(); self.order()
        self.get_provider.return_value=provider_order(self.local_id,status="COMPLETED")
        event={"event_type":"PAYMENT.CAPTURE.COMPLETED","resource":{"supplementary_data":{"related_ids":{"order_id":"ORDER123456789"}}}}
        for _ in range(2):
            response=self.client.post('/api/v2/webhooks/paypal',json=event)
            self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.store.get(job.id).status,JobStatus.pending)
        self.capture_provider.assert_not_called()
        self.assertEqual(len(self.store.list_dispatches(job.id)),1)

    def test_alipay_callback_cannot_settle_paypal_job(self):
        job=self.created()
        with patch.object(main,'verify_alipay_notification',return_value=True):
            response=self.client.post('/api/v2/webhooks/alipay',data={"app_id":"offline-test-app",
                "out_trade_no":job.id,"total_amount":"0.99","trade_status":"TRADE_SUCCESS"})
        self.assertEqual(response.text,'fail')
        self.assertEqual(self.store.get(job.id).status,JobStatus.pending_payment)

    def test_repeated_create_reuses_persisted_provider_id(self):
        self.created(); first=self.order()
        self.get_provider.return_value=provider_order(self.local_id)
        self.assertEqual(self.order(),first)
        self.create_provider.assert_called_once()


class PayPalValidationTests(unittest.TestCase):
    def test_receipt_rejects_wrong_order_and_invoice_multiple_units_or_captures(self):
        base=provider_order('local',status='COMPLETED')
        variants=[]
        for field in ('custom_id','invoice_id'):
            order=deepcopy(base); order['purchase_units'][0][field]='other'; variants.append(order)
        order=deepcopy(base); order['purchase_units']*=2; variants.append(order)
        order=deepcopy(base); order['purchase_units'][0]['payments']['captures']*=2; variants.append(order)
        for order in variants:
            with self.assertRaises(paypal.PayPalUnavailable): paypal.validate_order(order,'local','0.99',completed=True)

    def test_webhook_certificate_url_is_not_arbitrary(self):
        with patch.dict(os.environ,{'PAYPAL_MODE':'live','PAYPAL_CLIENT_ID':'x','PAYPAL_SECRET':'x','PAYPAL_WEBHOOK_ID':'x'}),patch.object(paypal,'api') as api:
            headers={name:'value' for name in ('paypal-auth-algo','paypal-transmission-id','paypal-transmission-sig','paypal-transmission-time')}
            headers['paypal-cert-url']='https://attacker.invalid/cert'
            self.assertFalse(paypal.verify_webhook(headers,{}));api.assert_not_called()


if __name__=='__main__': unittest.main()
