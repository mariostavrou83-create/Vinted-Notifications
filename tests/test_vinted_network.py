"""Offline owner configuration, challenge replay and session isolation regressions."""

import json
import os
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from test_search_controls import DatabaseFixture
from werkzeug.security import generate_password_hash

import db
import search_settings
import vinted_buyer as buyer
from vinted_http import BROWSER_USER_AGENT
from web_ui_plugin.web_ui import create_app

PROXY = "http://network-owner:network-password@fixed-proxy.example.test:8080"
KEY = "private-capsolver-key-0123456789"
CHALLENGE = {"url": "https://geo.captcha-delivery.com/captcha/?cid=test&t=fe"}
SAVED = {
    "csrf": "saved-csrf-token-0123456789",
    "cookies": {
        "access_token_web": "saved-access-token-0123456789",
        "refresh_token_web": "saved-refresh-token-0123456789",
        "anon_id": "genuine-anon-id",
        "datadome": "previous-security-cookie",
    },
}


def response(status=200, data=None, *, text=None):
    result = requests.Response()
    result.status_code = status
    result.url = buyer.BASE
    result._content = (
        text.encode() if text is not None else json.dumps(data or {}).encode()
    )
    result._content_consumed = True
    result.close = Mock()
    return result


class NetworkTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.clients = []
        self.environment = patch.dict(
            os.environ,
            {
                "VINTED_BUYER_PROXY_URL": "",
                "CAPSOLVER_API_KEY": "",
                "VINTED_CAPSOLVER_ENABLED": "",
            },
        )
        self.environment.start()

    def tearDown(self):
        try:
            for client in self.clients:
                client.session.close()
        finally:
            self.environment.stop()
            super().tearDown()

    def client(self, saved=None):
        client = buyer.Client(saved)
        self.clients.append(client)
        return client

    def configure(self, **overrides):
        form = {
            "buyer_proxy_url": PROXY,
            "buyer_capsolver_key": KEY,
            "buyer_capsolver_enabled": "yes",
        }
        form.update(overrides)
        buyer.save_network(form)

    def stored_session(self):
        with closing(search_settings.connection()) as conn:
            row = conn.execute("SELECT session FROM vinted_buyer WHERE id=1").fetchone()
        return buyer.decrypt(row[0])

    def save_session(self, saved):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?,verified_at=1,user_id='99' WHERE id=1",
                (buyer.encrypt(saved),),
            )

    def solver(self, state="solved"):
        return patch(
            "vinted_captcha.solve_datadome",
            return_value=SimpleNamespace(state=state, cookie="new-security-cookie"),
        )

    def test_credentials_are_encrypted_and_only_presence_reaches_settings(self):
        self.configure()
        with closing(search_settings.connection()) as conn:
            encrypted = conn.execute(
                "SELECT network FROM vinted_buyer WHERE id=1"
            ).fetchone()[0]
        for secret in (PROXY, "network-password", KEY):
            self.assertNotIn(secret.encode(), encrypted)
            self.assertNotIn(secret.encode(), Path(db.DB_PATH).read_bytes())
            self.assertNotIn(secret, json.dumps(buyer.settings()))
        self.assertEqual(
            buyer.settings()["network"],
            {"browser": True, "proxy": True, "capsolver": True, "enabled": True},
        )

    def test_blank_credentials_retain_values_and_saving_disables_autobuy(self):
        self.configure()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET enabled=1,pending=? WHERE id=1",
                (buyer.encrypt({"expires": 99999999999}),),
            )
        buyer.save_network({"buyer_capsolver_enabled": "yes"})
        self.assertEqual(
            buyer.network_configuration(),
            {"proxy": PROXY, "api_key": KEY, "enabled": True},
        )
        self.assertFalse(buyer.settings()["enabled"])
        self.assertFalse(buyer.settings()["pending_code"])

    def test_invalid_proxy_is_rejected_atomically_without_echoing_credentials(self):
        self.configure()
        original = buyer.network_configuration()
        for invalid in (
            "https://network-owner:network-password@host.example.test",
            "http://network-owner:network-password@host.example.test:8080/path",
            "http://network-owner:network-password@host.example.test:8080?token=x",
            "http://network-owner:network-password@host.example.test:70000",
            "ftp://network-owner:network-password@host.example.test:8080",
            "http://network-owner:network\npassword@host.example.test:8080",
        ):
            with self.subTest(proxy=invalid):
                with self.assertRaises(buyer.BuyerError) as error:
                    buyer.save_network({"buyer_proxy_url": invalid})
                self.assertNotIn("network-password", str(error.exception))
                self.assertEqual(buyer.network_configuration(), original)

    def test_enabled_solver_requires_both_private_proxy_and_key(self):
        for fields in ({}, {"buyer_proxy_url": PROXY}, {"buyer_capsolver_key": KEY}):
            with self.subTest(fields=tuple(fields)), self.assertRaises(
                buyer.BuyerError
            ):
                buyer.save_network({**fields, "buyer_capsolver_enabled": "yes"})
            self.assertFalse(buyer.network_configuration()["enabled"])

    def test_account_proxy_is_fixed_despite_egress_environment_and_ca_is_verified(self):
        self.configure()
        with patch.dict(
            os.environ,
            {
                "HTTPS_PROXY": "http://rotating-platform-proxy.example.test:1234",
                "HTTP_PROXY": "http://other-platform-proxy.example.test:1234",
                "REQUESTS_CA_BUNDLE": "/tmp/account-network-ca.pem",
            },
        ):
            client = self.client(SAVED)
            with patch.object(
                client.session, "send", return_value=response(data={"user": {"id": 99}})
            ) as send:
                self.assertEqual(client.identity()[0], "99")
        self.assertFalse(client.session.trust_env)
        self.assertEqual(send.call_args.kwargs["proxies"]["https"], PROXY)
        self.assertEqual(send.call_args.kwargs["verify"], "/tmp/account-network-ca.pem")

    def test_genuine_anon_cookie_refreshes_header_and_removed_tokens_drop_bearer(self):
        client = self.client(SAVED)
        self.assertEqual(client.session.headers["X-Anon-ID"], "genuine-anon-id")
        client.session.cookies.set(
            "anon_id", "renewed-anon-id", domain="www.vinted.co.uk", secure=True
        )
        for cookie in list(client.session.cookies):
            if cookie.name == "access_token_web":
                client.session.cookies.clear(cookie.domain, cookie.path, cookie.name)
        client.headers()
        self.assertEqual(client.session.headers["X-Anon-ID"], "renewed-anon-id")
        self.assertNotIn("Authorization", client.session.headers)
        client.session.cookies.set(
            "anon_id", "bad\r\nheader", domain="www.vinted.co.uk", secure=True
        )
        client.headers()
        self.assertNotIn("X-Anon-ID", client.session.headers)

    def test_explicit_challenge_replays_pre_payment_request_once_with_same_body(self):
        self.configure()
        client = self.client(SAVED)
        body = {"initiator": "buy", "item_id": 123}
        blocked = response(403, CHALLENGE)
        with self.solver() as solver, patch.object(
            client.session,
            "request",
            side_effect=[blocked, response(data={"conversation": {"id": 321}})],
        ) as request:
            self.assertEqual(
                client.request("POST", "/api/v2/conversations", body),
                {"conversation": {"id": 321}},
            )
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0], request.call_args_list[1])
        solver.assert_called_once()
        self.assertEqual(solver.call_args.kwargs["proxy"], PROXY)
        self.assertEqual(solver.call_args.kwargs["user_agent"], BROWSER_USER_AGENT)
        blocked.close.assert_called_once()

    def test_repeated_challenge_stops_after_one_solver_and_one_replay(self):
        self.configure()
        client = self.client(SAVED)
        with self.solver() as solver, patch.object(
            client.session,
            "request",
            side_effect=[response(403, CHALLENGE), response(403, CHALLENGE)],
        ) as request, self.assertRaises(buyer.BuyerError) as error:
            client.request("GET", "/api/v2/users/current")
        self.assertEqual(error.exception.reason, "security_challenge")
        self.assertEqual(request.call_count, 2)
        solver.assert_called_once()

    def test_unsolved_challenge_does_not_replay_request(self):
        self.configure()
        client = self.client(SAVED)
        with self.solver("failed") as solver, patch.object(
            client.session, "request", return_value=response(403, CHALLENGE)
        ) as request, self.assertRaises(buyer.BuyerError):
            client.request("GET", "/api/v2/users/current")
        request.assert_called_once()
        solver.assert_called_once()

    def test_generic_forbidden_and_rate_limits_never_spend_solver_credit(self):
        self.configure()
        for status, data in ((403, {}), (429, {}), (429, CHALLENGE)):
            with self.subTest(status=status, challenge=bool(data)):
                client = self.client(SAVED)
                with self.solver() as solver, patch.object(
                    client.session, "request", return_value=response(status, data)
                ) as request, self.assertRaises(buyer.BuyerError):
                    client.request("GET", "/api/v2/users/current")
                request.assert_called_once()
                solver.assert_not_called()

    def test_payment_challenge_is_never_solved_or_submitted_twice(self):
        self.configure()
        client = self.client(SAVED)
        with self.solver() as solver, patch.object(
            client.session, "request", return_value=response(403, CHALLENGE)
        ) as request, self.assertRaises(buyer.BuyerError):
            client.request("POST", "/api/v2/purchases/checkout-123/payment", {})
        request.assert_called_once()
        solver.assert_not_called()

    def test_homepage_challenge_replays_html_navigation_once(self):
        self.configure()
        client = self.client()
        blocked = response(
            403,
            text='<html><iframe src="https://geo.captcha-delivery.com/captcha/?cid=test&t=fe"></iframe></html>',
        )
        success = response(text='{"CSRF_TOKEN":"new-homepage-csrf-0123456789"}')
        with self.solver() as solver, patch.object(
            client.session, "get", side_effect=[blocked, success]
        ) as get:
            client.homepage()
        self.assertEqual(client.csrf, "new-homepage-csrf-0123456789")
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[0], get.call_args_list[1])
        self.assertIn("text/html", get.call_args.kwargs["headers"]["Accept"])
        solver.assert_called_once()
        self.assertFalse(buyer.settings()["connected"])

    def test_solver_cookie_merge_preserves_saved_identity_and_csrf(self):
        self.configure()
        self.save_session(SAVED)
        client = self.client(SAVED)
        with self.solver():
            self.assertTrue(client.solve_challenge(response(403, CHALLENGE), CHALLENGE))
        stored = self.stored_session()
        self.assertEqual(stored["csrf"], SAVED["csrf"])
        self.assertEqual(stored["cookies"]["datadome"], "new-security-cookie")
        for name in ("access_token_web", "refresh_token_web", "anon_id"):
            self.assertEqual(stored["cookies"][name], SAVED["cookies"][name])

    def test_solver_cannot_overwrite_a_concurrently_rotated_buyer_session(self):
        self.configure()
        client = self.client(SAVED)
        renewed = {
            "csrf": "renewed-csrf-token-0123456789",
            "cookies": {
                **SAVED["cookies"],
                "access_token_web": "renewed-access-token-0123456789",
                "refresh_token_web": "renewed-refresh-token-0123456789",
            },
        }
        self.save_session(renewed)
        with self.solver():
            self.assertTrue(client.solve_challenge(response(403, CHALLENGE), CHALLENGE))
        self.assertEqual(self.stored_session(), renewed)
        self.assertEqual(
            client.session.cookies.get_dict()["datadome"], "new-security-cookie"
        )

    def test_anonymous_solver_does_not_create_an_authenticated_buyer(self):
        self.configure()
        client = self.client()
        with self.solver():
            self.assertTrue(client.solve_challenge(response(403, CHALLENGE), CHALLENGE))
        self.assertIsNone(self.stored_session())
        self.assertFalse(buyer.settings()["connected"])
        self.assertNotIn("access_token_web", client.exported()["cookies"])


class NetworkDashboardTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.environment = patch.dict(
            os.environ,
            {
                "VINTED_BUYER_PROXY_URL": "",
                "CAPSOLVER_API_KEY": "",
                "VINTED_CAPSOLVER_ENABLED": "",
            },
        )
        self.environment.start()
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()

    def tearDown(self):
        self.environment.stop()
        super().tearDown()

    def owner(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE dashboard_auth SET password_hash=?",
                (generate_password_hash("offline owner password"),),
            )
        with self.client.session_transaction() as session:
            session["owner"] = True
            session["csrf"] = "network-offline-csrf"

    def form(self, **overrides):
        data = {
            "action": "buyer_network",
            "csrf": "network-offline-csrf",
            "buyer_proxy_url": PROXY,
            "buyer_capsolver_key": KEY,
            "buyer_capsolver_enabled": "yes",
        }
        data.update(overrides)
        return data

    def test_network_config_requires_owner_and_valid_csrf(self):
        self.assertEqual(self.client.get("/connections").location, "/login")
        with patch.object(buyer, "save_network") as save:
            self.client.post("/connections", data=self.form())
            save.assert_not_called()
            self.owner()
            response = self.client.post("/connections", data=self.form(csrf="wrong"))
            self.assertEqual(response.status_code, 400)
            save.assert_not_called()

    def test_post_redirect_clears_private_fields_and_preserves_search_history(self):
        self.owner()
        original_queries = db.get_queries()
        response = self.client.post("/connections", data=self.form())
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.location, "/connections")
        rendered = self.client.get(response.location).get_data(as_text=True)
        for private in (PROXY, "network-password", KEY):
            self.assertNotIn(private, rendered)
        self.assertIn('type="password"', rendered)
        self.assertNotIn(f'value="{KEY}"', rendered)
        self.assertEqual(db.get_queries(), original_queries)
        self.assertTrue(db.is_item_in_db_by_id(99))

    def test_invalid_credential_post_redirect_does_not_echo_submitted_values(self):
        self.owner()
        invalid = PROXY + "/unsupported-private-path"
        response = self.client.post(
            "/connections", data=self.form(buyer_proxy_url=invalid)
        )
        self.assertEqual(response.status_code, 303)
        rendered = self.client.get(response.location).get_data(as_text=True)
        for private in (invalid, "network-password", KEY):
            self.assertNotIn(private, rendered)
        self.assertFalse(buyer.network_configuration()["enabled"])


if __name__ == "__main__":
    unittest.main()
