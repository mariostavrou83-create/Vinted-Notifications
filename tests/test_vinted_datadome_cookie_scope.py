"""Accepted first-party security cookie metadata resolves contained root aliases."""

import time
import unittest
from unittest.mock import Mock, patch

import requests

import test_vinted_http as http_fixture
import test_vinted_network as network_fixture
import vinted_buyer as buyer

OLD = "old-private-security-cookie"
FRESH = "fresh-private-security-cookie"


def cookie(domain=".vinted.co.uk", *, path="/", host_only=False, **fields):
    value = requests.cookies.create_cookie(
        "datadome", FRESH, domain=domain, path=path, secure=True,
        expires=int(time.time()) + 3600, rest={"HttpOnly": None, "SameSite": "Lax"},
    )
    value.domain_specified = not host_only
    for key, item in fields.items():
        setattr(value, key, item)
    return value


def response(*cookies, status=200, data=None):
    result = network_fixture.response(status, data or {"user": {"id": 99}})
    result.url = buyer.BASE + "/api/v2/users/current"
    for value in cookies:
        result.cookies.set_cookie(value)
    return result


class SecurityCookieScopeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = network_fixture.NetworkTests(
            "test_payment_challenge_is_never_solved_or_submitted_twice"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def client(self):
        client = self.fixture.client(network_fixture.SAVED)
        return client

    def records(self, client):
        return [value for value in client.session.cookies if value.name == "datadome"]

    def test_real_transport_parent_set_cookie_replaces_solver_host_shadow_and_persists(self):
        self.fixture.save_session(network_fixture.SAVED)
        sdk = Mock()
        sdk.request.side_effect = [
            http_fixture.sdk_response(headers={
                "Set-Cookie": f"datadome={FRESH}; Domain=.vinted.co.uk; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=3600"
            }, content=b'{"user":{"id":99}}'),
            http_fixture.sdk_response(content=b'{"user":{"id":99}}'),
        ]
        with patch("vinted_http.curl_requests.Session", return_value=sdk):
            client = self.client()
            self.fixture.bind_client(client)
            original = client._normalize_security_cookie
            merged_counts = []
            received_metadata = []

            def normalize(received, now):
                merged_counts.append(len(self.records(client)))
                received_metadata.append(vars(next(value for value in received.cookies if value.name == "datadome")).copy())
                original(received, now)

            with patch.object(client, "_normalize_security_cookie", side_effect=normalize):
                self.assertEqual(client.request("GET", "/api/v2/users/current"), {"user": {"id": 99}})
            self.assertEqual(merged_counts, [2])  # Reproduces the actual old duplicate jar.
            retained = self.records(client)
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].domain, ".vinted.co.uk")
            self.assertTrue(retained[0].domain_specified)
            self.assertEqual(retained[0].get_nonstandard_attr("SameSite"), "Lax")
            self.assertEqual(retained[0].value, FRESH)
            self.assertEqual(vars(retained[0]), received_metadata[0])
            prepared = client.session.prepare_request(requests.Request("POST", buyer.BASE + "/api/v2/purchases/checkout/build"))
            self.assertEqual(prepared.headers["Cookie"].count("datadome="), 1)
            client.request("GET", "/api/v2/users/current")
            self.assertEqual(sum(value.name == "datadome" for value in sdk.cookies), 1)
            stored = self.fixture.stored_session()
            restored = self.fixture.client(stored)
            self.assertEqual(restored.exported()["cookie_records"], client.exported()["cookie_records"])
            for name in ("access_token_web", "refresh_token_web", "anon_id"):
                self.assertEqual(stored["cookies"][name], network_fixture.SAVED["cookies"][name])

    def test_received_metadata_is_retained_exactly_and_unrelated_paths_survive(self):
        client = self.client()
        client.session.cookies.set("datadome", OLD, domain="www.vinted.co.uk", path="/other", secure=True)
        fresh = cookie()
        client.update_tokens(response(fresh), {})
        retained = self.records(client)
        self.assertEqual(len(retained), 2)
        self.assertEqual(vars(next(value for value in retained if value.path == "/")), vars(fresh))
        self.assertEqual(next(value for value in retained if value.path == "/other").value, OLD)

    def test_fresh_host_only_www_cookie_does_not_remove_parent_api_context(self):
        client = self.client()
        client.session.cookies.clear("www.vinted.co.uk", "/", "datadome")
        parent = cookie()
        parent.value = OLD
        client.session.cookies.set_cookie(parent)
        fresh = cookie("www.vinted.co.uk", host_only=True)
        client.update_tokens(response(fresh), {})
        self.assertEqual(len(self.records(client)), 2)
        self.assertEqual(vars(client.session.cookies._cookies[".vinted.co.uk"]["/"]["datadome"]), vars(parent))
        prepared = client.session.prepare_request(requests.Request("GET", buyer.PICKUP_BASE + buyer.NOTIFICATIONS_PATH))
        self.assertEqual(prepared.headers["Cookie"].count("datadome="), 1)

    def test_bare_apex_host_only_cookie_cannot_cover_www(self):
        client = self.client()
        before = client.exported()
        client.update_tokens(response(cookie("vinted.co.uk", host_only=True)), {})
        self.assertEqual(client.exported(), before)

    def test_missing_or_ambiguous_fresh_authority_never_selects_existing_jar_order(self):
        client = self.client()
        client.session.cookies.set("datadome", OLD, domain=".vinted.co.uk", path="/", secure=True)
        before = client.exported()
        for received in (response(), response(cookie(), cookie("www.vinted.co.uk"))):
            client.update_tokens(received, {})
            self.assertEqual(client.exported(), before)

    def test_invalid_expired_or_deleted_cookie_is_not_installed(self):
        client = self.client()
        for value in list(self.records(client)):
            client.session.cookies.clear(value.domain, value.path, value.name)
        before = client.exported()
        for fresh in (
            cookie(secure=False), cookie(path="/checkout"),
            cookie(expires=int(time.time()) - 1), cookie(value=""),
            cookie("foreign.example"),
        ):
            client.update_tokens(response(fresh), {})
            self.assertEqual(client.exported(), before)
        for url in ("https://@www.vinted.co.uk/", "https://foreign.example/", "http://www.vinted.co.uk/"):
            received = response(cookie())
            received.url = url
            client.update_tokens(received, {})
            self.assertEqual(client.exported(), before)

    def test_rejected_payment_or_rate_limit_preserves_previous_jar_and_never_replays(self):
        for status in (403, 429):
            with self.subTest(status=status):
                client = self.client()
                before = client.exported()

                def reject(*args, **kwargs):
                    fresh = cookie()
                    client.session.cookies.set_cookie(fresh)  # Requests' automatic merge.
                    return response(fresh, status=status, data=network_fixture.CHALLENGE)

                with patch.object(client.session, "request", side_effect=reject) as send, self.fixture.solver() as solver:
                    with self.assertRaises(buyer.BuyerError):
                        client.request("POST", "/api/v2/purchases/checkout-123/checkout/payment", {})
                self.assertEqual(client.exported(), before)
                send.assert_called_once()
                solver.assert_not_called()

    def test_ambiguous_cookie_metadata_cannot_discard_an_accepted_payment_result(self):
        client = self.client()
        accepted = response(cookie(), cookie("www.vinted.co.uk"), data={"payment": {"status": "success"}})
        with patch.object(client.session, "request", return_value=accepted):
            self.assertEqual(client.request("POST", "/api/v2/purchases/checkout-123/checkout/payment", {}), {"payment": {"status": "success"}})

    def test_normalization_log_failure_cannot_discard_an_accepted_payment_result(self):
        client = self.client()
        accepted = response(cookie(), data={"payment": {"status": "success"}})
        with (
            patch.object(client.session, "request", return_value=accepted),
            patch.object(buyer.logger, "info", side_effect=RuntimeError("offline log failure")),
        ):
            self.assertEqual(client.request("POST", "/api/v2/purchases/checkout-123/checkout/payment", {}), {"payment": {"status": "success"}})
        self.assertEqual(len(self.records(client)), 1)
        self.assertEqual(self.records(client)[0].value, FRESH)


if __name__ == "__main__":
    unittest.main()
