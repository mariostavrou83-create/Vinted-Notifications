"""Mocked solver contracts: no paid tasks, proxies or Vinted calls are made."""

import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

import vinted_captcha as captcha

CHALLENGE = "https://geo.captcha-delivery.com/captcha/?cid=private-session&t=fe&referer=https%3A%2F%2Fwww.vinted.co.uk%2F"
PROXY = "http://proxy-user:private-password@proxy.example.com:8080"
API_KEY = "CAP-private-solver-key"
COOKIE = "private-datadome-token_123456789"


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def response(data=None, *, status=200, chunks=None):
    result = MagicMock(status_code=status)
    result.__enter__.return_value = result
    result.iter_content.return_value = (
        chunks if chunks is not None else [json.dumps(data).encode()]
    )
    return result


def created():
    return {"errorId": 0, "taskId": "task-123", "status": "idle"}


def solved(cookie=None, **solution):
    return {
        "errorId": 0,
        "status": "ready",
        "solution": {
            "cookie": cookie or "datadome=" + COOKIE + "; Path=/; Secure",
            **solution,
        },
    }


class ChallengeTests(unittest.TestCase):
    def test_generic_denial_error_names_and_success_never_create_challenge(self):
        for status, body in (
            (403, "Forbidden"),
            (403, '{"error":"captcha_required"}'),
            (200, json.dumps({"url": CHALLENGE})),
            (429, json.dumps({"url": CHALLENGE})),
        ):
            self.assertIsNone(
                captcha.extract_challenge(
                    SimpleNamespace(status_code=status, text=body)
                )
            )

    def test_explicit_json_html_and_redirect_urls_are_recognised(self):
        for key in ("url", "captcha_url", "challenge_url"):
            r = SimpleNamespace(status_code=403, text=json.dumps({key: CHALLENGE}))
            self.assertEqual(captcha.extract_challenge(r), CHALLENGE)
        r = SimpleNamespace(
            status_code=403,
            text='<iframe src="' + CHALLENGE.replace("&", "&amp;") + '"></iframe>',
        )
        self.assertEqual(captcha.extract_challenge(r), CHALLENGE)
        r = SimpleNamespace(
            status_code=403,
            text='<script src="' + CHALLENGE.replace("&", "&amp;") + '"></script>',
        )
        self.assertEqual(captcha.extract_challenge(r), CHALLENGE)
        r = SimpleNamespace(
            status_code=307,
            headers=requests.structures.CaseInsensitiveDict({"location": CHALLENGE}),
        )
        self.assertEqual(captcha.extract_challenge(r), CHALLENGE)

    def test_unsafe_hosts_credentials_ports_and_wrong_paths_are_rejected(self):
        unsafe = (
            CHALLENGE.replace("https:", "http:"),
            CHALLENGE.replace(
                "geo.captcha-delivery.com", "evil.geo.captcha-delivery.com"
            ),
            CHALLENGE.replace(
                "geo.captcha-delivery.com", "geo.captcha-delivery.com.evil.test"
            ),
            CHALLENGE.replace(
                "geo.captcha-delivery.com", "secret@geo.captcha-delivery.com"
            ),
            CHALLENGE.replace(
                "geo.captcha-delivery.com", "geo.captcha-delivery.com:444"
            ),
            CHALLENGE.replace("/captcha/", "/account/"),
            CHALLENGE + "#secret",
            CHALLENGE + "\nsecret",
            CHALLENGE + "&t=fe",
            CHALLENGE.replace("t=fe", "t=unknown"),
            CHALLENGE.replace("www.vinted.co.uk", "evil.test"),
            CHALLENGE.replace("t=fe", "missing=true"),
        )
        for url in unsafe:
            with self.subTest(url=url):
                self.assertIsNone(
                    captcha.extract_challenge(
                        SimpleNamespace(status_code=403, text=""), {"url": url}
                    )
                )

    def test_banned_ip_is_explicit_and_overlong_bodies_are_not_parsed(self):
        blocked = CHALLENGE.replace("t=fe", "t=bv")
        self.assertEqual(
            captcha.extract_challenge(
                SimpleNamespace(status_code=403, text=""), {"url": blocked}
            ),
            blocked,
        )
        body = " " * captcha.MAX_RESPONSE + '<iframe src="' + CHALLENGE + '"></iframe>'
        self.assertIsNone(
            captcha.extract_challenge(SimpleNamespace(status_code=403, text=body))
        )


class ProxyTests(unittest.TestCase):
    def test_documented_url_and_compact_proxy_formats(self):
        for raw, expected in (
            (PROXY, PROXY),
            (
                "proxy.example.com:8080:user:pwd",
                "http://user:pwd@proxy.example.com:8080",
            ),
            (
                "http:proxy.example.com:8080:user:pwd",
                "http://user:pwd@proxy.example.com:8080",
            ),
            (
                "socks5:proxy.example.com:1080:user:pwd",
                "socks5://user:pwd@proxy.example.com:1080",
            ),
            ("https://proxy.example.com:443/", "https://proxy.example.com:443"),
            (
                "http://user:p%40ss@proxy.example.com:8080",
                "http://user:p%40ss@proxy.example.com:8080",
            ),
            ("proxy.example.com:8080", "http://proxy.example.com:8080"),
        ):
            self.assertEqual(captcha.normalize_proxy(raw), expected)

    def test_missing_unsafe_and_nonremote_proxy_formats(self):
        for raw in (
            None,
            "",
            "http://localhost:8080",
            "http://127.0.0.1:8080",
            "http://10.0.0.1:8080",
            "http://proxy.example.com",
            "http://proxy.example.com:0",
            "http://proxy.example.com:65536",
            "http://proxy.example.com:8080/path",
            "http://proxy.example.com:8080?token=secret",
            "http://user@proxy.example.com:8080",
            "http://:pwd@proxy.example.com:8080",
            "http://user:@proxy.example.com:8080",
            "ftp://user:pwd@proxy.example.com:8080",
            "http://user:p%0ad@proxy.example.com:8080",
            "http://bad-.example.com:8080",
            "proxy.example.com:8080:user:pwd:more",
            "http://proxy.example.com:8080\nsecret",
        ):
            with self.subTest(proxy=raw):
                self.assertIsNone(captcha.normalize_proxy(raw))


class SolverTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.clock = Clock()
        self.session = MagicMock()
        self.session.__enter__.return_value = self.session
        self.factory = self.stack.enter_context(
            patch.object(captcha.requests, "Session", return_value=self.session)
        )
        self.stack.enter_context(
            patch.object(captcha.time, "monotonic", side_effect=self.clock.monotonic)
        )
        self.stack.enter_context(
            patch.object(captcha.time, "sleep", side_effect=self.clock.sleep)
        )

    def solve(self, **overrides):
        args = {
            "proxy": PROXY,
            "api_key": API_KEY,
            "user_agent": captcha.CHROME_USER_AGENT,
            "enabled": True,
        }
        args.update(overrides)
        return captcha.solve_datadome(args.pop("challenge_url", CHALLENGE), **args)

    def test_opt_in_missing_config_invalid_inputs_and_ip_bans_make_no_tasks(self):
        for args, state in (
            ({"enabled": False}, "disabled"),
            ({"enabled": "true"}, "disabled"),
            ({"api_key": ""}, "missing_config"),
            ({"proxy": ""}, "missing_config"),
            ({"api_key": "private key\nvalue"}, "missing_config"),
            ({"proxy": "http://localhost:8080"}, "invalid_proxy"),
            ({"user_agent": "Mozilla/5.0"}, "unsupported_browser"),
            ({"challenge_url": "https://evil.test/captcha/?t=fe"}, "invalid_challenge"),
            ({"challenge_url": CHALLENGE.replace("t=fe", "t=bv")}, "ip_blocked"),
            ({"website_url": "https://evil.test/"}, "invalid_challenge"),
        ):
            self.assertEqual(self.solve(**args).state, state)
        self.factory.assert_not_called()

    def test_success_current_task_type_fixed_proxy_ua_and_verified_api_calls(self):
        self.session.post.side_effect = [
            response(created()),
            response({"errorId": 0, "status": "processing"}),
            response(solved()),
        ]
        result = self.solve()
        self.assertEqual(result.state, "solved")
        self.assertEqual(result.cookie, COOKIE)
        self.assertEqual(result.polls, 2)
        self.assertEqual(self.clock.sleeps, [3, 3])
        calls = self.session.post.call_args_list
        self.assertEqual(calls[0].args, ("https://api.capsolver.com/createTask",))
        task = calls[0].kwargs["json"]["task"]
        self.assertEqual(
            task,
            {
                "type": "DatadomeSliderTask",
                "websiteURL": captcha.WEBSITE,
                "captchaUrl": CHALLENGE,
                "proxy": PROXY,
                "userAgent": captcha.CHROME_USER_AGENT,
            },
        )
        for call in calls:
            self.assertTrue(call.kwargs["verify"])
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertTrue(call.kwargs["stream"])
            self.assertGreater(min(call.kwargs["timeout"]), 0)
            self.assertLessEqual(sum(call.kwargs["timeout"]), captcha.MAX_SECONDS)
        self.assertEqual(
            calls[1].kwargs["json"], {"clientKey": API_KEY, "taskId": "task-123"}
        )
        self.session.__exit__.assert_called_once()

    def test_interstitial_uses_same_documented_proxy_required_task_type(self):
        url = CHALLENGE.replace("/captcha/", "/interstitial/")
        self.session.post.return_value = response(solved())
        self.assertEqual(self.solve(challenge_url=url).state, "solved")
        task = self.session.post.call_args.kwargs["json"]["task"]
        self.assertEqual(task["type"], "DatadomeSliderTask")
        self.assertEqual(task["proxy"], PROXY)

    def test_solver_cookie_and_metadata_are_never_in_public_diagnostics(self):
        self.session.post.return_value = response(
            solved("datadome=" + COOKIE + "; Domain=evil.test; Path=/")
        )
        result = self.solve()
        self.assertEqual(result.cookie, COOKIE)
        public = repr(result) + json.dumps(result.public())
        for secret in (COOKIE, API_KEY, PROXY, CHALLENGE, "evil.test"):
            self.assertNotIn(secret, public)
        self.assertEqual(result.public(), {"state": "solved", "polls": 0})

    def test_service_errors_and_network_exceptions_do_not_echo_secrets(self):
        secret = API_KEY + PROXY + COOKIE + CHALLENGE
        self.session.post.return_value = response(
            {"errorId": 1, "errorDescription": secret}
        )
        with self.assertNoLogs("vinted_captcha"):
            result = self.solve()
        self.assertEqual(result.state, "service_error")
        self.assertNotIn(secret, repr(result))
        self.session.post.side_effect = requests.exceptions.SSLError(secret)
        self.assertEqual(self.solve().state, "network_error")

    def test_invalid_cookie_names_token_characters_and_browser_mismatch_fail(self):
        for cookie in (
            "session=" + COOKIE,
            COOKIE,
            "datadome=",
            "datadome=short",
            "datadome=" + COOKIE + "\r\nSecret: x",
            "datadome=" + COOKIE + ",other",
            'datadome="' + COOKIE + '"',
        ):
            self.session.post.return_value = response(solved(cookie))
            self.assertEqual(self.solve().state, "invalid_cookie")
        self.session.post.return_value = response(solved(userAgent="Wrong browser"))
        self.assertEqual(self.solve().state, "invalid_cookie")

    def test_response_body_redirect_and_json_schemas_are_bounded(self):
        for fake, state in (
            (response(status=307), "service_error"),
            (response(chunks=[b"x" * (captcha.MAX_RESPONSE + 1)]), "invalid_response"),
            (response(chunks=[b"not json"]), "invalid_response"),
            (response({"errorId": False, "taskId": "task"}), "invalid_response"),
            (response({"errorId": 0, "taskId": "task\nsecret"}), "invalid_response"),
            (response([{"errorId": 0}]), "invalid_response"),
            (response({"errorId": 0}), "invalid_response"),
            (
                response({"errorId": 0, "status": "failed", "taskId": "task"}),
                "service_error",
            ),
        ):
            self.session.post.return_value = fake
            self.assertEqual(self.solve().state, state)

    def test_task_poll_limit_and_elapsed_time_stop_without_creating_another_task(self):
        self.session.post.side_effect = [response(created())] + [
            response({"errorId": 0, "status": "processing"}) for _ in range(3)
        ]
        with patch.object(captcha, "MAX_POLLS", 3):
            result = self.solve()
        self.assertEqual((result.state, result.polls), ("timeout", 3))
        self.assertEqual(self.session.post.call_count, 4)
        self.session.post.reset_mock()
        self.session.post.side_effect = None
        self.session.post.return_value = response(created())
        with patch.object(captcha, "MAX_SECONDS", 2):
            result = self.solve()
        self.assertEqual((result.state, result.polls), ("timeout", 0))
        self.assertEqual(self.session.post.call_count, 1)

    def test_slow_api_response_is_stopped_by_absolute_deadline(self):
        def slow_post(*args, **kwargs):
            self.clock.now += captcha.MAX_SECONDS + 1
            return response(created())

        self.session.post.side_effect = slow_post
        self.assertEqual(self.solve().state, "timeout")
        self.assertEqual(self.session.post.call_count, 1)

    def test_failed_unknown_or_mismatched_task_results_are_terminal(self):
        for data, state in (
            ({"errorId": 0, "status": "failed"}, "service_error"),
            ({"errorId": 0, "status": "unknown"}, "invalid_response"),
            (
                {
                    "errorId": 0,
                    "status": "ready",
                    "taskId": "another-task",
                    "solution": {"cookie": "datadome=" + COOKIE},
                },
                "invalid_response",
            ),
        ):
            self.session.post.side_effect = [response(created()), response(data)]
            self.assertEqual(self.solve().state, state)
