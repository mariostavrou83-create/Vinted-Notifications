"""Read-only production diagnosis is opt-in and cannot repeat after restart."""

import os
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

import requests
import test_vinted_buying as buying_tests
from test_search_controls import DatabaseFixture

import vinted_buyer
import vinted_connection_check
from search_settings import connection, ensure_schema


class StartupDiagnosisTests(DatabaseFixture, unittest.TestCase):
    def test_latest_public_listing_diagnosis_is_opt_in_and_once_per_release(self):
        self.batch(1, [110])
        with closing(connection()) as conn, conn:
            conn.execute(
                "UPDATE alert_outbox SET status='sent',sent_at=100,telegram_message_id=42 WHERE item_id='110'"
            )
            before = tuple(
                conn.execute(
                    "SELECT * FROM alert_outbox WHERE item_id='110'"
                ).fetchone()
            )
        with (
            patch.dict(
                os.environ,
                {
                    "MSJ_BUYER_CHECK_ON_START": "listing-probe",
                    "MSJ_LISTING_CHECK_ON_START": "1",
                },
            ),
            patch("vinted_buyer.settings", return_value={"connected": False}),
            patch(
                "vinted_gallery.fetch_listing",
                return_value={
                    "state": "ready",
                    "description": "Private seller text",
                    "photos": [],
                },
            ) as fetch,
            self.assertLogs("vinted_connection_check", level="INFO") as logs,
        ):
            vinted_connection_check.run_once()
            vinted_connection_check.run_once()
        fetch.assert_called_once_with("https://www.vinted.co.uk/items/110")
        self.assertIn("description_chars=19", " ".join(logs.output))
        self.assertNotIn("Private", " ".join(logs.output))
        with closing(connection()) as conn:
            self.assertEqual(
                before,
                tuple(
                    conn.execute(
                        "SELECT * FROM alert_outbox WHERE item_id='110'"
                    ).fetchone()
                ),
            )

    def test_disabled_listing_diagnosis_does_not_fetch(self):
        with (
            patch.dict(
                os.environ,
                {
                    "MSJ_BUYER_CHECK_ON_START": "no-listing",
                    "MSJ_LISTING_CHECK_ON_START": "0",
                },
            ),
            patch("vinted_buyer.settings", return_value={"connected": False}),
            patch("vinted_gallery.fetch_listing") as fetch,
        ):
            vinted_connection_check.run_once()
        fetch.assert_not_called()

    def test_bootstrap_probe_is_opt_in_once_and_stops_for_refusals(self):
        for index, (status, reason, expected) in enumerate(
            (
                (400, "http_error", True),
                (401, "credentials", True),
                (403, "forbidden", False),
                (429, "rate_limited", False),
                (400, "security_challenge", False),
            )
        ):
            error = vinted_buyer.BuyerError("private", status, reason=reason)
            with (
                patch.dict(
                    os.environ,
                    {
                        "MSJ_BUYER_CHECK_ON_START": f"bootstrap-{index}",
                        "MSJ_BUYER_BOOTSTRAP_CHECK_ON_START": "1",
                    },
                ),
                patch("vinted_buyer.settings", return_value={"connected": True}),
                patch("vinted_buyer.check_saved_connection", side_effect=error),
                patch("vinted_connection_check.diagnose_bootstrap") as probe,
            ):
                vinted_connection_check.run_once()
                vinted_connection_check.run_once()
            self.assertEqual(probe.call_count, int(expected))

    def test_production_version_16_upgrades_and_preserves_attempt_history(self):
        with closing(connection()) as conn, conn:
            conn.execute("DROP TABLE vinted_buy_attempts")
            conn.execute(
                "CREATE TABLE vinted_buy_attempts (item_id TEXT PRIMARY KEY, state TEXT NOT NULL, checkout_id TEXT, total INTEGER, message TEXT NOT NULL, updated REAL NOT NULL)"
            )
            conn.execute(
                "INSERT INTO vinted_buy_attempts VALUES ('123', 'unknown', 'checkout1', 1200, 'Check Vinted', 1)"
            )
            conn.execute(
                "UPDATE parameters SET value='16' WHERE key='msj_search_schema'"
            )
        self.assertIsNotNone(ensure_schema())
        with closing(connection()) as conn:
            saved = dict(conn.execute("SELECT * FROM vinted_buy_attempts").fetchone())
        self.assertEqual(saved["state"], "unknown")
        self.assertEqual(saved["total"], 1200)
        self.assertIsNone(saved["action_url"])
        self.assertIsNone(ensure_schema())

    def test_disabled_or_invalid_configuration_makes_no_request(self):
        for value in ("", "bad release", "a" * 81):
            with (
                patch.dict(os.environ, {"MSJ_BUYER_CHECK_ON_START": value}),
                patch("vinted_buyer.check_saved_connection") as check,
            ):
                vinted_connection_check.run_once()
                check.assert_not_called()

    def test_saved_connection_checked_once_across_restarts(self):
        with (
            patch.dict(os.environ, {"MSJ_BUYER_CHECK_ON_START": "release-1"}),
            patch("vinted_buyer.settings", return_value={"connected": True}),
            patch("vinted_buyer.check_saved_connection") as check,
        ):
            vinted_connection_check.run_once()
            vinted_connection_check.run_once()
            self.assertEqual(check.call_count, 1)

    def test_failure_is_sanitised_and_not_repeated(self):
        error = vinted_buyer.BuyerError(
            "token-value-must-not-be-logged", 307, reason="redirect", stage="homepage"
        )
        with (
            patch.dict(os.environ, {"MSJ_BUYER_CHECK_ON_START": "release-2"}),
            patch("vinted_buyer.settings", return_value={"connected": True}),
            patch("vinted_buyer.check_saved_connection", side_effect=error) as check,
            self.assertLogs("vinted_connection_check", level="INFO") as log,
        ):
            vinted_connection_check.run_once()
            vinted_connection_check.run_once()
        self.assertEqual(check.call_count, 1)
        self.assertIn("http=307", " ".join(log.output))
        self.assertNotIn("token-value", " ".join(log.output))

    def test_no_account_does_not_contact_vinted(self):
        with (
            patch.dict(os.environ, {"MSJ_BUYER_CHECK_ON_START": "release-3"}),
            patch("vinted_buyer.settings", return_value={"connected": False}),
            patch("vinted_buyer.check_saved_connection") as check,
        ):
            vinted_connection_check.run_once()
            check.assert_not_called()

    def test_fresh_ambiguous_renewal_uses_only_its_checked_session_cooldown(self):
        error = vinted_buyer.BuyerError(
            "private-failure", 400, reason="renewal_failed", stage="renewal"
        )
        error.session_fingerprint = "a" * 64
        with (
            patch.dict(
                os.environ, {"MSJ_BUYER_CHECK_ON_START": "ambiguous-renewal-check"}
            ),
            patch("vinted_buyer.settings", return_value={"connected": True}),
            patch("vinted_buyer.check_saved_connection", side_effect=error) as check,
            patch(
                "vinted_session_worker.note_ambiguous_failure", return_value=True
            ) as defer,
            self.assertLogs("vinted_connection_check", level="INFO") as logs,
        ):
            vinted_connection_check.run_once()
            vinted_connection_check.run_once()
        check.assert_called_once()
        defer.assert_called_once_with(error, session_fingerprint="a" * 64)
        self.assertIn("retry_after_seconds=900", " ".join(logs.output))
        self.assertNotIn(error.session_fingerprint, " ".join(logs.output))
        self.assertNotIn("private-failure", " ".join(logs.output))

    def test_recovery_schedule_error_keeps_original_sanitised_check_result(self):
        error = vinted_buyer.BuyerError(
            "private-failure", 400, reason="renewal_failed", stage="renewal"
        )
        with (
            patch.dict(
                os.environ, {"MSJ_BUYER_CHECK_ON_START": "schedule-error-check"}
            ),
            patch("vinted_buyer.settings", return_value={"connected": True}),
            patch("vinted_buyer.check_saved_connection", side_effect=error),
            patch(
                "vinted_session_worker.note_ambiguous_failure",
                side_effect=RuntimeError("private-scheduling-failure"),
            ),
            self.assertLogs("vinted_connection_check", level="INFO") as logs,
        ):
            vinted_connection_check.run_once()
        output = " ".join(logs.output)
        self.assertIn("stage=renewal reason=renewal_failed http=400", output)
        self.assertIn("scheduling unavailable: RuntimeError", output)
        self.assertNotIn("private-", output)


class BootstrapRotationTests(buying_tests.SessionRotationFixture, unittest.TestCase):
    """Use native response cookie handling; no real network or purchase calls."""

    transport_response = buying_tests.NativeCookieTransportTests.transport_response
    token_headers = buying_tests.NativeCookieTransportTests.token_headers

    def setUp(self):
        native = patch.object(vinted_buyer, "BrowserSession", requests.Session)
        native.start()
        self.addCleanup(native.stop)
        self.real_client = vinted_buyer.Client
        self.clients = []
        self.calls = []
        self.responses = []
        super().setUp()

    def client(self, *args, **kwargs):
        client = self.real_client(*args, **kwargs)
        client.session.close = Mock(wraps=client.session.close)
        self.clients.append(client)
        return client

    def native(self, adapter, prepared, **kwargs):
        self.calls.append(
            (prepared.method, prepared.url.removeprefix(vinted_buyer.BASE))
        )
        response = self.responses.pop(0)
        self.assertEqual(self.calls[-1], response["request"])
        client = self.clients[-1]
        self.assertIsNotNone(client._loaded_session_reference)
        self.assertIsNone(client._verified_session)
        if response.get("before"):
            response["before"]()
        if response.get("error"):
            raise response["error"]
        return self.transport_response(
            prepared,
            response.get("data"),
            status=response.get("status", 200),
            text=response.get("text", ""),
            cookies=response.get("cookies", ()),
        )

    def bootstrap(self, *, rotate=False, csrf="new-csrf-token-0123456789", **extra):
        return {
            "request": ("GET", "/"),
            "text": f'<meta name="csrf-token" content="{csrf}">',
            "cookies": (
                self.token_headers(self.new_access, self.new_refresh) if rotate else ()
            ),
            **extra,
        }

    def identity(self, *, user=99, rotate=False, **extra):
        return {
            "request": ("GET", "/api/v2/users/current"),
            "data": {"user": {"id": user}},
            "cookies": (
                self.token_headers(self.new_access, self.new_refresh) if rotate else ()
            ),
            **extra,
        }

    def controls(self):
        with closing(connection()) as conn:
            row = dict(conn.execute("SELECT * FROM vinted_buyer").fetchone())
            row.pop("session")
            return (
                row,
                tuple(conn.execute("SELECT * FROM vinted_buyer_access").fetchone()),
                [
                    tuple(row)
                    for row in conn.execute("SELECT * FROM vinted_search_budgets")
                ],
                [
                    tuple(row)
                    for row in conn.execute("SELECT * FROM vinted_buy_attempts")
                ],
            )

    def diagnose(self):
        with (
            patch.object(vinted_buyer, "Client", side_effect=self.client),
            patch.object(
                requests.adapters.HTTPAdapter,
                "send",
                autospec=True,
                side_effect=self.native,
            ),
            patch.object(vinted_buyer, "renew_saved_client") as renew,
            self.assertLogs("vinted_connection_check", level="INFO") as logs,
        ):
            vinted_connection_check.diagnose_bootstrap()
        renew.assert_not_called()
        self.assertEqual(self.responses, [])
        self.clients[-1].session.close.assert_called_once()
        output = " ".join(logs.output)
        for secret in (
            self.old_access,
            self.old_refresh,
            self.new_access,
            self.new_refresh,
        ):
            self.assertNotIn(secret, output)
        return output

    def test_verified_identity_rotation_persists_without_changing_controls(self):
        before = self.controls()
        original_seal = self.saved()[0]["session"]
        self.responses = [self.bootstrap(), self.identity(rotate=True)]
        output = self.diagnose()
        row, saved = self.saved()
        self.assertNotEqual(row["session"], original_seal)
        self.assertEqual(saved["cookies"]["access_token_web"], self.new_access)
        self.assertEqual(saved["cookies"]["refresh_token_web"], self.new_refresh)
        self.assertEqual(saved["csrf"], "new-csrf-token-0123456789")
        self.assertTrue(saved["cookie_records"])
        self.assertEqual(self.controls(), before)
        self.assertIn("matched_account=True", output)
        self.assertEqual(self.calls, [("GET", "/"), ("GET", "/api/v2/users/current")])

    def test_accepted_homepage_rotation_waits_for_same_buyer_identity(self):
        original_seal = self.saved()[0]["session"]
        self.responses = [
            self.bootstrap(rotate=True),
            self.identity(
                before=lambda: self.assertEqual(
                    self.saved()[0]["session"], original_seal
                )
            ),
        ]
        self.diagnose()
        self.assertEqual(
            self.saved()[1]["cookies"]["refresh_token_web"], self.new_refresh
        )

    def test_unchanged_responses_keep_exact_seal_and_controls(self):
        before = self.saved()[0]
        self.responses = [
            self.bootstrap(csrf="old-csrf-token-0123456789"),
            self.identity(),
        ]
        self.assertIn("matched_account=True", self.diagnose())
        self.assertEqual(self.saved()[0], before)

    def test_wrong_account_cannot_adopt_identity_tokens_or_save_bootstrap(self):
        before = self.saved()[0]
        self.responses = [
            self.bootstrap(),
            self.identity(user=100, rotate=True),
        ]
        output = self.diagnose()
        self.assertEqual(self.saved()[0], before)
        self.assertEqual(
            self.clients[-1].exported()["cookies"]["refresh_token_web"],
            self.old_refresh,
        )
        self.assertIn("stage=identity reason=account_changed", output)
        self.assertNotIn("matched_account=True", output)

    def test_malformed_identity_never_adopts_or_persists_response_tokens(self):
        for data in (
            {},
            {"user": []},
            {"user": "private"},
            {"user": {"id": "invalid"}},
        ):
            with self.subTest(data=data):
                before = self.saved()[0]
                self.responses = [
                    self.bootstrap(),
                    self.identity(rotate=True, data=data),
                ]
                output = self.diagnose()
                self.assertEqual(self.saved()[0], before)
                self.assertEqual(
                    self.clients[-1].exported()["cookies"]["refresh_token_web"],
                    self.old_refresh,
                )
                self.assertIn("stage=identity reason=not_confirmed", output)
                self.assertNotIn("matched_account=True", output)

    def test_rejected_homepage_does_not_save_or_contact_identity(self):
        before = self.saved()[0]
        self.responses = [self.bootstrap(rotate=True, status=503)]
        output = self.diagnose()
        self.assertEqual(self.saved()[0], before)
        self.assertEqual(
            self.clients[-1].exported()["cookies"]["refresh_token_web"],
            self.old_refresh,
        )
        self.assertEqual(self.calls, [("GET", "/")])
        self.assertIn("stage=homepage", output)
        self.assertNotIn("matched_account=True", output)

    def test_rejected_identity_does_not_save_accepted_homepage_rotation(self):
        before = self.saved()[0]
        self.responses = [
            self.bootstrap(rotate=True),
            self.identity(status=401, data={"message": "private refusal"}),
        ]
        output = self.diagnose()
        self.assertEqual(self.saved()[0], before)
        self.assertIn("stage=identity reason=credentials http=401", output)
        self.assertNotIn("private refusal", output)

    def test_missing_homepage_csrf_does_not_save_rotations(self):
        before = self.saved()[0]
        self.responses = [self.bootstrap(rotate=True, text="No security token")]
        output = self.diagnose()
        self.assertEqual(self.saved()[0], before)
        self.assertEqual(self.calls, [("GET", "/")])
        self.assertIn("stage=homepage reason=csrf", output)

    def test_identity_network_failure_does_not_save_homepage_rotation(self):
        before = self.saved()[0]
        self.responses = [
            self.bootstrap(rotate=True),
            self.identity(error=requests.ConnectionError("private network failure")),
        ]
        output = self.diagnose()
        self.assertEqual(self.saved()[0], before)
        self.assertIn("stage=identity reason=network", output)
        self.assertNotIn("private network failure", output)

    def test_replacement_during_rotating_identity_is_never_overwritten(self):
        self.assert_replacement(rotate=True)

    def test_replacement_during_unchanged_identity_is_not_reported_verified(self):
        self.assert_replacement(rotate=False)

    def assert_replacement(self, *, rotate):
        replacement = vinted_buyer.encrypt(
            {
                "cookies": {"access_token_web": "private-replacement"},
                "csrf": "private-csrf",
            }
        )

        def replace():
            with closing(connection()) as conn, conn:
                conn.execute(
                    "UPDATE vinted_buyer SET session=? WHERE id=1", (replacement,)
                )

        before_controls = self.controls()
        self.responses = [
            self.bootstrap(csrf="old-csrf-token-0123456789"),
            self.identity(rotate=rotate, before=replace),
        ]
        output = self.diagnose()
        self.assertEqual(self.saved()[0]["session"], replacement)
        self.assertEqual(self.controls(), before_controls)
        self.assertIn("stage=saved_session reason=saved_session", output)
        self.assertNotIn("matched_account=True", output)
        self.assertNotIn("private-replacement", output)


class ProcessShutdownTests(unittest.TestCase):
    def test_stuck_child_is_killed_after_bounded_grace(self):
        from process_watchdog import stop_process

        process = Mock()
        process.is_alive.side_effect = [True, True, False]
        self.assertTrue(stop_process(process, name="telegram", timeout=1))
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.join.call_args_list[0].kwargs, {"timeout": 1})
        self.assertEqual(process.join.call_args_list[1].kwargs, {"timeout": 2})

    def test_exited_child_is_reaped_without_termination(self):
        from process_watchdog import stop_process

        process = Mock()
        process.is_alive.return_value = False
        self.assertTrue(stop_process(process, name="telegram"))
        process.terminate.assert_not_called()
        process.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
