"""Same-account browser reconnection behind a mocked HTTP adapter; no live auth."""

import copy
import hashlib
import json
import sqlite3
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import test_vinted_buying as buying_tests

import db
import search_settings
import vinted_buyer as buyer


class BrowserImportTests(buying_tests.SessionRotationFixture, unittest.TestCase):
    transport_response = buying_tests.NativeCookieTransportTests.transport_response
    token_headers = buying_tests.NativeCookieTransportTests.token_headers
    browser_csrf = "browser-csrf-token-0123456789"
    now = 1791710000

    def setUp(self):
        native = patch.object(buyer, "BrowserSession", requests.Session)
        native.start()
        self.addCleanup(native.stop)
        clock = patch.object(buyer.time, "time", return_value=self.now)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        super().setUp()
        self.network = {
            "proxy": "http://offline-user:offline-secret@proxy.example.test:8080",
            "api_key": "offline-capsolver-key",
            "enabled": True,
        }
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET network=?,verified_at=?,max_total=2000,max_extra=350,"
                "pickup_mode='nearest',preferred_card_last4='1234',browser_info=?,pending=? WHERE id=1",
                (
                    buyer.encrypt(self.network),
                    self.now - 200,
                    json.dumps(buying_tests.DEVICE),
                    buyer.encrypt(
                        {"expires": self.now + 300, "challenge": "old-pending-code"}
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO vinted_buy_attempts(item_id,state,checkout_id,total,message,updated) "
                "VALUES ('123','unknown','existing-checkout',1884,'Reconcile first',?)",
                (self.now - 10,),
            )
        self.records = [
            {
                "name": name,
                "value": value,
                "domain": ".vinted.co.uk",
                "path": "/",
                "secure": True,
                "expires": self.now + 3600,
                "domain_specified": True,
                "domain_initial_dot": True,
                "path_specified": True,
                "discard": False,
            }
            for name, value in (
                ("access_token_web", "browser-access-token-0123456789"),
                ("refresh_token_web", "browser-refresh-token-0123456789"),
            )
        ]
        self.calls, self.clients = [], []
        self.real_client = buyer.Client
        self.identity_status = 200
        self.identity_user = 99
        self.rotate = False
        self.before_identity = None
        self.homepage_status = 200
        self.challenge = False
        self.snapshot = buyer.browser_session_snapshot()

    def client(self, *args, **kwargs):
        client = self.real_client(*args, **kwargs)
        client.session.close = Mock(wraps=client.session.close)
        self.clients.append(client)
        return client

    def native(self, adapter, prepared, **kwargs):
        path = prepared.url.removeprefix(buyer.BASE)
        self.calls.append((prepared.method, path))
        self.assertEqual(kwargs["proxies"]["https"], self.network["proxy"])
        self.assertEqual(prepared.method, "GET")
        self.assertIsNone(prepared.body)
        if path == "/":
            return self.transport_response(
                prepared,
                status=self.homepage_status,
                text='<meta name="csrf-token" content="homepage-csrf-token-0123456789">',
            )
        self.assertEqual(path, "/api/v2/users/current")
        if self.before_identity:
            self.before_identity()
        return self.transport_response(
            prepared,
            (
                {"url": "https://geo.captcha-delivery.com/captcha/"}
                if self.challenge
                else (
                    {"user": {"id": self.identity_user, "login": "owner"}}
                    if self.identity_status in (200, 201)
                    else {"error": "invalid_token"}
                )
            ),
            status=self.identity_status,
            cookies=(
                self.token_headers(self.new_access, self.new_refresh)
                if self.rotate
                else ()
            ),
        )

    def replace(
        self, *, records=None, csrf=browser_csrf, snapshot=None, commit_guard=None
    ):
        with patch.object(buyer, "Client", side_effect=self.client), patch.object(
            requests.adapters.HTTPAdapter,
            "send",
            autospec=True,
            side_effect=self.native,
        ):
            return buyer.replace_browser_session(
                tuple(self.records) if records is None else records,
                expected_user_id=(snapshot or self.snapshot)["user_id"],
                expected_session_fingerprint=(snapshot or self.snapshot)[
                    "session_fingerprint"
                ],
                csrf=csrf,
                commit_guard=commit_guard,
            )

    def controls(self):
        with closing(search_settings.connection()) as conn:
            return (
                tuple(
                    conn.execute(
                        "SELECT user_id,enabled,max_total,max_extra,pickup_mode,preferred_card_last4,"
                        "browser_info,network FROM vinted_buyer WHERE id=1"
                    ).fetchone()
                ),
                [
                    tuple(row)
                    for row in conn.execute("SELECT * FROM vinted_search_budgets")
                ],
                [
                    tuple(row)
                    for row in conn.execute("SELECT * FROM vinted_buy_attempts")
                ],
                [tuple(row) for row in conn.execute("SELECT * FROM queries")],
            )

    def reset_attempt(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET login_attempt=0 WHERE id=1")

    def test_verified_import_preserves_on_and_off_permission_and_all_purchase_controls(
        self,
    ):
        for enabled in (0, 1):
            with self.subTest(enabled=enabled):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute(
                        "UPDATE vinted_buyer SET enabled=?,login_attempt=0 WHERE id=1",
                        (enabled,),
                    )
                snapshot = buyer.browser_session_snapshot()
                before = self.controls()
                old = self.saved()[0]["session"]
                result = self.replace(snapshot=snapshot)
                row, saved = self.saved()
                self.assertNotEqual(row["session"], old)
                self.assertEqual(row["verified_at"], self.now)
                self.assertEqual(row["user_id"], "99")
                self.assertIsNone(row["pending"])
                self.assertEqual(saved["cookie_records"], self.records)
                self.assertEqual(saved["csrf"], self.browser_csrf)
                self.assertEqual(self.controls(), before)
                self.assertEqual(result["autobuy_on"], bool(enabled))
                self.assertTrue(result["account_verified"])
                self.assertTrue(result["same_buyer_account"])
                self.assertFalse(result["normal_renewal_verified"])
                self.assertFalse(result["checkout_created"])
                self.assertFalse(result["payment_submitted"])
                self.clients[-1].session.close.assert_called_once()
                self.assertEqual(
                    buyer.settings()["connection_health"]["state"], "verified"
                )
                self.assertFalse(buyer.settings()["password_saved"])
        self.assertEqual(self.calls, [("GET", "/api/v2/users/current")] * 2)
        public = json.dumps([result, buyer.settings(), buyer.public_auth_results()])
        for secret in (
            self.records[0]["value"],
            self.records[1]["value"],
            self.browser_csrf,
            "offline-secret",
            self.network["api_key"],
        ):
            self.assertNotIn(secret, public)
            self.assertNotIn(secret.encode(), Path(db.DB_PATH).read_bytes())

    def test_missing_browser_csrf_uses_homepage_then_identity_without_renewal(self):
        self.replace(csrf=None)
        self.assertEqual(self.calls, [("GET", "/"), ("GET", "/api/v2/users/current")])
        self.assertEqual(self.saved()[1]["csrf"], "homepage-csrf-token-0123456789")

    def test_identity_accepted_rotation_is_encrypted_and_replaces_imported_tokens(self):
        self.rotate = True
        self.replace()
        saved = self.saved()[1]
        self.assertEqual(saved["cookies"]["access_token_web"], self.new_access)
        self.assertEqual(saved["cookies"]["refresh_token_web"], self.new_refresh)
        self.assertNotIn(self.new_refresh.encode(), Path(db.DB_PATH).read_bytes())
        self.assertEqual(self.calls, [("GET", "/api/v2/users/current")])

    def test_identity_diagnostics_report_actual_accepted_http_status(self):
        self.identity_status = 201
        self.replace()
        self.assertEqual(
            buyer.public_auth_results()["buyer_session"]["http_status"], 201
        )
        self.assertEqual(buyer.settings()["connection_health"]["http_status"], 201)

    def test_cancel_commit_guard_preserves_session_after_identity_has_completed(self):
        old = self.saved()[0]
        entered = []

        @contextmanager
        def cancelled():
            entered.append(bool(self.calls))
            if self.calls:
                self.assertEqual(self.calls, [("GET", "/api/v2/users/current")])
                raise buyer.BuyerError(
                    "The secure browser connection was cancelled.",
                    reason="saved_session",
                    stage="saved_session",
                )
            yield

        with self.assertRaises(buyer.BuyerError):
            self.replace(commit_guard=cancelled)
        self.assertEqual(entered, [False, False, True])
        row = self.saved()[0]
        for field in ("session", "pending", "enabled", "verified_at"):
            self.assertEqual(row[field], old[field])
        self.clients[0].session.close.assert_called_once()

    def test_commit_guard_holds_through_durable_cas_save(self):
        old = self.saved()[0]["session"]
        events = []

        @contextmanager
        def guard():
            self.assertEqual(self.saved()[0]["session"], old)
            events.append("entered")
            yield
            events.append(
                "committed" if self.saved()[0]["session"] != old else "checked"
            )

        self.replace(commit_guard=guard)
        self.assertEqual(
            events, ["entered", "checked", "entered", "checked", "entered", "committed"]
        )

    def test_cancelled_lock_wait_stops_before_creating_client_or_reserving_attempt(
        self,
    ):
        old = self.saved()[0]

        @contextmanager
        def cancelled():
            raise ValueError("Private reconnect cancelled.")
            yield

        with patch.object(buyer, "Client") as client, self.assertRaises(ValueError):
            buyer.replace_browser_session(
                self.records,
                expected_user_id="99",
                expected_session_fingerprint=self.snapshot["session_fingerprint"],
                csrf=self.browser_csrf,
                commit_guard=cancelled,
            )
        client.assert_not_called()
        self.assertEqual(self.saved()[0], old)
        self.assertEqual(self.calls, [])

    def test_cancel_between_homepage_and_identity_stops_next_read(self):
        old = self.saved()[0]

        @contextmanager
        def cancelled_after_homepage():
            if self.calls:
                raise ValueError("Private reconnect cancelled.")
            yield

        with self.assertRaises(ValueError):
            self.replace(csrf=None, commit_guard=cancelled_after_homepage)
        self.assertEqual(self.calls, [("GET", "/")])
        self.assertEqual(self.saved()[0]["session"], old["session"])
        self.assertEqual(self.saved()[0]["enabled"], old["enabled"])
        self.clients[0].session.close.assert_called_once()

    def test_cancel_during_identity_stops_new_paid_solver(self):
        old = self.saved()[0]["session"]
        cancelled = []
        self.challenge, self.identity_status = True, 403
        self.before_identity = lambda: cancelled.append(True)

        @contextmanager
        def guard():
            if cancelled:
                raise ValueError("Private reconnect cancelled.")
            yield

        with patch.object(
            self.real_client, "solve_challenge"
        ) as solver, self.assertRaises(ValueError):
            self.replace(commit_guard=guard)
        solver.assert_not_called()
        self.assertEqual(self.calls, [("GET", "/api/v2/users/current")])
        self.assertEqual(self.saved()[0]["session"], old)
        self.clients[0].session.close.assert_called_once()

    def test_cancel_during_already_started_solver_stops_identity_replay(self):
        old = self.saved()[0]["session"]
        cancelled = []
        self.challenge, self.identity_status = True, 403

        @contextmanager
        def guard():
            if cancelled:
                raise ValueError("Private reconnect cancelled.")
            yield

        def solver_result(*args):
            cancelled.append(True)
            return True

        with patch.object(
            self.real_client, "solve_challenge", side_effect=solver_result
        ) as solver, self.assertRaises(ValueError):
            self.replace(commit_guard=guard)
        solver.assert_called_once()
        self.assertEqual(self.calls, [("GET", "/api/v2/users/current")])
        self.assertEqual(self.saved()[0]["session"], old)
        self.clients[0].session.close.assert_called_once()

    def test_diagnostic_binding_and_cleanup_failures_after_commit_stay_verified(self):
        operations = (
            (self.real_client, "bind_verified_session"),
            (buyer, "record_auth"),
            (buyer, "record_connection_result"),
            (requests.Session, "close"),
        )
        for target, name in operations:
            with self.subTest(operation=name):
                self.reset_attempt()
                snapshot = buyer.browser_session_snapshot()
                before = self.controls()
                old = self.saved()[0]["session"]
                with patch.object(
                    target, name, side_effect=sqlite3.OperationalError("private-marker")
                ), self.assertLogs("vinted_buyer", level="WARNING") as logs:
                    result = self.replace(snapshot=snapshot)
                self.assertEqual(result["outcome"], "verified")
                self.assertTrue(result["same_buyer_account"])
                self.assertNotEqual(self.saved()[0]["session"], old)
                self.assertEqual(self.saved()[0]["verified_at"], self.now)
                self.assertEqual(self.controls(), before)
                self.assertNotIn("private-marker", " ".join(logs.output))
                self.clients[-1].session.close.assert_called_once()

    def test_failed_attempt_metadata_error_does_not_hide_original_identity_rejection(
        self,
    ):
        self.identity_status = 401
        old = self.saved()[0]["session"]
        with patch.object(
            buyer,
            "record_connection_result",
            side_effect=sqlite3.OperationalError("private-marker"),
        ), self.assertRaises(buyer.BuyerError) as error:
            self.replace()
        self.assertEqual(error.exception.reason, "credentials")
        self.assertEqual(self.saved()[0]["session"], old)
        self.clients[0].session.close.assert_called_once()

    def test_invalid_cookie_or_csrf_input_cannot_make_auth_requests_or_destroy_saved_session(
        self,
    ):
        invalid = [
            [],
            self.records[:1],
            self.records + [self.records[0]],
            ["private-marker", {}],
        ]
        for field, value in (
            ("name", "datadome"),
            ("value", "private-marker"),
            ("domain", ".vinted.it"),
            ("domain", ".evil.example.test"),
            ("path", "/api/v2/users/current/child"),
            ("secure", False),
            ("expires", self.now),
            ("expires", True),
            ("expires", float("inf")),
            ("domain_specified", "private-marker"),
            ("private-marker", "private-marker"),
        ):
            changed = copy.deepcopy(self.records)
            changed[0][field] = value
            invalid.append(changed)
        duplicate = copy.deepcopy(self.records)
        duplicate[1]["name"] = "access_token_web"
        invalid.append(duplicate)
        invalid.append(
            [dict(record, domain_specified=False) for record in self.records]
        )
        missing = copy.deepcopy(self.records)
        del missing[0]["expires"]
        invalid.append(missing)
        old_row = self.saved()[0]
        before = self.controls()
        with patch.object(buyer, "Client") as client:
            for records in invalid:
                with self.subTest(cookie_count=len(records)), self.assertRaises(
                    buyer.BuyerError
                ) as error:
                    buyer.replace_browser_session(
                        records,
                        expected_user_id="99",
                        expected_session_fingerprint=self.snapshot[
                            "session_fingerprint"
                        ],
                        csrf=self.browser_csrf,
                    )
                self.assertNotIn("private-marker", str(error.exception))
            for csrf in ("private-marker\r\nInjected: header", {}, True, "a" * 1025):
                with self.subTest(csrf_type=type(csrf).__name__), self.assertRaises(
                    buyer.BuyerError
                ):
                    buyer.replace_browser_session(
                        self.records,
                        expected_user_id="99",
                        expected_session_fingerprint=self.snapshot[
                            "session_fingerprint"
                        ],
                        csrf=csrf,
                    )
            client.assert_not_called()
        self.assertEqual(self.saved()[0], old_row)
        self.assertEqual(self.controls(), before)

    def test_stale_revision_or_different_saved_account_is_rejected_before_network(self):
        old = self.saved()[0]
        with patch.object(buyer, "Client") as client:
            for user, revision in (
                ("99", "0" * 64),
                ("100", self.snapshot["session_fingerprint"]),
                ("99", "private-marker"),
                ({}, self.snapshot["session_fingerprint"]),
            ):
                with self.subTest(user_type=type(user).__name__), self.assertRaises(
                    buyer.BuyerError
                ):
                    buyer.replace_browser_session(
                        self.records,
                        expected_user_id=user,
                        expected_session_fingerprint=revision,
                        csrf=self.browser_csrf,
                    )
            client.assert_not_called()
        self.assertEqual(self.saved()[0], old)

    def test_proxy_key_and_solver_setting_changes_invalidate_browser_snapshot(self):
        old = self.saved()[0]["session"]
        for field, value in (
            ("proxy", "http://new-user:new-secret@proxy.example.test:8080"),
            ("api_key", "replacement-capsolver-key"),
            ("enabled", False),
        ):
            with self.subTest(field=field):
                updated = dict(self.network, **{field: value})
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute(
                        "UPDATE vinted_buyer SET network=? WHERE id=1",
                        (buyer.encrypt(updated),),
                    )
                self.assertNotEqual(
                    buyer.browser_session_snapshot()["session_fingerprint"],
                    self.snapshot["session_fingerprint"],
                )
                with patch.object(buyer, "Client") as client, self.assertRaises(
                    buyer.BuyerError
                ):
                    buyer.replace_browser_session(
                        self.records,
                        expected_user_id="99",
                        expected_session_fingerprint=self.snapshot[
                            "session_fingerprint"
                        ],
                        csrf=self.browser_csrf,
                    )
                client.assert_not_called()
                self.assertEqual(self.saved()[0]["session"], old)
                self.assertEqual(self.saved()[0]["enabled"], 1)

    def test_ca_configuration_change_also_invalidates_server_revision(self):
        with patch.dict(
            buyer.os.environ, {"REQUESTS_CA_BUNDLE": "/offline/changed-ca.pem"}
        ):
            self.assertNotEqual(
                buyer.browser_session_snapshot()["session_fingerprint"],
                self.snapshot["session_fingerprint"],
            )
            with patch.object(buyer, "Client") as client, self.assertRaises(
                buyer.BuyerError
            ):
                buyer.replace_browser_session(
                    self.records,
                    expected_user_id="99",
                    expected_session_fingerprint=self.snapshot["session_fingerprint"],
                    csrf=self.browser_csrf,
                )
            client.assert_not_called()

    def test_network_change_during_identity_cannot_save_browser_session(self):
        old = self.saved()[0]

        def change_network():
            with closing(search_settings.connection()) as conn, conn:
                conn.execute(
                    "UPDATE vinted_buyer SET network=? WHERE id=1",
                    (buyer.encrypt(dict(self.network, api_key="changed-key")),),
                )

        self.before_identity = change_network
        with self.assertRaises(buyer.BuyerError) as error:
            self.replace()
        self.assertEqual(error.exception.reason, "saved_session")
        row = self.saved()[0]
        for field in ("session", "pending", "enabled", "verified_at"):
            self.assertEqual(row[field], old[field])
        self.clients[0].session.close.assert_called_once()

    def test_concurrent_session_replacement_is_not_overwritten_after_identity(self):
        replacement = buyer.encrypt(
            {"cookies": {"access_token_web": "concurrent-access-0123456789"}}
        )

        def replace_elsewhere():
            with closing(search_settings.connection()) as conn, conn:
                conn.execute(
                    "UPDATE vinted_buyer SET session=? WHERE id=1", (replacement,)
                )

        self.before_identity = replace_elsewhere
        with self.assertRaises(buyer.BuyerError) as error:
            self.replace()
        self.assertEqual(error.exception.reason, "saved_session")
        self.assertEqual(self.saved()[0]["session"], replacement)
        self.assertEqual(self.saved()[0]["enabled"], 1)
        self.clients[0].session.close.assert_called_once()

    def test_wrong_account_and_rejected_identity_preserve_old_session_and_permission(
        self,
    ):
        for user, status, reason in (
            (100, 200, "account_changed"),
            (99, 401, "credentials"),
        ):
            with self.subTest(user=user, status=status):
                self.reset_attempt()
                self.now += 1
                # Equal timestamps intentionally cannot overwrite prior diagnostics.
                self.clock.return_value = self.now
                self.identity_user, self.identity_status = user, status
                old = self.saved()[0]
                before = self.controls()
                with self.assertRaises(buyer.BuyerError) as error:
                    self.replace()
                self.assertEqual(error.exception.reason, reason)
                row = self.saved()[0]
                for field in ("session", "pending", "enabled", "verified_at"):
                    self.assertEqual(row[field], old[field])
                self.assertEqual(self.controls(), before)
                self.clients[-1].session.close.assert_called_once()
                self.assertEqual(
                    buyer.public_auth_results()["buyer_session"]["reason"], reason
                )
                self.assertEqual(
                    buyer.settings()["connection_health"]["state"], "verified"
                )
        self.assertEqual(self.calls, [("GET", "/api/v2/users/current")] * 2)

    def test_homepage_failure_is_not_renewed_and_does_not_destroy_old_pending_code(
        self,
    ):
        self.homepage_status = 503
        old = self.saved()[0]
        with self.assertRaises(buyer.BuyerError):
            self.replace(csrf=None)
        row = self.saved()[0]
        for field in ("session", "pending", "enabled", "verified_at"):
            self.assertEqual(row[field], old[field])
        self.assertEqual(self.calls, [("GET", "/")])
        self.clients[0].session.close.assert_called_once()

    def test_cooldown_rejects_without_auth_or_permission_changes(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET login_attempt=? WHERE id=1", (self.now - 10,)
            )
        old = self.saved()[0]
        with patch.object(buyer, "Client") as client, self.assertRaises(
            buyer.BuyerError
        ):
            buyer.replace_browser_session(
                self.records,
                expected_user_id="99",
                expected_session_fingerprint=self.snapshot["session_fingerprint"],
                csrf=self.browser_csrf,
            )
        client.assert_not_called()
        self.assertEqual(self.saved()[0], old)

    def test_invalid_saved_account_has_no_browser_snapshot_or_requests(self):
        for column, value in (
            ("session", None),
            ("user_id", "private-marker"),
            ("verified_at", None),
        ):
            with self.subTest(column=column):
                row = self.saved()[0]
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute(
                        f"UPDATE vinted_buyer SET {column}=? WHERE id=1", (value,)
                    )
                with patch.object(buyer, "Client") as client, self.assertRaises(
                    buyer.BuyerError
                ):
                    buyer.browser_session_snapshot()
                client.assert_not_called()
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute(
                        f"UPDATE vinted_buyer SET {column}=? WHERE id=1", (row[column],)
                    )


class ConnectionHealthTests(buying_tests.SessionRotationFixture, unittest.TestCase):
    now = 1791710000

    def setUp(self):
        super().setUp()
        clock = patch.object(buyer.time, "time", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET verified_at=?,enabled=0 WHERE id=1",
                (self.now - 200,),
            )

    def metadata(
        self, reason, stage, checked, *, retry=None, blocked=False, matching=True
    ):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer_access SET checked=?,reason=?,stage=?,http_status=? WHERE id=1",
                (checked, reason, stage, 400 if stage == "renewal" else 401),
            )
            if retry is not None or blocked:
                fingerprint = (
                    hashlib.sha256(self.saved()[0]["session"]).hexdigest()
                    if matching
                    else "0" * 64
                )
                conn.execute(
                    "UPDATE vinted_buyer_maintenance SET session_fingerprint=?,retry_at=?,blocked=? WHERE id=1",
                    (fingerprint, retry or 0, int(blocked)),
                )

    def test_historical_connection_is_distinct_from_latest_renewal_rejection_and_cooldown(
        self,
    ):
        self.metadata("renewal_failed", "renewal", self.now - 10, retry=self.now + 900)
        settings = buyer.settings()
        health = settings["connection_health"]
        self.assertTrue(settings["connected"])
        self.assertEqual(health["state"], "cooldown")
        self.assertEqual(health["last_verified_at"], self.now - 200)
        self.assertEqual(health["last_checked_at"], self.now - 10)
        self.assertEqual(health["stage_code"], "renewal")
        self.assertEqual(health["http_status"], 400)
        self.assertEqual(health["retry_at"], self.now + 900)
        self.assertIn("BST", health["last_verified"])
        self.assertTrue(health["maintenance_independent_of_buying"])
        self.assertEqual(settings["enabled"], 0)

    def test_password_failure_does_not_relabel_saved_account_health(self):
        self.metadata("security_challenge", "sign_in", self.now - 10)
        health = buyer.settings()["connection_health"]
        self.assertEqual(health["state"], "verified")
        self.assertIsNone(health["last_checked_at"])

    def test_distinct_native_renewal_failure_survives_later_password_failure(self):
        with patch.object(buyer.time, "time", return_value=self.now - 20):
            buyer.record_auth("renewal_failed", "renewal", 400)
        with patch.object(buyer.time, "time", return_value=self.now - 10):
            buyer.record_auth("security_challenge", "sign_in", 403)
        health = buyer.settings()["connection_health"]
        self.assertEqual(health["state"], "attention")
        self.assertEqual(health["last_checked_at"], self.now - 20)
        self.assertEqual(health["stage_code"], "renewal")

    def test_old_failure_before_fresh_verification_is_not_current_health(self):
        with patch.object(buyer.time, "time", return_value=self.now - 300):
            buyer.record_auth("renewal_failed", "renewal", 400)
        self.assertEqual(buyer.settings()["connection_health"]["state"], "verified")

    def test_accepted_renewal_alone_is_not_same_account_identity_proof(self):
        buyer.record_auth("renewed", "renewal", 200)
        health = buyer.settings()["connection_health"]
        self.assertEqual(health["state"], "awaiting_verification")
        self.assertIn("Account verification is reported separately", health["message"])

    def test_terminal_rejection_is_bound_to_exact_saved_session(self):
        self.metadata("refresh_rejected", "renewal", self.now - 10, blocked=True)
        self.assertEqual(
            buyer.settings()["connection_health"]["state"], "reconnect_required"
        )
        self.metadata(
            "refresh_rejected", "renewal", self.now - 300, blocked=True, matching=False
        )
        self.assertEqual(buyer.settings()["connection_health"]["state"], "verified")

    def test_missing_session_is_not_connected_and_password_is_not_claimed_saved(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET session=NULL WHERE id=1")
        with patch.object(buyer, "Client") as client:
            settings = buyer.settings()
        self.assertFalse(settings["connected"])
        self.assertEqual(settings["connection_health"]["state"], "not_connected")
        self.assertFalse(settings["password_saved"])
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
