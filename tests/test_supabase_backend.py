"""Hosted owner identity, opaque web sessions and encrypted snapshot contracts."""

import base64
import fcntl
import gzip
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from cryptography.fernet import Fernet
from flask import Flask, redirect, session, url_for
from test_search_controls import DatabaseFixture
from werkzeug.security import generate_password_hash

import db
import search_settings
import supabase_backend as backend
from web_ui_plugin.web_ui import create_app

OWNER = "cc14b72d-50bb-48d8-bf9a-49893a2cda7f"
OTHER_OWNER = "1b1854fa-3f29-4b10-8b99-e49169672c91"
SETTINGS = {
    "SUPABASE_URL": "https://test-project.supabase.co",
    "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_offline_test_key",
    "SUPABASE_OWNER_ID": OWNER,
}


def response(body, status=200):
    result = Mock(status_code=status)
    result.iter_content.return_value = (
        [json.dumps(body).encode()] if body is not None else []
    )
    return result


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.http = Mock()
        self.client = backend.Client(
            backend.Config.from_environment(SETTINGS), self.http
        )

    def test_disabled_partial_configuration_and_key_privilege(self):
        self.assertIsNone(backend.Config.from_environment({}))
        with self.assertRaises(backend.SupabaseError):
            backend.Config.from_environment({"SUPABASE_URL": SETTINGS["SUPABASE_URL"]})
        payload = (
            base64.urlsafe_b64encode(json.dumps({"role": "service_role"}).encode())
            .decode()
            .rstrip("=")
        )
        with self.assertRaises(backend.SupabaseError):
            backend.Config.from_environment(
                {
                    **SETTINGS,
                    "SUPABASE_PUBLISHABLE_KEY": "header." + payload + ".signature",
                }
            )
        payload = (
            base64.urlsafe_b64encode(json.dumps({"role": "anon"}).encode())
            .decode()
            .rstrip("=")
        )
        config = backend.Config.from_environment(
            {**SETTINGS, "SUPABASE_PUBLISHABLE_KEY": "header." + payload + ".signature"}
        )
        self.assertNotIn("signature", repr(config))
        for url in (
            "http://test-project.supabase.co",
            "https://user:password@test-project.supabase.co",
            "https://test-project.supabase.co/elsewhere",
            "https://test-project.supabase.co.evil.example",
            "https://test-project.supabase.co?key=secret",
        ):
            with self.subTest(url=url), self.assertRaises(backend.SupabaseError):
                backend.Config.from_environment({**SETTINGS, "SUPABASE_URL": url})

    def test_publishable_key_requires_plausible_opaque_suffix(self):
        for key in (
            "sb_publishable_",
            "sb_publishable_short",
            "sb_publishable_eightchr",
            "sb_publishable_not-a-key#fragment",
            "sb_publishable_line\nbreak-key",
        ):
            with self.subTest(key=key), self.assertRaises(backend.SupabaseError):
                backend.Config.from_environment(
                    {**SETTINGS, "SUPABASE_PUBLISHABLE_KEY": key}
                )
        for suffix in ("aB09_test-opaque", "opaque" * 35):
            self.assertEqual(
                backend.Config.from_environment(
                    {**SETTINGS, "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_" + suffix}
                ).publishable_key,
                "sb_publishable_" + suffix,
            )

    def test_sign_in_verifies_server_identity_and_protects_transport(self):
        auth = response(
            {
                "access_token": "private-access",
                "refresh_token": "private-refresh",
                "expires_in": 3600,
                "user": {"id": OTHER_OWNER},
            }
        )
        user = response({"id": OWNER})
        self.http.request.side_effect = [auth, user]
        tokens = self.client.sign_in("owner@example.test", "private-password", now=100)
        self.assertEqual(tokens["expires_at"], 3700)
        login, verify = self.http.request.call_args_list
        self.assertEqual(
            login.args[:2],
            ("POST", SETTINGS["SUPABASE_URL"] + "/auth/v1/token?grant_type=password"),
        )
        self.assertEqual(
            verify.kwargs["headers"]["Authorization"], "Bearer private-access"
        )
        self.assertTrue(login.kwargs["verify"])
        self.assertFalse(login.kwargs["allow_redirects"])
        self.assertEqual(login.kwargs["timeout"], (5, 15))
        auth.close.assert_called_once()
        user.close.assert_called_once()

    def test_other_owner_and_non_auth_responses_are_rejected_without_details(self):
        self.http.request.return_value = response(
            {"id": OTHER_OWNER, "password": "provider-secret"}
        )
        with self.assertRaises(backend.SupabaseError) as raised:
            self.client.verify_owner("private-access")
        self.assertNotIn("provider-secret", str(raised.exception))
        for status in (302, 401, 403, 429, 500):
            self.http.request.return_value = response(
                {"error": "private-refresh provider-secret"}, status=status
            )
            with self.subTest(status=status), self.assertRaises(
                backend.SupabaseError
            ) as raised:
                self.client.verify_owner("private-access")
            self.assertNotIn("private-refresh", str(raised.exception))
        self.http.request.side_effect = requests.ConnectionError(
            "https://password@secret.example"
        )
        with self.assertRaises(backend.SupabaseError) as raised:
            self.client.verify_owner("private-access")
        self.assertNotIn("secret.example", str(raised.exception))

    def test_body_limit_and_header_injection_close_response(self):
        result = response(None)
        result.iter_content.return_value = [b"x" * (backend.AUTH_BODY_LIMIT + 1)]
        self.http.request.return_value = result
        with self.assertRaises(backend.SupabaseError):
            self.client.verify_owner("private-access")
        result.close.assert_called_once()
        self.http.reset_mock()
        with self.assertRaises(backend.SupabaseError):
            self.client.verify_owner("token\r\nsecret")
        self.http.request.assert_not_called()

    def test_backup_posts_verified_owner_ciphertext_with_rls_token(self):
        encrypted = Fernet(Fernet.generate_key()).encrypt(b"private sqlite")
        self.http.request.side_effect = [
            response({"id": OWNER}),
            response(None, status=201),
        ]
        self.client.upload_backup("private-access", encrypted)
        posted = self.http.request.call_args_list[-1]
        self.assertEqual(
            posted.kwargs["json"],
            {
                "owner_id": OWNER,
                "backup_kind": "sqlite-v1",
                "ciphertext": encrypted.decode(),
            },
        )
        self.assertEqual(
            posted.kwargs["headers"]["Authorization"], "Bearer private-access"
        )
        self.assertEqual(
            posted.kwargs["headers"]["Prefer"],
            "resolution=merge-duplicates,return=minimal",
        )
        self.assertNotIn("private sqlite", json.dumps(posted.kwargs["json"]))
        self.http.request.side_effect = [response({"id": OTHER_OWNER})]
        with self.assertRaises(backend.SupabaseError):
            self.client.upload_backup("wrong-owner-access", encrypted)
        self.assertEqual(self.http.request.call_args.args[0], "GET")

    def test_download_is_owner_filtered_and_validates_encrypted_response(self):
        encrypted = Fernet(Fernet.generate_key()).encrypt(b"private sqlite")
        self.http.request.side_effect = [
            response({"id": OWNER}),
            response([{"ciphertext": encrypted.decode()}]),
        ]
        self.assertEqual(self.client.download_backup("private-access"), encrypted)
        self.assertIn("owner_id=eq." + OWNER, self.http.request.call_args.args[1])
        self.http.request.side_effect = [
            response({"id": OWNER}),
            response([{"ciphertext": "plaintext secrets"}]),
        ]
        with self.assertRaises(backend.SupabaseError):
            self.client.download_backup("private-access")


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "app.sqlite"
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TABLE queries (id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute(
                "INSERT INTO queries VALUES (1, 'preserve search and photo layout')"
            )
        self.provider = Mock(config=backend.Config.from_environment(SETTINGS))
        self.now = 1000
        self.provider.sign_in.return_value = {
            "access_token": "private-access-token",
            "refresh_token": "private-refresh-token",
            "expires_at": 4600,
        }
        self.provider.verify_owner.return_value = OWNER
        self.app = Flask(__name__)
        self.app.config.update(
            SECRET_KEY="offline persistent dashboard secret" * 2, TESTING=True
        )
        self.integration = backend.install_dashboard(
            self.app,
            lambda: self.path,
            environ=SETTINGS,
            client=self.provider,
            clock=lambda: self.now,
        )

        @self.app.get("/")
        def dashboard():
            if not self.integration.is_authenticated():
                return redirect(url_for(self.integration.login_endpoint))
            return "existing alerts and photos"

        self.web = self.app.test_client()

    def login(self):
        self.web.get("/supabase/login")
        with self.web.session_transaction() as state:
            csrf = state["csrf"]
        result = self.web.post(
            "/supabase/login",
            data={
                "csrf": csrf,
                "email": "owner@example.test",
                "password": "private-password",
            },
        )
        self.assertEqual(result.location, "/")

    def test_login_encrypted_server_tokens_opaque_cookie_and_logout(self):
        self.assertEqual(self.web.get("/").location, "/supabase/login")
        self.login()
        with self.web.session_transaction() as state:
            self.assertTrue(state["owner"])
            self.assertNotIn("access_token", state)
            self.assertNotIn("refresh_token", state)
            self.assertEqual(len(state["supabase_session"]), 43)
        with closing(sqlite3.connect(self.path)) as conn:
            row = conn.execute(
                "SELECT token_hash,encrypted_tokens FROM supabase_dashboard_sessions"
            ).fetchone()
        self.assertNotIn(b"private-access-token", row[1])
        self.assertEqual(
            json.loads(self.integration.cipher.decrypt(row[1]))["access_token"],
            "private-access-token",
        )
        self.assertEqual(
            (self.path.parent / "supabase-session.key").stat().st_mode & 0o777, 0o600
        )
        self.assertEqual(self.web.get("/").data, b"existing alerts and photos")
        with self.app.test_request_context():
            session["supabase_session"] = "wrong-reference"
            self.assertFalse(self.integration.is_authenticated())
        with self.web.session_transaction() as state:
            reference = state["supabase_session"]
        with self.app.test_request_context():
            session["supabase_session"] = reference
            self.integration.logout()
        self.assertEqual(self.web.get("/").location, "/supabase/login")

    def test_csrf_owner_cookie_bypass_and_persistent_login_limit(self):
        with self.web.session_transaction() as state:
            state["owner"] = True
        self.assertEqual(self.web.get("/").location, "/supabase/login")
        self.web.get("/supabase/login")
        self.assertEqual(
            self.web.post(
                "/supabase/login",
                data={"email": "owner@example.test", "password": "private-password"},
            ).status_code,
            400,
        )
        self.provider.sign_in.side_effect = backend.SupabaseError(
            "Owner sign-in rejected."
        )
        with self.web.session_transaction() as state:
            csrf = state["csrf"]
        for _ in range(15):
            self.assertEqual(
                self.web.post(
                    "/supabase/login",
                    data={
                        "csrf": csrf,
                        "email": "owner@example.test",
                        "password": "wrong",
                    },
                ).status_code,
                200,
            )
        self.assertEqual(
            self.web.post(
                "/supabase/login",
                data={"csrf": csrf, "email": "owner@example.test", "password": "wrong"},
            ).status_code,
            429,
        )

    def test_expiry_refresh_owner_check_and_failed_refresh(self):
        self.login()
        self.now = 4580
        self.provider.refresh.return_value = {
            "access_token": "rotated-access",
            "refresh_token": "rotated-refresh",
            "expires_at": 8180,
        }
        self.assertEqual(self.web.get("/").status_code, 200)
        self.provider.refresh.assert_called_once_with("private-refresh-token", now=4580)
        self.provider.verify_owner.reset_mock()
        self.assertEqual(self.web.get("/").status_code, 200)
        self.provider.verify_owner.assert_called_once_with("rotated-access")
        self.provider.verify_owner.side_effect = backend.SupabaseError(
            "Owner sign-in rejected."
        )
        self.assertEqual(self.web.get("/").location, "/supabase/login")

    def test_concurrent_refresh_does_not_reuse_rotating_refresh_token(self):
        self.login()
        self.now = 4580
        with (self.path.parent / "supabase-refresh.lock").open("w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.web.get("/").location, "/supabase/login")
            self.provider.refresh.assert_not_called()
        self.provider.refresh.return_value = {
            "access_token": "rotated-access",
            "refresh_token": "rotated-refresh",
            "expires_at": 8180,
        }
        self.assertEqual(self.web.get("/").status_code, 200)
        self.provider.refresh.assert_called_once_with("private-refresh-token", now=4580)
        with self.web.session_transaction() as state:
            reference = state["supabase_session"]
        with self.app.test_request_context():
            session["supabase_session"] = reference
            digest = self.integration._session_hash()
            self.assertEqual(
                self.integration._refresh_tokens(digest)["access_token"],
                "rotated-access",
            )
        self.provider.refresh.assert_called_once()

    def test_database_snapshot_encrypted_consistent_and_live_state_preserved(self):
        self.login()
        encrypted = self.integration.create_backup()
        key_path = self.path.parent / "supabase-backup.key"
        self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)
        cipher = Fernet(key_path.read_bytes())
        payload = cipher.decrypt(encrypted)
        self.assertTrue(payload.startswith(backend.SNAPSHOT_MAGIC))
        self.assertNotIn(b"preserve search", encrypted)
        restored = self.path.parent / "review-copy.sqlite"
        restored.write_bytes(gzip.decompress(payload[len(backend.SNAPSHOT_MAGIC) :]))
        with closing(sqlite3.connect(restored)) as conn:
            self.assertEqual(
                conn.execute("SELECT name FROM queries").fetchone()[0],
                "preserve search and photo layout",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM supabase_dashboard_sessions"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM supabase_dashboard_sessions"
                ).fetchone()[0],
                1,
            )
        self.assertEqual(
            cipher.decrypt(self.integration.create_backup())[
                : len(backend.SNAPSHOT_MAGIC)
            ],
            backend.SNAPSHOT_MAGIC,
        )

    def test_backup_route_posts_no_plaintext_and_rejects_anonymous_download(self):
        self.assertEqual(
            self.web.get("/supabase/backup/download").location, "/supabase/login"
        )
        self.login()
        with self.web.session_transaction() as state:
            csrf = state["csrf"]
        result = self.web.post("/supabase/backup", data={"csrf": csrf})
        self.assertIn(b"Encrypted cloud backup saved", result.data)
        token, encrypted = self.provider.upload_backup.call_args.args
        self.assertEqual(token, "private-access-token")
        self.assertTrue(encrypted.startswith(b"gAAAA"))
        self.provider.download_backup.return_value = encrypted
        result = self.web.get("/supabase/backup/download")
        self.assertEqual(result.data, encrypted)
        self.assertIn("attachment", result.headers["Content-Disposition"])

    def test_snapshot_failure_is_safe_and_no_unencrypted_upload_occurs(self):
        self.login()
        with self.web.session_transaction() as state:
            csrf = state["csrf"]
        with patch.object(
            self.integration,
            "create_backup",
            side_effect=backend.SupabaseError(
                "The encrypted database backup could not be created."
            ),
        ):
            result = self.web.post("/supabase/backup", data={"csrf": csrf})
        self.assertIn(b"could not be created", result.data)
        self.provider.upload_backup.assert_not_called()


class HostedAppTests(DatabaseFixture, unittest.TestCase):
    """Exercise the real dashboard guard with the optional hosted login enabled."""

    def setUp(self):
        super().setUp()
        self.provider = Mock(config=backend.Config.from_environment(SETTINGS))
        self.provider.sign_in.return_value = {
            "access_token": "private-hosted-access",
            "refresh_token": "private-hosted-refresh",
            "expires_at": time.time() + 3600,
        }
        self.provider.verify_owner.return_value = OWNER
        with patch.dict("os.environ", SETTINGS), patch(
            "supabase_backend.Client", return_value=self.provider
        ):
            self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.web = self.app.test_client()

    def test_legacy_login_and_owner_cookie_cannot_bypass_hosted_auth(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE dashboard_auth SET password_hash=?",
                (generate_password_hash("offline existing local password"),),
            )
        with self.web.session_transaction() as state:
            state.update(owner=True, csrf="offline-csrf")
        for path in (
            "/",
            "/connections",
            "/search/1",
            "/reference/private-photo",
            "/supabase/backup",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.web.get(path).location, "/supabase/login")
        self.assertEqual(self.web.get("/login").location, "/supabase/login")
        self.assertEqual(self.web.get("/setup").location, "/supabase/login")
        self.assertEqual(self.web.get("/healthz").json, {"status": "ok"})

    def test_hosted_owner_enters_existing_dashboard_and_logout_revokes_reference(self):
        original = db.get_queries()
        self.web.get("/supabase/login")
        with self.web.session_transaction() as state:
            csrf = state["csrf"]
        result = self.web.post(
            "/supabase/login",
            data={
                "csrf": csrf,
                "email": "owner@example.test",
                "password": "private-owner-password",
            },
        )
        self.assertEqual(result.location, "/")
        self.assertEqual(self.web.get("/").status_code, 200)
        self.assertEqual(db.get_queries(), original)
        self.assertEqual(self.web.get("/connections").status_code, 200)
        self.assertEqual(self.web.post("/logout").status_code, 400)
        with self.web.session_transaction() as state:
            csrf = state["csrf"]
        self.assertEqual(
            self.web.post("/logout", data={"csrf": csrf}).location, "/login"
        )
        self.assertEqual(self.web.get("/").location, "/supabase/login")
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM supabase_dashboard_sessions"
                ).fetchone()[0],
                0,
            )

    def test_provider_outage_does_not_prevent_csrf_protected_local_logout(self):
        self.web.get("/supabase/login")
        with self.web.session_transaction() as state:
            csrf = state["csrf"]
        self.web.post(
            "/supabase/login",
            data={
                "csrf": csrf,
                "email": "owner@example.test",
                "password": "private-owner-password",
            },
        )
        self.provider.verify_owner.side_effect = backend.SupabaseError(
            "Supabase authentication is unavailable."
        )
        for path in (
            "/",
            "/connections",
            "/search/1",
            "/reference/private-photo",
            "/supabase/backup",
            "/supabase/backup/download",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.web.get(path).location, "/supabase/login")
        self.assertEqual(self.web.post("/logout").status_code, 400)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM supabase_dashboard_sessions"
                ).fetchone()[0],
                1,
            )
        with self.web.session_transaction() as state:
            csrf = state["csrf"]
        self.provider.verify_owner.reset_mock()
        self.assertEqual(
            self.web.post("/logout", data={"csrf": csrf}).location, "/login"
        )
        self.provider.verify_owner.assert_not_called()
        with self.web.session_transaction() as state:
            self.assertNotIn("owner", state)
            self.assertNotIn("supabase_session", state)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM supabase_dashboard_sessions"
                ).fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
