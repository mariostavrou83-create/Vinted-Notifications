"""Offline evidence for API-host cookie scope; no live checkout or payment."""

import json
import unittest
from unittest.mock import patch

import requests
from test_search_controls import DatabaseFixture

import vinted_buyer as buyer


class PickupCookieEvidenceTests(DatabaseFixture, unittest.TestCase):
    path = "/shipping-estimation/external/shipping_orders/987654321/nearby_pickup_points"
    params = {"country_code": "GB", "latitude": 51.234567, "longitude": -0.123456}
    secrets = {
        "access_token_web": "fictional-private-access-0123456789",
        "refresh_token_web": "fictional-private-refresh-0123456789",
    }
    csrf = "fictional-private-csrf-0123456789"

    def setUp(self):
        super().setUp()
        native = patch.object(buyer, "BrowserSession", requests.Session)
        native.start()
        self.addCleanup(native.stop)

    def client(self, domain, *, domain_specified=True):
        client = buyer.Client(
            {
                "csrf": self.csrf,
                "cookie_records": [
                    {
                        "name": name,
                        "value": value,
                        "domain": domain,
                        "path": "/",
                        "secure": True,
                        "domain_specified": domain_specified,
                    }
                    for name, value in self.secrets.items()
                ],
            }
        )
        self.addCleanup(client.session.close)
        return client

    def response(self, request, *, status=403):
        response = requests.Response()
        response.request = request
        response.url = request.url
        response.status_code = status
        response._content = json.dumps(
            {"code": "forbidden"} if status == 403 else {"shipping_points": []}
        ).encode()
        response._content_consumed = True
        return response

    def refusal(self, client, expected_count):
        before = client.exported()
        sent = []

        def transport(adapter, prepared, **kwargs):
            sent.append(prepared)
            return self.response(prepared)

        with (
            patch.object(requests.adapters.HTTPAdapter, "send", autospec=True,
                         side_effect=transport),
            patch.object(client, "solve_challenge") as solver,
            self.assertLogs("vinted_buyer", level="INFO") as logs,
            self.assertRaises(buyer.BuyerError) as failure,
        ):
            client.request("GET", self.path, params=self.params)
        self.assertEqual(failure.exception.reason, "forbidden")
        self.assertEqual(failure.exception.stage, "pickup_points")
        solver.assert_not_called()
        self.assertEqual(len(sent), 1)
        request = sent[0]
        self.assertEqual(request.method, "GET")
        self.assertTrue(request.url.startswith(buyer.PICKUP_BASE + self.path + "?"))
        self.assertIsNone(request.body)
        cookie = request.headers.get("Cookie", "")
        for name, value in self.secrets.items():
            self.assertEqual(name + "=" + value in cookie, bool(expected_count))
        self.assertEqual(client.exported(), before)
        evidence = next(line for line in logs.output if "pickup cookie evidence" in line)
        self.assertIn(f"access_sent={expected_count}", evidence)
        self.assertIn(f"refresh_sent={expected_count}", evidence)
        self.assertIn("csrf_sent=True", evidence)
        text = " ".join(logs.output)
        for private in (*self.secrets.values(), self.csrf, self.path,
                        str(self.params["latitude"]), str(self.params["longitude"])):
            self.assertNotIn(private, text)

    def test_host_only_www_credentials_are_not_widened_or_retried_after_403(self):
        self.refusal(self.client("www.vinted.co.uk", domain_specified=False), 0)

    def test_parent_domain_credentials_are_counted_on_the_actual_api_request(self):
        self.refusal(self.client(".vinted.co.uk"), 1)

    def test_expired_parent_credentials_are_not_counted_as_sent(self):
        client = self.client(".vinted.co.uk")
        for cookie in client.session.cookies:
            cookie.expires = 1
        self.refusal(client, 0)

    def test_invalid_pickup_parameters_cannot_trigger_evidence_or_network(self):
        client = self.client(".vinted.co.uk")
        with patch.object(client.session, "request") as network, patch.object(
            client.session, "prepare_request"
        ) as prepared, self.assertRaises(buyer.BuyerError):
            client.request("GET", self.path, params=dict(self.params, country_code="FR"))
        network.assert_not_called()
        prepared.assert_not_called()

    def test_optional_evidence_failure_does_not_change_the_real_request(self):
        client = self.client(".vinted.co.uk")
        original_prepare = client.session.prepare_request
        preparations = 0
        sent = []

        def prepare(request):
            nonlocal preparations
            preparations += 1
            if preparations == 1:
                raise RuntimeError("private diagnostic error")
            return original_prepare(request)

        def transport(adapter, prepared, **kwargs):
            sent.append(prepared)
            return self.response(prepared, status=200)

        with patch.object(client.session, "prepare_request", side_effect=prepare), patch.object(
            requests.adapters.HTTPAdapter, "send", autospec=True, side_effect=transport
        ), self.assertLogs("vinted_buyer", level="INFO") as logs:
            result = client.request("GET", self.path, params=self.params)
        self.assertEqual(result, {"shipping_points": []})
        self.assertEqual(len(sent), 1)
        self.assertIn("access_token_web=" + self.secrets["access_token_web"], sent[0].headers["Cookie"])
        self.assertTrue(any("available=False" in line for line in logs.output))
        self.assertNotIn("private diagnostic error", " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
