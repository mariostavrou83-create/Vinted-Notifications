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
