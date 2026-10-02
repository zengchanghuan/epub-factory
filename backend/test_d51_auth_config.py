"""R13 account-token configuration guard; local HMAC only, no real secrets."""
from __future__ import annotations

import importlib.util
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from jose import jwt


class AuthConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.network = []
        for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo"):
            guard = patch(target, side_effect=AssertionError("R13 auth tests forbid external I/O"))
            self.network.append(guard.start())
            self.addCleanup(guard.stop)

    def tearDown(self):
        for guard in self.network:
            guard.assert_not_called()

    def module(self, secret=None, **extra):
        environment = {"JWT_EXPIRE_DAYS": "7", **extra}
        if secret is not None:
            environment["JWT_SECRET"] = secret
        source = Path(__file__).parent / "app" / "auth" / "jwt.py"
        spec = importlib.util.spec_from_file_location("offline_r13_jwt", source)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, environment, clear=True):
            spec.loader.exec_module(module)
        return module

    @staticmethod
    def forged(secret, *, algorithm="HS256", **values):
        now = datetime.now(timezone.utc)
        payload = {"sub": "victim-account", "iat": now, "exp": now + timedelta(hours=1), **values}
        return jwt.encode(payload, secret, algorithm=algorithm)

    def test_missing_empty_whitespace_and_public_placeholders_cannot_issue_or_decode(self):
        for secret in (None, "", "  \t", "CHANGE_ME_IN_PRODUCTION_PLEASE",
                       "replace_with_a_long_random_secret_key", " CHANGE_ME_IN_PRODUCTION_PLEASE "):
            with self.subTest(secret=secret):
                module = self.module(secret)
                token = self.forged("CHANGE_ME_IN_PRODUCTION_PLEASE")
                with patch.object(module.jwt, "decode", side_effect=AssertionError("invalid config decoder")), \
                     patch.object(module.jwt, "encode", side_effect=AssertionError("invalid config signer")):
                    self.assertIsNone(module.decode_access_token(token))
                    with self.assertRaises(HTTPException) as denied:
                        module.create_access_token("victim-account")
                    self.assertEqual(denied.exception.status_code, 503)
                    self.assertNotIn("CHANGE_ME", denied.exception.detail)

    def test_explicit_configured_key_roundtrip_preserves_algorithm_and_default_lifetime(self):
        module = self.module("offline-secret-for-unit-test-only")
        token = module.create_access_token("owner-account")
        self.assertEqual(module.decode_access_token(token), "owner-account")
        claims = jwt.decode(token, "offline-secret-for-unit-test-only", algorithms=["HS256"])
        self.assertEqual(claims["exp"] - claims["iat"], 7 * 86400)
        self.assertEqual(jwt.get_unverified_header(token)["alg"], "HS256")

    def test_explicit_key_bytes_are_not_trimmed_or_subject_to_new_strength_rules(self):
        for secret in ("short", "  explicit-test-key  "):
            with self.subTest(secret=secret):
                module = self.module(secret)
                token = module.create_access_token("owner")
                self.assertEqual(jwt.decode(token, secret, algorithms=["HS256"])["sub"], "owner")
                self.assertEqual(module.decode_access_token(token), "owner")

    def test_published_default_forgery_does_not_validate_under_real_config(self):
        module = self.module("offline-private-test-key")
        for public in ("CHANGE_ME_IN_PRODUCTION_PLEASE", "replace_with_a_long_random_secret_key"):
            self.assertIsNone(module.decode_access_token(self.forged(public)))

    def test_expired_malformed_wrong_algorithm_or_wrong_key_still_rejected(self):
        key = "offline-private-test-key"
        module = self.module(key)
        values = ("not.a.jwt", self.forged("different-test-key"), self.forged(key, algorithm="HS384"),
                  self.forged(key, exp=datetime.now(timezone.utc) - timedelta(hours=1)))
        for token in values:
            self.assertIsNone(module.decode_access_token(token))

    def test_existing_expiry_configuration_and_explicit_override_preserved(self):
        module = self.module("offline-private-test-key", JWT_EXPIRE_DAYS="3")
        for override, expected in ((None, 3), (2, 2)):
            token = module.create_access_token("owner", expire_days=override)
            claims = jwt.decode(token, "offline-private-test-key", algorithms=["HS256"])
            self.assertEqual(claims["exp"] - claims["iat"], expected * 86400)

    def test_missing_configuration_is_a_clear_http_503_not_server_error(self):
        module = self.module()
        app = FastAPI()

        @app.post("/issue")
        def issue():
            return {"access_token": module.create_access_token("owner")}

        with TestClient(app) as client:
            response = client.post("/issue")
        self.assertEqual(response.status_code, 503)
        self.assertIn("配置未就绪", response.json()["detail"])
        self.assertNotIn("access_token", response.json())


if __name__ == "__main__":
    unittest.main(verbosity=2)
