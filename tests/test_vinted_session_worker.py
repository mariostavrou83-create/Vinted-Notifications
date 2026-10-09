"""Real cookie transport with fictional upstream replies; never a live purchase."""

import base64
import json
import threading
import time
import unittest
from contextlib import closing
from unittest.mock import patch

import requests
import test_vinted_buying as buying_tests

import search_settings
import vinted_buyer as buyer
import vinted_session_worker as worker


def token(expiry, label):
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": expiry, "test": label}).encode())
        .decode()
        .rstrip("=")
    )
    return f"test-header.{payload}.test-signature"


class MaintenanceTests(buying_tests.SessionRotationFixture, unittest.TestCase):
    transport_response = buying_tests.NativeCookieTransportTests.transport_response
    token_headers = buying_tests.NativeCookieTransportTests.token_headers

    def setUp(self):
        # Keep the real Requests cookie pipeline behind our mocked HTTP adapter.
        native_session = patch.object(buyer, "BrowserSession", requests.Session)
        native_session.start()
        self.addCleanup(native_session.stop)
        self.now = int(time.time())
        self.old_access = token(self.now + 60, "old-access")
        self.old_refresh = token(self.now + 7 * 86400, "old-refresh")
        self.new_access = token(self.now + 1900, "new-access")
        self.new_refresh = token(self.now + 7 * 86400, "new-refresh")
        self.calls = []
        self.renew_status = 200
        self.identity_status = 200
        self.renewed_user = 99
        super().setUp()

    def native(self, adapter, prepared, **kwargs):
        path = prepared.url.removeprefix(buyer.BASE)
        self.calls.append((prepared.method, path))
        if path == "/":
            self.assertNotIn("access_token_web", prepared.headers.get("Cookie", ""))
            return self.transport_response(
                prepared,
                text='<meta name="csrf-token" content="fresh-csrf-token-0123456789">',
            )
        if path == "/web/api/auth/refresh":
            self.assertEqual(prepared.method, "POST")
            self.assertIsNone(prepared.body)
            self.assertNotIn("Content-Type", prepared.headers)
            self.assertIn(
                self.saved()[1]["cookies"]["refresh_token_web"],
                prepared.headers["Cookie"],
            )
            self.assertNotIn("Authorization", prepared.headers)
            self.assertEqual(worker.maintain_connection(), "busy")
            return self.transport_response(
                prepared,
                {} if self.renew_status == 200 else {"error": "private-error"},
                status=self.renew_status,
                cookies=self.token_headers(self.new_access, self.new_refresh),
            )
        self.assertEqual(path, "/api/v2/users/current")
        rotated = self.new_access in prepared.headers.get("Cookie", "")
        return self.transport_response(
            prepared,
            {"user": {"id": self.renewed_user if rotated else 99}},
            status=self.identity_status,
        )

    def run_cycle(self, at=None):
        with patch.object(
            worker.time, "time", return_value=at or self.now
        ), patch.object(
            requests.adapters.HTTPAdapter,
            "send",
            autospec=True,
            side_effect=self.native,
        ):
            return worker.maintain_connection()

    def controls(self):
        with closing(search_settings.connection()) as conn:
            return (
                tuple(
                    conn.execute(
                        "SELECT user_id,enabled,max_total,max_extra,pickup_mode,preferred_card_last4 FROM vinted_buyer"
                    ).fetchone()
                ),
                [tuple(r) for r in conn.execute("SELECT * FROM vinted_search_budgets")],
                [tuple(r) for r in conn.execute("SELECT * FROM vinted_buy_attempts")],
            )

    def test_due_connection_renews_once_and_keeps_purchase_controls(self):
        before = self.controls()
        with self.assertLogs("vinted_buyer", level="INFO") as logs:
            self.assertEqual(self.run_cycle(), "verified")
        self.assertEqual(
            self.calls,
            [
                ("GET", "/api/v2/users/current"),
                ("GET", "/"),
                ("POST", "/web/api/auth/refresh"),
                ("GET", "/api/v2/users/current"),
            ],
        )
        self.assertEqual(
            self.saved()[1]["cookies"]["refresh_token_web"], self.new_refresh
        )
        self.assertEqual(self.controls(), before)
        self.assertNotIn(self.new_access, " ".join(logs.output))
        self.assertEqual(self.run_cycle(), "cooldown")
        self.assertEqual(self.run_cycle(self.now + 300), "fresh")
        self.assertEqual(len(self.calls), 4)

    def test_refused_renewal_is_blocked_across_ticks_until_connection_changes(self):
        self.renew_status = 400
        original = self.saved()[0]["session"]
        with self.assertLogs(level="INFO") as logs:
            self.assertEqual(self.run_cycle(), "blocked")
            self.assertEqual(self.run_cycle(self.now + 3600), "blocked")
        self.assertEqual(self.saved()[0]["session"], original)
        self.assertEqual(len(self.calls), 3)
        self.assertNotIn("private-error", " ".join(logs.output))
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?", (buyer.encrypt(self.saved()[1]),)
            )
        self.renew_status = 200
        self.assertEqual(self.run_cycle(), "verified")

    def test_rotations_use_latest_saved_tokens_over_simulated_hour(self):
        self.assertEqual(self.run_cycle(), "verified")
        first = self.new_refresh
        self.new_access = token(self.now + 3600, "second-access")
        self.new_refresh = token(self.now + 8 * 86400, "second-refresh")
        self.assertEqual(self.run_cycle(self.now + 1800), "verified")
        self.assertNotEqual(self.saved()[1]["cookies"]["refresh_token_web"], first)
        self.assertEqual(self.run_cycle(self.now + 3300), "fresh")
        self.new_access = token(self.now + 5700, "third-access")
        self.new_refresh = token(self.now + 9 * 86400, "third-refresh")
        self.assertEqual(self.run_cycle(self.now + 3600), "verified")
        self.assertEqual(len(self.calls), 12)
        self.assertEqual(self.controls()[2], [])

    def test_missing_unverified_fresh_or_unknown_session_makes_no_request(self):
        for change in ({"verified_at": None}, {"session": None}):
            row, _ = self.saved()
            with closing(search_settings.connection()) as conn, conn:
                for key, value in change.items():
                    conn.execute(f"UPDATE vinted_buyer SET {key}=?", (value,))
            self.assertEqual(self.run_cycle(), "idle")
            with closing(search_settings.connection()) as conn, conn:
                for key in change:
                    conn.execute(f"UPDATE vinted_buyer SET {key}=?", (row[key],))
        for access, refresh in (
            (token(self.now + 3600, "fresh"), self.old_refresh),
            ("opaque-token", "opaque-refresh"),
            (self.old_access, ""),
        ):
            saved = {
                "cookies": {"access_token_web": access, "refresh_token_web": refresh}
            }
            with closing(search_settings.connection()) as conn, conn:
                conn.execute(
                    "UPDATE vinted_buyer SET session=?", (buyer.encrypt(saved),)
                )
            self.assertEqual(self.run_cycle(), "fresh")
        self.assertEqual(self.calls, [])

    def test_transient_refusal_observes_cooldown_then_retries(self):
        self.identity_status = 429
        self.assertEqual(self.run_cycle(), "cooldown")
        self.assertEqual(self.run_cycle(self.now + 899), "cooldown")
        self.assertEqual(len(self.calls), 1)
        self.identity_status = 200
        self.assertEqual(self.run_cycle(self.now + 900), "verified")

    def test_lock_skips_busy_buyer_without_requests_or_state_changes(self):
        with buyer.exclusive():
            self.assertEqual(self.run_cycle(), "busy")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.run_cycle(), "verified")

    def test_interactive_lock_waits_for_maintenance_and_times_out_safely(self):
        entered = threading.Event()
        release = threading.Event()

        def holding():
            with buyer.exclusive():
                entered.set()
                release.wait(1)

        thread = threading.Thread(target=holding)
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaises(buyer.BuyerError) as error, buyer.exclusive(
                wait_seconds=0.01
            ):
                self.fail("Maintenance still holds the buyer lock")
            self.assertEqual(error.exception.reason, "busy")
            timer = threading.Timer(0.05, release.set)
            timer.start()
            with buyer.exclusive(wait_seconds=0.5):
                self.assertTrue(release.is_set())
            timer.join()
        finally:
            release.set()
            thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_changed_identity_cannot_become_a_verified_connection(self):
        self.renewed_user = 100
        self.assertEqual(self.run_cycle(), "blocked")
        row, saved = self.saved()
        self.assertEqual((row["user_id"], row["verified_at"]), ("99", 1))
        self.assertEqual(saved["cookies"]["refresh_token_web"], self.new_refresh)
        self.assertEqual(self.run_cycle(self.now + 3600), "blocked")

    def test_recent_rotation_on_identity_read_does_not_refresh_again(self):
        original = self.native

        def rotated(adapter, prepared, **kwargs):
            if prepared.url.endswith("/users/current"):
                self.calls.append((prepared.method, "/api/v2/users/current"))
                return self.transport_response(
                    prepared,
                    {"user": {"id": 99}},
                    cookies=self.token_headers(self.new_access, self.new_refresh),
                )
            return original(adapter, prepared, **kwargs)

        with patch.object(worker.time, "time", return_value=self.now), patch.object(
            requests.adapters.HTTPAdapter, "send", autospec=True, side_effect=rotated
        ):
            self.assertEqual(worker.maintain_connection(), "verified")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(
            self.saved()[1]["cookies"]["access_token_web"], self.new_access
        )

    def test_version_18_upgrade_preserves_account_and_budget_settings(self):
        before = self.controls()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DROP TABLE vinted_buyer_maintenance")
            conn.execute(
                "UPDATE parameters SET value='18' WHERE key='msj_search_schema'"
            )
        self.assertIsNotNone(search_settings.ensure_schema())
        self.assertEqual(self.controls(), before)
        self.assertEqual(self.run_cycle(), "verified")
        self.assertIsNone(search_settings.ensure_schema())
