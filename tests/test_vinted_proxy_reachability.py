"""Anonymous proxy diagnosis, without buyer mutations or real HTTP requests."""

import json
import unittest
from unittest.mock import Mock, patch

import requests
import vinted_buyer as buyer
import vinted_network_check as network


class ProxyReachabilityTests(unittest.TestCase):
    def setUp(self):
        self.proxy = "http://private-user:private-password@private-proxy:12323"
        self.probe = Mock()
        self.probe.cookies = {}
        self.probe.headers = {}
        self.probe.proxies = {}
        self.response = Mock(status_code=200)
        self.response.iter_content.return_value = [
            b"ip=8.8.8.8\nloc=GB\nvisit_scheme=https\nwarp=off\n"
        ]
        self.probe.get.return_value = self.response
        for target, value in (
            ("vinted_network_check.BrowserSession", Mock(return_value=self.probe)),
            ("vinted_buyer.network_configuration", Mock(return_value={"proxy": self.proxy})),
            ("vinted_buyer.connection", Mock(side_effect=AssertionError("No buyer database writes"))),
            ("vinted_buyer.connected_client", Mock(side_effect=AssertionError("No buyer account requests"))),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_uses_one_anonymous_verified_request_and_preserves_settings(self):
        result = network.check_proxy_transport()
        self.assertEqual(result, {"outcome": "verified", "stage": "complete", "country": "GB"})
        self.assertFalse(self.probe.trust_env)
        self.assertIs(self.probe.verify, True)
        self.assertEqual(self.probe.proxies, {"http": self.proxy, "https": self.proxy})
        self.probe.get.assert_called_once_with(
            network.EXIT_URL, timeout=(2, 4), allow_redirects=False, stream=True
        )
        self.response.close.assert_called_once()
        self.probe.close.assert_called_once()
        buyer.connection.assert_not_called()
        buyer.connected_client.assert_not_called()

    def test_timeouts_are_redacted_and_never_fall_back_to_a_direct_request(self):
        self.probe.get.side_effect = requests.exceptions.Timeout("private-proxy private-token")
        with self.assertLogs("vinted_network_check", level="INFO") as logs:
            result = network.check_proxy_transport()
        self.assertEqual(result["reason"], "timeout")
        self.assertNotIn("private", json.dumps(result) + str(logs.output))
        self.probe.get.assert_called_once()
        self.probe.close.assert_called_once()
        self.assertIn("Buying settings were unchanged", network.proxy_transport_summary(result))

    def test_missing_proxy_makes_no_request(self):
        buyer.network_configuration.return_value = {"proxy": ""}
        self.assertEqual(network.check_proxy_transport()["stage"], "configuration")
        self.probe.get.assert_not_called()

    def test_saved_vinted_credentials_cannot_be_used_by_the_probe(self):
        for field in ("Cookie", "Authorization", "X-CSRF-Token"):
            with self.subTest(field=field):
                self.probe.headers = {field: "private-value"}
                self.assertEqual(network.check_proxy_transport()["stage"], "anonymous_probe")
        self.probe.get.assert_not_called()

    def test_cookie_jar_must_be_empty(self):
        self.probe.cookies = {"access_token_web": "private-token"}
        self.assertEqual(network.check_proxy_transport()["stage"], "anonymous_probe")
        self.probe.get.assert_not_called()

    def test_proxy_and_tls_failures_have_distinct_safe_results(self):
        for error, reason in (
            (requests.exceptions.ProxyError, "proxy_refused"),
            (requests.exceptions.SSLError, "tls"),
            (requests.exceptions.ConnectionError, "unconfirmed"),
        ):
            with self.subTest(error=error):
                self.probe.get.side_effect = error("private-body")
                result = network.check_proxy_transport()
                self.assertEqual(result["reason"], reason)
                self.assertNotIn("private", json.dumps(result))

    def test_redirect_is_not_followed_or_reported_as_a_verified_proxy(self):
        self.response.status_code = 302
        result = network.check_proxy_transport()
        self.assertEqual(result["outcome"], "unverified")
        self.assertEqual(result["http_status"], 302)
        self.response.close.assert_called_once()

    def test_malformed_or_oversized_trace_cannot_leak_into_results(self):
        for raw in (b"private-body", b"x=" + b"z" * 17000, b"ip=private-ip\nloc=GB\nvisit_scheme=https\n"):
            with self.subTest(size=len(raw)):
                self.response.iter_content.return_value = [raw]
                result = network.check_proxy_transport()
                self.assertEqual(result["outcome"], "unverified")
                self.assertNotIn("private", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
