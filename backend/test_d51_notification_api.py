"""R13 notification HTTP contracts: real temporary SQLite, no external I/O.

Most cases replace the authentication decoder with explicit user objects; the
JWT integration case uses real local HMAC tokens, real decoding and user lookup.
Authorization, public projection, cursors and store filtering are always real;
no model/payment/worker is invoked.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


class NotificationApiTests(unittest.TestCase):
    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="epub-r13-notifications-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self._patch(patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + str(self.root / "bootstrap.db"),
            "EPUB_PERSISTENT_STORE": "1", "REPAIR_UPLOAD_DIR": str(self.root / "repair"),
            "EPUB_TRANSLATION_CHECKPOINT_DB": str(self.root / "checkpoints.db"),
            "CELERY_BROKER_URL": "", "REDIS_URL": "", "SENTRY_DSN": "",
            "NOTIFY_EMAIL_ENABLED": "0", "OWNER_PAYMENT_EMAIL_ENABLED": "0",
            "ALIPAY_APP_ID": "", "ADMIN_SECRET": "", "SKIP_PAYMENT_CHECK": "0",
        }, clear=True))
        self._patch(patch("dotenv.load_dotenv", return_value=False))
        self.network = [self._patch(patch(target, side_effect=AssertionError("R13 forbids network")))
                        for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo")]
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from app import main
        from app.models import Job, JobNotification, JobStatus, NotificationStatus, OutputMode, User
        from app.storage_db import Base, PersistentJobStore
        self.main, self.Job, self.Notification = main, Job, JobNotification
        self.JobStatus, self.NotificationStatus, self.OutputMode, self.User = JobStatus, NotificationStatus, OutputMode, User
        self.Store = PersistentJobStore
        self.engine = create_engine("sqlite:///" + str(self.root / "notifications.db"),
                                    connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.store = PersistentJobStore(engine=self.engine)
        self._patch(patch.object(main, "job_store", self.store))
        self.auth = self._patch(patch.object(main, "get_current_user_optional", return_value=None))
        self.worker = self._patch(patch.object(main, "run_job", side_effect=AssertionError("No worker in R13 API tests")))
        api = FastAPI()
        api.add_api_route("/api/v2/notifications", main.list_notifications_v2, methods=["GET"])
        self.client = TestClient(api)
        self.addCleanup(self.client.close)
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        for guard in self.network:
            guard.assert_not_called()
        self.worker.assert_not_called()

    def job(self, key="book", **values):
        fields = dict(id=key, source_filename="private-title.epub", input_path="/private/private-title.epub",
                      trace_id="offline-r13", output_mode=self.OutputMode.simplified,
                      status=self.JobStatus.success, user_id="alice", access_token="token-" + key,
                      token_expires_at=self.now + timedelta(hours=1), creator_session="same-session",
                      creator_ip="testclient")
        fields.update(values)
        self.store.add(self.Job(**fields))
        return self.store.get(key)

    def notice(self, job_id="book", *, created_at=None, channel="in_app", user_id=None, payload=None):
        item = self.Notification(job_id=job_id, channel=channel, user_id=user_id,
                                 status=self.NotificationStatus.sent,
                                 payload=payload if payload is not None else {"status": "success", "message": "完成"},
                                 created_at=created_at or self.now)
        self.store.add_notification(item)
        return item

    def get(self, *, job_id=None, limit=None, cursor=None, token=None, **kwargs):
        params = dict(kwargs.pop("params", {}))
        for key, value in (("job_id", job_id), ("limit", limit), ("cursor", cursor)):
            if value is not None:
                params[key] = value
        headers = dict(kwargs.pop("headers", {}))
        if token is not None:
            headers["X-Job-Token"] = token
        return self.client.get("/api/v2/notifications", params=params, headers=headers, **kwargs)

    def ok(self, **kwargs):
        result = self.get(**kwargs)
        self.assertEqual(result.status_code, 200, result.text)
        body = result.json()
        self.assertEqual(set(body), {"items", "next_cursor"})
        return body

    def test_anonymous_unscoped_query_is_401_without_any_store_query(self):
        with patch.object(self.store, "get", side_effect=AssertionError("unauthenticated lookup")), \
             patch.object(self.store, "list_notification_page", side_effect=AssertionError("unscoped list")):
            for headers in ({}, {"X-Client-Session": "same-session"}, {"X-Job-Token": "token-book"}):
                self.assertEqual(self.get(headers=headers).status_code, 401)

    def test_inactive_user_cannot_list_or_authorize_by_ownership(self):
        self.job()
        self.auth.return_value = self.User(id="alice", is_active=False)
        with patch.object(self.store, "list_notification_page", side_effect=AssertionError("disabled account list")):
            self.assertEqual(self.get().status_code, 403)
            self.assertEqual(self.get(job_id="book").status_code, 403)

    def test_owner_can_read_explicit_job_without_token_but_other_user_cannot(self):
        self.job()
        self.notice()
        self.auth.return_value = self.User(id="alice")
        self.assertEqual(len(self.ok(job_id="book")["items"]), 1)
        self.auth.return_value = self.User(id="bob")
        with patch.object(self.store, "list_notification_page", side_effect=AssertionError("foreign job list")):
            self.assertEqual(self.get(job_id="book").status_code, 403)

    def test_explicit_job_valid_token_is_an_independent_capability(self):
        self.job()
        self.notice()
        for user in (None, self.User(id="bob")):
            self.auth.return_value = user
            self.assertEqual(len(self.ok(job_id="book", token="token-book")["items"]), 1)

    def test_token_header_query_and_job_scoped_cookie_supported(self):
        self.job()
        self.notice()
        self.assertEqual(len(self.ok(job_id="book", token="token-book")["items"]), 1)
        self.assertEqual(len(self.ok(job_id="book", params={"token": "token-book"})["items"]), 1)
        self.client.cookies.set("job_token_book", "token-book")
        self.assertEqual(len(self.ok(job_id="book")["items"]), 1)
        self.assertEqual(self.get(job_id="book", token="wrong").status_code, 403)

    def test_invalid_expired_and_expiryless_tokens_are_rejected_before_page_query(self):
        for key, expiry in (("expired", self.now - timedelta(seconds=1)), ("expiryless", None),
                            ("valid", self.now + timedelta(hours=1))):
            self.job(key, token_expires_at=expiry)
        with patch.object(self.store, "list_notification_page", side_effect=AssertionError("invalid token page")):
            for key, token in (("expired", "token-expired"), ("expiryless", "token-expiryless"),
                               ("valid", "wrong"), ("valid", "token-expired")):
                with self.subTest(job=key, token=token):
                    self.assertEqual(self.get(job_id=key, token=token).status_code, 403)

    def test_legacy_ip_session_and_missing_token_never_authorize(self):
        self.job("legacy", access_token=None, token_expires_at=None)
        self.job("modern")
        with patch.object(self.store, "list_notification_page", side_effect=AssertionError("legacy list")):
            for key in ("legacy", "modern"):
                self.assertEqual(self.get(job_id=key, headers={"X-Client-Session": "same-session"}).status_code, 403)

    def test_unknown_job_is_404_even_with_token(self):
        with patch.object(self.store, "list_notification_page", side_effect=AssertionError("unknown job list")):
            self.assertEqual(self.get(job_id="does-not-exist", token="anything").status_code, 404)

    def test_authenticated_scope_joins_current_job_owner_not_notification_user(self):
        self.job("own", user_id="alice")
        self.job("foreign", user_id="bob")
        self.job("anonymous", user_id=None)
        self.notice("own", user_id="bob")
        self.notice("foreign", user_id="alice")
        self.notice("anonymous", user_id="alice")
        self.notice("missing", user_id="alice")
        self.notice("own", user_id="alice", channel="email")
        self.auth.return_value = self.User(id="alice")
        result = self.ok()
        self.assertEqual([row["job_id"] for row in result["items"]], ["own"])
        self.assertIsNone(result["next_cursor"])

    def test_empty_authorized_scope_returns_explicit_null_cursor(self):
        self.job()
        self.assertEqual(self.ok(job_id="book", token="token-book"), {"items": [], "next_cursor": None})
        self.auth.return_value = self.User(id="alice")
        self.assertEqual(self.ok(), {"items": [], "next_cursor": None})

    def test_limit_default_and_max_are_bounded_at_store(self):
        self.job()
        for _ in range(105):
            self.notice()
        with patch.object(self.store, "list_notification_page", wraps=self.store.list_notification_page) as read:
            self.assertEqual(len(self.ok(job_id="book", token="token-book")["items"]), 20)
            self.assertEqual(read.call_args.kwargs["limit"], 21)
            self.assertEqual(len(self.ok(job_id="book", token="token-book", limit=100)["items"]), 100)
            self.assertEqual(read.call_args.kwargs["limit"], 101)

    def test_invalid_limits_are_rejected_not_clamped_or_unbounded(self):
        self.auth.return_value = self.User(id="alice")
        with patch.object(self.store, "list_notification_page", side_effect=AssertionError("invalid limit queried")):
            for value in (0, -1, 101, "1.5", "true", "", "9999999999999999999999"):
                with self.subTest(limit=value):
                    self.assertEqual(self.get(limit=value).status_code, 422)

    def test_keyset_order_is_created_desc_then_id_desc_with_no_duplicate_or_omission(self):
        self.job()
        for offset in (0, 0, 1, -1, 0, 1, -2):
            self.notice(created_at=self.now + timedelta(seconds=offset))
        complete = self.ok(job_id="book", token="token-book", limit=100)["items"]
        keys = [(row["created_at"], row["id"]) for row in complete]
        self.assertEqual(keys, sorted(keys, reverse=True))
        self.assertEqual(len({row["id"] for row in complete}), 7)
        gathered, cursor = [], None
        while True:
            page = self.ok(job_id="book", token="token-book", limit=2, cursor=cursor)
            gathered.extend(page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(gathered, complete)

    def test_newer_inserts_between_pages_do_not_shift_old_page_boundary(self):
        self.job()
        for offset in range(5):
            self.notice(created_at=self.now - timedelta(seconds=offset))
        original = self.ok(job_id="book", token="token-book", limit=100)["items"]
        first = self.ok(job_id="book", token="token-book", limit=2)
        self.notice(created_at=self.now + timedelta(seconds=10))
        self.notice(created_at=self.now)  # Tie timestamp, later identity.
        remainder = self.ok(job_id="book", token="token-book", limit=100, cursor=first["next_cursor"])
        self.assertEqual(first["items"] + remainder["items"], original)
        self.assertIsNone(remainder["next_cursor"])

    def test_job_cursor_cannot_cross_to_other_job_or_user_scope(self):
        for key in ("a", "b"):
            self.job(key)
            for _ in range(3):
                self.notice(key)
        cursor = self.ok(job_id="a", token="token-a", limit=1)["next_cursor"]
        self.assertEqual(self.get(job_id="b", token="token-b", cursor=cursor).status_code, 400)
        self.auth.return_value = self.User(id="alice")
        self.assertEqual(self.get(cursor=cursor).status_code, 400)

    def test_user_cursor_cannot_cross_to_other_user_or_job_scope(self):
        self.job()
        for _ in range(3):
            self.notice()
        self.auth.return_value = self.User(id="alice")
        cursor = self.ok(limit=1)["next_cursor"]
        self.assertEqual(self.get(job_id="book", cursor=cursor).status_code, 400)
        self.auth.return_value = self.User(id="bob")
        self.assertEqual(self.get(cursor=cursor).status_code, 400)

    def test_malformed_cursor_is_400_and_never_falls_back_to_first_page(self):
        self.job()
        malformed = ["", "not-base64!", "a" * 10000]
        for obj in ({}, [], {"scope": "job:book", "created_at": "not-a-date", "id": 0},
                    {"scope": "job:book", "created_at": "2026-01-01", "id": -1}):
            malformed.append(base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("="))
        with patch.object(self.store, "list_notification_page", side_effect=AssertionError("bad cursor queried")):
            for cursor in malformed:
                with self.subTest(cursor=cursor[:45]):
                    self.assertEqual(self.get(job_id="book", token="token-book", cursor=cursor).status_code, 400)

    def test_public_payload_reprojects_legacy_sensitive_fields_and_untrusted_message(self):
        self.job()
        self.notice(payload={
            "job_id": "other-private-job", "status": "failed", "message": "SecretTitle /private/book.epub owner@example.invalid token-secret",
            "error_code": "PARTIAL_TRANSLATION", "completed_at": "2026-01-01T00:00:00+00:00",
            "access_token": "token-secret", "source_filename": "SecretTitle.epub", "output_path": "/private/book.epub",
            "email": "owner@example.invalid", "download_url": "https://private.invalid/?token=token-secret",
            "nested": {"secrets": "token-secret"},
        })
        item = self.ok(job_id="book", token="token-book")["items"][0]
        self.assertEqual(set(item), {"id", "job_id", "channel", "status", "payload", "created_at"})
        public = item["payload"]
        self.assertEqual(set(public), {"job_id", "status", "message", "error_code", "completed_at"})
        self.assertEqual(public["job_id"], "book")
        self.assertEqual(public["status"], "failed")
        self.assertEqual(public["error_code"], "PARTIAL_TRANSLATION")
        for secret in ("SecretTitle", "/private/", "owner@example", "token-secret", "other-private-job", "private.invalid"):
            self.assertNotIn(secret, json.dumps(item, ensure_ascii=False))

    def test_invalid_legacy_status_error_and_timestamp_cannot_leak_through(self):
        self.job()
        self.notice(payload={"status": "secret-status", "message": "secret-message",
                             "error_code": "secret-error-code", "completed_at": "secret-timestamp"})
        item = self.ok(job_id="book", token="token-book")["items"][0]
        self.assertNotIn("secret-", json.dumps(item))
        self.assertIsNone(item["payload"]["error_code"])

    def test_cursor_survives_store_reload_with_stable_notification_ids(self):
        self.job()
        for _ in range(5):
            self.notice()
        expected = self.ok(job_id="book", token="token-book", limit=100)["items"]
        first = self.ok(job_id="book", token="token-book", limit=2)
        reloaded = self.Store(engine=self.engine)
        with patch.object(self.main, "job_store", reloaded):
            rest = self.ok(job_id="book", token="token-book", limit=100, cursor=first["next_cursor"])
        self.assertEqual(first["items"] + rest["items"], expected)

    def test_cursor_never_replaces_fresh_capability_authorization(self):
        from sqlalchemy.orm import Session
        from app.storage_db import JobRecord
        self.job()
        for _ in range(3):
            self.notice()
        cursor = self.ok(job_id="book", token="token-book", limit=1)["next_cursor"]
        with Session(self.engine) as session:
            session.get(JobRecord, "book").token_expires_at = self.now - timedelta(seconds=1)
            session.commit()
        with patch.object(self.store, "list_notification_page", side_effect=AssertionError("expired paged query")):
            self.assertEqual(self.get(job_id="book", token="token-book", cursor=cursor).status_code, 403)

    def test_correct_scope_cursor_still_requires_utc_timestamp_and_valid_position_types(self):
        self.job()
        for _ in range(3):
            self.notice()
        cursor = self.ok(job_id="book", token="token-book", limit=1)["next_cursor"]
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        mutations = ({"at": "2026-01-01T00:00:00"}, {"at": "2026-01-01T00:00:00+08:00"},
                     {"at": "not-a-date"}, {"id": 0}, {"id": ""}, {"id": "a" * 97},
                     {"v": True}, {"unexpected": "field"})
        with patch.object(self.store, "list_notification_page", side_effect=AssertionError("invalid position query")):
            for mutation in mutations:
                bad = base64.urlsafe_b64encode(json.dumps({**value, **mutation}).encode()).decode().rstrip("=")
                with self.subTest(mutation=mutation):
                    self.assertEqual(self.get(job_id="book", token="token-book", cursor=bad).status_code, 400)

    def test_real_http_jwt_authentication_enforces_owner_disabled_and_public_key_guard(self):
        from jose import jwt as jose_jwt
        from app import storage
        from app.auth import jwt as token_module
        from app.auth.deps import get_current_user_optional

        for owner in ("alice", "bob"):
            self.store.create_user(self.User(id=owner))
            self.job(owner + "-book", user_id=owner)
            self.notice(owner + "-book")
        with patch.object(self.main, "get_current_user_optional", get_current_user_optional), \
             patch.object(storage, "job_store", self.store), \
             patch.object(token_module, "_SECRET_KEY", "r13-explicit-local-integration-key"):
            headers = {}
            for owner in ("alice", "bob"):
                headers[owner] = {"Authorization": "Bearer " + token_module.create_access_token(owner)}
                own = self.ok(headers=headers[owner])
                self.assertEqual([row["job_id"] for row in own["items"]], [owner + "-book"])
                self.assertEqual(len(self.ok(job_id=owner + "-book", headers=headers[owner])["items"]), 1)
                foreign = "bob" if owner == "alice" else "alice"
                self.assertEqual(self.get(job_id=foreign + "-book", headers=headers[owner]).status_code, 403)

            self.store.update_user(self.User(id="alice", is_active=False))
            self.assertEqual(self.get(headers=headers["alice"]).status_code, 403)
            self.assertEqual(self.get(job_id="alice-book", headers=headers["alice"]).status_code, 403)

            forged = jose_jwt.encode({"sub": "bob", "exp": self.now + timedelta(hours=1)},
                                     "CHANGE_ME_IN_PRODUCTION_PLEASE", algorithm="HS256")
            forged_headers = {"Authorization": "Bearer " + forged}
            # An explicit private configuration rejects the public-key forgery.
            self.assertEqual(self.get(headers=forged_headers).status_code, 401)
            # An absent/misconfigured deployment must not revive the old default.
            for secret in ("", "CHANGE_ME_IN_PRODUCTION_PLEASE"):
                with patch.object(token_module, "_SECRET_KEY", secret):
                    self.assertEqual(self.get(headers=forged_headers).status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
