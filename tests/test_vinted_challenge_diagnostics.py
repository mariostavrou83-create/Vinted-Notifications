"""Refusal evidence stays structural and cannot replay or change payment errors."""

import unittest
from unittest.mock import patch

import requests
from requests.structures import CaseInsensitiveDict

import test_vinted_network as fixture
import vinted_buyer as buyer
import vinted_challenge_diagnostics as diagnostics

URL = "https://geo.captcha-delivery.com/captcha/?cid=private-challenge-id&t=fe"
PAYMENT = "/api/v2/purchases/checkout-123/checkout/payment"


class ChallengeEvidenceTests(unittest.TestCase):
    def evidence(self, data, *, headers=None, challenged=True):
        response = fixture.response(403, data)
        response.headers = CaseInsensitiveDict(headers or {})
        return diagnostics.refusal_evidence(
            response, data, operation="payment_submit", challenged=challenged
        )

    def test_validated_url_structure_and_documented_header_presence_only(self):
        evidence = self.evidence({"url": URL}, headers={
            "x-datadome": "private-provider-value",
            "x-dd-b": "private-header-value",
            "x-set-cookie": "private-cookie-value",
        })
        self.assertEqual(evidence["operation"], "payment_submit")
        self.assertTrue(evidence["x_datadome"])
        self.assertTrue(evidence["x_dd_b"])
        self.assertTrue(evidence["x_set_cookie"])
        self.assertTrue(evidence["challenge_url_extractable"])
        self.assertEqual(evidence["challenge_url_kind"], "captcha")
        self.assertEqual(evidence["challenge_url_marker"], "fe")
        self.assertEqual(evidence["challenge_url_source"], "json")
        self.assertNotIn("private", repr(evidence))

    def test_interstitial_path_and_bv_marker_do_not_claim_an_ip_ban(self):
        evidence = self.evidence({
            "challenge_url": "https://ct.captcha-delivery.com/interstitial/?cid=private&t=bv"
        })
        self.assertEqual(evidence["challenge_url_kind"], "interstitial")
        self.assertEqual(evidence["challenge_url_marker"], "bv")
        self.assertNotIn("blocked", repr(evidence))
        self.assertNotIn("banned", repr(evidence))

    def test_unrecognised_challenge_is_not_reported_as_supported(self):
        for url in (
            "https://other.captcha-delivery.com/captcha/?t=fe",
            "https://geo.captcha-delivery.com/unknown/?t=fe",
            "https://geo.captcha-delivery.com/captcha/?t=private-marker",
            "https://private-user@geo.captcha-delivery.com/captcha/?t=fe",
        ):
            with self.subTest(url=url):
                evidence = self.evidence({"url": url})
                self.assertFalse(evidence["challenge_url_extractable"])
                self.assertEqual(evidence["challenge_url_kind"], "unrecognised")
                self.assertEqual(evidence["challenge_url_marker"], "none")
                self.assertNotIn("private", repr(evidence))

    def test_generic_refusal_and_unknown_operation_do_not_invent_challenge_evidence(self):
        response = fixture.response(403, {"code": 106})
        evidence = diagnostics.refusal_evidence(
            response, {"code": 106}, operation={"private": "value"}, challenged=False
        )
        self.assertEqual(evidence["operation"], "buyer_request")
        self.assertEqual(evidence["challenge_url_kind"], "none")
        self.assertFalse(evidence["challenge_url_extractable"])

    def test_prepared_cookie_counts_and_csrf_presence_never_return_values(self):
        response = fixture.response(403, {"url": URL})
        response.request = requests.Request("POST", buyer.BASE + PAYMENT, headers={
            "Cookie": "access_token_web=private-auth; datadome=private-old; datadome=private-new; refresh_token_web=private-refresh",
            "X-CSRF-Token": "private-security-token",
        }).prepare()
        evidence = diagnostics.refusal_evidence(
            response, {"url": URL}, operation="payment_submit", challenged=True
        )
        self.assertTrue(evidence["prepared_cookie_evidence"])
        self.assertEqual(evidence["prepared_access_cookies"], 1)
        self.assertEqual(evidence["prepared_datadome_cookies"], 2)
        self.assertTrue(evidence["prepared_csrf_present"])
        self.assertNotIn("private", repr(evidence))

    def test_absent_prepared_metadata_differs_from_proven_cookie_absence(self):
        response = fixture.response(403, {})
        evidence = diagnostics.refusal_evidence(
            response, {}, operation="payment_submit", challenged=False
        )
        self.assertFalse(evidence["prepared_cookie_evidence"])
        self.assertIsNone(evidence["prepared_access_cookies"])
        response.request = requests.Request("POST", buyer.BASE + PAYMENT).prepare()
        evidence = diagnostics.refusal_evidence(
            response, {}, operation="payment_submit", challenged=False
        )
        self.assertTrue(evidence["prepared_cookie_evidence"])
        self.assertEqual(evidence["prepared_access_cookies"], 0)
        self.assertEqual(evidence["prepared_datadome_cookies"], 0)
        self.assertFalse(evidence["prepared_csrf_present"])


class PaymentRefusalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.NetworkTests(
            "test_payment_challenge_is_never_solved_or_submitted_twice"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.configure()
        self.client = self.fixture.client(fixture.SAVED)

    def test_payment_challenge_logs_only_fixed_evidence_and_never_solves_or_replays(self):
        data = {"url": URL}
        response = fixture.response(403, data)
        response.headers["X-DD-B"] = "private-header-value"
        response.cookies.set("datadome", "private-security-cookie", domain="www.vinted.co.uk")
        before = self.client.exported()
        with (
            self.fixture.solver() as solver,
            patch.object(self.client.session, "request", return_value=response) as request,
            self.assertLogs("vinted_challenge_diagnostics", level="INFO") as logs,
            self.assertRaises(buyer.BuyerError) as error,
        ):
            self.client.request("POST", PAYMENT, {"checksum": "private-checksum"})
        self.assertEqual(error.exception.reason, "security_challenge")
        self.assertIsNone(error.exception.api_code)
        self.assertEqual(self.client.exported(), before)
        request.assert_called_once()
        solver.assert_not_called()
        text = " ".join(logs.output)
        self.assertIn("operation=payment_submit", text)
        self.assertIn("challenge_url_kind=captcha", text)
        self.assertIn("challenge_url_marker=fe", text)
        self.assertIn("datadome_set_cookie=True", text)
        for secret in (URL, "private", "https://", "checksum"):
            self.assertNotIn(secret, text)

    def test_numeric_backend_refusal_keeps_existing_reason_and_code(self):
        response = fixture.response(403, {"code": 106, "message": "private provider text"})
        with (
            self.fixture.solver() as solver,
            patch.object(self.client.session, "request", return_value=response) as request,
            self.assertLogs("vinted_challenge_diagnostics", level="INFO") as logs,
            self.assertRaises(buyer.BuyerError) as error,
        ):
            self.client.request("POST", PAYMENT, {})
        self.assertEqual(error.exception.reason, "forbidden")
        self.assertEqual(error.exception.api_code, 106)
        self.assertIn("challenged=False", " ".join(logs.output))
        request.assert_called_once()
        solver.assert_not_called()

    def test_failed_optional_diagnostics_cannot_change_or_retry_the_payment_error(self):
        with (
            self.fixture.solver() as solver,
            patch.object(self.client.session, "request", return_value=fixture.response(403, {"url": URL})) as request,
            patch.object(diagnostics, "log_refusal_evidence", side_effect=RuntimeError("private diagnostic failure")),
            self.assertRaises(buyer.BuyerError) as error,
        ):
            self.client.request("POST", PAYMENT, {})
        self.assertEqual(error.exception.reason, "security_challenge")
        request.assert_called_once()
        solver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
