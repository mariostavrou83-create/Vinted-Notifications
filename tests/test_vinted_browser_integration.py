"""Real browser request/cookie pipeline with a mocked SDK and no live requests."""

import json
import os
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock, patch

from curl_cffi.requests.headers import Headers
from test_search_controls import DatabaseFixture

import search_settings
import vinted_buyer as buyer
from vinted_http import BROWSER_USER_AGENT

PROXY = "http://browser-user:browser-password@fixed-proxy.example.test:8080"
SAVED = {
    "csrf": "saved-browser-csrf-0123456789",
    "cookies": {
        "access_token_web": "saved-browser-access-0123456789",
        "refresh_token_web": "saved-browser-refresh-0123456789",
        "anon_id": "saved-browser-anon",
        "datadome": "saved-browser-security-0123456789",
    },
}
CHALLENGE = {"url": "https://geo.captcha-delivery.com/captcha/?cid=test&t=fe"}
SOLVED = "solved-browser-security-0123456789"


def sdk_response(url, *, status=200, data=None, text="", cookies=()):
    return SimpleNamespace(
        status_code=status,
        url=url,
        reason="OK" if status == 200 else "Forbidden",
        headers=Headers(
            [("Content-Type", "application/json" if data is not None else "text/html")]
            + [("Set-Cookie", value) for value in cookies]
        ),
        content=json.dumps(data).encode() if data is not None else text.encode(),
        close=Mock(),
    )


def token_headers(prefix):
    return tuple(
        f"{name}={prefix}-{name}-0123456789; Domain=.vinted.co.uk; Path=/; Secure; HttpOnly"
        for name in ("access_token_web", "refresh_token_web")
    )


class BrowserCookieIntegrationTests(DatabaseFixture, unittest.TestCase):
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
        self.sdk = Mock()
        self.constructor = patch(
            "vinted_http.curl_requests.Session", return_value=self.sdk
        )
        self.constructor.start()
        self.clients = []
        buyer.save_network(
            {
                "buyer_proxy_url": PROXY,
                "buyer_capsolver_key": "CAP-browser-offline-key-0123456789",
                "buyer_capsolver_enabled": "yes",
            }
        )

    def tearDown(self):
        try:
            for client in self.clients:
                client.session.close()
        finally:
            self.constructor.stop()
            self.environment.stop()
            super().tearDown()

    def verified_client(self):
        client = buyer.Client(SAVED)
        self.clients.append(client)
        saved = client.exported()
        sealed = buyer.encrypt(saved)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?,user_id='99',verified_at=1 WHERE id=1",
                (sealed,),
            )
        client.bind_verified_session("99", sealed, saved)
        return client

    def stored(self):
        with closing(search_settings.connection()) as conn:
            sealed = conn.execute(
                "SELECT session FROM vinted_buyer WHERE id=1"
            ).fetchone()[0]
        return buyer.decrypt(sealed)

    def assert_transport(self, outgoing):
        self.assertEqual(outgoing["proxies"], {"all": PROXY})
        self.assertEqual(outgoing["headers"]["User-Agent"], BROWSER_USER_AGENT)
        self.assertNotIn("Authorization", outgoing["headers"])
        self.assertNotIn("Cookie", outgoing["headers"])
        self.assertTrue(outgoing["verify"])
        self.assertFalse(outgoing["allow_redirects"])

    def test_readonly_identity_challenge_does_not_start_solver_or_replay(self):
        client = self.verified_client()
        stored = self.stored()
        self.sdk.request.return_value = sdk_response(
            buyer.BASE + "/api/v2/users/current", status=403, data=CHALLENGE
        )
        with patch.object(buyer, "Client", return_value=client), patch(
            "vinted_captcha.solve_datadome"
        ) as solver:
            with self.assertRaises(buyer.BuyerError):
                buyer.connected_client(solve_challenges=False)
            solver.assert_not_called()
        self.sdk.request.assert_called_once()
        self.assertEqual(self.stored(), stored)

    def test_readonly_expired_identity_does_not_refresh_or_submit_requests(self):
        client = self.verified_client()
        stored = self.stored()
        self.sdk.request.return_value = sdk_response(
            buyer.BASE + "/api/v2/users/current",
            status=401,
            data={"error": "invalid_token"},
        )
        with patch.object(buyer, "Client", return_value=client), patch.object(
            buyer, "renew_saved_client"
        ) as renewal:
            with self.assertRaises(buyer.BuyerError):
                buyer.connected_client(solve_challenges=False, allow_refresh=False)
            renewal.assert_not_called()
        self.sdk.request.assert_called_once()
        self.assertEqual(self.stored(), stored)

    def test_challenge_cookies_roll_back_before_solving_and_real_replay_keeps_solution(
        self,
    ):
        for kind in ("api", "homepage", "listing"):
            with self.subTest(kind=kind):
                client = self.verified_client()
                calls = []

                def native(method, url, *, kind=kind, calls=calls, **outgoing):
                    self.assert_transport(outgoing)
                    calls.append((method, url, outgoing["content"]))
                    current = self.sdk.cookies.get_dict()
                    self.assertEqual(
                        current["access_token_web"],
                        SAVED["cookies"]["access_token_web"],
                    )
                    self.assertEqual(
                        current["refresh_token_web"],
                        SAVED["cookies"]["refresh_token_web"],
                    )
                    if len(calls) == 1:
                        return sdk_response(
                            url,
                            status=403,
                            data=CHALLENGE,
                            cookies=token_headers("rejected")
                            + (
                                "anon_id=rejected-anon; Domain=.vinted.co.uk; Path=/; Secure",
                            ),
                        )
                    self.assertEqual(current["datadome"], SOLVED)
                    self.assertEqual(current["anon_id"], SAVED["cookies"]["anon_id"])
                    if kind == "api":
                        return sdk_response(url, data={"user": {"id": 99}})
                    text = (
                        '<meta name="csrf-token" content="new-browser-csrf-0123456789">'
                    )
                    if kind == "listing":
                        item = {
                            "id": "123",
                            "seller_id": "100",
                            "price": {"amount": "15.00", "currency_code": "GBP"},
                            "can_buy": True,
                            "is_reserved": False,
                            "is_hidden": False,
                        }
                        text += (
                            '<script id="__NEXT_DATA__">'
                            + json.dumps(item)
                            + "</script>"
                        )
                    return sdk_response(url, text=text)

                def solve(*args, client=client, **kwargs):
                    # The rejected response was merged by BrowserSession, then
                    # the buyer transaction restored it before this callback.
                    self.assertEqual(client.exported()["cookies"], SAVED["cookies"])
                    self.assertEqual(kwargs["proxy"], PROXY)
                    self.assertEqual(kwargs["user_agent"], BROWSER_USER_AGENT)
                    return SimpleNamespace(state="solved", cookie=SOLVED)

                self.sdk.request.side_effect = native
                with patch(
                    "vinted_captcha.solve_datadome", side_effect=solve
                ) as solver:
                    if kind == "api":
                        self.assertEqual(client.identity()[0], "99")
                    elif kind == "homepage":
                        client.homepage()
                    else:
                        self.assertEqual(
                            client.listing_page(buyer.BASE + "/items/123", "123")[
                                "item"
                            ]["user_id"],
                            "100",
                        )
                solver.assert_called_once()
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0], calls[1])
                saved = self.stored()
                self.assertEqual(saved["cookies"]["datadome"], SOLVED)
                self.assertEqual(
                    saved["cookie_records"], client.exported()["cookie_records"]
                )
                self.assertNotIn("rejected", json.dumps(saved))

    def test_second_challenge_restores_solved_cookie_without_another_task_or_replay(
        self,
    ):
        client = self.verified_client()
        self.sdk.request.side_effect = lambda method, url, **kw: sdk_response(
            url, status=403, data=CHALLENGE, cookies=token_headers("rejected")
        )
        with patch(
            "vinted_captcha.solve_datadome",
            return_value=SimpleNamespace(state="solved", cookie=SOLVED),
        ) as solver, self.assertRaises(buyer.BuyerError) as error:
            client.identity()
        self.assertEqual(error.exception.reason, "security_challenge")
        self.assertEqual(self.sdk.request.call_count, 2)
        solver.assert_called_once()
        self.assertEqual(client.exported()["cookies"]["datadome"], SOLVED)
        self.assertEqual(self.stored(), client.exported())
        self.assertNotIn("rejected", json.dumps(self.stored()))

    def test_payment_and_rate_limited_responses_never_solve_or_replay_and_restore_cookies(
        self,
    ):
        for payment, status, data in (
            (True, 403, CHALLENGE),
            (True, 200, {"error": "invalid_token"}),
            (False, 429, CHALLENGE),
        ):
            with self.subTest(payment=payment, status=status):
                client = self.verified_client()
                before = client.exported()
                self.sdk.request.reset_mock()
                self.sdk.request.side_effect = (
                    lambda method, url, status=status, data=data, **kw: sdk_response(
                        url, status=status, data=data, cookies=token_headers("rejected")
                    )
                )
                path = (
                    "/api/v2/purchases/purchase-123/checkout/payment"
                    if payment
                    else "/api/v2/users/current"
                )
                with patch(
                    "vinted_captcha.solve_datadome"
                ) as solver, self.assertRaises(buyer.BuyerError):
                    client.request(
                        "POST" if payment else "GET", path, {} if payment else None
                    )
                self.sdk.request.assert_called_once()
                solver.assert_not_called()
                self.assertEqual(client.exported(), before)
                self.assertEqual(self.stored(), before)

    def test_native_refresh_uses_real_response_cookie_scope_and_survives_restart(self):
        client = self.verified_client()

        def native(method, url, **outgoing):
            self.assert_transport(outgoing)
            self.assertEqual(
                (method, url), ("POST", buyer.BASE + "/web/api/auth/refresh")
            )
            self.assertIsNone(outgoing["content"])
            self.assertIsNone(outgoing["headers"]["Content-Type"])
            return sdk_response(
                url,
                data={"access_token": "body-other-credential-0123456789"},
                cookies=tuple(
                    header + "; Expires=Fri, 01 Jan 2100 00:00:00 GMT"
                    for header in token_headers("accepted")
                ),
            )

        self.sdk.request.side_effect = native
        client.request("POST", "/web/api/auth/refresh")
        restored = buyer.Client(self.stored())
        self.clients.append(restored)
        self.assertEqual(restored.exported(), client.exported())
        tokens = [
            cookie
            for cookie in restored.session.cookies
            if cookie.name.endswith("token_web")
        ]
        self.assertEqual(len(tokens), 2)
        for cookie in tokens:
            self.assertEqual(cookie.domain, ".vinted.co.uk")
            self.assertEqual(cookie.path, "/")
            self.assertEqual(cookie.expires, 4102444800)
            self.assertTrue(cookie.value.startswith("accepted-"))
        self.assertNotIn("body-other", json.dumps(self.stored()))

    def test_two_step_login_accepts_server_context_without_datadome_task(self):
        client = buyer.Client()
        self.clients.append(client)
        self.sdk.request.side_effect = lambda method, url, **kw: sdk_response(
            url,
            status=401,
            data={"payload": {"id": 123}},
            cookies=(
                "anon_id=two-step-context; Domain=www.vinted.co.uk; Path=/; Secure",
            ),
        )
        with patch("vinted_captcha.solve_datadome") as solver:
            self.assertEqual(
                client.request("POST", "/web/api/auth/oauth", {}, allow_challenge=True),
                {"challenge_id": "123"},
            )
        self.assertEqual(client.exported()["cookies"]["anon_id"], "two-step-context")
        solver.assert_not_called()
        self.sdk.request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
