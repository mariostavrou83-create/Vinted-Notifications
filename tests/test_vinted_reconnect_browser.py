"""Owner browser isolation without starting a browser or contacting a website."""

import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import vinted_reconnect_browser as reconnect

OWNER = "owner-session-abcdefghijklmnop"
OTHER = "other-session-abcdefghijklmnop"
REVISION = "a" * 64
PROXY = "http://owner:private-proxy-secret@127.0.0.1:8080"


def auth_cookies():
    return [
        {
            "name": name,
            "value": f"fictional-{name}-credential-value",
            "domain": ".vinted.co.uk",
            "path": "/",
            "secure": True,
            "expires": -1,
        }
        for name in reconnect.COOKIE_NAMES
    ]


class FakeProvider:
    def __init__(self, proxy, *, navigate=True):
        self.proxy = proxy
        self.navigate = navigate
        self.warning = ""
        self.closed = False
        self.input_count = 0
        self.types = []
        self.thread_ids = [threading.get_ident()]
        self.cookie_values = auth_cookies()

    def frame(self):
        self.thread_ids.append(threading.get_ident())
        return b"fictional-jpeg"

    def action(self, action):
        self.thread_ids.append(threading.get_ident())
        self.input_count += 1
        self.types.append(action["type"])

    def cookies(self):
        self.thread_ids.append(threading.get_ident())
        return self.cookie_values

    def close(self):
        self.thread_ids.append(threading.get_ident())
        self.closed = True


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.revision = REVISION
        self.providers = []

        def factory(proxy, *, navigate=True):
            provider = FakeProvider(proxy, navigate=navigate)
            self.providers.append(provider)
            return provider

        self.manager = reconnect.BrowserManager(
            factory, clock=lambda: self.now, revision_reader=lambda: self.revision
        )

    def tearDown(self):
        self.manager.shutdown()

    def create(self, owner=OWNER):
        return self.manager.create(owner, REVISION, PROXY)["id"]

    def ready(self):
        identifier = self.create()
        _, frame = self.manager.frame(OWNER, identifier)
        return identifier, frame

    def action(self, kind="click", **fields):
        identifier, frame = self.ready()
        return identifier, {"type": kind, "seq": 1, "frame_seq": frame, **fields}

    def assert_reason(self, reason, operation):
        with self.assertRaises(reconnect.BrowserError) as error:
            operation()
        self.assertEqual(error.exception.reason, reason)

    def test_lazy_one_worker_and_no_secrets_in_public_state(self):
        self.assertIsNone(self.manager._thread)
        identifier = self.create()
        again = self.manager.create(OWNER, REVISION, PROXY)
        self.assertEqual(identifier, again["id"])
        self.assertEqual(len(self.providers), 1)
        self.assertNotIn("credential", str(again))
        self.assertNotIn("private-proxy-secret", str(again))
        self.assertNotIn(REVISION, str(again))
        self.assert_reason("busy", lambda: self.create(OTHER))

    def test_every_browser_operation_and_cleanup_uses_same_worker(self):
        identifier, action = self.action("text", text="private-input-secret")
        self.manager.action(OWNER, identifier, action)
        self.manager.extract_auth(OWNER, identifier)
        self.manager.cancel(OWNER, identifier)
        identifiers = set(self.providers[0].thread_ids)
        self.assertEqual(len(identifiers), 1)
        self.assertNotIn(threading.get_ident(), identifiers)
        self.assertTrue(self.providers[0].closed)

    def test_other_owner_cannot_view_type_extract_or_cancel(self):
        identifier, action = self.action(x=1, y=2)
        for method in (
            lambda: self.manager.state(OTHER, identifier),
            lambda: self.manager.frame(OTHER, identifier),
            lambda: self.manager.action(OTHER, identifier, action),
            lambda: self.manager.extract_auth(OTHER, identifier),
            lambda: self.manager.cancel(OTHER, identifier),
        ):
            self.assert_reason("closed", method)
        self.assertFalse(self.providers[0].closed)
        self.assertEqual(self.providers[0].input_count, 0)

    def test_replaced_buyer_revision_closes_without_input(self):
        identifier, action = self.action(x=1, y=2)
        self.revision = "b" * 64
        self.assert_reason(
            "replaced", lambda: self.manager.action(OWNER, identifier, action)
        )
        self.assertTrue(self.providers[0].closed)
        self.assertEqual(self.providers[0].input_count, 0)

    def test_idle_and_absolute_limits_do_not_extend_with_frames(self):
        identifier = self.create()
        self.now = reconnect.IDLE_TTL - 1
        self.manager.frame(OWNER, identifier)
        self.now += 1
        self.assert_reason("expired", lambda: self.manager.state(OWNER, identifier))
        identifier = self.create()
        _, frame = self.manager.frame(OWNER, identifier)
        self.now += reconnect.IDLE_TTL - 1
        self.manager.action(
            OWNER,
            identifier,
            {"type": "click", "x": 1, "y": 2, "seq": 1, "frame_seq": frame},
        )
        self.now = 300 + reconnect.ABSOLUTE_TTL
        self.assert_reason("expired", lambda: self.manager.state(OWNER, identifier))
        self.assertTrue(self.providers[-1].closed)

    def test_stale_frame_and_duplicate_sequence_prevent_repeat_submission(self):
        identifier, action = self.action("key", key="Enter")
        _, latest = self.manager.frame(OWNER, identifier)
        self.assert_reason(
            "stale_frame", lambda: self.manager.action(OWNER, identifier, action)
        )
        action["frame_seq"] = latest
        self.manager.action(OWNER, identifier, action)
        self.manager.frame(OWNER, identifier)
        self.manager.action(OWNER, identifier, action)
        self.assertEqual(self.providers[0].input_count, 1)

    def test_no_action_before_a_picture_or_with_future_sequence(self):
        identifier = self.create()
        self.assert_reason(
            "stale_frame",
            lambda: self.manager.action(
                OWNER,
                identifier,
                {"type": "click", "x": 1, "y": 2, "seq": 1, "frame_seq": 0},
            ),
        )
        _, frame = self.manager.frame(OWNER, identifier)
        self.assert_reason(
            "invalid",
            lambda: self.manager.action(
                OWNER,
                identifier,
                {"type": "click", "x": 1, "y": 2, "seq": 9, "frame_seq": frame},
            ),
        )
        self.assertEqual(self.providers[0].input_count, 0)

    def test_finite_bounds_and_fixed_commands(self):
        identifier, valid = self.action(x=1, y=2)
        invalid = [
            dict(valid, x=float("nan")),
            dict(valid, y=float("inf")),
            dict(valid, x=True),
            dict(valid, x=-1),
            dict(valid, x=reconnect.WIDTH),
            dict(valid, url="https://example.com"),
            dict(valid, seq=True),
            dict(valid, type=[]),
            {"type": "key", "key": "Control+L", "seq": 1, "frame_seq": 1},
            {"type": "key", "key": [], "seq": 1, "frame_seq": 1},
            {"type": "text", "text": "secret\n", "seq": 1, "frame_seq": 1},
            {"type": "text", "text": "\ud800", "seq": 1, "frame_seq": 1},
            {"type": "text", "text": "x" * 1025, "seq": 1, "frame_seq": 1},
            {"type": "scroll", "delta_y": 801, "seq": 1, "frame_seq": 1},
            {"type": "javascript", "script": "secret", "seq": 1, "frame_seq": 1},
        ]
        for action in invalid:
            with self.subTest(action_type=action.get("type")):
                self.assert_reason(
                    "invalid",
                    lambda action=action: self.manager.action(
                        OWNER, identifier, action
                    ),
                )
        self.assertEqual(self.providers[0].input_count, 0)

    def test_only_two_bound_cookies_leave_internal_manager(self):
        identifier = self.create()
        self.providers[0].cookie_values.append(
            {"name": "datadome", "value": "challenge-secret"}
        )
        envelope = self.manager.extract_auth(OWNER, identifier)
        self.assertEqual(
            [cookie["name"] for cookie in envelope.cookie_records],
            list(reconnect.COOKIE_NAMES),
        )
        self.assertEqual(envelope.revision, REVISION)
        self.assertNotIn("credential", repr(envelope))
        self.assertNotIn(REVISION, repr(envelope))
        self.assertEqual(envelope.csrf, "")

    def test_unknown_duplicate_expired_insecure_or_wrong_scope_cookies_reject(self):
        identifier = self.create()
        for changes in (
            {"domain": "evil.example"},
            {"domain": "vinted.co.uk"},
            {"partitionKey": "https://www.vinted.co.uk"},
            {"partitioned": True},
            {"secure": False},
            {"expires": 1},
            {"expires": float("nan")},
            {"path": "/api/v2/users/current/else"},
            {"value": "tiny"},
        ):
            cookies = auth_cookies()
            cookies[0].update(changes)
            self.providers[0].cookie_values = cookies
            self.assert_reason(
                "missing_credentials",
                lambda: self.manager.extract_auth(OWNER, identifier),
            )
        cookies = auth_cookies()
        cookies.append(dict(cookies[0]))
        self.providers[0].cookie_values = cookies
        self.assert_reason(
            "missing_credentials", lambda: self.manager.extract_auth(OWNER, identifier)
        )
        self.assertFalse(self.providers[0].closed)

    def test_cancel_and_shutdown_release_frame_cookie_context(self):
        identifier = self.create()
        self.manager.frame(OWNER, identifier)
        self.manager.cancel(OWNER, identifier)
        self.assertIsNone(self.manager._active)
        self.assertTrue(self.providers[0].closed)
        self.assert_reason(
            "closed", lambda: self.manager.extract_auth(OWNER, identifier)
        )

    def test_cancel_is_owner_bound_idempotent_after_import_changes_revision(self):
        identifier = self.create()
        self.revision = "b" * 64
        self.assert_reason("closed", lambda: self.manager.cancel(OTHER, identifier))
        self.assertFalse(self.providers[0].closed)
        expected = {"id": identifier, "state": "closed"}
        self.assertEqual(self.manager.cancel(OWNER, identifier), expected)
        self.assertTrue(self.providers[0].closed)
        self.assertEqual(self.manager.cancel(OWNER, identifier), expected)
        self.assert_reason("closed", lambda: self.manager.cancel(OTHER, identifier))

    def test_provider_error_is_fixed_and_does_not_leave_broken_session(self):
        def broken(_proxy):
            raise RuntimeError("private-input-secret private-proxy-secret")

        self.manager._factory = broken
        self.assert_reason("unavailable", self.create)
        self.assertIsNone(self.manager._active)

    def test_runtime_smoke_never_resolves_proxy_or_opens_vinted(self):
        with patch.object(reconnect, "_proxy_settings", side_effect=AssertionError):
            self.assertEqual(
                self.manager.runtime_check(), {"available": True, "stage": "ready"}
            )
        self.assertIsNone(self.providers[0].proxy)
        self.assertFalse(self.providers[0].navigate)
        self.assertTrue(self.providers[0].closed)
        identifier = self.create()
        self.assertEqual(
            self.manager.runtime_check(), {"available": False, "stage": "active"}
        )
        self.assertEqual(self.manager.state(OWNER, identifier)["state"], "ready")

    def test_runtime_smoke_missing_dependency_and_sandbox_failure_are_sanitized(self):
        for error, stage in (
            (ImportError("secret"), "dependencies"),
            (
                reconnect.BrowserError("unavailable", stage="browser_start"),
                "browser_start",
            ),
        ):
            with patch.object(self.manager, "_factory", side_effect=error):
                self.assertEqual(
                    self.manager.runtime_check(), {"available": False, "stage": stage}
                )


class NetworkAndRuntimeTests(unittest.TestCase):
    def test_allowlist_supports_real_auth_selector_captcha_and_assets_only(self):
        for url, method, navigation in (
            (reconnect.LOGIN_URL, "GET", True),
            (reconnect.BASE + "/member/register/select_type?ref_url=/", "GET", True),
            (reconnect.BASE + "/web/api/auth/refresh", "POST", False),
            (reconnect.BASE + "/api/v2/users/current", "GET", False),
            ("https://geo.captcha-delivery.com/captcha/?id=fictional", "GET", True),
            ("https://ct.captcha-delivery.com/captcha/check", "POST", False),
            ("https://marketplace-web-assets.vinted.com/app.js", "GET", False),
            ("https://images1.vinted.net/image.jpg", "GET", False),
        ):
            with self.subTest(host=urlsplit_host(url)):
                self.assertTrue(
                    reconnect.request_allowed(url, method, navigation=navigation)
                )

    def test_unknown_writes_external_oauth_purchase_paths_and_navigation_fail_closed(
        self,
    ):
        for url, method, navigation in (
            (reconnect.BASE + "/api/v2/conversations", "POST", False),
            (reconnect.BASE + "/api/v2/checkout/123", "GET", False),
            (reconnect.BASE + "/web/api/payment", "POST", False),
            (reconnect.BASE + "/web/api/auth/register", "POST", False),
            (reconnect.BASE + "/items/123", "GET", True),
            (reconnect.BASE + "/api/v2/users/123", "GET", False),
            (reconnect.BASE + "/web/api/auth/oauth", "PUT", False),
            (reconnect.BASE + "/web/api/auth/oauth", "OPTIONS", False),
            ("https://accounts.google.com/signin", "GET", True),
            ("https://www.vinted.co.uk.evil.example/member/login", "GET", True),
            ("https://www.vinted.co.uk:8080/member/login", "GET", True),
            ("https://owner:secret@www.vinted.co.uk/member/login", "GET", True),
            ("http://www.vinted.co.uk/member/login", "GET", True),
            ("https://images1.vinted.net/image.jpg", "GET", True),
            ("https://unknown.vinted.net/image.jpg", "GET", False),
            (reconnect.BASE + "/web/api/auth%2Foauth", "POST", False),
        ):
            with self.subTest(method=method):
                self.assertFalse(
                    reconnect.request_allowed(url, method, navigation=navigation)
                )

    def test_auth_writes_require_known_bounded_operation_body(self):
        body = {
            "client_id": "web",
            "scope": "user",
            "grant_type": "password",
            "username": "example-owner",
            "password": "example-value",
        }
        url = reconnect.BASE + "/web/api/auth/oauth"
        allowed = lambda data: reconnect.request_allowed(
            url, "POST", post_data=data, content_type="application/json"
        )
        self.assertTrue(allowed(json.dumps(body)))
        self.assertFalse(allowed(None))
        for change in (
            {"grant_type": "refresh_token"},
            {"grant_type": "registration"},
            {"client_id": "mobile"},
            {"scope": "payment"},
            {"username": ""},
            {"password": ""},
            {"password": "x" * 1025},
            {"purchase": True},
            {"redirect_uri": "https://evil.example"},
        ):
            with self.subTest(keys=tuple(change)):
                self.assertFalse(allowed(json.dumps(dict(body, **change))))
        for invalid in (
            "[]",
            "not-json",
            '["x"]',
            "{" + '"client_id":"web",' * 2 + '"scope":"user"}',
        ):
            self.assertFalse(allowed(invalid))
        two_factor = {
            "client_id": "web",
            "scope": "user",
            "grant_type": "password",
            "password_type": "two_factor_challenge_code",
            "control_code": "fictional-id",
            "verification_code": "123456",
            "is_trusted_device": False,
        }
        self.assertTrue(allowed(json.dumps(two_factor)))
        self.assertFalse(allowed(json.dumps(dict(two_factor, is_trusted_device=1))))
        self.assertFalse(allowed(json.dumps(dict(two_factor, verification_code="123"))))
        for forbidden in (
            "/web/api/auth/login",
            "/web/api/auth/verify",
            "/web/api/auth/two_factor",
        ):
            self.assertFalse(
                reconnect.request_allowed(
                    reconnect.BASE + forbidden,
                    "POST",
                    post_data=json.dumps(body),
                    content_type="application/json",
                )
            )
        self.assertFalse(
            reconnect.request_allowed(
                reconnect.BASE + "/web/api/auth/refresh",
                "POST",
                post_data=json.dumps({"payment": True}),
                content_type="application/json",
            )
        )

    def test_credentials_in_query_and_duplicate_form_fields_are_denied(self):
        for key in (
            "password",
            "PASSWORD",
            "username",
            "refresh_token",
            "verification_code",
        ):
            self.assertFalse(
                reconnect.request_allowed(
                    reconnect.BASE + "/member/login?" + key + "=fictional",
                    "GET",
                    navigation=True,
                )
            )
        form = "client_id=web&scope=user&grant_type=password&username=example&password=fictional"
        self.assertTrue(
            reconnect.request_allowed(
                reconnect.BASE + "/web/api/auth/oauth",
                "POST",
                post_data=form,
                content_type="application/x-www-form-urlencoded",
            )
        )
        self.assertFalse(
            reconnect.request_allowed(
                reconnect.BASE + "/web/api/auth/oauth",
                "POST",
                post_data=form + "&username=another",
                content_type="application/x-www-form-urlencoded",
            )
        )

    def test_browser_launch_is_sandboxed_ephemeral_no_serviceworker_download_or_secrets(
        self,
    ):
        calls = []
        page = SimpleNamespace(
            set_default_timeout=lambda timeout: calls.append(("timeout", timeout)),
            goto=lambda url, **kwargs: calls.append(("goto", url)),
        )
        context = SimpleNamespace(
            route=lambda *args: calls.append(("route", args[0])),
            route_web_socket=lambda *args: calls.append(("websocket", args[0])),
            new_page=lambda: page,
            on=lambda *args: calls.append(("popup", args[0])),
            close=lambda: calls.append(("context_close",)),
        )
        browser = SimpleNamespace(
            new_context=lambda **kwargs: (calls.append(("context", kwargs)) or context),
            close=lambda: calls.append(("browser_close",)),
        )
        driver = SimpleNamespace(
            chromium=SimpleNamespace(
                launch=lambda **kwargs: (calls.append(("launch", kwargs)) or browser)
            ),
            stop=lambda: calls.append(("driver_stop",)),
        )
        module = SimpleNamespace(
            sync_playwright=lambda: SimpleNamespace(start=lambda: driver)
        )
        with patch.dict("sys.modules", {"playwright.sync_api": module}), patch.dict(
            "os.environ", {"PRIVATE_APPLICATION_SECRET": "not-for-browser"}
        ):
            provider = reconnect._PlaywrightProvider(None, navigate=False)
            provider.close()
        launch = next(entry[1] for entry in calls if entry[0] == "launch")
        options = next(entry[1] for entry in calls if entry[0] == "context")
        self.assertTrue(launch["chromium_sandbox"])
        self.assertFalse(launch["handle_sigint"])
        self.assertFalse(launch["handle_sigterm"])
        self.assertFalse(launch["handle_sighup"])
        self.assertNotIn("args", launch)
        self.assertNotIn("PRIVATE_APPLICATION_SECRET", launch["env"])
        self.assertEqual(options["service_workers"], "block")
        self.assertFalse(options["accept_downloads"])
        self.assertNotIn("ignore_https_errors", options)
        self.assertNotIn("record_video_dir", options)
        self.assertIn(("goto", "about:blank"), calls)
        self.assertLess(
            calls.index(("route", "**/*")), calls.index(("goto", "about:blank"))
        )
        self.assertIn(("websocket", "**/*"), calls)
        self.assertIn(("popup", "page"), calls)
        self.assertIn(("context_close",), calls)
        self.assertIn(("browser_close",), calls)
        self.assertIn(("driver_stop",), calls)

    def route_fixture(
        self, responses, *, url=None, method="GET", navigation=False, post_data=None
    ):
        calls = []
        queue = [
            SimpleNamespace(
                status=status,
                headers=headers,
                dispose=lambda: calls.append(("dispose",)),
            )
            for status, headers in responses
        ]
        request = SimpleNamespace(
            url=url or reconnect.LOGIN_URL,
            method=method,
            is_navigation_request=lambda: navigation,
            post_data=post_data,
            headers={
                "content-type": "application/json",
                "authorization": "fictional-private",
                "cookie": "fictional-cookie",
                "host": "www.vinted.co.uk",
                "accept": "text/html",
            },
        )

        def fetch(**kwargs):
            calls.append(("fetch", kwargs))
            return queue.pop(0)

        route = SimpleNamespace(
            request=request,
            fetch=fetch,
            fulfill=lambda **kwargs: calls.append(("fulfill", kwargs)),
            abort=lambda: calls.append(("abort",)),
        )
        provider = reconnect._PlaywrightProvider.__new__(reconnect._PlaywrightProvider)
        provider.network_allowed = True
        provider.warning = ""
        return provider, route, calls

    def test_redirect_guard_never_fulfills_3xx_or_replays_post_credentials(self):
        body = json.dumps(
            {
                "client_id": "web",
                "scope": "user",
                "grant_type": "password",
                "username": "fictional-owner",
                "password": "fictional-value",
            }
        )
        for method, url, data, location in (
            ("GET", reconnect.LOGIN_URL, None, "https://evil.example/"),
            (
                "GET",
                reconnect.LOGIN_URL,
                None,
                "https://geo.captcha-delivery.com/captcha/",
            ),
            ("GET", reconnect.LOGIN_URL, None, "/api/v2/conversations"),
            ("GET", reconnect.LOGIN_URL, None, "/web/api/payment"),
            ("POST", reconnect.BASE + "/web/api/auth/oauth", body, reconnect.LOGIN_URL),
            (
                "POST",
                reconnect.BASE + "/web/api/auth/oauth",
                body,
                "https://ct.captcha-delivery.com/captcha/check",
            ),
        ):
            with self.subTest(method=method, location=location):
                provider, route, calls = self.route_fixture(
                    [(307, {"location": location})],
                    method=method,
                    url=url,
                    post_data=data,
                )
                provider._route(route)
                self.assertEqual(sum(entry[0] == "fetch" for entry in calls), 1)
                self.assertFalse(any(entry[0] == "fulfill" for entry in calls))
                self.assertIn(("abort",), calls)
                self.assertIn(("dispose",), calls)
                self.assertEqual(provider.warning, "unsupported_flow")

    def test_cross_origin_redirect_cannot_inject_content_or_cookies_for_any_resource(
        self,
    ):
        for navigation in (False, True):
            provider, route, calls = self.route_fixture(
                [
                    (
                        302,
                        {
                            "location": "https://geo.captcha-delivery.com/captcha/",
                            "set-cookie": "fictional-challenge",
                        },
                    )
                ],
                navigation=navigation,
            )
            provider._route(route)
            self.assertEqual(sum(entry[0] == "fetch" for entry in calls), 1)
            self.assertFalse(any(entry[0] == "fulfill" for entry in calls))
            self.assertIn(("abort",), calls)
            self.assertEqual(provider.warning, "unsupported_flow")

    def test_driver_debug_logging_is_rejected_before_start_without_env_mutation(self):
        starts = []
        module = SimpleNamespace(sync_playwright=lambda: starts.append(True))
        for variable, value in (
            ("PWDEBUG", "1"),
            ("PWDEBUG", "console"),
            ("DEBUG", "pw:protocol"),
            ("DEBUG", "*"),
            ("DEBUG", "app:*"),
        ):
            with patch.dict("sys.modules", {"playwright.sync_api": module}), patch.dict(
                "os.environ", {"PWDEBUG": "", "DEBUG": "", variable: value}
            ):
                with self.assertRaises(reconnect.BrowserError) as error:
                    reconnect._PlaywrightProvider(None, navigate=False)
                self.assertEqual(error.exception.stage, "browser_start")
                self.assertEqual(__import__("os").environ[variable], value)
        self.assertEqual(starts, [])

    def test_redirect_guard_checks_each_get_hop_and_disposes_responses(self):
        provider, route, calls = self.route_fixture(
            [
                (302, {"location": "/member/login"}),
                (200, {"set-cookie": "fictional-secure-cookie"}),
            ],
            navigation=True,
        )
        provider._route(route)
        fetches = [entry[1] for entry in calls if entry[0] == "fetch"]
        self.assertEqual(len(fetches), 2)
        self.assertTrue(
            all(
                entry["max_redirects"] == 0 and entry["max_retries"] == 0
                for entry in fetches
            )
        )
        self.assertTrue(all(0 < entry["timeout"] <= 6000 for entry in fetches))
        self.assertEqual(fetches[1]["method"], "GET")
        self.assertEqual(fetches[1]["post_data"], "")
        self.assertEqual(fetches[1]["url"], reconnect.BASE + "/member/login")
        self.assertEqual(fetches[1]["headers"], {"accept": "text/html"})
        fulfills = [entry[1] for entry in calls if entry[0] == "fulfill"]
        self.assertEqual(len(fulfills), 1)
        self.assertEqual(fulfills[0]["response"].status, 200)
        self.assertEqual(
            fulfills[0]["response"].headers["set-cookie"], "fictional-secure-cookie"
        )
        self.assertEqual(sum(entry[0] == "dispose" for entry in calls), 2)

    def test_redirect_loop_and_callback_exception_are_bounded_and_silent(self):
        provider, route, calls = self.route_fixture(
            [(302, {"location": "/member/login"})] * 4, navigation=True
        )
        provider._route(route)
        self.assertEqual(sum(entry[0] == "fetch" for entry in calls), 4)
        self.assertIn(("abort",), calls)
        self.assertFalse(any(entry[0] == "fulfill" for entry in calls))
        provider, route, calls = self.route_fixture([])
        route.fetch = lambda **_: (_ for _ in ()).throw(
            RuntimeError("fictional-private-response")
        )
        provider._route(route)
        self.assertIn(("abort",), calls)
        self.assertEqual(provider.warning, "unsupported_flow")

    def test_frame_masks_all_editable_fields_including_revealed_password_and_codes(
        self,
    ):
        provider = reconnect._PlaywrightProvider.__new__(reconnect._PlaywrightProvider)
        captured = {}
        provider.page = SimpleNamespace(
            screenshot=lambda **kwargs: (captured.update(kwargs) or b"jpeg")
        )
        self.assertEqual(provider.frame(), b"jpeg")
        self.assertIn("input, textarea", captured["style"])
        self.assertIn("[contenteditable]", captured["style"])
        self.assertIn("transparent !important", captured["style"])
        self.assertEqual(captured["caret"], "hide")

    def test_cookie_collection_includes_identity_and_refresh_paths(self):
        provider = reconnect._PlaywrightProvider.__new__(reconnect._PlaywrightProvider)
        urls = []
        provider.context = SimpleNamespace(
            cookies=lambda values: urls.extend(values) or []
        )
        self.assertEqual(provider.cookies(), [])
        self.assertEqual(
            urls,
            [
                "https://www.vinted.co.uk/api/v2/users/current",
                "https://www.vinted.co.uk/web/api/auth/refresh",
            ],
        )

    def test_timed_out_queued_job_never_runs_late(self):
        manager = reconnect.BrowserManager(
            FakeProvider, revision_reader=lambda: REVISION
        )
        entered, release = threading.Event(), threading.Event()
        submitted = []
        blocker = reconnect._Job(
            lambda: (entered.set(), release.wait(1)), time.monotonic() + 2
        )
        try:
            with manager._lifecycle:
                manager._thread = threading.Thread(target=manager._run, daemon=True)
                manager._thread.start()
            manager._jobs.put_nowait(blocker)
            self.assertTrue(entered.wait(1))
            with self.assertRaises(reconnect.BrowserError) as error:
                manager._call(
                    lambda: submitted.append("late credential action"), timeout=0.02
                )
            self.assertEqual(error.exception.reason, "timeout")
            release.set()
            manager._jobs.join()
            self.assertEqual(submitted, [])
        finally:
            release.set()
            manager.shutdown()


def urlsplit_host(url):
    from urllib.parse import urlsplit

    return urlsplit(url).hostname


if __name__ == "__main__":
    unittest.main()
