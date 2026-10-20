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

    def create(self, *, pdf=False, batch=False, filename="example.epub", content=None, **data):
        fields = {"payment_method": "paypal", "output_mode": "simplified", **data}
        if pdf:
            fields.pop("output_mode")
            response = self.client.post("/api/v2/pdf-jobs", files={"file": ("example.pdf", b"%PDF-1.4\nSYNTHETIC")}, data=fields)
        elif batch:
            response = self.client.post("/api/v2/batches", files=[("files", (f"{i}.epub", minimal_epub_bytes())) for i in range(2)], data=fields)
        else:
            response = self.client.post("/api/v2/jobs", files={"file": (filename, minimal_epub_bytes() if content is None else content)}, data=fields)
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

    def test_unknown_method_fails_before_gateway(self):
        for fields in ({"payment_method": "unknown"},):
            self.assertEqual(self.create(**fields).status_code, 400)
        self.pay.assert_not_called(); self.create_provider.assert_not_called()

    def test_service_pricing_rounds_up_and_has_floor(self):
        self.assertEqual(checkout.service_usd("3.99"), "1.49")
        self.assertEqual(checkout.service_usd("14.01"), "2.41")

    def test_fee_rounding_covers_net_and_charges_fixed_fee_once(self):
        from decimal import Decimal
        for cny in ['0.01','3.99','7.87','7.88','14.01','39.50','99999.00']:
            for base in ['0.00','0.99']:
                gross=Decimal(checkout.service_usd(cny,base_usd=base))
                net=Decimal(cny)/Decimal('7')+Decimal(base)
                self.assertGreaterEqual(gross,Decimal('1.49'))
                self.assertGreaterEqual(gross*Decimal('.956')-Decimal('.30'),net)
                if gross>Decimal('1.49'):
                    self.assertLess((gross-Decimal('.01'))*Decimal('.956')-Decimal('.30'),net)
        self.assertEqual(checkout.polish_usd('3.99')['total_usd'],'1.95')
        self.assertEqual(checkout.polish_usd('3.99')['price_usd'],'0.96')

    def test_polish_estimate_matches_created_order_and_does_not_charge(self):
        with patch("app.domain.precision_polish_service.inspect_precision_polish_source",
                return_value={"char_count":120000,"candidates":1}):
            response=self.client.post('/api/v2/estimate-polish',
                files={'file':('sample.epub',minimal_epub_bytes())},data={'output_mode':'simplified'})
            self.assertEqual(response.status_code,200,response.text)
            estimate=response.json()
            job=self.created(enable_precision_polish='true')
        self.assertEqual(estimate['price_cny'],'3.99')
        self.assertEqual(estimate['price_usd'],'0.96')
        self.assertEqual(estimate['total_usd'],job.expected_amount)
        self.create_provider.assert_not_called()
        self.pay.assert_not_called()

    def test_multilingual_polish_paypal_capture_freezes_language(self):
        from test_d41_precision_contract import polish_fixture_bytes
        from app.domain.payment_entitlement import precision_polish_entitlement_reason
        from app.storage_db import PayPalOrderRecord
        from sqlalchemy.orm import Session
        for language, text in [('en', 'This are a book.'), ('ja', 'これは本です。'), ('ko', '한국어 책입니다.'), ('zh-TW', '這是繁體中文。')]:
            with self.subTest(language=language):
                fixture = polish_fixture_bytes('<p>' + text + '</p>')
                response = self.client.post('/api/v2/estimate-polish', files={'file':('sample.epub',fixture)},
                                            data={'polish_language':language})
                self.assertEqual(response.status_code, 200, response.text)
                with patch('app.domain.precision_polish_service.inspect_precision_polish_source',
                           return_value={'char_count':len(text),'candidates':1}) as inspect:
                    job = self.created(enable_precision_polish='true', polish_language=language)
                self.assertEqual(inspect.call_args.kwargs['language'], language)
                self.assertEqual(job.payment_entitlement['precision_polish']['language'], language)
                self.assertEqual(response.json()['total_usd'], job.expected_amount)
                amount = job.expected_amount
                self.order()
                self.get_provider.return_value = provider_order(self.local_id, amount, 'APPROVED')
                self.capture_provider.return_value = provider_order(self.local_id, amount, 'COMPLETED')
                self.assertEqual(self.capture().status_code, 200)
                saved = self.store.get(job.id)
                self.assertEqual(precision_polish_entitlement_reason(saved), '')
                saved.translation_stats['precision_polish']['language'] = 'zh-CN'
                self.assertEqual(precision_polish_entitlement_reason(saved), 'entitlement_mismatch')
                with Session(self.engine) as session:
                    session.query(PayPalOrderRecord).delete(); session.commit()

    def test_unknown_polish_language_rejected_before_payment(self):
        response = self.create(enable_precision_polish='true', polish_language='xx')
        self.assertEqual(response.status_code, 400)
        self.create_provider.assert_not_called()
        self.pay.assert_not_called()

    def test_txt_polish_five_languages_match_usd_quote_and_capture(self):
        from app.domain.payment_entitlement import precision_polish_entitlement_reason
        from app.storage_db import PayPalOrderRecord
        from sqlalchemy.orm import Session
        for language,text in [('en','This are a book.'),('ja','これは本です。'),('ko','한국어 책입니다.'),('zh-TW','這是繁體書。'),('zh-CN','他觉得很窝心。')]:
            with self.subTest(language=language):
                quote=self.client.post('/api/v2/estimate-polish',files={'file':('book.txt',text.encode())},data={'polish_language':language})
                self.assertEqual(quote.status_code,200,quote.text)
                job=self.created(filename='book.txt',content=text.encode(),enable_precision_polish='true',polish_language=language)
                amount=job.expected_amount
                self.assertEqual(amount,quote.json()['total_usd'])
                self.order()
                self.get_provider.return_value=provider_order(self.local_id,amount,'APPROVED')
                self.capture_provider.return_value=provider_order(self.local_id,amount,'COMPLETED')
                self.assertEqual(self.capture().status_code,200)
                self.assertEqual(precision_polish_entitlement_reason(self.store.get(job.id)),'')
                with Session(self.engine) as session:
                    session.query(PayPalOrderRecord).delete();session.commit()

    def test_existing_ai_order_is_not_repriced_on_create_or_capture(self):
        with patch.object(main, "_prepare_translation_request", return_value=(
                {"price_cny":"3.99"},None,None,[])), patch.object(checkout,'service_usd',return_value='0.99'):
            job=self.created(enable_translation='true')
        with patch.object(checkout,'service_usd',side_effect=AssertionError('no repricing')):
            self.order()
            self.get_provider.return_value=provider_order(self.local_id,'0.99','APPROVED')
            self.capture_provider.return_value=provider_order(self.local_id,'0.99','COMPLETED')
            self.assertEqual(self.capture().status_code,200)
            self.assertEqual(self.store.get(job.id).expected_amount,'0.99')

    def test_translation_and_polish_get_frozen_usd_entitlements(self):
        for translated in (True, False):
            with self.subTest(translated=translated), patch.object(main, "_prepare_translation_request",
                    return_value=({"price_cny": "14.01", "total_chars": 120000}, None, None, [])), patch(
                    "app.domain.precision_polish_service.inspect_precision_polish_source",
                    return_value={"char_count": 120000, "candidates": 1}):
                job = self.created(enable_translation=str(translated).lower(), enable_precision_polish=str(not translated).lower())
                amount = "2.41" if translated else "1.95"
                self.assertEqual(job.expected_amount, amount)
                self.assertEqual(job.payment_entitlement["state"], "quoted")
                self.order()
                self.get_provider.return_value = provider_order(self.local_id, amount, "APPROVED")
                self.capture_provider.return_value = provider_order(self.local_id, amount, "COMPLETED")
                self.assertEqual(self.capture().status_code, 200)
                saved = self.store.get(job.id)
                self.assertEqual(saved.payment_entitlement["state"], "paid")
                self.assertEqual(saved.payment_entitlement["amount"], amount)
                if not translated:
                    from app.domain.payment_entitlement import precision_polish_entitlement_reason
                    self.assertEqual(precision_polish_entitlement_reason(saved), "")
                # Provider fixtures use unique order IDs in production.
                from app.storage_db import PayPalOrderRecord
                from sqlalchemy.orm import Session
                with Session(self.engine) as session:
                    session.query(PayPalOrderRecord).delete(); session.commit()
        self.pay.assert_not_called()

    def test_translation_confirmation_preserves_usd_after_reload(self):
        from test_d20_translation_confirmation import _preflight
        from app.storage_db import PersistentJobStore
        with patch.object(main, "_prepare_translation_request", return_value=(
                {"price_cny": "14.01", "total_chars": 120000}, _preflight(), None, [])):
            job = self.created(enable_translation="true", profile_confirmation="true")
        self.assertEqual(job.status, JobStatus.awaiting_confirmation)
        self.assertEqual(self.client.post(f"/api/v2/paypal/orders/{job.id}").status_code, 409)
        with patch.object(main, "job_store", PersistentJobStore(self.engine)), patch.object(
                main, "_calc_translation_price", side_effect=AssertionError("Must not reprice")):
            result = self.client.post(f"/api/v2/jobs/{job.id}/confirm-profile", json={})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["currency"], "USD")
        self.assertEqual(result.json()["amount"], "2.41")
        self.assertIn("paypal-checkout", result.json()["pay_url"])
        self.assertEqual(self.store.get(job.id).translation_stats["payment_checkout"]["currency"], "USD")
        self.pay.assert_not_called()

    def repair(self, method="paypal"):
        from test_epub_fixture import repairable_epub_bytes
        self.stack.enter_context(patch.object(main, "_REPAIR_UPLOAD_DIR", self.root / "repairs"))
        self.repair_run = self.stack.enter_context(patch.object(main, "_ensure_repair_running", return_value=True))
        response = self.client.post("/api/v2/repair/diagnose", files={"file": ("repair.epub", repairable_epub_bytes())},
                                    data={"payment_method": method})
        self.assertEqual(response.status_code, 200, response.text)
        self.job_id = response.json()["job_id"]
        self.local_id = "repair_" + self.job_id
        return response.json()

    def test_repair_capture_is_idempotent_and_cannot_use_alipay_proof(self):
        data = self.repair()
        self.assertEqual(data["currency"], "USD")
        pay = self.client.post(f"/api/v2/repair/{self.job_id}/pay")
        self.assertEqual(pay.status_code, 200, pay.text)
        self.assertEqual(pay.json()["amount"], "0.99")
        self.order()
        with patch.object(main, "verify_alipay_notification", return_value=True):
            response = self.client.post("/api/v2/webhooks/alipay", data={"out_trade_no": self.local_id,
                "app_id": "offline-test-app", "total_amount": "0.99", "trade_status": "TRADE_SUCCESS"})
            self.assertEqual(response.text, "fail")
        self.repair_run.assert_not_called()
        self.get_provider.return_value = provider_order(self.local_id, status="APPROVED")
        self.capture_provider.return_value = provider_order(self.local_id, status="COMPLETED")
        self.assertEqual(self.capture().status_code, 200)
        self.get_provider.return_value = provider_order(self.local_id, status="COMPLETED")
        self.assertEqual(self.capture().status_code, 200)
        self.capture_provider.assert_called_once()
        self.repair_run.assert_called_once()
        self.assertEqual(main._repair_job_get(self.job_id)["status"], "paid")

    def test_repair_webhook_and_recovery_verify_currency_and_binding(self):
        self.repair()
        self.client.post(f"/api/v2/repair/{self.job_id}/pay")
        self.order()
        self.get_provider.return_value = provider_order(self.local_id, status="COMPLETED")
        self.get_provider.return_value["purchase_units"][0]["amount"]["currency_code"] = "CNY"
        self.assertEqual(self.capture().status_code, 503)
        self.repair_run.assert_not_called()
        self.get_provider.return_value = provider_order(self.local_id, status="COMPLETED")
        event = {"event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {
            "supplementary_data": {"related_ids": {"order_id": "ORDER123456789"}}}}
        self.assertEqual(self.client.post("/api/v2/webhooks/paypal", json=event).status_code, 200)
        self.assertEqual(main._repair_job_get(self.job_id)["status"], "paid")
        self.repair_run.assert_called_once()

    def test_repair_scheduled_recovery_and_old_order_isolation(self):
        self.repair()
        self.client.post(f"/api/v2/repair/{self.job_id}/pay")
        self.order()
        self.get_provider.return_value = provider_order(self.local_id, status="COMPLETED")
        main._repair_job_set(self.job_id, next_payment_check_at=0)
        with patch("app.infra.alipay.query_verified_trade", side_effect=AssertionError("Wrong gateway")):
            result = main._repair_check_payment(self.job_id, background=True)
        self.assertEqual(result["payment_check"], "verified_paid")
        self.repair_run.assert_called_once()
        self.repair("alipay")
        self.assertEqual(self.client.get(f"/api/v2/paypal/orders/{self.local_id}").status_code, 409)

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
