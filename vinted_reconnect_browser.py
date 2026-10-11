"""Private, owner-operated, temporary Vinted sign-in browser.

The dashboard supplies authentication and CSRF protection. This manager binds
each browser to that authenticated session and the encrypted buyer revision.
It never buys, verifies an account, persists browser state, or logs input.
Only the separate same-account importer may consume ``AuthEnvelope``.
"""

import hashlib
import json
import math
import os
import queue
import re
import secrets
import threading
import time
from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

BASE = "https://www.vinted.co.uk"
# The canonical target of the UK's public Sign up | Log in link. The owner
# chooses the ordinary Log in control, without a registration write.
LOGIN_URL = BASE + "/member/register/select_type?ref_url=%2F"
WIDTH, HEIGHT = 390, 760
ABSOLUTE_TTL, IDLE_TTL = 600, 300
MAX_FRAME_BYTES = 2_000_000
COOKIE_NAMES = ("access_token_web", "refresh_token_web")
COOKIE_PATHS = {
    "access_token_web": "/api/v2/users/current",
    "refresh_token_web": "/web/api/auth/refresh",
}
COOKIE_DOMAINS = frozenset({"www.vinted.co.uk", ".www.vinted.co.uk", ".vinted.co.uk"})
KEYS = frozenset(
    {
        "Enter",
        "Tab",
        "Shift+Tab",
        "Backspace",
        "Delete",
        "Escape",
        "Space",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "Home",
        "End",
    }
)
AUTH_POST_PATHS = frozenset(
    {
        "/web/api/auth/oauth",
        "/web/api/auth/refresh",
    }
)
SECRET_QUERY_KEYS = frozenset(
    {
        "password",
        "username",
        "email",
        "access_token",
        "refresh_token",
        "token",
        "code",
        "verification_code",
    }
)
LOGIN_PATH = re.compile(
    r"^/member/(?:signup/select_type|register/select_type|(?:general/)?login"
    r"(?:/(?:select_type|email|verification|two_factor))?)/?$"
)
READ_PATH = re.compile(
    r"^/(?:api/v2|web/api)/(?:auth(?:/[^/]*)?|users/current|configurations?"
    r"|features|experiments|countries|locales|cookie_consents?|consents)(?:/)?$"
)
FORBIDDEN_PATH = re.compile(
    r"(?:^|[/_.-])(?:checkouts?|conversations?|payments?|transactions?|orders?"
    r"|purchases?|wallet|bank_accounts?|credit_cards?|billing)(?:$|[/_.-])",
    re.IGNORECASE,
)
CAPTCHA_HOSTS = frozenset({"geo.captcha-delivery.com", "ct.captcha-delivery.com"})
ASSET_HOSTS = frozenset(
    {"static.vinted.com", "marketplace-web-assets.vinted.com", "images1.vinted.net"}
)
MASK_STYLE = """
input, textarea, [contenteditable]:not([contenteditable="false"]) {
    color: transparent !important;
    -webkit-text-security: disc !important;
    text-shadow: none !important;
    filter: blur(14px) !important;
}
"""
MESSAGES = {
    "unavailable": "The private sign-in browser is unavailable. Your saved connection was not changed.",
    "busy": "Another private sign-in window is open. Close it before starting another.",
    "expired": "This sign-in window expired. Start a fresh connection window.",
    "closed": "This sign-in window is closed. Your saved connection was not changed.",
    "replaced": "The saved buyer connection changed. Check the saved account before reconnecting.",
    "invalid": "This sign-in action is invalid. Refresh the private sign-in view.",
    "stale_frame": "The sign-in view changed. Wait for the latest picture, then try that action again.",
    "timeout": "The sign-in browser did not finish in time. Start a fresh connection window.",
    "missing_credentials": "Complete Vinted's ordinary sign-in and security checks before verifying the connection.",
    "proxy_required": "A saved fixed UK buyer proxy is required for this private sign-in window.",
}


class BrowserError(ValueError):
    """Fixed public reason only; no browser exception or provider body."""

    def __init__(self, reason="unavailable", *, stage=None):
        self.reason = reason if reason in MESSAGES else "unavailable"
        self.stage = (
            stage
            if stage in ("dependencies", "browser_start", "context_start", "page_open")
            else None
        )
        super().__init__(MESSAGES[self.reason])


@dataclass(frozen=True)
class AuthEnvelope:
    """Internal transfer only. Never serialize this object into an HTTP reply."""

    cookie_records: tuple = field(repr=False)
    revision: str = field(repr=False)
    csrf: str = field(default="", repr=False)


@dataclass
class _Session:
    identifier: str
    owner_digest: str = field(repr=False)
    revision: str = field(repr=False)
    started: float
    touched: float
    provider: object = field(default=None, repr=False)
    frame_seq: int = 0
    last_action_seq: int = 0
    aborted: threading.Event = field(default_factory=threading.Event, repr=False)


@dataclass
class _Job:
    operation: object = field(repr=False)
    deadline: float
    future: Future = field(default_factory=Future, repr=False)
    cancelled: threading.Event = field(default_factory=threading.Event, repr=False)


def _unique_pairs(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate field")
        result[name] = value
    return result


def _auth_body_allowed(path, post_data, content_type):
    """Only auth schemas already used by the buyer client, without logging input."""
    if path == "/web/api/auth/refresh":
        return post_data in (None, "")
    if path != "/web/api/auth/oauth" or not isinstance(post_data, str):
        return False
    if len(post_data) > 8192 or not isinstance(content_type, str):
        return False
    try:
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type == "application/json":
            body = json.loads(post_data, object_pairs_hook=_unique_pairs)
        elif media_type == "application/x-www-form-urlencoded":
            body = _unique_pairs(
                parse_qsl(
                    post_data,
                    keep_blank_values=True,
                    strict_parsing=True,
                    max_num_fields=10,
                )
            )
        else:
            return False
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return False
    if not isinstance(body, dict) or not all(isinstance(key, str) for key in body):
        return False
    if (
        body.get("client_id") != "web"
        or body.get("scope") != "user"
        or body.get("grant_type") != "password"
    ):
        return False
    base = {"client_id", "scope", "grant_type"}
    if set(body) == base | {"username", "password"}:
        return (
            isinstance(body["username"], str)
            and 1 <= len(body["username"]) <= 254
            and isinstance(body["password"], str)
            and 1 <= len(body["password"]) <= 1024
            and "\x00" not in body["username"] + body["password"]
        )
    if set(body) != base | {
        "password_type",
        "control_code",
        "verification_code",
        "is_trusted_device",
    }:
        return False
    trusted = body["is_trusted_device"]
    return (
        body["password_type"] == "two_factor_challenge_code"
        and isinstance(body["control_code"], str)
        and bool(re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,1024}", body["control_code"]))
        and isinstance(body["verification_code"], str)
        and bool(re.fullmatch(r"[0-9A-Za-z-]{4,12}", body["verification_code"]))
        and (
            type(trusted) is bool
            or (
                media_type == "application/x-www-form-urlencoded"
                and trusted in ("true", "false")
            )
        )
    )


def request_allowed(
    url, method, *, navigation=False, post_data=None, content_type=None
):
    """Exact HTTPS origins and known authentication operations only."""
    if (
        not isinstance(url, str)
        or len(url) > 8192
        or method not in ("GET", "HEAD", "POST", "OPTIONS")
    ):
        return False
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or not parsed.hostname
            or "\\" in url
            or re.search(r"%2[fF]|%5[cC]|%00", parsed.path)
        ):
            return False
    except (ValueError, UnicodeError):
        return False
    host, path = parsed.hostname, unquote(parsed.path or "/")
    try:
        query = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=30)
    except (ValueError, UnicodeError):
        return False
    if any(name.lower() in SECRET_QUERY_KEYS for name, _ in query):
        return False
    if FORBIDDEN_PATH.search(path):
        return False
    if host in CAPTCHA_HOSTS:
        return method in ("GET", "HEAD", "POST")
    if host in ASSET_HOSTS:
        return not navigation and method in ("GET", "HEAD")
    if host != "www.vinted.co.uk":
        return False
    if method in ("GET", "HEAD"):
        if path == "/" or LOGIN_PATH.fullmatch(path):
            return True
        if navigation:
            return False
        return bool(
            READ_PATH.fullmatch(path)
            or path.startswith(("/_next/static/", "/assets/"))
            or path in ("/favicon.ico", "/manifest.json")
        )
    return (
        not navigation
        and method == "POST"
        and path in AUTH_POST_PATHS
        and not parsed.query
        and _auth_body_allowed(path, post_data, content_type)
    )


def _owner_digest(owner_key):
    if not isinstance(owner_key, str) or not 16 <= len(owner_key) <= 512:
        raise BrowserError("invalid")
    try:
        return hashlib.sha256(owner_key.encode()).hexdigest()
    except UnicodeError:
        raise BrowserError("invalid") from None


def _valid_revision(revision):
    return isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{64}", revision)


def _saved_revision():
    import vinted_buyer

    return vinted_buyer.browser_session_snapshot()["session_fingerprint"]


def _proxy_settings(value):
    if value is None:
        import vinted_buyer

        value = vinted_buyer.network_configuration()["proxy"]
    if not value:
        raise BrowserError("proxy_required")
    try:
        import vinted_buyer

        parsed = urlsplit(vinted_buyer.proxy_url(value))
        host = parsed.hostname
        if ":" in host:
            host = "[" + host + "]"
        result = {"server": f"{parsed.scheme}://{host}:{parsed.port}"}
        if parsed.username:
            result["username"] = unquote(parsed.username)
            result["password"] = unquote(parsed.password)
        return result
    except (ValueError, TypeError, UnicodeError):
        raise BrowserError("proxy_required") from None


def _cookies_for_import(cookies):
    if not isinstance(cookies, list) or len(cookies) > 200:
        raise BrowserError("missing_credentials")
    records = []
    for name in COOKIE_NAMES:
        matches = []
        for cookie in cookies:
            if not isinstance(cookie, dict) or cookie.get("name") != name:
                continue
            domain, path = cookie.get("domain"), cookie.get("path")
            value, expiry = cookie.get("value"), cookie.get("expires")
            applicable = COOKIE_PATHS[name]
            if (
                domain not in COOKIE_DOMAINS
                or "partitionKey" in cookie
                or cookie.get("partitioned") is not None
                or cookie.get("secure") is not True
                or not isinstance(path, str)
                or not path.startswith("/")
                or not (
                    path == applicable
                    or (
                        applicable.startswith(path)
                        and (
                            path.endswith("/")
                            or applicable[len(path) :].startswith("/")
                        )
                    )
                )
                or not isinstance(value, str)
                or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{16,8192}", value)
                or type(expiry) not in (int, float)
                or not math.isfinite(expiry)
                or (expiry != -1 and not time.time() < expiry <= 253402300799)
            ):
                raise BrowserError("missing_credentials")
            matches.append(
                {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path,
                    "secure": True,
                    "expires": None if expiry == -1 else int(expiry),
                    "domain_specified": domain.startswith("."),
                    "domain_initial_dot": domain.startswith("."),
                    "path_specified": True,
                    "discard": expiry == -1,
                }
            )
        if len(matches) != 1:
            raise BrowserError("missing_credentials")
        records.extend(matches)
    return tuple(records)


class _PlaywrightProvider:
    """All methods run on the manager's one browser thread."""

    def __init__(self, proxy, *, navigate=True):
        # The Playwright driver inherits process environment. Fail closed
        # before it starts if debugging could expose inputs or cookie data.
        # Do not mutate the server's global environment from this thread.
        if os.environ.get("PWDEBUG") not in (None, "", "0") or os.environ.get(
            "DEBUG"
        ) not in (None, "", "0"):
            raise BrowserError("unavailable", stage="browser_start")
        from playwright.sync_api import sync_playwright

        self.playwright = self.browser = self.context = self.page = None
        self.warning = ""
        self.network_allowed = navigate
        stage = "browser_start"
        try:
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(
                headless=True,
                chromium_sandbox=True,
                handle_sigint=False,
                handle_sigterm=False,
                handle_sighup=False,
                proxy=proxy,
                timeout=10_000,
                env={
                    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                    "HOME": "/tmp",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                },
            )
            stage = "context_start"
            self.context = self.browser.new_context(
                viewport={"width": WIDTH, "height": HEIGHT},
                locale="en-GB",
                accept_downloads=False,
                service_workers="block",
            )
            self.context.route("**/*", self._route)
            self.context.route_web_socket("**/*", lambda socket: socket.close())
            self.page = self.context.new_page()
            self.context.on("page", self._popup)
            self.page.set_default_timeout(3_000)
            stage = "page_open"
            self.page.goto(
                LOGIN_URL if navigate else "about:blank",
                timeout=12_000,
                wait_until="domcontentloaded",
            )
        except Exception:  # noqa: BLE001 - browser exceptions can contain secrets.
            self.close()
            raise BrowserError("unavailable", stage=stage) from None

    def _route(self, route):
        response = None
        try:
            request = route.request
            method, navigation = request.method, request.is_navigation_request()
            if not self.network_allowed or not request_allowed(
                request.url,
                method,
                navigation=navigation,
                post_data=request.post_data if method == "POST" else None,
                content_type=(
                    request.headers.get("content-type") if method == "POST" else None
                ),
            ):
                if navigation or method not in ("GET", "HEAD"):
                    self.warning = "unsupported_flow"
                route.abort()
                return
            deadline = time.monotonic() + 6
            url = request.url
            original = urlsplit(url)
            origin = (original.scheme, original.hostname, original.port or 443)
            options = {"max_redirects": 0, "max_retries": 0}
            for hop in range(4):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                response = route.fetch(timeout=max(1, remaining * 1000), **options)
                if not 300 <= response.status < 400:
                    if 200 <= response.status < 300 or 400 <= response.status <= 599:
                        route.fulfill(response=response)
                        return
                    break
                # Chromium otherwise follows redirects without route policy
                # re-evaluation. Never replay a credential-bearing POST or
                # fulfill a redirect that the browser could follow itself.
                if method not in ("GET", "HEAD") or hop == 3:
                    break
                location = response.headers.get("location")
                if not isinstance(location, str) or not 0 < len(location) <= 8192:
                    break
                destination = urljoin(url, location)
                if not request_allowed(destination, method, navigation=navigation):
                    break
                target = urlsplit(destination)
                if (target.scheme, target.hostname, target.port or 443) != origin:
                    # Fetch+fulfill serves content and Set-Cookie under the
                    # original request's host; never elevate another origin.
                    break
                response.dispose()
                response = None
                url = destination
                headers = {
                    key: value
                    for key, value in request.headers.items()
                    if key.lower()
                    not in {
                        "authorization",
                        "proxy-authorization",
                        "cookie",
                        "host",
                        "content-length",
                        "content-type",
                    }
                }
                options = {
                    "max_redirects": 0,
                    "max_retries": 0,
                    "url": url,
                    "method": method,
                    "post_data": "",
                    "headers": headers,
                }
            self.warning = "unsupported_flow"
            route.abort()
        except Exception:  # noqa: BLE001 - no secret-bearing callback errors.
            self.warning = "unsupported_flow"
            try:
                route.abort()
            except Exception:  # noqa: BLE001, S110 - closed-route cleanup is silent.
                pass
        finally:
            if response is not None:
                try:
                    response.dispose()
                except Exception:  # noqa: BLE001, S110 - no provider exception output.
                    pass

    def _popup(self, page):
        if page is not self.page:
            self.warning = "unsupported_flow"
            try:
                page.close()
            except Exception:  # noqa: BLE001, S110 - no popup exception output.
                pass

    def frame(self):
        return self.page.screenshot(
            type="jpeg",
            quality=60,
            animations="disabled",
            caret="hide",
            style=MASK_STYLE,
            timeout=3_000,
        )

    def action(self, action):
        kind = action["type"]
        if kind == "click":
            self.page.mouse.click(action["x"], action["y"])
        elif kind == "drag":
            self.page.mouse.move(action["x"], action["y"])
            self.page.mouse.down()
            try:
                self.page.mouse.move(action["end_x"], action["end_y"], steps=12)
            finally:
                self.page.mouse.up()
        elif kind == "text":
            self.page.keyboard.insert_text(action["text"])
        elif kind == "key":
            self.page.keyboard.press(action["key"])
        elif kind == "scroll":
            self.page.mouse.wheel(0, action["delta_y"])

    def cookies(self):
        return self.context.cookies(
            [BASE + COOKIE_PATHS[name] for name in COOKIE_NAMES]
        )

    def close(self):
        for attribute in ("context", "browser", "playwright"):
            target = getattr(self, attribute, None)
            setattr(self, attribute, None)
            if target is not None:
                try:
                    target.stop() if attribute == "playwright" else target.close()
                except Exception:  # noqa: BLE001, S110 - cleanup must not log secrets.
                    pass
        self.page = None


class BrowserManager:
    """One temporary browser, one dedicated sync thread, bounded commands."""

    def __init__(self, provider_factory=None, *, clock=None, revision_reader=None):
        self._factory = provider_factory or _PlaywrightProvider
        self._clock = clock or time.monotonic
        self._revision_reader = revision_reader or _saved_revision
        self._jobs = queue.Queue(maxsize=8)
        self._thread = None
        self._lifecycle = threading.Lock()
        self._active = self._last = None
        self._stopping = threading.Event()
        self._current_job = None

    def _call(self, operation, timeout=8):
        with self._lifecycle:
            if self._stopping.is_set():
                raise BrowserError("closed")
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="vinted-owner-signin", daemon=True
                )
                self._thread.start()
        job = _Job(operation, self._clock() + timeout)
        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            raise BrowserError("busy") from None
        try:
            return job.future.result(timeout=timeout)
        except TimeoutError:
            job.cancelled.set()
            job.future.cancel()
            raise BrowserError("timeout") from None

    def _job_valid(self):
        job = self._current_job
        if job and (job.cancelled.is_set() or self._clock() >= job.deadline):
            raise BrowserError("timeout")

    def _run(self):
        while not self._stopping.is_set():
            try:
                job = self._jobs.get(timeout=0.25)
            except queue.Empty:
                self._expire()
                continue
            self._current_job = job
            result = None
            try:
                if job.future.set_running_or_notify_cancel():
                    self._job_valid()
                    self._expire()
                    result = job.operation()
                    self._job_valid()
                    job.future.set_result(result)
            except BrowserError as exc:
                if not job.future.done():
                    job.future.set_exception(exc)
                if exc.reason in ("timeout", "unavailable"):
                    self._close("expired" if exc.reason == "timeout" else "closed")
            except Exception:  # noqa: BLE001 - fixed public error, never provider text.
                self._close("closed")
                if not job.future.done():
                    job.future.set_exception(BrowserError("unavailable"))
            finally:
                job.operation = None
                result = self._current_job = None
                self._jobs.task_done()
                job = None
        self._close("closed")
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                break
            job.operation = None
            if not job.future.done():
                job.future.set_exception(BrowserError("closed"))
            self._jobs.task_done()

    def _close(self, reason):
        current, self._active = self._active, None
        if current is None:
            return
        self._last = (current.identifier, current.owner_digest, reason)
        if current.provider is not None:
            try:
                current.provider.close()
            except Exception:  # noqa: BLE001, S110 - remaining state still closes.
                pass
        current.provider = None
        current.aborted.set()

    def _expire(self):
        current = self._active
        if current and (
            current.aborted.is_set()
            or self._clock() - current.started >= ABSOLUTE_TTL
            or self._clock() - current.touched >= IDLE_TTL
        ):
            self._close("closed" if current.aborted.is_set() else "expired")

    def _bound(self, owner_key, identifier):
        digest = _owner_digest(owner_key)
        current = self._active
        if (
            current is None
            or identifier != current.identifier
            or not secrets.compare_digest(digest, current.owner_digest)
        ):
            if (
                self._last
                and identifier == self._last[0]
                and secrets.compare_digest(digest, self._last[1])
            ):
                raise BrowserError(self._last[2])
            raise BrowserError("closed")
        self._job_valid()
        if current.aborted.is_set():
            self._close("closed")
            raise BrowserError("closed")
        if self._revision_reader() != current.revision:
            self._close("replaced")
            raise BrowserError("replaced")
        return current

    def _public(self, current):
        return {
            "id": current.identifier,
            "state": "ready",
            "width": WIDTH,
            "height": HEIGHT,
            "frame_seq": current.frame_seq,
            "next_seq": current.last_action_seq + 1,
            "expires_in": max(0, int(ABSOLUTE_TTL - self._clock() + current.started)),
            "warning": (
                "unsupported_flow"
                if getattr(current.provider, "warning", "") == "unsupported_flow"
                else ""
            ),
        }

    def create(self, owner_key, revision, proxy=None):
        digest = _owner_digest(owner_key)
        if not _valid_revision(revision):
            raise BrowserError("invalid")

        def operation():
            if self._active is not None:
                if secrets.compare_digest(digest, self._active.owner_digest):
                    return self._public(self._bound(owner_key, self._active.identifier))
                raise BrowserError("busy")
            if self._revision_reader() != revision:
                raise BrowserError("replaced")
            self._job_valid()
            configuration = _proxy_settings(proxy)
            now = self._clock()
            current = _Session(secrets.token_urlsafe(32), digest, revision, now, now)
            self._active = current
            current.provider = self._factory(configuration)
            self._job_valid()
            return self._public(self._bound(owner_key, current.identifier))

        return self._call(operation, timeout=25)

    def runtime_check(self):
        """Anonymous sandbox smoke only: no proxy, Vinted navigation or cookies."""

        def operation():
            if self._active is not None:
                return {"available": False, "stage": "active"}
            provider = None
            try:
                self._job_valid()
                provider = self._factory(None, navigate=False)
                self._job_valid()
                return {"available": True, "stage": "ready"}
            except ImportError:
                return {"available": False, "stage": "dependencies"}
            except BrowserError as exc:
                return {"available": False, "stage": exc.stage or "browser_start"}
            finally:
                if provider is not None:
                    provider.close()

        try:
            return self._call(operation, timeout=25)
        except BrowserError as exc:
            return {
                "available": False,
                "stage": ("timeout" if exc.reason == "timeout" else "browser_start"),
            }

    def state(self, owner_key, identifier):
        return self._call(lambda: self._public(self._bound(owner_key, identifier)))

    def frame(self, owner_key, identifier):
        def operation():
            current = self._bound(owner_key, identifier)
            image = current.provider.frame()
            self._job_valid()
            self._bound(owner_key, identifier)
            if not isinstance(image, bytes) or not 0 < len(image) <= MAX_FRAME_BYTES:
                raise BrowserError("unavailable")
            current.frame_seq += 1
            return image, current.frame_seq

        return self._call(operation)

    def action(self, owner_key, identifier, action):
        if not isinstance(action, dict) or len(action) > 8:
            raise BrowserError("invalid")
        kind = action.get("type")
        if not isinstance(kind, str):
            raise BrowserError("invalid")
        expected = {
            "click": {"x", "y"},
            "drag": {"x", "y", "end_x", "end_y"},
            "key": {"key"},
            "text": {"text"},
            "scroll": {"delta_y"},
        }.get(kind)
        if expected is None or set(action) != expected | {"type", "seq", "frame_seq"}:
            raise BrowserError("invalid")
        if any(
            type(action[key]) is not int or not 0 <= action[key] <= 1_000_000
            for key in ("seq", "frame_seq")
        ):
            raise BrowserError("invalid")
        for key in expected & {"x", "y", "end_x", "end_y"}:
            maximum = WIDTH if key.endswith("x") else HEIGHT
            if (
                type(action[key]) not in (int, float)
                or not math.isfinite(action[key])
                or not 0 <= action[key] < maximum
            ):
                raise BrowserError("invalid")
        if kind == "key" and (
            not isinstance(action["key"], str) or action["key"] not in KEYS
        ):
            raise BrowserError("invalid")
        if kind == "text" and (
            not isinstance(action["text"], str)
            or not 1 <= len(action["text"]) <= 1024
            or any(
                ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                for char in action["text"]
            )
        ):
            raise BrowserError("invalid")
        if kind == "scroll" and (
            type(action["delta_y"]) is not int or not -800 <= action["delta_y"] <= 800
        ):
            raise BrowserError("invalid")
        # Copy only the validated fixed action fields; caller mutation cannot
        # substitute commands between enqueueing and execution.
        payload = dict(action)

        def operation():
            current = self._bound(owner_key, identifier)
            if payload["seq"] <= current.last_action_seq:
                return self._public(current)
            if payload["seq"] != current.last_action_seq + 1:
                raise BrowserError("invalid")
            if payload["frame_seq"] != current.frame_seq or current.frame_seq == 0:
                raise BrowserError("stale_frame")
            self._job_valid()
            current.last_action_seq = payload["seq"]
            current.touched = self._clock()
            try:
                current.provider.action(payload)
            finally:
                # Do not retain typed input in worker closures after delivery.
                payload.clear()
            self._job_valid()
            return self._public(self._bound(owner_key, identifier))

        return self._call(operation)

    def extract_auth(self, owner_key, identifier):
        def operation():
            current = self._bound(owner_key, identifier)
            records = _cookies_for_import(current.provider.cookies())
            self._job_valid()
            self._bound(owner_key, identifier)
            return AuthEnvelope(records, current.revision)

        return self._call(operation)

    def cancel(self, owner_key, identifier):
        return self._call(lambda: self._cancel(owner_key, identifier))

    def _cancel(self, owner_key, identifier):
        digest = _owner_digest(owner_key)
        self._job_valid()
        current = self._active
        if current is None:
            if (
                self._last
                and identifier == self._last[0]
                and secrets.compare_digest(digest, self._last[1])
            ):
                return {"id": identifier, "state": "closed"}
            raise BrowserError("closed")
        if identifier != current.identifier or not secrets.compare_digest(
            digest, current.owner_digest
        ):
            raise BrowserError("closed")
        # A successful importer legitimately changes the saved revision.
        # Closing requires owner/id binding, but never the old revision.
        self._close("closed")
        return {"id": identifier, "state": "closed"}

    def shutdown(self):
        """Local lifecycle hook. No browser work is performed on the caller."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=3)


MANAGER = BrowserManager()
