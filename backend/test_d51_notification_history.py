"""R13 opt-in real-book notification authorization and unchanged download gate.

Inherits the four R1 entitlement/history methods intentionally, then exercises
notification pagination against jobs bound to the same three SHA-pinned source
and prior artifact files. It does not translate books or contact a gateway.
"""
from __future__ import annotations

import hashlib
import shutil
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import test_d37_entitlement_history as history


class HistoricalNotificationTests(history.HistoricalEntitlementTests):
    def seed_notification_book(self, book, *, suffix="", user_id="history-owner"):
        from app.models import Job, JobNotification, JobStatus, NotificationStatus, OutputMode
        key = book["key"] + suffix
        output = self.outputs / (key + ".epub")
        shutil.copyfile(self.history_outputs / book["output"], output)
        now = datetime.now(timezone.utc)
        job = Job(id=key, trace_id="r13-real-book", source_filename=book["input"],
                  input_path=str(self.history_uploads / book["input"]), output_path=str(output),
                  output_mode=OutputMode.simplified, status=JobStatus.success, user_id=user_id,
                  access_token="history-token-" + key, creator_session="history-original-session",
                  token_expires_at=now + timedelta(hours=1))
        self.store.add(job)
        for index in range(5):
            self.store.add_notification(JobNotification(
                job_id=key, channel="in_app", status=NotificationStatus.sent,
                created_at=now - timedelta(seconds=index // 2),
                payload={"job_id": key, "status": "success", "message": book["input"],
                         "output_path": str(output), "source_filename": book["input"],
                         "access_token": job.access_token, "email": "historical-owner@example.invalid"},
            ))
        self.store.add_notification(JobNotification(job_id=key, channel="email", status=NotificationStatus.sent,
                                                     payload={"email": "historical-owner@example.invalid"}))
        return job, output

    def notification_get(self, job=None, *, cursor=None, limit=2, token=None):
        params = {"limit": limit}
        if job is not None:
            params["job_id"] = job.id
        if cursor is not None:
            params["cursor"] = cursor
        headers = {"X-Job-Token": token} if token is not None else {}
        response = self.client.get("/api/v2/notifications", params=params, headers=headers)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response

    def test_real_books_require_strict_notification_job_capability(self):
        for book in history.BOOKS:
            with self.subTest(book=book["key"]):
                job, output = self.seed_notification_book(book)
                with patch.object(self.main, "get_current_user_optional", return_value=None):
                    for token in (None, "foreign-token"):
                        self.assertEqual(self.notification_get(job, token=token).status_code, 403)
                    legacy = self.client.get("/api/v2/notifications", params={"job_id": job.id},
                                             headers={"X-Client-Session": job.creator_session})
                    self.assertEqual(legacy.status_code, 403)
                    response = self.notification_get(job, token=job.access_token)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(len(response.json()["items"]), 2)
                    self.assertNotIn(book["input"], response.text)
                    self.assertNotIn(job.access_token, response.text)
                    self.assertNotIn("historical-owner@example.invalid", response.text)
                self.assertEqual(history.sha256(output), book["output_sha256"])

    def test_real_book_owner_scope_and_cursor_do_not_cross_other_owner(self):
        from app.models import User
        for book in history.BOOKS:
            self.seed_notification_book(book)
            self.seed_notification_book(book, suffix="-foreign", user_id="other-owner")
        with patch.object(self.main, "get_current_user_optional", return_value=User(id="history-owner")):
            response = self.notification_get(limit=4)
            self.assertEqual(response.status_code, 200, response.text)
            first = response.json()
            cursor, items = first["next_cursor"], list(first["items"])
            self.assertIsNotNone(cursor)
            with patch.object(self.main, "get_current_user_optional", return_value=User(id="other-owner")):
                self.assertEqual(self.notification_get(cursor=cursor, limit=4).status_code, 400)
            while cursor:
                response = self.notification_get(cursor=cursor, limit=4)
                self.assertEqual(response.status_code, 200, response.text)
                page = response.json()
                items.extend(page["items"])
                cursor = page["next_cursor"]
        self.assertEqual(len(items), 15)
        self.assertEqual(len({row["id"] for row in items}), 15)
        self.assertEqual({row["job_id"] for row in items}, {book["key"] for book in history.BOOKS})

    def test_real_book_notification_refresh_preserves_download_and_cursor_after_reload(self):
        from app.storage_db import PersistentJobStore
        for book in history.BOOKS:
            with self.subTest(book=book["key"]):
                job, output = self.seed_notification_book(book)
                with patch.object(self.main, "get_current_user_optional", return_value=None):
                    first = self.notification_get(job, token=job.access_token)
                    self.assertEqual(first.status_code, 200, first.text)
                    first = first.json()
                    reloaded = PersistentJobStore(engine=self.store._engine)
                    with patch.object(self.main, "job_store", reloaded):
                        rest = self.notification_get(job, token=job.access_token, cursor=first["next_cursor"], limit=100)
                        self.assertEqual(rest.status_code, 200, rest.text)
                        items = first["items"] + rest.json()["items"]
                        self.assertEqual(len(items), 5)
                        self.assertEqual(len({row["id"] for row in items}), 5)
                        self.assertIsNone(rest.json()["next_cursor"])
                        detail = self.client.get(f"/api/v2/jobs/{job.id}", headers={"X-Job-Token": job.access_token})
                        self.assertEqual(detail.status_code, 200, detail.text)
                        self.assertEqual(detail.headers["cache-control"], "no-store")
                        download = self.client.get(detail.json()["download_url"])
                        self.assertEqual(download.status_code, 200, download.text[:200])
                        self.assertEqual(hashlib.sha256(download.content).hexdigest(), book["output_sha256"])
                self.assertEqual(history.sha256(output), book["output_sha256"])
                self.enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
