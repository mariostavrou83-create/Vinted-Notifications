"""Offline Requests-to-browser transport contract and credential isolation."""

import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from curl_cffi.requests import exceptions as curl_errors
from curl_cffi.requests.headers import Headers

from vinted_http import (
    API_HEADERS,
    BROWSER_CLIENT_HINTS,
    BROWSER_IMPERSONATE,
    BROWSER_USER_AGENT,
    NAVIGATION_HEADERS,
    BrowserSession,
)

BASE = "https://www.vinted.co.uk"


def sdk_response(*, headers=None, content=b"{}", chunks=None, status=200):
    response = SimpleNamespace(
        status_code=status,
        url=BASE + "/api/v2/users/current",
        reason="OK",
        headers=Headers(headers or {"Content-Type": "application/json"}),
        content=content,
        close=Mock(),
    )
    response.iter_content = Mock(return_value=iter(chunks or []))
    return response


class BrowserSessionTests(unittest.TestCase):
    def setUp(self):
        self.sdk = Mock()
        self.constructor = patch(
            "vinted_http.curl_requests.Session", return_value=self.sdk
        )
        self.factory = self.constructor.start()
        self.addCleanup(self.constructor.stop)
        self.session = BrowserSession()
        self.session.trust_env = False
        self.addCleanup(self.session.close)
        self.sdk.request.return_value = sdk_response()

    def test_requests_pipeline_preserves_prepared_body_headers_and_json_response(self):
        result = sdk_response(content=b'{"user":{"id":99}}')
        self.sdk.request.return_value = result
        hook = Mock(side_effect=lambda response, **kw: response)
        response = self.session.post(
            BASE + "/web/api/auth/oauth",
            json={"grant_type": "refresh_token", "refresh_token": "private-token"},
            headers={"X-CSRF-Token": "private-csrf"},
            hooks={"response": hook},
            timeout=(4, 12),
            allow_redirects=False,
        )
        self.assertIsInstance(response, requests.Response)
        self.assertIsInstance(response.request, requests.PreparedRequest)
        self.assertEqual(response.json(), {"user": {"id": 99}})
        args, sent = self.sdk.request.call_args
        self.assertEqual(args, ("POST", BASE + "/web/api/auth/oauth"))
        self.assertEqual(json.loads(sent["content"])["refresh_token"], "private-token")
        self.assertEqual(sent["headers"]["X-CSRF-Token"], "private-csrf")
        self.assertEqual(sent["headers"]["User-Agent"], BROWSER_USER_AGENT)
        self.assertEqual(sent["timeout"], (4, 12))
        self.assertTrue(sent["verify"])
        self.factory.assert_called_once_with(
            impersonate="chrome146",
            default_headers=False,
            verify=True,
            trust_env=False,
            retry=0,
        )
        hook.assert_called_once()
        response.close()
        response.close()
        result.close.assert_called_once()

    def test_browser_version_platform_and_hints_cannot_drift_via_header_override(self):
        self.session.headers["User-Agent"] = "unexpected-session-UA"
        self.session.get(
            BASE + "/",
            headers={
                "user-agent": "unexpected-request-UA",
                "sec-ch-ua-platform": '"macOS"',
                "sec-ch-ua": '"Chromium";v="150"',
            },
            allow_redirects=False,
        )
        sent = requests.structures.CaseInsensitiveDict(
            self.sdk.request.call_args.kwargs["headers"]
        )
        self.assertEqual(BROWSER_IMPERSONATE, "chrome146")
        self.assertIn("Chrome/146.0.0.0", sent["User-Agent"])
        self.assertIn("Windows NT 10.0", sent["User-Agent"])
        self.assertEqual(sent["Sec-CH-UA-Platform"], '"Windows"')
        self.assertEqual(
            sent["Sec-CH-UA"],
            '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        )
        self.assertEqual(sent["Sec-CH-UA-Mobile"], "?0")
        for name, value in BROWSER_CLIENT_HINTS.items():
            self.assertEqual(sent[name], value)

    def test_navigation_and_api_headers_have_distinct_document_fetch_contracts(self):
        for configured, mode, dest, accept in (
            (NAVIGATION_HEADERS, "navigate", "document", "text/html"),
            (API_HEADERS, "cors", "empty", "application/json"),
        ):
            self.session.get(BASE + "/", headers=configured, allow_redirects=False)
            sent = self.sdk.request.call_args.kwargs["headers"]
            self.assertEqual(sent["Sec-Fetch-Mode"], mode)
            self.assertEqual(sent["Sec-Fetch-Dest"], dest)
            self.assertTrue(sent["Accept"].startswith(accept))
            self.assertEqual(sent["User-Agent"], BROWSER_USER_AGENT)
        self.assertNotIn("Sec-Fetch-User", API_HEADERS)
        self.assertNotIn("Upgrade-Insecure-Requests", API_HEADERS)

    def test_cookie_domains_paths_and_new_cookie_rotation_are_preserved(self):
        jar = self.session.cookies
        jar.set(
            "access_token_web", "old-secret", domain="www.vinted.co.uk", secure=True
        )
        jar.set("path-cookie", "private-path", domain="www.vinted.co.uk", path="/only")
        jar.set("other-cookie", "private-other", domain="different.test", secure=True)
        self.sdk.request.return_value = sdk_response(
            headers=[
                ("Set-Cookie", "access_token_web=new-secret; Path=/; Secure; HttpOnly"),
                (
                    "Set-Cookie",
                    "refresh_token_web=new-refresh; Domain=.vinted.co.uk; Path=/; Secure",
                ),
                ("Set-Cookie", "alien=reject-me; Domain=different.test; Path=/"),
            ]
        )
        response = self.session.get(
            BASE + "/api/v2/users/current", allow_redirects=False
        )
        self.assertNotIn("Cookie", self.sdk.request.call_args.kwargs["headers"])
        copied = self.sdk.cookies
        self.assertIsNot(copied, jar)
        self.assertEqual(
            {(c.name, c.domain, c.path, c.secure) for c in copied},
            {
                (c.name, c.domain, c.path, c.secure)
                for c in jar
                if c.name != "refresh_token_web"
            },
        )
        self.assertEqual(copied.get("access_token_web"), "old-secret")
        self.assertEqual(response.cookies.get("access_token_web"), "new-secret")
        self.assertEqual(response.cookies.get("refresh_token_web"), "new-refresh")
        self.assertNotIn("path-cookie", response.cookies)
        self.assertNotIn("other-cookie", response.cookies)
        self.assertNotIn("alien", response.cookies)
        self.assertEqual(jar.get("access_token_web"), "new-secret")
        rotated = next(c for c in response.cookies if c.name == "refresh_token_web")
        self.assertEqual(rotated.domain, ".vinted.co.uk")
        self.assertEqual(rotated.path, "/")
        self.assertTrue(rotated.secure)

    def test_unchanged_sdk_cookie_jar_cannot_appear_as_returned_refresh_tokens(self):
        self.session.cookies.set("access_token_web", "old-private-token")
        self.session.cookies.set("refresh_token_web", "old-private-refresh")
        response = self.session.post(
            BASE + "/web/api/auth/refresh", json={}, allow_redirects=False
        )
        self.assertEqual(response.cookies.get_dict(), {})
        self.assertEqual(
            self.session.cookies.get("access_token_web"), "old-private-token"
        )

    def test_cookie_deletion_applies_to_session_without_inventing_returned_token(self):
        self.session.cookies.set("access_token_web", "old", domain="www.vinted.co.uk")
        self.sdk.request.return_value = sdk_response(
            headers={"Set-Cookie": "access_token_web=; Path=/; Max-Age=0"}
        )
        response = self.session.get(BASE + "/", allow_redirects=False)
        self.assertNotIn("access_token_web", self.session.cookies)
        self.assertNotIn("access_token_web", response.cookies)

    def test_redirects_are_returned_without_following_even_when_requested(self):
        self.sdk.request.return_value = sdk_response(
            status=307,
            headers={"Location": "https://different.test/?private=secret"},
        )
        response = self.session.get(BASE + "/", allow_redirects=True)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.history, [])
        self.assertFalse(self.sdk.request.call_args.kwargs["allow_redirects"])
        self.sdk.request.assert_called_once()

    def test_streaming_chunks_are_bounded_and_early_close_stops_consumption(self):
        consumed = []

        def chunks():
            consumed.append(1)
            yield b"abcdefghijkl"
            consumed.append(2)
            yield b"do-not-consume"

        result = sdk_response()
        result.iter_content.return_value = chunks()
        self.sdk.request.return_value = result
        response = self.session.get(BASE + "/", stream=True, allow_redirects=False)
        self.assertIs(response._content, False)
        iterator = response.iter_content(4)
        self.assertEqual(next(iterator), b"abcd")
        self.assertEqual(next(iterator), b"efgh")
        response.close()
        with self.assertRaises(StopIteration):
            next(iterator)
        iterator.close()
        response.close()
        self.assertEqual(consumed, [1])
        result.close.assert_called_once()
        self.assertTrue(self.sdk.request.call_args.kwargs["stream"])

    def test_exhausted_stream_joins_bytes_and_releases_response(self):
        result = sdk_response(chunks=[b"abc", b"defgh"])
        self.sdk.request.return_value = result
        response = self.session.get(BASE + "/", stream=True, allow_redirects=False)
        self.assertEqual(
            list(response.iter_content(2)), [b"ab", b"c", b"de", b"fg", b"h"]
        )
        response.close()
        result.close.assert_called_once()

    def test_partial_raw_read_preserves_remainder_for_the_stream_and_eof(self):
        result = sdk_response(chunks=[b"abcdef", b"gh"])
        self.sdk.request.return_value = result
        response = self.session.get(BASE + "/", stream=True, allow_redirects=False)
        self.assertEqual(response.raw.read(3), b"abc")
        self.assertEqual(list(response.iter_content(2)), [b"de", b"f", b"gh"])
        self.assertEqual(response.raw.read(4), b"")
        result.close.assert_called_once()

    def test_raw_reads_crossing_eof_return_all_remaining_bytes_before_closing(self):
        result = sdk_response(chunks=[b"abc", b"def"])
        self.sdk.request.return_value = result
        response = self.session.get(BASE + "/", stream=True, allow_redirects=False)
        self.assertEqual(response.raw.read(0), b"")
        self.assertEqual(response.raw.read(2), b"ab")
        self.assertEqual(response.raw.read(10), b"cdef")
        self.assertEqual(response.raw.read(2), b"")
        result.close.assert_called_once()

    def test_environment_proxy_ca_and_client_certificate_are_forwarded(self):
        self.session.trust_env = True
        with patch.dict(
            os.environ,
            {
                "HTTPS_PROXY": "http://proxy-user:private-proxy@proxy.test:8080",
                "REQUESTS_CA_BUNDLE": "/tmp/test-ca.pem",
            },
            clear=True,
        ):
            self.session.get(
                BASE + "/",
                cert=("/tmp/cert.pem", "/tmp/key.pem"),
                allow_redirects=False,
            )
        sent = self.sdk.request.call_args.kwargs
        self.assertEqual(
            sent["proxies"], {"all": "http://proxy-user:private-proxy@proxy.test:8080"}
        )
        self.assertEqual(sent["verify"], "/tmp/test-ca.pem")
        self.assertEqual(sent["cert"], ("/tmp/cert.pem", "/tmp/key.pem"))

    def test_no_proxy_and_untrusted_environment_disable_independent_curl_proxy(self):
        for trust_env, no_proxy in ((False, ""), (True, "www.vinted.co.uk")):
            with self.subTest(trust_env=trust_env):
                self.session.trust_env = trust_env
                with patch.dict(
                    os.environ,
                    {
                        "HTTPS_PROXY": "http://private-env-proxy.test:8080",
                        "NO_PROXY": no_proxy,
                    },
                    clear=True,
                ):
                    self.session.get(BASE + "/", allow_redirects=False)
                self.assertEqual(
                    self.sdk.request.call_args.kwargs["proxies"], {"all": ""}
                )

    def test_explicit_request_proxy_and_ca_override_session_defaults(self):
        self.session.proxies = {"https": "http://session-proxy.test:8080"}
        self.session.verify = "/tmp/session-ca.pem"
        self.session.get(
            BASE + "/",
            proxies={"https": "http://request-proxy.test:8080"},
            verify="/tmp/request-ca.pem",
            allow_redirects=False,
        )
        sent = self.sdk.request.call_args.kwargs
        self.assertEqual(sent["proxies"], {"all": "http://request-proxy.test:8080"})
        self.assertEqual(sent["verify"], "/tmp/request-ca.pem")

    def test_tls_verification_cannot_be_disabled(self):
        with self.assertRaises(requests.exceptions.SSLError):
            self.session.get(BASE + "/", verify=False, allow_redirects=False)
        self.sdk.request.assert_not_called()

    def test_sdk_failures_keep_requests_categories_without_sensitive_details(self):
        for sdk_type, expected in (
            (curl_errors.ConnectTimeout, requests.exceptions.ConnectTimeout),
            (curl_errors.ReadTimeout, requests.exceptions.ReadTimeout),
            (curl_errors.Timeout, requests.exceptions.Timeout),
            (curl_errors.SSLError, requests.exceptions.SSLError),
            (curl_errors.ProxyError, requests.exceptions.ProxyError),
            (curl_errors.ConnectionError, requests.exceptions.ConnectionError),
            (curl_errors.RequestException, requests.exceptions.RequestException),
        ):
            with self.subTest(error=sdk_type.__name__):
                self.sdk.request.side_effect = sdk_type(
                    "private-token private-proxy private-url"
                )
                with self.assertRaises(expected) as raised:
                    self.session.get(BASE + "/", allow_redirects=False)
                self.assertNotIn("private", str(raised.exception))
                self.assertIsNone(raised.exception.request)
                self.assertIsNone(raised.exception.response)

    def test_stream_failure_is_normalized_and_closes_the_sdk_response(self):
        def chunks():
            yield b"abc"
            raise curl_errors.Timeout("private-token private-url")

        result = sdk_response()
        result.iter_content.return_value = chunks()
        self.sdk.request.return_value = result
        response = self.session.get(BASE + "/", stream=True, allow_redirects=False)
        with self.assertRaises(requests.exceptions.Timeout) as raised:
            list(response.iter_content(2))
        self.assertNotIn("private", str(raised.exception))
        result.close.assert_called_once()

    def test_cleanup_error_cannot_replace_the_original_stream_timeout(self):
        def chunks():
            raise curl_errors.Timeout("private-read-error")
            yield b"unreachable"

        for consume in (
            lambda response: list(response.iter_content(2)),
            lambda response: response.raw.read(2),
        ):
            result = sdk_response()
            result.iter_content.return_value = chunks()
            result.close.side_effect = curl_errors.SSLError("private-close-error")
            self.sdk.request.return_value = result
            response = self.session.get(BASE + "/", stream=True, allow_redirects=False)
            with self.assertRaises(requests.exceptions.Timeout) as raised:
                consume(response)
            self.assertNotIn("private", str(raised.exception))
            result.close.assert_called_once()

    def test_streaming_prepared_upload_is_not_reserialized(self):
        body = io.BytesIO(b"original-upload")
        response = self.session.post(
            BASE + "/api/v2/upload", data=body, allow_redirects=False
        )
        self.assertIs(self.sdk.request.call_args.kwargs["content"], body)
        self.assertEqual(response.request.headers["Content-Length"], "15")


if __name__ == "__main__":
    unittest.main()
