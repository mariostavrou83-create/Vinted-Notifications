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
        self.assertEqual(evidence["message_code_type"], "missing")
        self.assertEqual(evidence["message_code"], "none")

    def test_rejected_candidate_format_is_observed_without_solver_acceptance(self):
        for marker, label in (("it", "it"), ("private", "other"), (None, "missing")):
            url = "https://ct.captcha-delivery.com/interstitial/?cid=private&referer=https%3A%2F%2Fapi.vinted.co.uk%2Fprivate"
            if marker is not None:
                url += "&t=" + marker
            evidence = self.evidence({"url": url})
            self.assertFalse(evidence["challenge_url_extractable"])
            self.assertEqual(evidence["challenge_candidate_host"], "ct")
            self.assertEqual(evidence["challenge_candidate_path"], "interstitial")
            self.assertEqual(evidence["challenge_candidate_marker"], label)
            self.assertEqual(evidence["challenge_candidate_referer"], "api")
            self.assertNotIn("private", repr(evidence))

    def test_html_candidate_observation_does_not_expose_challenge_values(self):
        response = fixture.response(403, {})
        response._content = b'<iframe src="https://ct.captcha-delivery.com/interstitial/?t=it&amp;cid=private"></iframe>'
        evidence = diagnostics.refusal_evidence(response, {}, operation="checkout_build", challenged=True)
        self.assertEqual(evidence["challenge_candidate_source"], "html")
        self.assertEqual(evidence["challenge_candidate_marker"], "it")
        self.assertTrue(evidence["challenge_url_extractable"])
        self.assertEqual(evidence["challenge_url_marker"], "it")
        self.assertNotIn("private", repr(evidence))

    def test_exact_machine_code_labels_are_evidence_without_inventing_a_cause(self):
        for code in ("access_denied", "permission_denied", "request_forbidden",
                     "request_rate_limit_exceeded", "action_not_allowed", "account_restricted"):
            with self.subTest(code=code):
                evidence = self.evidence({"code": 106, "message_code": code}, challenged=False)
                self.assertEqual(evidence["message_code_type"], "string")
                self.assertEqual(evidence["message_code"], code)
                self.assertFalse(evidence["challenged"])
                self.assertFalse(evidence["challenge_url_extractable"])

    def test_unknown_and_non_string_message_codes_never_expose_upstream_data(self):
        for code in ("private_token_identifier", "private@example.com", "access_denied private",
                     "ACCESS_DENIED", "access_denied\n", "private" * 20000, ""):
            with self.subTest(length=len(code)):
                evidence = self.evidence({"code": 106, "message_code": code}, challenged=False)
                self.assertEqual(evidence["message_code_type"], "string")
                self.assertEqual(evidence["message_code"], "other")
                self.assertNotIn("private", repr(evidence))
        for code in (None, 106, True, {"private": "value"}, ["access_denied"]):
            with self.subTest(type=type(code).__name__):
                evidence = self.evidence({"message_code": code}, challenged=False)
                self.assertEqual(evidence["message_code_type"], "non_string")
                self.assertEqual(evidence["message_code"], "none")
                self.assertNotIn("private", repr(evidence))

    def test_retry_after_only_reports_presence_and_bounded_numeric_seconds(self):
        absent = self.evidence({}, challenged=False)
        self.assertFalse(absent["retry_after_present"])
        self.assertEqual(absent["retry_after_kind"], "missing")
        self.assertIsNone(absent["retry_after_seconds"])
        for value, kind, seconds in (
            ("0", "seconds", 0), ("60", "seconds", 60), ("86400", "seconds", 86400),
            ("86401", "outside_bound", None), ("999999999999", "non_numeric", None),
            ("private-date-or-header", "non_numeric", None), ("-1", "non_numeric", None),
            ("1.5", "non_numeric", None), ("６０", "non_numeric", None),
            ({"private": "value"}, "non_string", None),
        ):
            with self.subTest(kind=kind, seconds=seconds):
                evidence = self.evidence({}, headers={"retry-after": value}, challenged=False)
                self.assertTrue(evidence["retry_after_present"])
                self.assertEqual(evidence["retry_after_kind"], kind)
                self.assertEqual(evidence["retry_after_seconds"], seconds)
                self.assertNotIn("private", repr(evidence))

    def test_logs_drop_unknown_machine_codes_messages_and_retry_after_text(self):
        data = {"code": 106, "message_code": "private_machine_code", "message": "private message"}
        response = fixture.response(403, data)
        response.headers["Retry-After"] = "private retry value"
        with self.assertLogs(diagnostics.logger, level="INFO") as logs:
            diagnostics.log_refusal_evidence(response, data, operation="checkout_build", challenged=False)
        text = " ".join(logs.output)
        self.assertIn("message_code_type=string message_code=other", text)
        self.assertIn("retry_after_present=True retry_after_kind=non_numeric retry_after_seconds=None", text)
        self.assertNotIn("private", text)

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
        response = fixture.response(403, {"code": 106, "message": "private provider text", "message_code": "access_denied"})
        response.headers["Retry-After"] = "60"
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
        self.assertIn("message_code=access_denied", " ".join(logs.output))
        self.assertIn("retry_after_seconds=60", " ".join(logs.output))
        self.assertNotIn("private", " ".join(logs.output))
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
