"""Read-only production diagnosis is opt-in and cannot repeat after restart."""

import os
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

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
        with patch.dict(
            os.environ,
            {
                "MSJ_BUYER_CHECK_ON_START": "listing-probe",
                "MSJ_LISTING_CHECK_ON_START": "1",
            },
        ), patch("vinted_buyer.settings", return_value={"connected": False}), patch(
            "vinted_gallery.fetch_listing",
            return_value={
                "state": "ready",
                "description": "Private seller text",
                "photos": [],
            },
        ) as fetch, self.assertLogs(
            "vinted_connection_check", level="INFO"
        ) as logs:
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
        with patch.dict(
            os.environ,
            {
                "MSJ_BUYER_CHECK_ON_START": "no-listing",
                "MSJ_LISTING_CHECK_ON_START": "0",
            },
        ), patch("vinted_buyer.settings", return_value={"connected": False}), patch(
            "vinted_gallery.fetch_listing"
        ) as fetch:
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
            with patch.dict(
                os.environ,
                {
                    "MSJ_BUYER_CHECK_ON_START": f"bootstrap-{index}",
                    "MSJ_BUYER_BOOTSTRAP_CHECK_ON_START": "1",
                },
            ), patch("vinted_buyer.settings", return_value={"connected": True}), patch(
                "vinted_buyer.check_saved_connection", side_effect=error
            ), patch(
                "vinted_connection_check.diagnose_bootstrap"
            ) as probe:
                vinted_connection_check.run_once()
                vinted_connection_check.run_once()
            self.assertEqual(probe.call_count, int(expected))

    def test_bootstrap_read_closes_client_without_changing_saved_permissions(self):
        saved = {"csrf": "private-old-csrf", "cookies": {"access_token_web": "private"}}
        with closing(connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?,user_id='99',verified_at=100,enabled=1",
                (vinted_buyer.encrypt(saved),),
            )
            before = tuple(conn.execute("SELECT * FROM vinted_buyer").fetchone())
        client = Mock(csrf=saved["csrf"])
        client.homepage.side_effect = lambda: setattr(
            client, "csrf", "private-new-csrf"
        )
        client.identity.return_value = ("99", "private-owner")
        with patch("vinted_buyer.Client", return_value=client), self.assertLogs(
            "vinted_connection_check", level="INFO"
        ) as logs:
            vinted_connection_check.diagnose_bootstrap()
        client.homepage.assert_called_once()
        client.identity.assert_called_once()
        client.request.assert_not_called()
        client.session.close.assert_called_once()
        self.assertIn("matched_account=True", " ".join(logs.output))
        self.assertNotIn("private", " ".join(logs.output))
        with closing(connection()) as conn:
            self.assertEqual(
                before, tuple(conn.execute("SELECT * FROM vinted_buyer").fetchone())
            )

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
            with patch.dict(os.environ, {"MSJ_BUYER_CHECK_ON_START": value}), patch(
                "vinted_buyer.check_saved_connection"
            ) as check:
                vinted_connection_check.run_once()
                check.assert_not_called()

    def test_saved_connection_checked_once_across_restarts(self):
        with patch.dict(os.environ, {"MSJ_BUYER_CHECK_ON_START": "release-1"}), patch(
            "vinted_buyer.settings", return_value={"connected": True}
        ), patch("vinted_buyer.check_saved_connection") as check:
            vinted_connection_check.run_once()
            vinted_connection_check.run_once()
            self.assertEqual(check.call_count, 1)

    def test_failure_is_sanitised_and_not_repeated(self):
        error = vinted_buyer.BuyerError(
            "token-value-must-not-be-logged", 307, reason="redirect", stage="homepage"
        )
        with patch.dict(os.environ, {"MSJ_BUYER_CHECK_ON_START": "release-2"}), patch(
            "vinted_buyer.settings", return_value={"connected": True}
        ), patch(
            "vinted_buyer.check_saved_connection", side_effect=error
        ) as check, self.assertLogs(
            "vinted_connection_check", level="INFO"
        ) as log:
            vinted_connection_check.run_once()
            vinted_connection_check.run_once()
        self.assertEqual(check.call_count, 1)
        self.assertIn("http=307", " ".join(log.output))
        self.assertNotIn("token-value", " ".join(log.output))

    def test_no_account_does_not_contact_vinted(self):
        with patch.dict(os.environ, {"MSJ_BUYER_CHECK_ON_START": "release-3"}), patch(
            "vinted_buyer.settings", return_value={"connected": False}
        ), patch("vinted_buyer.check_saved_connection") as check:
            vinted_connection_check.run_once()
            check.assert_not_called()

    def test_fresh_ambiguous_renewal_uses_only_its_checked_session_cooldown(self):
        error = vinted_buyer.BuyerError(
            "private-failure", 400, reason="renewal_failed", stage="renewal"
        )
        error.session_fingerprint = "a" * 64
        with patch.dict(
            os.environ, {"MSJ_BUYER_CHECK_ON_START": "ambiguous-renewal-check"}
        ), patch("vinted_buyer.settings", return_value={"connected": True}), patch(
            "vinted_buyer.check_saved_connection", side_effect=error
        ) as check, patch(
            "vinted_session_worker.note_ambiguous_failure", return_value=True
        ) as defer, self.assertLogs(
            "vinted_connection_check", level="INFO"
        ) as logs:
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
        with patch.dict(
            os.environ, {"MSJ_BUYER_CHECK_ON_START": "schedule-error-check"}
        ), patch("vinted_buyer.settings", return_value={"connected": True}), patch(
            "vinted_buyer.check_saved_connection", side_effect=error
        ), patch(
            "vinted_session_worker.note_ambiguous_failure",
            side_effect=RuntimeError("private-scheduling-failure"),
        ), self.assertLogs(
            "vinted_connection_check", level="INFO"
        ) as logs:
            vinted_connection_check.run_once()
        output = " ".join(logs.output)
        self.assertIn("stage=renewal reason=renewal_failed http=400", output)
        self.assertIn("scheduling unavailable: RuntimeError", output)
        self.assertNotIn("private-", output)


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
