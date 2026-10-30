"""Bounded error evidence must not reinterpret a refusal or retry payment."""

import unittest
from unittest.mock import Mock, patch

import vinted_buyer as buyer


class ApiErrorEvidenceTests(unittest.TestCase):
    def response(self, status=403):
        return Mock(status_code=status, headers={}, text="")

    def test_numeric_payment_error_is_evidence_without_changing_classification(self):
        data = {
            "code": 106,
            "message": "NEVER-PUBLISH private upstream detail",
            "message_code": "NEVER-PUBLISH-UNKNOWN-CODE",
        }
        with self.assertLogs(buyer.logger, level="INFO") as logs:
            error = buyer.response_error(self.response(), data, "request")
        self.assertEqual(error.api_code, 106)
        self.assertEqual(error.reason, "forbidden")
        self.assertEqual(error.status, 403)
        self.assertEqual(error.stage, "request")
        self.assertNotIn("NEVER-PUBLISH", str(error))
        self.assertNotIn("NEVER-PUBLISH", " ".join(logs.output))

    def test_bounded_numeric_strings_remain_compatible(self):
        error = buyer.response_error(self.response(), {"code": "106"}, "request")
        self.assertEqual(error.api_code, 106)
        self.assertEqual(error.reason, "forbidden")
        self.assertEqual(buyer.BuyerError("offline", api_code="0").api_code, 0)
        self.assertEqual(buyer.BuyerError("offline", api_code=1000).api_code, 1000)

    def test_malformed_or_unbounded_api_codes_are_ignored(self):
        for value in (True, False, 106.0, -1, 1001, "10001", "106-secret", {}, [], None):
            with self.subTest(value=value):
                self.assertIsNone(buyer.BuyerError("offline", api_code=value).api_code)
                error = buyer.response_error(self.response(), {"code": value}, "request")
                self.assertIsNone(error.api_code)
                self.assertEqual(error.reason, "forbidden")

    def test_credentials_classification_is_unchanged(self):
        error = buyer.response_error(self.response(401), {"code": 100}, "identity")
        self.assertEqual(error.api_code, 100)
        self.assertEqual(error.reason, "credentials")

    def test_payment_refusal_retains_evidence_and_never_retries_request(self):
        response = self.response()
        response.json.return_value = {"code": 106, "message": "private-detail"}
        response.cookies = None
        with patch.object(buyer, "network_configuration", return_value={"proxy": "", "api_key": "", "enabled": False}):
            client = buyer.Client()
        try:
            with patch.object(client.session, "request", return_value=response) as request:
                with self.assertRaises(buyer.BuyerError) as caught:
                    client.request("POST", "/api/v2/purchases/offline/checkout/payment", {"checksum": "offline"})
            self.assertEqual(caught.exception.api_code, 106)
            self.assertEqual(caught.exception.reason, "forbidden")
            request.assert_called_once()
        finally:
            client.session.close()


if __name__ == "__main__":
    unittest.main()
