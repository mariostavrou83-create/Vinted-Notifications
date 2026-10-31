"""One read-only security-state repair keeps accepted auth and crash safety."""

import time
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

import requests

import search_settings
import test_vinted_datadome_cookie_scope as cookie_fixture
import test_vinted_network as fixture
import vinted_buyer as buyer
import vinted_security_cookie_repair as repair

ACCESS = "fresh-accepted-access-token-0123456789"


class SecurityRepairTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.NetworkTests("test_payment_challenge_is_never_solved_or_submitted_twice")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        seed = self.fixture.client(fixture.SAVED)
        old = cookie_fixture.cookie()
        old.value = "old-parent-security-cookie"
        seed.session.cookies.set_cookie(old)
        self.saved = seed.exported()
        self.fixture.save_session(self.saved)
        self.client = self.fixture.client(self.saved)
        self.now = time.time()
        self.request = {"version": "offline-v1", "expires": self.now + 600}
        self.calls = []
        self.home_cookie = cookie_fixture.cookie()
        self.final_user = 99
        self.final_status = 200
        self.before_final = None
        self.identity_count = 0

    def dd(self, saved):
        return [value for value in saved["cookie_records"] if value["name"] == "datadome"]

    def send(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("timeout")))
        if url == buyer.BASE + "/":
            result = fixture.response(text='<meta name="csrf-token" content="fresh-homepage-csrf-token-0123456789">')
            result.url = url
            home_cookies = self.home_cookie if isinstance(self.home_cookie, list) else [self.home_cookie]
            for value in home_cookies:
                if value is not None:
                    result.cookies.set_cookie(value)
            result.cookies.set("access_token_web", ACCESS, domain="www.vinted.co.uk", secure=True)
        else:
            self.identity_count += 1
            if self.identity_count == 2 and self.before_final:
                self.before_final()
            result = fixture.response(
                status=self.final_status if self.identity_count == 2 else 200,
                data=fixture.CHALLENGE if self.identity_count == 2 and self.final_status != 200
                else {"user": {"id": self.final_user if self.identity_count == 2 else 99}},
            )
            result.url = url
        for value in result.cookies:
            self.client.session.cookies.set_cookie(value)
        return result

    def run_repair(self):
        with (
            patch.object(buyer, "Client", return_value=self.client),
            patch.object(self.client.session, "request", side_effect=self.send),
            patch("vinted_captcha.solve_datadome") as solver,
        ):
            result = repair.run_once(request=self.request, now=self.now)
        solver.assert_not_called()
        return result

    def test_fresh_parent_authority_and_expected_identity_repair_once_without_payment(self):
        self.assertEqual(self.run_repair(), "applied")
        saved = self.fixture.stored_session()
        self.assertEqual(len(self.dd(saved)), 1)
        self.assertEqual(self.dd(saved)[0]["domain"], ".vinted.co.uk")
        self.assertEqual(saved["cookies"]["access_token_web"], ACCESS)
        self.assertEqual([call[0] for call in self.calls], ["GET"] * 3)
        self.assertTrue(all(call[2] == (2, 3) for call in self.calls))
        before = list(self.calls)
        self.assertEqual(self.run_repair(), "already_attempted")
        self.assertEqual(self.calls, before)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(conn.execute("SELECT enabled FROM vinted_buyer WHERE id=1").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM vinted_buy_attempts").fetchone()[0], 0)

    def test_no_fresh_cookie_restores_only_security_state_and_keeps_auth_rotation(self):
        self.home_cookie = None
        self.assertEqual(self.run_repair(), "fresh_authority_missing")
        saved = self.fixture.stored_session()
        self.assertEqual(self.dd(saved), self.dd(self.saved))
        self.assertEqual(saved["cookies"]["access_token_web"], ACCESS)
        self.assertIn("fresh-homepage", saved["csrf"])

    def test_narrow_www_authority_cannot_remove_parent_api_scope(self):
        self.home_cookie = cookie_fixture.cookie("www.vinted.co.uk", host_only=True)
        self.assertEqual(self.run_repair(), "fresh_authority_missing")
        saved = self.fixture.stored_session()
        self.assertEqual(self.dd(saved), self.dd(self.saved))
        self.assertEqual(saved["cookies"]["access_token_web"], ACCESS)

    def test_multiple_new_cookie_scopes_restore_original_security_state(self):
        self.home_cookie = [cookie_fixture.cookie(), cookie_fixture.cookie("www.vinted.co.uk")]
        self.assertEqual(self.run_repair(), "fresh_authority_missing")
        saved = self.fixture.stored_session()
        self.assertEqual(self.dd(saved), self.dd(self.saved))
        self.assertEqual(saved["cookies"]["access_token_web"], ACCESS)

    def test_unrelated_cookie_path_is_preserved_during_success(self):
        self.client.session.cookies.set("datadome", "unrelated-private-security-cookie", domain="www.vinted.co.uk", path="/other", secure=True)
        self.saved = self.client.exported()
        self.fixture.save_session(self.saved)
        self.assertEqual(self.run_repair(), "applied")
        saved = self.fixture.stored_session()
        self.assertEqual(len(self.dd(saved)), 2)
        self.assertEqual(next(value for value in self.dd(saved) if value["path"] == "/other"), next(value for value in self.dd(self.saved) if value["path"] == "/other"))

    def test_accepted_homepage_snapshot_is_crash_safe_before_final_identity(self):
        def interrupt():
            saved = self.fixture.stored_session()
            self.assertEqual(self.dd(saved), self.dd(self.saved))
            self.assertEqual(saved["cookies"]["access_token_web"], ACCESS)
            raise KeyboardInterrupt()

        self.before_final = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_repair()
        self.assertEqual(self.dd(self.fixture.stored_session()), self.dd(self.saved))
        self.assertEqual(self.run_repair(), "already_attempted")

    def test_failed_second_read_keeps_accepted_auth_and_original_security(self):
        self.before_final = Mock(side_effect=requests.ConnectionError("offline read failure"))
        self.assertEqual(self.run_repair(), "read_failed")
        saved = self.fixture.stored_session()
        self.assertEqual(self.dd(saved), self.dd(self.saved))
        self.assertEqual(saved["cookies"]["access_token_web"], ACCESS)

    def test_changed_account_does_not_keep_tentative_security_state(self):
        self.final_user = 100
        self.assertEqual(self.run_repair(), "account_changed")
        saved = self.fixture.stored_session()
        self.assertEqual(self.dd(saved), self.dd(self.saved))
        self.assertEqual(saved["cookies"]["access_token_web"], ACCESS)

    def test_security_refusal_never_solves_replays_or_loses_accepted_auth(self):
        self.final_status = 403
        self.assertEqual(self.run_repair(), "security_challenge")
        saved = self.fixture.stored_session()
        self.assertEqual(self.dd(saved), self.dd(self.saved))
        self.assertEqual(saved["cookies"]["access_token_web"], ACCESS)
        self.assertEqual(len(self.calls), 3)

    def test_newer_login_wins_cas_without_old_session_restore(self):
        newer = {"csrf": "new-login-csrf-0123456789", "cookies": {
            **fixture.SAVED["cookies"], "access_token_web": "newer-login-access-token-0123456789"
        }}
        self.before_final = lambda: self.fixture.save_session(newer)
        self.assertEqual(self.run_repair(), "connection_replaced")
        self.assertEqual(self.fixture.stored_session(), newer)

    def test_owner_arriving_during_homepage_prevents_next_get_and_keeps_auth(self):
        checks = 0

        def yield_owner():
            nonlocal checks
            checks += 1
            if checks >= 4:
                raise buyer.BuyerError("Owner waiting.", reason="busy")

        with patch.object(repair, "yield_to_purchase", side_effect=yield_owner):
            self.assertEqual(self.run_repair(), "owner_waiting")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.dd(self.fixture.stored_session()), self.dd(self.saved))
        self.assertEqual(self.fixture.stored_session()["cookies"]["access_token_web"], ACCESS)

    def test_disabled_expired_or_unbounded_configuration_does_not_read_marketplace(self):
        for value in ({"version": "bad space", "expires": self.now + 10},
                      {"version": "v1", "expires": self.now - 1},
                      {"version": "v1", "expires": self.now + 3601}):
            self.assertEqual(repair.run_once(request=value, now=self.now), "invalid_request")
        self.assertFalse(self.calls)

    def test_log_or_close_failure_cannot_relabel_an_applied_repair(self):
        with (
            patch.object(repair.logger, "info", side_effect=RuntimeError("offline logging failure")),
            patch.object(self.client.session, "close", side_effect=RuntimeError("offline cleanup failure")),
        ):
            self.assertEqual(self.run_repair(), "applied")
        with closing(search_settings.connection()) as conn:
            state = conn.execute("SELECT value FROM parameters WHERE key LIKE 'vinted_security_cookie_repair_%'").fetchone()[0]
        self.assertEqual(state, "applied")
        self.assertEqual(len(self.dd(self.fixture.stored_session())), 1)

    def test_later_owner_priority_cannot_overwrite_completed_marker(self):
        self.assertEqual(self.run_repair(), "applied")
        calls = list(self.calls)
        with patch.object(buyer, "exclusive", side_effect=buyer.BuyerError("Owner waiting.", reason="busy")):
            self.assertEqual(repair.run_once(request=self.request, now=self.now), "owner_waiting")
        with closing(search_settings.connection()) as conn:
            state = conn.execute("SELECT value FROM parameters WHERE key LIKE 'vinted_security_cookie_repair_%'").fetchone()[0]
        self.assertEqual(state, "applied")
        self.assertEqual(self.calls, calls)


if __name__ == "__main__":
    unittest.main()
