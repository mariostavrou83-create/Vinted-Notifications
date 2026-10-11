"""Private reconnect routes with real Flask protection and an offline browser."""

import copy
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_search_controls import DatabaseFixture
from werkzeug.security import generate_password_hash

import search_settings
import vinted_buyer
import vinted_reconnect_routes as reconnect
from web_ui_plugin.web_ui import create_app

PREFIX = "/vinted/reconnect"
PRIVATE = "offline-browser-secret-that-must-not-appear"
FRAME = b"\xff\xd8offline-frame\xff\xd9"
COOKIES = [
    {
        "name": name,
        "value": PRIVATE,
        "domain": ".vinted.co.uk",
        "path": "/",
        "secure": True,
    }
    for name in ("access_token_web", "refresh_token_web")
]


class OfflineBrowser:
    """Manager contract fake; no browser, credentials, HTTP or hidden retries."""

    def __init__(self):
        self.sessions = {}
        self.counter = 0
        for name in (
            "create",
            "state",
            "frame",
            "action",
            "extract_auth",
            "cancel",
            "runtime_check",
        ):
            setattr(self, name, Mock(side_effect=getattr(self, "_" + name)))

    def _record(self, owner, identifier):
        row = self.sessions[identifier]
        if row["owner"] != owner:
            raise ValueError(PRIVATE)
        return row

    def _safe(self, row):
        return {
            "id": row["id"],
            "state": "active",
            "width": 390,
            "height": 720,
            "expires_in": 500,
            "next_seq": row["next_seq"],
            "frame_seq": row["frame_seq"],
            "cookies": COOKIES,
            "csrf": PRIVATE,
            "password": PRIVATE,
            "url": "https://www.vinted.co.uk/member/signup?private=" + PRIVATE,
        }

    def _create(self, owner, revision):
        self.counter += 1
        identifier = "offline-browser-" + str(self.counter)
        row = {
            "id": identifier,
            "owner": owner,
            "revision": revision,
            "next_seq": 1,
            "frame_seq": 0,
        }
        self.sessions[identifier] = row
        return self._safe(row)

    def _state(self, owner, identifier):
        return self._safe(self._record(owner, identifier))

    def _frame(self, owner, identifier):
        row = self._record(owner, identifier)
        row["frame_seq"] += 1
        return FRAME, row["frame_seq"]

    def _action(self, owner, identifier, action):
        row = self._record(owner, identifier)
        if action["seq"] != row["next_seq"] or action["frame_seq"] != row["frame_seq"]:
            raise ValueError(PRIVATE)
        row["next_seq"] += 1
        return self._safe(row)

    def _extract_auth(self, owner, identifier):
        row = self._record(owner, identifier)
        return SimpleNamespace(
            cookie_records=copy.deepcopy(COOKIES),
            csrf=PRIVATE,
            revision=row["revision"],
        )

    def _cancel(self, owner, identifier):
        self._record(owner, identifier)
        self.sessions.pop(identifier)

    def _runtime_check(self):
        return {
            "available": True,
            "stage": "ready",
            "cookies": COOKIES,
            "message": PRIVATE,
        }


class VintedReconnectRoutesTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.manager = OfflineBrowser()
        self.routes = None

        def install(app):
            self.routes = reconnect.ReconnectRoutes(app, self.manager)
            return self.routes

        self.install = install
        self.block_network = patch(
            "requests.Session.request", side_effect=AssertionError("No live HTTP")
        )
        self.block_network.start()
        self.addCleanup(self.block_network.stop)
        with (
            patch("supabase_backend.install_dashboard", return_value=None),
            patch.object(reconnect, "install_reconnect", side_effect=install),
        ):
            self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.assertIsNotNone(self.routes, "create_app must mount the protected routes")
        self.client = self.app.test_client()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE dashboard_auth SET password_hash=?",
                (generate_password_hash("offline owner password"),),
            )
        self.snapshot = patch.object(
            vinted_buyer,
            "browser_session_snapshot",
            return_value={
                "user_id": "saved-buyer-99",
                "session_fingerprint": "offline-saved-revision",
            },
        )
        self.snapshot.start()
        self.addCleanup(self.snapshot.stop)
        self.importer = patch.object(
            vinted_buyer,
            "replace_browser_session",
            return_value={
                "outcome": "verified",
                "same_buyer_account": True,
                "autobuy_on": True,
                "cookies": COOKIES,
                "user_id": PRIVATE,
            },
        )
        self.replace = self.importer.start()
        self.addCleanup(self.importer.stop)

        def verified_import(*args, **kwargs):
            with kwargs["commit_guard"]():
                return self.replace.return_value

        self.replace.side_effect = verified_import

    def owner(self, client=None, *, csrf="offline-owner-csrf", hosted_ref="local"):
        client = client or self.client
        with client.session_transaction() as session:
            session["owner"] = True
            session["csrf"] = csrf
            if hosted_ref != "local":
                session["supabase_session"] = hosted_ref
        return client

    def post(self, suffix, client=None, **values):
        client = client or self.client
        with client.session_transaction() as session:
            csrf = session.get("csrf", "")
        return client.post(PREFIX + suffix, data={"csrf": csrf, **values})

    def start(self):
        self.owner()
        response = self.post("/start")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["state"], "active")
        with self.client.session_transaction() as session:
            return session["vinted_reconnect_id"]

    def preserved_state(self):
        with closing(search_settings.connection()) as conn:
            return {
                table: [tuple(row) for row in conn.execute("SELECT * FROM " + table)]
                for table in (
                    "vinted_buyer",
                    "vinted_buy_attempts",
                    "queries",
                    "search_preferences",
                    "items",
                    "alert_outbox",
                    "parameters",
                )
            }

    def assert_private_response(self, response):
        self.assertNotIn(PRIVATE.encode(), response.get_data())
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn(
            "frame-ancestors 'none'", response.headers["Content-Security-Policy"]
        )

    def test_every_route_requires_owner_authentication_and_posts_require_csrf(self):
        self.assertEqual(self.client.get(PREFIX).location, "/login")
        self.client.get("/login")
        for suffix in ("/state", "/frame"):
            response = self.client.get(PREFIX + suffix)
            self.assertEqual(response.status_code, 401, suffix)
            self.assert_private_response(response)
        for suffix in ("/start", "/input", "/finish", "/cancel", "/readiness"):
            response = self.post(suffix)
            self.assertEqual(response.status_code, 401, suffix)
        self.owner()
        for suffix in ("/start", "/input", "/finish", "/cancel", "/readiness"):
            self.assertEqual(self.client.post(PREFIX + suffix).status_code, 400, suffix)
        self.manager.create.assert_not_called()
        self.replace.assert_not_called()

    def test_page_state_and_frame_are_private_and_no_secret_enters_signed_cookie(self):
        identifier = self.start()
        before = self.preserved_state()
        page = self.client.get(PREFIX)
        self.assertEqual(page.status_code, 200)
        self.assert_private_response(page)
        response = self.client.get(PREFIX + "/state")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("id", response.json)
        self.assertNotIn("cookies", response.json)
        self.assert_private_response(response)
        frame = self.client.get(PREFIX + "/frame")
        self.assertEqual(frame.data, FRAME)
        self.assertEqual(frame.mimetype, "image/jpeg")
        self.assertEqual(frame.headers["X-Frame-Sequence"], "1")
        self.assert_private_response(frame)
        with self.client.session_transaction() as session:
            stored = dict(session)
        self.assertEqual(stored["vinted_reconnect_id"], identifier)
        self.assertNotIn(PRIVATE, json.dumps(stored))
        self.assertNotIn("offline-saved-revision", json.dumps(stored))
        self.assertNotIn("saved-buyer-99", json.dumps(stored))
        self.assertEqual(self.preserved_state(), before)
        self.manager.action.assert_not_called()
        self.manager.extract_auth.assert_not_called()
        self.replace.assert_not_called()

    def test_another_owner_cookie_jar_cannot_read_or_close_the_private_browser(self):
        identifier = self.start()
        original_owner = self.manager.create.call_args.args[0]
        for changed in ("csrf", "supabase_session"):
            with self.subTest(changed=changed):
                other = self.owner(
                    self.app.test_client(),
                    csrf=(
                        "different-owner-csrf"
                        if changed == "csrf"
                        else "offline-owner-csrf"
                    ),
                    hosted_ref=(
                        "different-hosted-session"
                        if changed == "supabase_session"
                        else "local"
                    ),
                )
                with other.session_transaction() as session:
                    session["vinted_reconnect_id"] = identifier
                response = other.get(PREFIX + "/frame")
                self.assertEqual(response.status_code, 409)
                self.assert_private_response(response)
                self.assertEqual(self.post("/cancel", client=other).status_code, 200)
                self.assertIn(identifier, self.manager.sessions)
                self.manager.frame.assert_not_called()
                self.manager.cancel.assert_not_called()
        self.assertEqual(self.routes.attempts[identifier].owner, original_owner)
        self.assertEqual(self.client.get(PREFIX + "/state").status_code, 200)
        self.replace.assert_not_called()

    def test_hosted_auth_expiry_blocks_every_browser_api_without_browser_reads(self):
        hosted = SimpleNamespace(
            is_authenticated=Mock(return_value=True),
            temporarily_unavailable=Mock(return_value=False),
            login_endpoint="login",
        )
        with (
            patch("supabase_backend.install_dashboard", return_value=hosted),
            patch.object(reconnect, "install_reconnect", side_effect=self.install),
        ):
            self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()
        identifier = self.start()
        hosted.is_authenticated.return_value = False
        for suffix in ("/state", "/frame"):
            response = self.client.get(PREFIX + suffix)
            self.assertEqual(response.status_code, 401)
            self.assert_private_response(response)
        for suffix in ("/start", "/input", "/finish", "/cancel", "/readiness"):
            response = self.post(suffix)
            self.assertEqual(response.status_code, 401)
            self.assert_private_response(response)
        self.manager.state.assert_not_called()
        self.manager.frame.assert_not_called()
        self.manager.action.assert_not_called()
        self.manager.extract_auth.assert_not_called()
        self.manager.cancel.assert_called_once()
        self.assertNotIn(identifier, self.manager.sessions)
        self.assertNotIn(identifier, self.routes.attempts)
        self.replace.assert_not_called()

    def test_oversized_request_rejected_before_action_or_form_processing(self):
        self.start()
        self.assertEqual(
            self.post(
                "/input", type="text", seq="1", frame_seq="0", text="x" * 9000
            ).status_code,
            413,
        )
        self.manager.action.assert_not_called()
        self.replace.assert_not_called()

    def test_chunked_request_body_limit_does_not_depend_on_content_length(self):
        self.start()
        response = self.client.post(
            PREFIX + "/input",
            data={
                "csrf": "offline-owner-csrf",
                "type": "text",
                "seq": "1",
                "frame_seq": "0",
                "text": "x" * 9000,
            },
            environ_overrides={"CONTENT_LENGTH": None, "wsgi.input_terminated": True},
        )
        self.assertEqual(response.status_code, 413)
        self.manager.action.assert_not_called()
        self.replace.assert_not_called()

    def test_frame_sequence_rejects_stale_action_and_only_accepts_current_frame(self):
        self.start()
        frame = self.client.get(PREFIX + "/frame")
        sequence = frame.headers["X-Frame-Sequence"]
        stale = self.post(
            "/input", type="click", seq="1", frame_seq="0", x="20", y="30"
        )
        self.assertEqual(stale.status_code, 409)
        self.assert_private_response(stale)
        accepted = self.post(
            "/input", type="click", seq="1", frame_seq=sequence, x="20", y="30"
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json["next_seq"], 2)
        self.assertEqual(accepted.json["frame_seq"], 1)
        self.assert_private_response(accepted)
        self.assertEqual(
            self.manager.action.call_args.args[2],
            {"type": "click", "seq": 1, "frame_seq": 1, "x": 20.0, "y": 30.0},
        )
        replay = self.post(
            "/input", type="click", seq="1", frame_seq=sequence, x="20", y="30"
        )
        self.assertEqual(replay.status_code, 409)
        self.replace.assert_not_called()

    def test_malformed_or_unsupported_input_never_reaches_manager(self):
        self.start()
        for values in (
            {
                "type": "goto",
                "seq": "1",
                "frame_seq": "0",
                "url": "https://example.test",
            },
            {
                "type": "click",
                "seq": "not-an-integer",
                "frame_seq": "0",
                "x": "1",
                "y": "1",
            },
            {"type": "text", "seq": "1", "frame_seq": "0", "text": "x" * 1025},
        ):
            with self.subTest(values=values):
                response = self.post("/input", **values)
                self.assertIn(response.status_code, (400, 409))
                self.assert_private_response(response)
        self.manager.action.assert_not_called()
        self.replace.assert_not_called()

    def test_finish_imports_exact_bound_account_once_and_keeps_all_buying_state(self):
        identifier = self.start()
        before = self.preserved_state()
        first = self.post("/finish")
        second = self.post("/finish")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["outcome"], "verified")
        self.assertTrue(first.json["same_buyer_account"])
        self.assertTrue(first.json["autobuy_on"])
        self.assertEqual(second.json, first.json)
        self.assert_private_response(first)
        self.replace.assert_called_once()
        args, kwargs = self.replace.call_args
        self.assertEqual(args, (COOKIES,))
        self.assertEqual(kwargs["expected_user_id"], "saved-buyer-99")
        self.assertEqual(
            kwargs["expected_session_fingerprint"], "offline-saved-revision"
        )
        self.assertEqual(kwargs["csrf"], PRIVATE)
        self.assertTrue(callable(kwargs["commit_guard"]))
        self.manager.extract_auth.assert_called_once()
        self.manager.cancel.assert_called_once()
        self.assertNotIn(identifier, self.manager.sessions)
        self.assertEqual(self.client.get(PREFIX + "/state").json, first.json)
        self.assertEqual(
            self.post(
                "/input", type="text", seq="1", frame_seq="0", text="anything"
            ).status_code,
            409,
        )
        self.assertEqual(self.client.get(PREFIX + "/frame").status_code, 409)
        self.assertEqual(self.preserved_state(), before)

    def test_readiness_checks_runtime_only_and_returns_whitelisted_fields(self):
        self.owner()
        before = self.preserved_state()
        result = self.post("/readiness")
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json["available"])
        self.assertEqual(result.json["stage"], "ready")
        self.assertNotIn("cookies", result.json)
        self.assert_private_response(result)
        self.manager.runtime_check.assert_called_once_with()
        self.manager.create.assert_not_called()
        self.manager.extract_auth.assert_not_called()
        self.replace.assert_not_called()
        self.assertEqual(self.preserved_state(), before)

    def test_readiness_runtime_exception_is_sanitized_without_browser_login(self):
        self.owner()
        self.manager.runtime_check.side_effect = RuntimeError(PRIVATE)
        with self.assertNoLogs(self.app.logger, level="WARNING"):
            result = self.post("/readiness")
        self.assertIn(result.status_code, (200, 503))
        self.assertFalse(result.json["available"])
        self.assert_private_response(result)
        self.manager.create.assert_not_called()
        self.replace.assert_not_called()

    def test_stale_connection_different_buyer_and_exception_are_unverified_without_retry(
        self,
    ):
        for outcome in ("stale", "different", "exception"):
            with self.subTest(outcome=outcome):
                self.start()
                self.replace.reset_mock()
                self.manager.extract_auth.reset_mock()
                self.manager.cancel.reset_mock()
                before = self.preserved_state()
                self.replace.side_effect = {
                    "stale": vinted_buyer.BuyerError(
                        PRIVATE, reason="saved_session", stage="saved_session"
                    ),
                    "exception": RuntimeError(PRIVATE),
                    "different": None,
                }[outcome]
                self.replace.return_value = {
                    "outcome": "verified",
                    "same_buyer_account": False,
                    "cookies": COOKIES,
                }
                result = self.post("/finish")
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json["outcome"], "unverified")
                self.assert_private_response(result)
                self.assertIn("No checkout or payment started", result.json["message"])
                self.assertEqual(self.post("/finish").json, result.json)
                self.assertEqual(self.client.get(PREFIX + "/state").json, result.json)
                self.replace.assert_called_once()
                self.manager.extract_auth.assert_called_once()
                self.manager.cancel.assert_called_once()
                self.assertEqual(self.preserved_state(), before)

    def test_concurrent_finish_reserves_once_and_status_remains_pollable(self):
        identifier = self.start()
        with self.client.session_transaction() as session:
            cookie_session = dict(session)
        second_client = self.app.test_client()
        with second_client.session_transaction() as session:
            session.update(cookie_session)
        entered, release = threading.Event(), threading.Event()

        def blocked_import(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3), "test did not release importer")
            with kwargs["commit_guard"]():
                return {
                    "outcome": "verified",
                    "same_buyer_account": True,
                    "autobuy_on": False,
                }

        self.replace.side_effect = blocked_import
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(self.post, "/finish")
            try:
                self.assertTrue(entered.wait(3), "finish did not reach importer")
                repeated = self.post("/finish", client=second_client)
                status = second_client.get(PREFIX + "/state")
                self.assertEqual(repeated.status_code, 202)
                self.assertEqual(status.status_code, 202)
                self.assertEqual(status.json["state"], "verifying")
                self.assertEqual(second_client.get(PREFIX + "/frame").status_code, 409)
                self.assertEqual(
                    self.post(
                        "/input",
                        client=second_client,
                        type="key",
                        seq="1",
                        frame_seq="0",
                        key="Enter",
                    ).status_code,
                    409,
                )
                self.manager.frame.assert_not_called()
                self.manager.action.assert_not_called()
            finally:
                release.set()
            first = pending.result(timeout=3)
        self.assertEqual(first.json["outcome"], "verified")
        self.assertEqual(self.post("/finish", client=second_client).json, first.json)
        self.replace.assert_called_once()
        self.manager.extract_auth.assert_called_once()
        self.manager.cancel.assert_called_once()
        self.assertNotIn(identifier, self.manager.sessions)

    def test_cancel_waits_for_submitted_verification_before_returning(self):
        identifier = self.start()
        before = self.preserved_state()
        with self.client.session_transaction() as session:
            cookie_session = dict(session)
        second_client = self.app.test_client()
        with second_client.session_transaction() as session:
            session.update(cookie_session)
        entered, release, cancellation_started = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def blocked_import(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3), "test did not release importer")
            with kwargs["commit_guard"]():
                return {
                    "outcome": "verified",
                    "same_buyer_account": True,
                    "autobuy_on": False,
                }

        def cancel():
            cancellation_started.set()
            return self.post("/cancel", client=second_client)

        self.replace.side_effect = blocked_import
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = executor.submit(self.post, "/finish")
            cancelled = None
            try:
                self.assertTrue(entered.wait(3), "finish did not reach importer")
                cancelled = executor.submit(cancel)
                self.assertTrue(cancellation_started.wait(3), "cancel did not start")
                with self.assertRaises(TimeoutError):
                    cancelled.result(timeout=0.05)
            finally:
                release.set()
            first = pending.result(timeout=3)
            result = cancelled.result(timeout=3)
        self.assertEqual(first.json["outcome"], "unverified")
        self.assertEqual(result.json["state"], "closed")
        self.assertNotIn(identifier, self.manager.sessions)
        self.assertNotIn(identifier, self.routes.attempts)
        self.replace.assert_called_once()
        self.manager.extract_auth.assert_called_once()
        self.assertEqual(self.preserved_state(), before)

    def test_extraction_failure_is_consumed_once_and_closes_without_import(self):
        identifier = self.start()
        before = self.preserved_state()
        self.manager.extract_auth.side_effect = RuntimeError(PRIVATE)
        with self.assertNoLogs(self.app.logger, level="WARNING"):
            result = self.post("/finish")
        self.assertEqual(result.json["outcome"], "unverified")
        self.assert_private_response(result)
        self.assertEqual(self.post("/finish").json, result.json)
        self.assertEqual(self.client.get(PREFIX + "/state").json, result.json)
        self.manager.extract_auth.assert_called_once()
        self.manager.cancel.assert_called_once()
        self.replace.assert_not_called()
        self.assertNotIn(identifier, self.manager.sessions)
        self.assertEqual(self.preserved_state(), before)

    def test_extracted_revision_mismatch_is_consumed_without_import_or_retry(self):
        identifier = self.start()
        before = self.preserved_state()
        self.manager.extract_auth.side_effect = None
        self.manager.extract_auth.return_value = SimpleNamespace(
            cookie_records=copy.deepcopy(COOKIES),
            csrf=PRIVATE,
            revision="different-offline-revision",
        )
        result = self.post("/finish")
        self.assertEqual(result.json["outcome"], "unverified")
        self.assert_private_response(result)
        self.assertEqual(self.post("/finish").json, result.json)
        self.assertEqual(self.client.get(PREFIX + "/state").json, result.json)
        self.manager.extract_auth.assert_called_once()
        self.manager.cancel.assert_called_once()
        self.replace.assert_not_called()
        self.assertNotIn(identifier, self.manager.sessions)
        self.assertEqual(self.preserved_state(), before)

    def test_attempt_expiring_during_verification_cannot_commit_a_replacement(self):
        identifier = self.start()
        before = self.preserved_state()
        entered, release = threading.Event(), threading.Event()

        def blocked_import(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3), "test did not release importer")
            with kwargs["commit_guard"]():
                return {
                    "outcome": "verified",
                    "same_buyer_account": True,
                    "autobuy_on": False,
                }

        self.replace.side_effect = blocked_import
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(self.post, "/finish")
            try:
                self.assertTrue(entered.wait(3), "finish did not reach importer")
                self.routes.attempts[identifier].created -= 601
            finally:
                release.set()
            result = pending.result(timeout=3)
        self.assertEqual(result.json["outcome"], "unverified")
        self.assert_private_response(result)
        self.replace.assert_called_once()
        self.manager.extract_auth.assert_called_once()
        self.manager.cancel.assert_called_once()
        self.assertNotIn(identifier, self.manager.sessions)
        self.assertEqual(self.client.get(PREFIX + "/state").status_code, 409)
        self.assertNotIn(identifier, self.routes.attempts)
        self.assertEqual(self.preserved_state(), before)

    def test_cancellation_after_successful_guard_does_not_claim_rollback(self):
        identifier = self.start()
        result = self.post("/finish")
        self.assertEqual(result.json["outcome"], "verified")
        self.assertNotIn(identifier, self.manager.sessions)
        closed = self.post("/cancel")
        self.assertEqual(closed.json["state"], "closed")
        self.assertNotIn("previous", closed.json["message"].lower())
        self.assertNotIn("rollback", closed.json["message"].lower())
        self.replace.assert_called_once()
        self.manager.cancel.assert_called_once()
        self.assertNotIn(identifier, self.routes.attempts)

    def test_existing_disconnect_action_closes_browser_before_disconnect(self):
        identifier = self.start()
        before = self.preserved_state()

        def disconnect():
            self.assertNotIn(identifier, self.manager.sessions)
            self.assertNotIn(identifier, self.routes.attempts)

        with patch.object(vinted_buyer, "disconnect", side_effect=disconnect) as call:
            result = self.client.post(
                "/connections",
                data={"csrf": "offline-owner-csrf", "action": "buyer_disconnect"},
            )
        self.assertEqual(result.status_code, 302)
        call.assert_called_once()
        self.manager.cancel.assert_called_once()
        self.replace.assert_not_called()
        self.assertEqual(self.preserved_state(), before)

    def test_cancel_logout_and_expired_attempt_close_only_this_browser(self):
        for operation in ("cancel", "logout", "expire"):
            with self.subTest(operation=operation):
                identifier = self.start()
                self.manager.cancel.reset_mock()
                before = self.preserved_state()
                if operation == "cancel":
                    result = self.post("/cancel")
                    self.assertEqual(result.json["state"], "closed")
                    self.assertEqual(self.post("/cancel").status_code, 200)
                elif operation == "logout":
                    self.assertEqual(
                        self.client.post(
                            "/logout", data={"csrf": "offline-owner-csrf"}
                        ).status_code,
                        302,
                    )
                    self.assertEqual(
                        self.client.get(PREFIX + "/state").status_code, 401
                    )
                else:
                    self.routes.attempts[identifier].created -= 601
                    self.assertEqual(
                        self.client.get(PREFIX + "/state").status_code, 409
                    )
                self.manager.cancel.assert_called_once()
                self.assertNotIn(identifier, self.manager.sessions)
                self.assertNotIn(identifier, self.routes.attempts)
                self.assertEqual(self.preserved_state(), before)
        self.replace.assert_not_called()

    def test_manager_errors_have_fixed_public_messages_and_do_not_log_secrets(self):
        self.owner()
        self.manager.create.side_effect = RuntimeError(PRIVATE)
        with self.assertNoLogs(self.app.logger, level="WARNING"):
            result = self.post("/start")
        self.assertEqual(result.status_code, 503)
        self.assert_private_response(result)
        self.manager.create.side_effect = self.manager._create
        self.start()
        for suffix, method in (("/state", "state"), ("/frame", "frame")):
            getattr(self.manager, method).side_effect = RuntimeError(PRIVATE)
            with self.assertNoLogs(self.app.logger, level="WARNING"):
                result = self.client.get(PREFIX + suffix)
            self.assertEqual(result.status_code, 409)
            self.assert_private_response(result)
        self.replace.assert_not_called()


if __name__ == "__main__":
    unittest.main()
