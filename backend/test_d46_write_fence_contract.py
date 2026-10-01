"""R8 HTTP command races, using isolated SQLite and real endpoint handlers."""
import unittest
from unittest.mock import patch

import test_d43_dispatch_contract as fixtures
import test_d19_order_admin as admin_fixtures


class CommandFenceTests(unittest.TestCase):
    _patch = fixtures.DispatchContractTests._patch
    tearDown = fixtures.DispatchContractTests.tearDown
    job = fixtures.DispatchContractTests.job

    def setUp(self):
        fixtures.DispatchContractTests.setUp(self)
        self.client.app.add_api_route("/jobs/{job_id}", self.main.get_job_v2, methods=["GET"])
        self.client.app.add_api_route("/api/v2/jobs/{job_id}/download", self.main.download_result_v2, methods=["GET"])
        self.headers = {"X-Job-Token": "owner-only"}

    def cancel(self, key):
        return self.client.post(f"/jobs/{key}/cancel", headers=self.headers)

    def test_completed_between_read_and_cancel_cannot_be_cancelled_or_lose_download(self):
        self.job("race", status=self.Status.running, translation_stats={"attempt_id": "a"})
        update = self.store.update_status
        def race(*args, **kwargs):
            update("race", self.Status.success, "winner completed", output_path=str(self.source))
            return update(*args, **kwargs)
        with patch.object(self.store, "update_status", side_effect=race):
            response = self.cancel("race")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.get("race").status, self.Status.success)
        self.assertEqual(self.store.list_stages("race"), [])
        download = self.client.get("/api/v2/jobs/race/download", headers=self.headers)
        self.assertEqual(download.status_code, 200, download.text[:100])
        self.assertEqual(download.content, self.source.read_bytes())

    def test_old_attempt_and_explicit_empty_cannot_cancel_new_attempt(self):
        update = self.store.update_status
        for key, initial in (("old", "a"), ("empty", "")):
            with self.subTest(key=key):
                self.job(key, status=self.Status.running, translation_stats={"attempt_id": initial})
                def race(*args, **kwargs):
                    update(key, self.Status.running, "new owner", translation_stats={"attempt_id": "new"})
                    return update(*args, **kwargs)
                with patch.object(self.store, "update_status", side_effect=race):
                    response = self.cancel(key)
                self.assertEqual(response.status_code, 409, response.text)
                current = self.store.get(key)
                self.assertEqual(current.status, self.Status.running)
                self.assertEqual(current.translation_stats["attempt_id"], "new")
                self.assertEqual(self.store.list_stages(key), [])

    def test_progress_during_cancel_does_not_make_cancellation_unusable(self):
        self.job("progress", status=self.Status.running, translation_stats={"attempt_id": "a"})
        update = self.store.update_status
        def race(*args, **kwargs):
            update("progress", self.Status.running, "more progress", translation_stats={"cached_chunks": 9})
            return update(*args, **kwargs)
        with patch.object(self.store, "update_status", side_effect=race):
            response = self.cancel("progress")
        self.assertEqual(response.status_code, 200, response.text)
        current = self.store.get("progress")
        self.assertEqual(current.status, self.Status.cancelled)
        self.assertEqual(current.translation_stats["cached_chunks"], 9)
        self.assertEqual(len(self.store.list_stages("progress")), 1)

    def test_late_cancel_stage_does_not_pollute_restarted_attempt(self):
        self.job("stage", status=self.Status.running, translation_stats={"attempt_id": "a"})
        add_stage = self.store.add_stage
        def race(*args, **kwargs):
            self.store.update_status("stage", self.Status.running, "new attempt",
                                     translation_stats={"attempt_id": "new"}, allow_cancelled_transition=True)
            return add_stage(*args, **kwargs)
        with patch.object(self.store, "add_stage", side_effect=race):
            response = self.cancel("stage")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.get("stage").translation_stats["attempt_id"], "new")
        self.assertEqual(self.store.list_stages("stage"), [])

    def test_restart_rejects_changed_terminal_snapshot(self):
        self.job("restart", status=self.Status.failed, enable_translation=True,
                 translation_stats={"attempt_id": "a"})
        restart = self.store.restart_translation_attempt
        def race(*args, **kwargs):
            self.store.update_status("restart", self.Status.failed, "newer diagnostic snapshot")
            return restart(*args, **kwargs)
        with patch.object(self.store, "restart_translation_attempt", side_effect=race):
            response = self.client.post("/jobs/restart/restart", headers=self.headers)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.get("restart").translation_stats["attempt_id"], "a")
        self.publish.assert_not_called()


class AdminCommandFenceTests(unittest.TestCase):
    setUpClass = classmethod(admin_fixtures.AdminTests.setUpClass.__func__)
    setUp = admin_fixtures.AdminTests.setUp
    tearDown = admin_fixtures.AdminTests.tearDown
    job = admin_fixtures.AdminTests.job
    login = admin_fixtures.AdminTests.login

    def test_ambiguous_enqueue_failure_cannot_fail_an_already_running_or_finished_attempt(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.admin.router import make_router
        from app.models import JobStatus
        for status in (JobStatus.running, JobStatus.success):
            with self.subTest(status=status):
                key = status.value
                self.job(key, enable_translation=False)
                self.trade.return_value = {"out_trade_no": key, "trade_status": "TRADE_SUCCESS", "total_amount": "5.99"}
                def ambiguous(job, _background):
                    self.store.update_status(job.id, status, "already executing or completed")
                    raise ConnectionError("Ambiguous delivery acknowledgement")
                app = FastAPI()
                app.include_router(make_router(self.store, self.uploads, self.outputs, ambiguous))
                with TestClient(app, base_url="https://testserver") as client:
                    self.login(client)
                    response = client.post(f"/api/admin/orders/{key}/retry", json={"acknowledge_cost": True})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.store.get(key).status, status)
                self.assertEqual(self.store.get(key).message, "already executing or completed")
                stages = self.store.list_stages(key)
                self.assertEqual(stages[-1].metadata["attempt_id"], self.store.get(key).translation_stats["attempt_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
