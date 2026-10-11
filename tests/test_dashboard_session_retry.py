"""Real hosted sessions recover behind the dashboard's transient-failure guard."""

import fcntl
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from test_search_controls import DatabaseFixture
from test_supabase_backend import OTHER_OWNER, OWNER, SETTINGS, response

import db
import search_settings
import supabase_backend as backend
import vinted_buyer
import vinted_reconnect_routes as reconnect
from web_ui_plugin.web_ui import create_app

ACCESS = "offline-dashboard-private-access"
REFRESH = "offline-dashboard-private-refresh"
ROTATED_ACCESS = "offline-dashboard-rotated-access"
ROTATED_REFRESH = "offline-dashboard-rotated-refresh"
PRIVATE = "offline-provider-private-message-do-not-expose"
FIND = "Offline private search notes: resell at GBP 99"
PREFIX = "/vinted/reconnect"


class DashboardSessionRetryTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.now = 1000
        self.http = Mock()
        self.provider = backend.Client(
            backend.Config.from_environment(SETTINGS), self.http
        )
        self.manager = SimpleNamespace(
            create=Mock(
                return_value={"id": "offline-private-browser", "state": "active"}
            ),
            state=Mock(return_value={"state": "active"}),
            frame=Mock(return_value=(b"offline-private-frame", 1)),
            action=Mock(return_value={"state": "active"}),
            extract_auth=Mock(),
            cancel=Mock(),
            runtime_check=Mock(return_value={"available": True, "stage": "ready"}),
        )

        def install_auth(app, database_path):
            self.integration = backend.DashboardIntegration(
                app, database_path, self.provider, clock=lambda: self.now
            )
            return self.integration

        def install_reconnect(app):
            self.routes = reconnect.ReconnectRoutes(app, self.manager)
            return self.routes

        with (
            patch.object(backend, "install_dashboard", side_effect=install_auth),
            patch.object(reconnect, "install_reconnect", side_effect=install_reconnect),
        ):
            self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.web = self.app.test_client()
        self.block_network = patch(
            "requests.Session.request", side_effect=AssertionError("No live HTTP")
        )
        self.block_network.start()
        self.addCleanup(self.block_network.stop)
        search_settings.update_search(1, "query_name", FIND)
        search_settings.update_search(1, "reminder", FIND)
        self.http.request.side_effect = [
            response(self.token_pair(ACCESS, REFRESH)),
            response({"id": OWNER}),
        ]
        self.web.get("/supabase/login")
        csrf = self.session()["csrf"]
        result = self.web.post(
            "/supabase/login",
            data={
                "csrf": csrf,
                "email": "offline-owner@example.test",
                "password": PRIVATE,
            },
        )
        self.assertEqual(result.location, "/")
        self.http.request.reset_mock()
        self.http.request.side_effect = None
        self.http.request.return_value = response({"id": OWNER})

    def token_pair(self, access, refresh):
        return {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": 3600,
            "user": {"id": OWNER},
        }

    def session(self):
        with self.web.session_transaction() as value:
            return dict(value)

    def post(self, path, **values):
        return self.web.post(path, data={"csrf": self.session()["csrf"], **values})

    def stored_session(self):
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            row = conn.execute(
                "SELECT token_hash,owner_id,encrypted_tokens,expires_at FROM supabase_dashboard_sessions"
            ).fetchone()
        return row, json.loads(self.integration.cipher.decrypt(row[2]))

    def dump(self):
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            return tuple(conn.iterdump())

    def business_state(self):
        with closing(search_settings.connection()) as conn:
            return {
                table: [tuple(row) for row in conn.execute("SELECT * FROM " + table)]
                for table in (
                    "queries",
                    "search_preferences",
                    "items",
                    "alert_outbox",
                    "vinted_buyer",
                    "vinted_buy_attempts",
                    "parameters",
                )
            }

    def assert_retry(self, result, *, json_response=False):
        self.assertEqual(result.status_code, 503)
        self.assertIsNone(result.location)
        self.assertEqual(result.headers["Retry-After"], "3")
        self.assertIn("no-store", result.headers["Cache-Control"])
        self.assertEqual(result.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(result.headers["Referrer-Policy"], "no-referrer")
        for private in (
            ACCESS,
            REFRESH,
            ROTATED_ACCESS,
            ROTATED_REFRESH,
            PRIVATE,
            FIND,
            "offline-private-frame",
        ):
            self.assertNotIn(private.encode(), result.data)
        if json_response:
            self.assertEqual(result.json["state"], "waiting")
        else:
            self.assertIn(b"Connection check paused", result.data)
            self.assertIn(b"without entering your password again", result.data)

    def assert_session_retained(self, before):
        after = self.session()
        self.assertEqual(after["supabase_session"], before["supabase_session"])
        self.assertEqual(after["csrf"], before["csrf"])
        self.assertTrue(after["owner"])
        for private in (ACCESS, REFRESH, ROTATED_ACCESS, ROTATED_REFRESH, PRIVATE):
            self.assertNotIn(private, json.dumps(after))

    def start_browser(self):
        with patch.object(
            vinted_buyer,
            "browser_session_snapshot",
            return_value={
                "user_id": "99",
                "session_fingerprint": "offline-buyer-revision",
            },
        ):
            result = self.post(PREFIX + "/start")
        self.assertEqual(result.status_code, 200)
        self.http.request.reset_mock()
        return self.session()["vinted_reconnect_id"]

    def test_owner_timeout_serves_retry_page_retains_encrypted_session_and_recovers(
        self,
    ):
        before_session, before_dump = self.session(), self.dump()
        sealed, tokens = self.stored_session()
        self.assertEqual(tokens["access_token"], ACCESS)
        self.assertNotIn(ACCESS.encode(), sealed[2])
        self.assertEqual(
            (Path(db.DB_PATH).parent / "supabase-session.key").stat().st_mode & 0o777,
            0o600,
        )
        self.http.request.side_effect = requests.ReadTimeout(PRIVATE)
        with self.assertLogs(self.app.logger, level="WARNING") as captured:
            result = self.web.get("/search/1")
        self.assert_retry(result)
        self.assertIn(b'href="/search/1"', result.data)
        self.assertNotIn(PRIVATE, " ".join(captured.output))
        self.assertIn("reason=timeout", " ".join(captured.output))
        self.assertEqual(self.dump(), before_dump)
        self.assert_session_retained(before_session)
        self.http.request.assert_called_once()
        self.http.request.side_effect = None
        self.http.request.return_value = response({"id": OWNER})
        self.http.request.reset_mock()
        recovered = self.web.get("/search/1")
        self.assertEqual(recovered.status_code, 200)
        self.assertIn(FIND.encode(), recovered.data)
        self.assert_session_retained(before_session)
        self.http.request.assert_called_once()
        self.assertEqual(self.http.request.call_args.args[0], "GET")
        self.assertEqual(
            self.http.request.call_args.kwargs["headers"]["Authorization"],
            "Bearer " + ACCESS,
        )

    def test_retryable_provider_statuses_never_render_private_views_or_mutate_state(
        self,
    ):
        before_session, before_dump = self.session(), self.dump()
        for status in (429, 502, 503):
            with self.subTest(status=status):
                self.http.request.return_value = response(
                    {"error": PRIVATE}, status=status
                )
                for path in (
                    "/",
                    "/connections",
                    "/reference/private-example",
                    "/supabase/backup/download",
                ):
                    result = self.web.get(path)
                    self.assert_retry(result)
                    self.assertEqual(self.dump(), before_dump)
                    self.assert_session_retained(before_session)

    def test_transient_post_is_not_applied_or_replayed_when_retry_authenticates(self):
        before_session, before_dump = self.session(), self.dump()
        self.http.request.side_effect = requests.ConnectionError(PRIVATE)
        paused = self.post("/search/1/pause", revision="2")
        self.assert_retry(paused)
        self.assertIn(b'href="/"', paused.data)
        self.assertEqual(self.dump(), before_dump)
        self.assert_session_retained(before_session)
        self.http.request.side_effect = None
        recovered = self.web.get("/")
        self.assertEqual(recovered.status_code, 200)
        self.assertFalse(search_settings.get_search(1)["paused"])
        self.assertEqual(self.dump(), before_dump)

    def test_unmatched_external_looking_paths_use_only_the_local_dashboard_retry(self):
        before_session, before_dump = self.session(), self.dump()
        self.http.request.side_effect = requests.ReadTimeout(PRIVATE)
        for path in ("//evil.example/private", "/\\evil.example/private"):
            with self.subTest(path=path):
                result = self.web.get("/", environ_overrides={"PATH_INFO": path})
                self.assert_retry(result)
                self.assertIn(b'href="/"', result.data)
                self.assertNotIn(b"evil.example", result.data)
                self.assertEqual(self.dump(), before_dump)
                self.assert_session_retained(before_session)

    def test_refresh_lock_waits_without_exchanging_then_rotates_once_after_unlock(self):
        before_session = self.session()
        self.now = 4580
        before_dump = self.dump()
        lock_path = Path(db.DB_PATH).parent / "supabase-refresh.lock"
        with lock_path.open("w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            waiting = self.web.get("/")
            self.assert_retry(waiting)
            self.assertEqual(self.dump(), before_dump)
            self.assert_session_retained(before_session)
            self.http.request.assert_not_called()
        before_business = self.business_state()
        self.http.request.side_effect = [
            response(self.token_pair(ROTATED_ACCESS, ROTATED_REFRESH)),
            response({"id": OWNER}),
        ]
        recovered = self.web.get("/")
        self.assertEqual(recovered.status_code, 200)
        self.assert_session_retained(before_session)
        self.assertEqual(self.business_state(), before_business)
        refresh, verify = self.http.request.call_args_list
        self.assertEqual(refresh.args[0], "POST")
        self.assertTrue(
            refresh.args[1].endswith("/auth/v1/token?grant_type=refresh_token")
        )
        self.assertEqual(refresh.kwargs["json"], {"refresh_token": REFRESH})
        self.assertEqual(
            verify.kwargs["headers"]["Authorization"], "Bearer " + ROTATED_ACCESS
        )
        row, tokens = self.stored_session()
        self.assertNotIn(ROTATED_ACCESS.encode(), row[2])
        self.assertEqual(tokens["access_token"], ROTATED_ACCESS)
        self.assertEqual(tokens["refresh_token"], ROTATED_REFRESH)
        self.assertNotIn("verification_pending", tokens)
        self.http.request.reset_mock()
        self.http.request.side_effect = None
        self.now += 1
        self.assertEqual(self.web.get("/").status_code, 200)
        self.http.request.assert_called_once()
        self.assertEqual(self.http.request.call_args.args[0], "GET")

    def test_two_interrupted_refresh_exchanges_require_sign_in_without_another_post(
        self,
    ):
        self.now = 4580
        row, tokens = self.stored_session()
        tokens["refresh_exchange_attempts"] = 2
        sealed = self.integration.cipher.encrypt(json.dumps(tokens).encode())
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute(
                "UPDATE supabase_dashboard_sessions SET encrypted_tokens=? WHERE token_hash=?",
                (sealed, row[0]),
            )
        before_session, before_dump = self.session(), self.dump()
        with self.web as web:
            result = web.get("/search/1")
            failure = self.integration.auth_failure()
            self.assertEqual(failure["reason"], "refresh_uncertain")
            self.assertEqual(failure["stage"], "refresh_exchange")
            self.assertIs(failure["requires_sign_in"], True)
        self.assertEqual(result.status_code, 503)
        self.assertIsNone(result.location)
        self.assertEqual(result.headers["Retry-After"], "3")
        self.assertIn("no-store", result.headers["Cache-Control"])
        self.assertIn(b"after two interrupted exchanges", result.data)
        self.assertIn(b'href="/supabase/login"', result.data)
        self.assertIn(b"Sign in again", result.data)
        self.assertNotIn(b"Try again", result.data)
        self.assertNotIn(b"without entering your password", result.data)
        for private in (ACCESS, REFRESH, PRIVATE, FIND):
            self.assertNotIn(private.encode(), result.data)
        self.assert_session_retained(before_session)
        self.assertEqual(self.dump(), before_dump)
        self.assertEqual(self.stored_session()[0][2], sealed)
        self.assertEqual(self.web.get("/").status_code, 503)
        self.http.request.assert_not_called()
        self.assert_session_retained(before_session)
        self.assertEqual(self.dump(), before_dump)

    def test_accepted_rotation_survives_owner_timeout_and_retry_does_not_reuse_refresh(
        self,
    ):
        before_session, before_business = self.session(), self.business_state()
        self.now = 4580
        self.http.request.side_effect = [
            response(self.token_pair(ROTATED_ACCESS, ROTATED_REFRESH)),
            requests.ReadTimeout(PRIVATE),
        ]
        result = self.web.get("/")
        self.assert_retry(result)
        self.assert_session_retained(before_session)
        self.assertEqual(self.business_state(), before_business)
        row, tokens = self.stored_session()
        self.assertNotIn(ROTATED_REFRESH.encode(), row[2])
        self.assertEqual(tokens["refresh_token"], ROTATED_REFRESH)
        self.assertTrue(tokens["verification_pending"])
        self.http.request.reset_mock()
        self.http.request.side_effect = None
        self.now += 1
        recovered = self.web.get("/")
        self.assertEqual(recovered.status_code, 200)
        self.http.request.assert_called_once()
        self.assertEqual(self.http.request.call_args.args[0], "GET")
        self.assertEqual(
            self.http.request.call_args.kwargs["headers"]["Authorization"],
            "Bearer " + ROTATED_ACCESS,
        )
        self.assertNotIn("verification_pending", self.stored_session()[1])
        self.assert_session_retained(before_session)
        self.assertEqual(self.business_state(), before_business)

    def test_reconnect_apis_wait_without_frames_or_cancel_and_resume_same_browser(self):
        identifier = self.start_browser()
        before_session, before_dump = self.session(), self.dump()
        self.http.request.side_effect = requests.ReadTimeout(PRIVATE)
        for suffix in ("/state", "/frame"):
            self.assert_retry(self.web.get(PREFIX + suffix), json_response=True)
        for suffix in ("/input", "/finish", "/cancel", "/start", "/readiness"):
            self.assert_retry(self.post(PREFIX + suffix), json_response=True)
        self.manager.frame.assert_not_called()
        self.manager.state.assert_not_called()
        self.manager.action.assert_not_called()
        self.manager.extract_auth.assert_not_called()
        self.manager.runtime_check.assert_not_called()
        self.manager.cancel.assert_not_called()
        self.manager.create.assert_called_once()
        self.assertIn(identifier, self.routes.attempts)
        self.assertEqual(self.dump(), before_dump)
        self.assert_session_retained(before_session)
        self.http.request.side_effect = None
        self.assertEqual(self.web.get(PREFIX + "/state").json["state"], "active")
        frame = self.web.get(PREFIX + "/frame")
        self.assertEqual(frame.data, b"offline-private-frame")
        self.manager.frame.assert_called_once()
        self.manager.cancel.assert_not_called()
        self.assertEqual(self.session()["vinted_reconnect_id"], identifier)

    def test_terminal_owner_failure_still_requires_login_and_closes_private_browser(
        self,
    ):
        for body, status in (({"id": OTHER_OWNER}, 200), ({"error": PRIVATE}, 401)):
            with self.subTest(status=status):
                self.http.request.return_value = response({"id": OWNER})
                self.http.request.side_effect = None
                self.start_browser()
                self.manager.cancel.reset_mock()
                before_dump = self.dump()
                self.http.request.return_value = response(body, status=status)
                result = self.web.get("/")
                self.assertEqual(result.location, "/supabase/login")
                self.assertNotIn(FIND.encode(), result.data)
                blocked = self.web.get(PREFIX + "/frame")
                self.assertEqual(blocked.status_code, 401)
                self.assertEqual(blocked.json["state"], "stopped")
                self.assertNotIn(PRIVATE.encode(), blocked.data)
                self.manager.frame.assert_not_called()
                self.manager.cancel.assert_called_once()
                self.assertEqual(self.dump(), before_dump)


if __name__ == "__main__":
    unittest.main()
