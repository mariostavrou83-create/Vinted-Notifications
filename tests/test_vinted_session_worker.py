"""Real cookie transport with fictional upstream replies; never a live purchase."""

import asyncio
import base64
import hashlib
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
        self.renew_error = {"error": "private-error"}
        self.identity_status = 200
        self.renewed_user = 99
        super().setUp()
        # Seed the full canonical records production exports. An accepted
        # identity read may upgrade a legacy flat view without rotating tokens.
        client = buyer.Client(self.saved()[1])
        try:
            canonical = client.exported()
        finally:
            client.session.close()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?", (buyer.encrypt(canonical),)
            )

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
                {} if self.renew_status == 200 else self.renew_error,
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

    def test_unknown_400_cools_down_across_ticks_then_retries_the_same_session(self):
        self.renew_status = 400
        original = self.saved()[0]["session"]
        before = self.controls()
        with self.assertLogs(level="INFO") as logs:
            self.assertEqual(self.run_cycle(), "cooldown")
            self.assertEqual(self.run_cycle(self.now + 899), "cooldown")
        self.assertEqual(self.saved()[0]["session"], original)
        self.assertEqual(self.controls(), before)
        self.assertEqual(len(self.calls), 3)
        self.assertNotIn("private-error", " ".join(logs.output))
        self.renew_status = 200
        self.assertEqual(self.run_cycle(self.now + 900), "verified")
        self.assertEqual(len(self.calls), 7)
        self.assertEqual(self.controls(), before)

    def test_repeated_unknown_400_remains_limited_to_one_attempt_per_15_minutes(self):
        self.renew_status = 400
        original = self.saved()[0]["session"]
        self.assertEqual(self.run_cycle(), "cooldown")
        self.assertEqual(self.run_cycle(self.now + 900), "cooldown")
        # Re-entering the worker does not lose its persistent cooldown.
        self.assertEqual(self.run_cycle(self.now + 1799), "cooldown")
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(self.saved()[0]["session"], original)
        self.assertEqual(self.controls()[2], [])

    def test_explicit_refresh_rejection_remains_blocked_until_connection_changes(self):
        self.renew_status = 400
        self.renew_error = {"error": "invalid_grant"}
        original = self.saved()[0]["session"]
        self.assertEqual(self.run_cycle(), "blocked")
        self.assertEqual(self.run_cycle(self.now + 3600), "blocked")
        self.assertEqual(self.saved()[0]["session"], original)
        self.assertEqual(len(self.calls), 3)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?", (buyer.encrypt(self.saved()[1]),)
            )
        self.renew_status = 200
        self.assertEqual(self.run_cycle(), "verified")

    def test_other_terminal_errors_do_not_inherit_ambiguous_400_cooldown(self):
        for reason, stage, status in (
            ("refresh_rejected", "renewal", 400),
            ("credentials", "renewal", 400),
            ("account_restricted", "renewal", 400),
            ("security_challenge", "renewal", 400),
            ("account_changed", "identity", 400),
            ("renewal_failed", "renewal", 422),
            ("renewal_failed", "identity", 400),
        ):
            with self.subTest(reason=reason, stage=stage, status=status):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute(
                        "UPDATE vinted_buyer_maintenance SET session_fingerprint=NULL,retry_at=0,blocked=0"
                    )
                error = buyer.BuyerError(
                    "Offline fixed error", status, reason=reason, stage=stage
                )
                error.session_fingerprint = hashlib.sha256(
                    self.saved()[0]["session"]
                ).hexdigest()
                with patch.object(buyer, "connected_client", side_effect=error) as call:
                    self.assertEqual(self.run_cycle(), "blocked")
                    self.assertEqual(self.run_cycle(self.now + 3600), "blocked")
                call.assert_called_once()

    def replacement_session(self):
        _, saved = self.saved()
        for cookie in saved["cookie_records"]:
            if cookie["name"] == "access_token_web":
                cookie["value"] = token(self.now + 3600, "replacement-access")
                saved["cookies"][cookie["name"]] = cookie["value"]
        sealed = buyer.encrypt(saved)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET session=?", (sealed,))
        return sealed

    def test_missing_error_metadata_can_only_block_the_unchanged_original_session(self):
        error = buyer.BuyerError(
            "Fixed offline rejection", 400, reason="refresh_rejected", stage="renewal"
        )
        fingerprint = hashlib.sha256(self.saved()[0]["session"]).hexdigest()
        with patch.object(buyer, "connected_client", side_effect=error):
            self.assertEqual(self.run_cycle(), "blocked")
        self.assertEqual(
            self.maintenance_state(),
            (1, fingerprint, self.now + worker.RETRY_DELAY, 1),
        )
        self.assertEqual(self.calls, [])

    def test_failed_maintenance_with_no_reference_cannot_block_a_replacement(self):
        original = self.saved()[0]["session"]
        original_fingerprint = hashlib.sha256(original).hexdigest()
        replacement = []
        native = self.native
        self.renew_status = 400

        def replace_during_renewal(adapter, prepared, **kwargs):
            if prepared.url.endswith("/web/api/auth/refresh"):
                replacement.append(self.replacement_session())
            return native(adapter, prepared, **kwargs)

        self.native = replace_during_renewal
        self.assertEqual(self.run_cycle(), "session_changed")
        self.assertEqual(self.saved()[0]["session"], replacement[0])
        self.assertEqual(
            self.maintenance_state(),
            (1, original_fingerprint, self.now + worker.RETRY_DELAY, 0),
        )
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.run_cycle(), "fresh")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.controls()[2], [])

    def test_stale_failure_reference_cannot_block_a_replacement(self):
        original_fingerprint = hashlib.sha256(self.saved()[0]["session"]).hexdigest()
        error = buyer.BuyerError(
            "Fixed offline CAS failure", reason="saved_session", stage="saved_session"
        )
        error.session_fingerprint = original_fingerprint
        replacement = []

        def replaced(**kwargs):
            replacement.append(self.replacement_session())
            raise error

        with patch.object(buyer, "connected_client", side_effect=replaced):
            self.assertEqual(self.run_cycle(), "session_changed")
        self.assertEqual(self.saved()[0]["session"], replacement[0])
        self.assertEqual(
            self.maintenance_state(),
            (1, original_fingerprint, self.now + worker.RETRY_DELAY, 0),
        )
        self.assertEqual(self.run_cycle(), "fresh")
        self.assertEqual(self.calls, [])

    def test_successful_connection_cannot_apply_its_cooldown_to_a_replacement(self):
        original_fingerprint = hashlib.sha256(self.saved()[0]["session"]).hexdigest()
        connected = buyer.connected_client
        replacement = []

        def replaced_after_connection(**kwargs):
            client = connected(**kwargs)
            replacement.append(self.replacement_session())
            return client

        with patch.object(
            buyer, "connected_client", side_effect=replaced_after_connection
        ):
            self.assertEqual(self.run_cycle(), "session_changed")
        self.assertEqual(self.saved()[0]["session"], replacement[0])
        self.assertEqual(
            self.maintenance_state(),
            (1, original_fingerprint, self.now + worker.RETRY_DELAY, 0),
        )
        self.assertEqual(self.run_cycle(), "fresh")
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.controls()[2], [])

    def test_due_maintenance_runs_before_the_first_startup_sleep(self):
        _, saved = self.saved()
        # The JWT is still fresh, but Requests stops sending the cookie sooner.
        # Startup must schedule from that earlier cookie expiry.
        for cookie in saved["cookie_records"]:
            if cookie["name"] == "access_token_web":
                cookie["value"] = token(self.now + 3600, "longer-jwt-access")
                cookie["expires"] = self.now + 60
                saved["cookies"]["access_token_web"] = cookie["value"]
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET session=?", (buyer.encrypt(saved),))
        events = []

        async def thread_call(function):
            self.assertIs(function, worker.maintain_connection)
            events.append(self.run_cycle())

        async def sleep(seconds):
            self.assertEqual(seconds, 60)
            events.append("sleep")
            raise asyncio.CancelledError

        with patch.object(
            worker.asyncio, "to_thread", side_effect=thread_call
        ), patch.object(worker.asyncio, "sleep", side_effect=sleep), self.assertRaises(
            asyncio.CancelledError
        ):
            asyncio.run(worker.run())
        self.assertEqual(events, ["verified", "sleep"])
        self.assertEqual(len(self.calls), 4)

    def test_startup_does_not_clear_an_existing_terminal_block(self):
        fingerprint = hashlib.sha256(self.saved()[0]["session"]).hexdigest()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer_maintenance SET session_fingerprint=?,blocked=1",
                (fingerprint,),
            )
        self.assertEqual(self.run_cycle(), "blocked")
        self.assertEqual(self.calls, [])

    def maintenance_state(self):
        with closing(search_settings.connection()) as conn:
            return tuple(
                conn.execute("SELECT * FROM vinted_buyer_maintenance").fetchone()
            )

    def test_fresh_ambiguous_check_adopts_cooldown_only_for_its_exact_session(self):
        original = self.saved()[0]["session"]
        fingerprint = hashlib.sha256(original).hexdigest()
        before = self.controls()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer_maintenance SET session_fingerprint=?,blocked=1",
                (fingerprint,),
            )
        error = buyer.BuyerError(
            "Fixed offline failure", 400, reason="renewal_failed", stage="renewal"
        )
        with patch.object(worker.time, "time", return_value=self.now):
            self.assertTrue(
                worker.note_ambiguous_failure(error, session_fingerprint=fingerprint)
            )
        self.assertEqual(self.maintenance_state(), (1, fingerprint, self.now + 900, 0))
        self.assertEqual(self.saved()[0]["session"], original)
        self.assertEqual(self.controls(), before)
        self.assertEqual(self.run_cycle(self.now + 899), "cooldown")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.run_cycle(self.now + 900), "verified")

    def test_failure_adoption_rejects_other_errors_or_a_newer_session(self):
        fingerprint = hashlib.sha256(self.saved()[0]["session"]).hexdigest()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer_maintenance SET session_fingerprint=?,retry_at=12,blocked=1",
                (fingerprint,),
            )
        before = self.maintenance_state()
        for reason, stage, status in (
            ("refresh_rejected", "renewal", 400),
            ("credentials", "renewal", 400),
            ("security_challenge", "renewal", 400),
            ("account_restricted", "renewal", 400),
            ("renewal_failed", "identity", 400),
            ("renewal_failed", "renewal", 422),
        ):
            error = buyer.BuyerError(
                "Fixed offline failure", status, reason=reason, stage=stage
            )
            self.assertFalse(
                worker.note_ambiguous_failure(error, session_fingerprint=fingerprint)
            )
            self.assertEqual(self.maintenance_state(), before)
        error = buyer.BuyerError(
            "Fixed offline failure", 400, reason="renewal_failed", stage="renewal"
        )
        for invalid in (None, "", "not-a-fingerprint", "f" * 63, "g" * 64, "0" * 64):
            self.assertFalse(
                worker.note_ambiguous_failure(error, session_fingerprint=invalid)
            )
            self.assertEqual(self.maintenance_state(), before)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?", (buyer.encrypt(self.saved()[1]),)
            )
        self.assertFalse(
            worker.note_ambiguous_failure(error, session_fingerprint=fingerprint)
        )
        self.assertEqual(self.maintenance_state(), before)
        self.assertEqual(self.calls, [])

    def test_failure_adoption_never_competes_with_an_active_buyer_operation(self):
        fingerprint = hashlib.sha256(self.saved()[0]["session"]).hexdigest()
        before = self.maintenance_state()
        error = buyer.BuyerError(
            "Fixed offline failure", 400, reason="renewal_failed", stage="renewal"
        )
        with buyer.exclusive():
            self.assertFalse(
                worker.note_ambiguous_failure(error, session_fingerprint=fingerprint)
            )
        self.assertEqual(self.maintenance_state(), before)
        self.assertEqual(self.calls, [])

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
