"""Private diagnostics and provider input, without live requests or paid tasks."""

import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import ExitStack, closing, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from test_search_controls import DatabaseFixture
from test_supabase_backend import OWNER, SETTINGS

import search_settings
import supabase_backend
import vinted_buyer as real_buyer
import vinted_network_check as check
from vinted_http import BROWSER_USER_AGENT
from web_ui_plugin.web_ui import create_app

PROXY = "http://offline-owner:offline-password@proxy.example.com:12323"
KEY = "CAP-offline-private-key"
EXIT = {"ip": "8.8.8.8", "country_code": "GB"}


def response(body=None, *, status=200, raw=None):
    result = Mock(status_code=status)
    result.iter_content.return_value = [
        json.dumps(body).encode() if raw is None else raw
    ]
    return result


def trace(body=EXIT, *, status=200):
    return response(
        status=status,
        raw=(
            "ip="
            + body["ip"]
            + "\nloc="
            + body["country_code"]
            + "\nvisit_scheme=https\nwarp=off\n"
        ).encode(),
    )


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "private.sqlite3"

        def connection():
            return sqlite3.connect(path)

        self.connection = connection
        with closing(connection()) as conn, conn:
            conn.executescript(
                "CREATE TABLE vinted_buyer(id INTEGER PRIMARY KEY,enabled INTEGER);"
                "INSERT INTO vinted_buyer VALUES(1,1);"
                "CREATE TABLE parameters(key TEXT PRIMARY KEY,value TEXT);"
                "INSERT INTO parameters VALUES('existing_setting','unchanged');"
            )
        self.config = {"proxy": PROXY, "api_key": KEY, "enabled": True}
        self.client = SimpleNamespace(
            network=self.config,
            solver_attempted=False,
            solver_solved=False,
            session=SimpleNamespace(
                proxies={"http": PROXY, "https": PROXY},
                headers={"User-Agent": BROWSER_USER_AGENT},
                trust_env=False,
                close=Mock(),
            ),
        )
        self.buyer = SimpleNamespace(
            exclusive=nullcontext,
            connection=connection,
            network_configuration=Mock(return_value=self.config),
            connected_client=Mock(return_value=self.client),
            BuyerError=real_buyer.BuyerError,
            AUTH_REASONS=real_buyer.AUTH_REASONS,
            AUTH_STAGES=real_buyer.AUTH_STAGES,
        )
        self.probes = []

        def browser():
            probe = Mock()
            probe.cookies = {}
            probe.headers = {"User-Agent": BROWSER_USER_AGENT}
            probe.proxies = {}
            probe.get.return_value = trace()
            self.probes.append(probe)
            return probe

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.browser = self.stack.enter_context(
            patch.object(check, "BrowserSession", side_effect=browser)
        )
        self.solver = Mock()
        self.balance_response = response(
            {"errorId": 0, "balance": 10, "packages": [KEY, PROXY]}
        )
        self.solver.post.return_value = self.balance_response
        self.solver_factory = self.stack.enter_context(
            patch.object(check.requests, "Session", return_value=self.solver)
        )
        self.stack.enter_context(patch.object(check, "version", return_value="0.16.3"))

    def run_check(self):
        with self.assertLogs(check.logger, level="INFO") as logs:
            result = check.check_connection(buyer=self.buyer)
        self.assertNotIn(KEY, str(logs.output) + json.dumps(result))
        self.assertNotIn(PROXY, str(logs.output) + json.dumps(result))
        self.assertNotIn(EXIT["ip"], str(logs.output) + json.dumps(result))
        self.assertFalse(result["checkout_created"])
        self.assertFalse(result["payment_submitted"])
        with closing(self.connection()) as conn:
            self.assertEqual(
                conn.execute("SELECT enabled FROM vinted_buyer").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM parameters WHERE key='existing_setting'"
                ).fetchone()[0],
                "unchanged",
            )
        return result

    def test_fresh_anonymous_proxy_connections_and_read_only_balance(self):
        result = self.run_check()
        self.assertEqual(result["outcome"], "verified")
        self.assertEqual(result["connections_checked"], 3)
        self.assertEqual(result["capsolver_balance_usd"], "10.00")
        self.assertTrue(result["same_buyer_account"])
        self.assertEqual(result["supported_challenge_recovery"], "not_exercised")
        self.assertEqual(len({id(p) for p in self.probes}), 3)
        for probe in self.probes:
            self.assertEqual(probe.cookies, {})
            self.assertEqual(probe.proxies, {"http": PROXY, "https": PROXY})
            self.assertFalse(probe.trust_env)
            self.assertTrue(probe.verify)
            probe.get.assert_called_once_with(
                check.EXIT_URL, timeout=(2, 4), allow_redirects=False, stream=True
            )
            probe.close.assert_called_once()
            probe.get.return_value.close.assert_called_once()
        self.solver.post.assert_called_once_with(
            check.BALANCE_URL,
            json={"clientKey": KEY},
            timeout=(2, 4),
            allow_redirects=False,
            stream=True,
        )
        self.assertFalse(self.solver.trust_env)
        self.assertTrue(self.solver.verify)
        self.balance_response.close.assert_called_once()
        self.client.session.close.assert_called_once()

    def test_missing_configuration_keeps_autobuy_off_without_any_request(self):
        self.buyer.network_configuration.return_value = {
            "proxy": "",
            "api_key": "",
            "enabled": False,
        }
        result = self.run_check()
        self.assertEqual(result["outcome"], "needs_configuration")
        self.browser.assert_not_called()
        self.solver_factory.assert_not_called()
        self.buyer.connected_client.assert_not_called()

    def test_wrong_country_and_rotating_exit_stop_before_solver_or_account(self):
        for bodies, stage in (
            ([{**EXIT, "country_code": "US"}], "proxy_country"),
            ([EXIT, {**EXIT, "ip": "1.1.1.1"}, EXIT], "proxy_rotation"),
        ):
            with self.subTest(stage=stage):
                self.browser.side_effect = [
                    Mock(
                        cookies={},
                        headers={},
                        proxies={},
                        get=Mock(return_value=trace(b)),
                    )
                    for b in bodies
                ]
                result = self.run_check()
                self.assertEqual(result["stage"], stage)
                self.assertEqual(result["outcome"], "unverified")
                self.solver_factory.assert_not_called()
                self.buyer.connected_client.assert_not_called()

    def test_proxy_failure_cannot_fall_back_directly_or_leak_exception(self):
        probe = Mock(cookies={}, headers={}, proxies={})
        probe.get.side_effect = requests.exceptions.ProxyError(PROXY)
        self.browser.side_effect = None
        self.browser.return_value = probe
        result = self.run_check()
        self.assertEqual(result["stage"], "proxy_exit")
        self.assertEqual(self.browser.call_count, 1)
        self.solver_factory.assert_not_called()
        self.buyer.connected_client.assert_not_called()

    def test_redirect_oversized_and_malformed_proxy_responses_are_closed(self):
        for candidate in (
            response(EXIT, status=302),
            response(raw=b"x" * (check.MAX_RESPONSE_BYTES + 1)),
            response(raw=b"not json"),
            response(raw=b"[" * 1200 + b"0" + b"]" * 1200),
            response({"ip": {}, "country_code": "GB"}),
            trace({"ip": "127.0.0.1", "country_code": "GB"}),
            response([EXIT]),
        ):
            with self.subTest(response=candidate):
                self.browser.side_effect = None
                self.browser.return_value = Mock(
                    cookies={}, headers={}, proxies={}, get=Mock(return_value=candidate)
                )
                result = self.run_check()
                self.assertEqual(result["outcome"], "unverified")
                candidate.close.assert_called_once()
                self.solver_factory.assert_not_called()

    def test_trace_duplicates_and_alternate_routes_are_rejected(self):
        for raw in (
            b"ip=8.8.8.8\nip=1.1.1.1\nloc=GB\nvisit_scheme=https\nwarp=off\n",
            b"ip=8.8.8.8\nloc=GB\nvisit_scheme=http\nwarp=off\n",
            b"ip=8.8.8.8\nloc=GB\nvisit_scheme=https\nwarp=on\n",
        ):
            self.browser.side_effect = None
            self.browser.return_value = Mock(
                cookies={},
                headers={},
                proxies={},
                get=Mock(return_value=response(raw=raw)),
            )
            self.assertEqual(self.run_check()["outcome"], "unverified")
            self.solver_factory.assert_not_called()

    def test_documented_trace_without_optional_warp_field_is_accepted(self):
        candidate = response(raw=b"ip=8.8.8.8\nloc=GB\nvisit_scheme=https\n")
        self.assertEqual(
            check.response_trace(candidate, deadline=time.monotonic() + 25), EXIT
        )
        candidate.close.assert_called_once()

    def test_slow_stream_closes_and_never_reaches_buyer_or_solver(self):
        with patch.object(check.time, "monotonic", side_effect=[0, 0, 26]):
            self.assertEqual(self.run_check()["stage"], "proxy_exit_timeout")
        self.solver_factory.assert_not_called()
        self.buyer.connected_client.assert_not_called()

    def test_invalid_key_zero_balance_and_invalid_balance_stop_before_buyer(self):
        for body, stage in (
            ({"errorId": 1, "errorDescription": KEY}, "capsolver_key"),
            ({"errorId": 0, "balance": 0}, "capsolver_credit"),
            ({"errorId": 0, "balance": True}, "capsolver_balance"),
            ({"errorId": 0, "balance": "NaN"}, "capsolver_balance"),
            ({"errorId": 0, "balance": -1}, "capsolver_balance"),
        ):
            with self.subTest(stage=stage):
                self.solver.post.return_value = response(body)
                self.assertEqual(self.run_check()["stage"], stage)
                self.buyer.connected_client.assert_not_called()

    def test_changed_exit_after_restart_is_rejected_without_replacing_baseline(self):
        baseline = {
            "endpoint_digest": check.digest(PROXY),
            "exit_digest": check.digest("1.1.1.1"),
            "run_id": "earlier-service-start",
        }
        with closing(self.connection()) as conn, conn:
            conn.execute(
                "INSERT INTO parameters VALUES (?,?)",
                (check.MARKER, json.dumps(baseline)),
            )
        result = self.run_check()
        self.assertEqual(result["stage"], "proxy_exit_changed")
        self.assertIs(result["across_restart_match"], False)
        with closing(self.connection()) as conn:
            self.assertEqual(
                json.loads(
                    conn.execute(
                        "SELECT value FROM parameters WHERE key=?", (check.MARKER,)
                    ).fetchone()[0]
                ),
                baseline,
            )
        self.solver_factory.assert_not_called()

    def test_successful_restart_observation_is_distinct_from_first_observation(self):
        first = self.run_check()
        self.assertIsNone(first["across_restart_match"])
        with patch.object(check, "RUN_ID", "new-service-start"):
            second = self.run_check()
        self.assertIs(second["across_restart_match"], True)

    def test_failed_or_changed_buyer_account_never_reports_verified(self):
        self.buyer.connected_client.side_effect = real_buyer.BuyerError(
            PROXY, 403, reason="security_challenge", stage="identity"
        )
        result = self.run_check()
        self.assertEqual(result["outcome"], "unverified")
        self.assertEqual(result["reason"], "security_challenge")
        self.assertNotIn("same_buyer_account", result)

    def test_account_transport_must_use_same_proxy_and_browser_identity(self):
        for field in ("proxy", "agent", "environment"):
            with self.subTest(field=field):
                self.client.session.proxies = {"http": PROXY, "https": PROXY}
                self.client.session.headers["User-Agent"] = BROWSER_USER_AGENT
                self.client.session.trust_env = False
                if field == "proxy":
                    self.client.session.proxies["https"] = (
                        "http://other.example.com:12323"
                    )
                elif field == "agent":
                    self.client.session.headers["User-Agent"] = "Different Browser"
                else:
                    self.client.session.trust_env = True
                self.assertEqual(
                    self.run_check()["stage"], "buyer_connection_alignment"
                )

    def test_solver_attempt_alone_is_not_successful_challenge_handling(self):
        self.client.solver_attempted = True
        result = self.run_check()
        self.assertEqual(result["supported_challenge_recovery"], "attempted_unverified")
        self.assertIn("not established", check.summary(result))
        self.client.solver_solved = True
        result = self.run_check()
        self.assertEqual(
            result["supported_challenge_recovery"], "accepted_on_account_read"
        )

    def test_browser_transport_version_mismatch_makes_no_request(self):
        with patch.object(check, "version", return_value="0.15.0"):
            self.assertEqual(self.run_check()["stage"], "browser_transport")
        self.browser.assert_not_called()
        self.solver_factory.assert_not_called()


class PrivateRouteTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.provider = Mock(config=supabase_backend.Config.from_environment(SETTINGS))
        self.provider.sign_in.return_value = {
            "access_token": "offline-access",
            "refresh_token": "offline-refresh",
            "expires_at": time.time() + 3600,
        }
        self.provider.verify_owner.return_value = OWNER
        with patch.dict("os.environ", SETTINGS), patch(
            "supabase_backend.Client", return_value=self.provider
        ):
            self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.web = self.app.test_client()

    def login(self):
        self.web.get("/supabase/login")
        with self.web.session_transaction() as state:
            csrf = state["csrf"]
        self.web.post(
            "/supabase/login",
            data={
                "csrf": csrf,
                "email": "offline@example.test",
                "password": "offline-password",
            },
        )
        with self.web.session_transaction() as state:
            return state["csrf"]

    def test_anonymous_and_forged_legacy_owner_cannot_execute_check(self):
        with patch.object(check, "check_connection") as execute:
            with self.web.session_transaction() as state:
                state.update(csrf="offline-csrf", owner=True)
            result = self.web.post(
                "/connections",
                data={"csrf": "offline-csrf", "action": "buyer_network_check"},
            )
            self.assertEqual(result.location, "/supabase/login")
            execute.assert_not_called()

    def test_owner_requires_csrf_and_success_redirect_does_not_repeat_check(self):
        csrf = self.login()
        with patch.object(
            check,
            "check_connection",
            return_value={
                "outcome": "needs_configuration",
                "missing": ["fixed UK proxy"],
            },
        ) as execute:
            self.assertEqual(
                self.web.post(
                    "/connections", data={"action": "buyer_network_check"}
                ).status_code,
                400,
            )
            execute.assert_not_called()
            result = self.web.post(
                "/connections", data={"csrf": csrf, "action": "buyer_network_check"}
            )
            self.assertEqual(result.status_code, 303)
            execute.assert_called_once_with()
            self.assertEqual(self.web.get(result.location).status_code, 200)
            execute.assert_called_once_with()

    def test_iproyal_copy_row_is_saved_privately_and_preserves_search_preferences(self):
        csrf = self.login()
        with closing(search_settings.connection()) as conn:
            searches = conn.execute("SELECT * FROM queries").fetchall()
            preferences = conn.execute("SELECT * FROM search_preferences").fetchall()
        result = self.web.post(
            "/connections",
            data={
                "csrf": csrf,
                "action": "buyer_network",
                "buyer_proxy_url": "proxy.example.com:12323:offline-owner:offline-password",
                "buyer_capsolver_key": KEY,
                "buyer_capsolver_enabled": "yes",
            },
        )
        self.assertEqual(result.status_code, 303)
        self.assertEqual(real_buyer.network_configuration()["proxy"], PROXY)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(conn.execute("SELECT * FROM queries").fetchall(), searches)
            self.assertEqual(
                conn.execute("SELECT * FROM search_preferences").fetchall(), preferences
            )
            self.assertNotIn(
                KEY, str(conn.execute("SELECT network FROM vinted_buyer").fetchone()[0])
            )
        page = self.web.get(result.location).get_data(as_text=True)
        self.assertNotIn(PROXY, page)
        self.assertNotIn(KEY, page)
        self.assertNotIn("offline-password", page)
        self.assertIn("configured", page)

    def test_example_brackets_are_rejected_without_losing_existing_private_settings(
        self,
    ):
        csrf = self.login()
        real_buyer.save_network(
            {
                "buyer_proxy_url": PROXY,
                "buyer_capsolver_key": KEY,
                "buyer_capsolver_enabled": "yes",
            }
        )
        original = real_buyer.network_configuration()
        result = self.web.post(
            "/connections",
            data={
                "csrf": csrf,
                "action": "buyer_network",
                "buyer_proxy_url": "http://<offline-owner>:<offline-password>@<proxy.example.com>:12323",
                "buyer_capsolver_enabled": "yes",
            },
        )
        self.assertEqual(result.status_code, 303)
        self.assertEqual(real_buyer.network_configuration(), original)
        page = self.web.get(result.location).get_data(as_text=True)
        self.assertIn("Remove angle brackets", page)
        self.assertNotIn("offline-password", page)

    def test_old_password_failure_and_unknown_renewal_do_not_prompt_reconnect(self):
        self.login()
        with closing(real_buyer.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?,verified_at=100,user_id='99'",
                (real_buyer.encrypt({"cookies": {}}),),
            )
        for reason, stage, must_reconnect in (
            ("credentials", "sign_in", False),
            ("renewal_failed", "renewal", False),
            ("refresh_rejected", "renewal", True),
        ):
            with self.subTest(reason=reason):
                real_buyer.record_auth(reason, stage, 400)
                page = self.web.get("/connections").get_data(as_text=True)
                self.assertEqual("Use the reconnect form below" in page, must_reconnect)


class StartupTests(DatabaseFixture, unittest.TestCase):
    def test_service_check_is_opt_in_and_reserved_once_before_execution(self):
        def execute(**kwargs):
            with closing(real_buyer.connection()) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM parameters WHERE key=?",
                        (check.STARTUP_MARKER,),
                    ).fetchone()[0],
                    "offline-release",
                )
            return {"outcome": "unverified", "stage": "proxy_exit"}

        with patch.dict(
            os.environ, {"MSJ_NETWORK_CHECK_ON_START": "offline-release"}
        ), patch.object(
            check, "check_connection", side_effect=execute
        ) as execute_check:
            self.assertEqual(check.run_once()["outcome"], "unverified")
            self.assertIsNone(check.run_once())
            execute_check.assert_called_once_with(buyer=real_buyer)

    def test_unset_or_invalid_startup_labels_make_no_request(self):
        for release in ("", "0", "off", "false", "bad release", "a" * 81):
            with patch.dict(
                os.environ, {"MSJ_NETWORK_CHECK_ON_START": release}
            ), patch.object(check, "check_connection") as execute:
                self.assertIsNone(check.run_once())
                execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
