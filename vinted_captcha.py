"""Opt-in, bounded DataDome solving for the owner's fixed buyer connection.

The caller supplies its private API key, fixed proxy and matching browser UA.
This module never reads environment secrets, logs solver payloads, renews a
Vinted session, retries a Vinted request or starts checkout/payment. Only an
explicit, allowlisted DataDome challenge can create one paid solver task.

Reference: masolupo/vinted-live-feed/feed/session.py (MIT). Current task contract
checked against https://docs.capsolver.com/en/guide/captcha/datadome/ and
https://docs.capsolver.com/en/guide/api-how-to-use-proxy/ on 8 October 2026.
Unlike the older reference, the documented DatadomeSliderTask handles both
slider and interstitial challenges. TLS verification remains enabled.
"""

import ipaddress
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urlsplit

import requests

WEBSITE = "https://www.vinted.co.uk/"
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
CHALLENGE_HOSTS = frozenset(("geo.captcha-delivery.com", "ct.captcha-delivery.com"))
MAX_RESPONSE = 65536
# The first query follows the DataDome sample's one-second delay. Later
# processing queries retain the general getTaskResult three-second interval.
# Thirteen queries preserve the former twelve-query/36-second wait window
# (1 + 12 * 3 = 37), still bounded by the existing absolute deadline.
MAX_POLLS = 13
MAX_SECONDS = 60
FIRST_POLL_INTERVAL = 1
POLL_INTERVAL = 3
_PROTOCOLS = frozenset(("http", "https", "socks4", "socks5"))
ERROR_CODES = frozenset(
    (
        "ERROR_SERVICE_UNAVALIABLE",
        "ERROR_RATE_LIMIT",
        "ERROR_INVALID_TASK_DATA",
        "ERROR_BAD_REQUEST",
        "ERROR_TASKID_INVALID",
        "ERROR_TASK_TIMEOUT",
        "ERROR_SETTLEMENT_FAILED",
        "ERROR_KEY_DENIED_ACCESS",
        "ERROR_ZERO_BALANCE",
        "ERROR_TASK_NOT_SUPPORTED",
        "ERROR_CAPTCHA_UNSOLVABLE",
        "ERROR_UNKNOWN_QUESTION",
        "ERROR_PROXY_BANNED",
        "ERROR_INVALID_IMAGE",
        "ERROR_PARSE_IMAGE_FAIL",
        "ERROR_IP_BANNED",
        "ERROR_KEY_TEMP_BLOCKED",
    )
)


@dataclass(frozen=True)
class SolverResult:
    state: str
    cookie: str = field(default="", repr=False)
    polls: int = 0
    code: str = ""
    stage: str = ""
    http_status: int | None = None
    category: str = ""
    provider_error: str = ""

    def public(self):
        """Safe diagnostics; a solved cookie remains server-side only."""
        result = {"state": self.state, "polls": self.polls}
        for field_name in (
            "code",
            "stage",
            "http_status",
            "category",
            "provider_error",
        ):
            value = getattr(self, field_name)
            if value not in (None, ""):
                result[field_name] = value
        return result


def _safe_error(data, status=200, *, request_payload=None):
    """Known codes and fixed categories only; never copy provider descriptions."""
    raw_code = data.get("errorCode") if isinstance(data, dict) else None
    normalized = (
        raw_code.strip().upper()
        if isinstance(raw_code, str) and len(raw_code) <= 100
        else ""
    )
    code = normalized if normalized in ERROR_CODES else "other"
    description = data.get("errorDescription") if isinstance(data, dict) else None
    description = description.lower()[:4096] if isinstance(description, str) else ""
    category = "other"
    if (
        re.fullmatch(r"ERROR_[A-Z_]{1,64}", normalized)
        and ("TASK" in normalized or "TYPE" in normalized)
        and ("SUPPORT" in normalized or "TYPE" in normalized)
    ):
        category = "unsupported_task"
    for phrase, fixed in (
        ("proxy ip banned", "proxy_banned"),
        ("proxy is banned", "proxy_banned"),
        ("invalid proxy format", "proxy_format"),
        ("invalid proxy", "proxy_format"),
        ("useragent does not match", "browser_identity"),
        ("user-agent does not match", "browser_identity"),
        ("unsupported useragent", "browser_identity"),
        ("task type is not supported", "unsupported_task"),
        ("task type not supported", "unsupported_task"),
        ("unsupported task type", "unsupported_task"),
        ("unknown task type", "unsupported_task"),
        ("not supported task type", "unsupported_task"),
        ("task type not recognized", "unsupported_task"),
        ("type has not been supported", "unsupported_task"),
        ("type is not supported", "unsupported_task"),
        ("html is required", "challenge_html"),
        ("provide html", "challenge_html"),
        ("invalid captchaurl", "challenge_url"),
        ("captchaurl is required", "challenge_url"),
        ("insufficient balance", "insufficient_credit"),
    ):
        if phrase in description:
            category = fixed
            break
    if code == "ERROR_INVALID_TASK_DATA":
        if category == "other":
            category = "invalid_task_data"
        for phrase, fixed in (
            ("proxy format", "proxy_format"),
            ("invalid proxy", "proxy_format"),
            ("useragent", "browser_identity"),
            ("user-agent", "browser_identity"),
            ("captchaurl", "challenge_url"),
            ("html", "challenge_html"),
        ):
            if phrase in description:
                category = fixed
                break
    result = {
        "code": code,
        "category": category,
        "http_status": status if type(status) is int and 100 <= status <= 599 else None,
    }
    # A provider error NAME is diagnostic data. Accept only its bounded enum
    # format, and explicitly exclude any request credential or task identifier.
    # Descriptions, URLs, solution values and response bodies are never exported.
    private_values = []
    if isinstance(request_payload, dict):
        private_values.extend(request_payload.get(k) for k in ("clientKey", "taskId"))
        task = request_payload.get("task")
        if isinstance(task, dict):
            private_values.extend(task.get(k) for k in ("proxy", "captchaUrl"))
            try:
                parts = urlsplit(task.get("proxy", ""))
                private_values.extend((parts.username, parts.password))
                raw_proxy = task.get("proxy", "")
                if isinstance(raw_proxy, str) and "://" not in raw_proxy:
                    fields = raw_proxy.split(":")
                    if len(fields) in (4, 5):
                        private_values.extend(fields[-2:])
            except (ValueError, TypeError):
                pass
        if re.fullmatch(r"ERROR_[A-Z_]{1,64}", normalized) and not any(
            isinstance(value, str) and len(value) >= 4 and value.upper() in normalized
            for value in private_values
        ):
            result["provider_error"] = normalized
    return result


def _failure(error, data, stage, polls=0):
    detail = (
        {}
        if data is None
        else (
            data
            if isinstance(data, dict)
            and set(data) <= {"code", "category", "http_status", "provider_error"}
            else _safe_error(data)
        )
    )
    return SolverResult(
        error,
        polls=polls,
        code=detail.get("code", ""),
        category=detail.get("category", ""),
        stage=stage,
        http_status=detail.get("http_status"),
        provider_error=detail.get("provider_error", ""),
    )


def _clean_string(value, maximum):
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and not any(ord(char) <= 32 or ord(char) == 127 for char in value)
        and "\\" not in value
    )


def _challenge_state(value):
    if not _clean_string(value, 8192):
        return "invalid_challenge"
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in CHALLENGE_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
            or parsed.path
            not in ("/captcha", "/captcha/", "/interstitial", "/interstitial/")
        ):
            return "invalid_challenge"
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=64)
        if query.get("t") == ["bv"]:
            return "ip_blocked"
        if query.get("t") != ["fe"]:
            return "invalid_challenge"
        if "referer" in query:
            if len(query["referer"]) != 1 or not _clean_string(
                query["referer"][0], 4096
            ):
                return "invalid_challenge"
            referer = urlsplit(query["referer"][0])
            if (
                referer.scheme != "https"
                or referer.hostname not in ("www.vinted.co.uk", "vinted.co.uk")
                or referer.username is not None
                or referer.password is not None
                or referer.port not in (None, 443)
            ):
                return "invalid_challenge"
    except (ValueError, UnicodeError):
        return "invalid_challenge"
    return "ready"


class _ChallengeLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag not in ("iframe", "a", "form", "script") or len(self.urls) >= 16:
            return
        attrs = dict(attrs)
        value = attrs.get("src") or attrs.get("href") or attrs.get("action")
        if _challenge_state(value) in ("ready", "ip_blocked"):
            self.urls.append(value)


def extract_challenge(response, data=None):
    """Return an explicit allowed URL; a generic 403 never starts a solver task."""
    status = getattr(response, "status_code", None)
    if status in (301, 302, 303, 307, 308):
        headers = getattr(response, "headers", {})
        value = headers.get("Location") if isinstance(headers, Mapping) else None
        if _challenge_state(value) in ("ready", "ip_blocked"):
            return value
        return None
    if status != 403:
        return None
    text = getattr(response, "text", "")
    if data is None and isinstance(text, str) and len(text) <= MAX_RESPONSE:
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            pass
    if isinstance(data, dict):
        for key in ("url", "captcha_url", "challenge_url"):
            value = data.get(key)
            if _challenge_state(value) in ("ready", "ip_blocked"):
                return value
    if isinstance(text, str) and len(text) <= MAX_RESPONSE and "<" in text:
        parser = _ChallengeLinks()
        parser.feed(text)
        if parser.urls:
            return parser.urls[0]
    return None


def _proxy_host(host):
    if not host or len(host) > 253:
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return (
            bool(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host))
            and "." in host
            and all(
                len(label) <= 63
                and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
                for label in host.split(".")
            )
            and not host.lower().endswith((".local", ".localhost", ".internal"))
        )


def normalize_proxy(value):
    """Accept documented fixed-proxy formats; never rotate or contact a proxy."""
    if not _clean_string(value, 4096):
        return None
    if "://" not in value:
        parts = value.split(":")
        protocol = parts.pop(0) if parts[0] in _PROTOCOLS else "http"
        if len(parts) not in (2, 4):
            return None
        host, port = parts[:2]
        auth = ""
        if len(parts) == 4:
            if not parts[2] or not parts[3]:
                return None
            auth = quote(parts[2], safe="") + ":" + quote(parts[3], safe="") + "@"
        value = f"{protocol}://{auth}{host}:{port}"
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in _PROTOCOLS
            or not _proxy_host(parsed.hostname)
            or parsed.port is None
            or not 1 <= parsed.port <= 65535
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or (parsed.username is not None and not parsed.username)
            or (parsed.password is not None and not parsed.password)
            or (parsed.username is None) != (parsed.password is None)
            or (
                parsed.username is not None
                and (
                    not _clean_string(unquote(parsed.username), 4096)
                    or not _clean_string(unquote(parsed.password), 4096)
                )
            )
        ):
            return None
        return value.removesuffix("/")
    except (ValueError, UnicodeError):
        return None


def _cookie(solution, user_agent):
    if not isinstance(solution, dict):
        return ""
    if solution.get("userAgent", user_agent) != user_agent:
        return ""
    value = solution.get("cookie")
    if (
        not isinstance(value, str)
        or len(value) > 8192
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return ""
    first = value.split(";", 1)[0].strip()
    if not first.startswith("datadome="):
        return ""
    token = first[len("datadome=") :]
    # RFC 6265 cookie-octet excludes control chars, whitespace, quotes, comma,
    # semicolon and backslash. A tiny placeholder is not a usable solver token.
    if not 16 <= len(token) <= 4096 or not re.fullmatch(
        r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]+", token
    ):
        return ""
    return token


def solver_proxy(value):
    """Serialize the SAME endpoint using the DataDome task's documented form."""
    parsed = urlsplit(value)
    username, password = unquote(parsed.username or ""), unquote(parsed.password or "")
    # URL syntax preserves IPv6 and credentials containing delimiter characters.
    if ":" in parsed.hostname or ":" in username or ":" in password:
        return value
    parts = [parsed.hostname, str(parsed.port)]
    if username:
        parts.extend((username, password))
    if parsed.scheme != "http":
        parts.insert(0, parsed.scheme)
    return ":".join(parts)


def _post(session, path, payload, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, "timeout"
    timeout = (min(3, remaining / 2), min(8, remaining / 2))
    try:
        with session.post(
            "https://api.capsolver.com/" + path,
            json=payload,
            stream=True,
            allow_redirects=False,
            verify=True,
            timeout=timeout,
        ) as response:
            status = response.status_code
            if status not in (200, 400, 401):
                return _safe_error(None, status), "service_error"
            body = bytearray()
            for chunk in response.iter_content(8192):
                if time.monotonic() >= deadline:
                    return None, "timeout"
                if len(body) + len(chunk) > MAX_RESPONSE:
                    return None, "invalid_response"
                body.extend(chunk)
            data = json.loads(body)
    except requests.RequestException:
        return None, "network_error"
    except (ValueError, TypeError, RecursionError):
        return None, "invalid_response"
    if not isinstance(data, dict) or type(data.get("errorId")) is not int:
        return None, "invalid_response"
    if data["errorId"] != 0:
        return _safe_error(data, status, request_payload=payload), "service_error"
    if status != 200:
        return _safe_error(data, status, request_payload=payload), "service_error"
    return data, None


def solve_datadome(
    challenge_url, *, proxy, api_key, user_agent, enabled=False, website_url=WEBSITE
):
    """Create at most one task and return fixed diagnostics plus a private cookie.

    The caller must use this proxy and UA for the challenged connection, and
    prevent concurrent/repeated solving for that connection. IP bans, unknown
    denials and all failed solver results remain terminal here.
    """
    if enabled is not True:
        return SolverResult("disabled")
    state = _challenge_state(challenge_url)
    if state != "ready":
        return SolverResult(state)
    if website_url not in (WEBSITE, WEBSITE.removesuffix("/")):
        return SolverResult("invalid_challenge")
    if not proxy or not isinstance(api_key, str) or not api_key:
        return SolverResult("missing_config")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", api_key):
        return SolverResult("missing_config")
    proxy = normalize_proxy(proxy)
    if proxy is None:
        return SolverResult("invalid_proxy")
    if user_agent != CHROME_USER_AGENT:
        return SolverResult("unsupported_browser")
    deadline = time.monotonic() + MAX_SECONDS
    with requests.Session() as session:
        data, error = _post(
            session,
            "createTask",
            {
                "clientKey": api_key,
                "task": {
                    "type": "DatadomeSliderTask",
                    "websiteURL": WEBSITE,
                    "captchaUrl": challenge_url,
                    "proxy": solver_proxy(proxy),
                    "userAgent": user_agent,
                },
            },
            deadline,
        )
        if error:
            return _failure(error, data, "create_task")
        if data.get("status") == "ready":
            cookie = _cookie(data.get("solution"), user_agent)
            return SolverResult("solved" if cookie else "invalid_cookie", cookie)
        if data.get("status") == "failed":
            return _failure("service_error", data, "create_task")
        task = data.get("taskId")
        if not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task):
            return SolverResult("invalid_response")
        for poll in range(1, MAX_POLLS + 1):
            interval = FIRST_POLL_INTERVAL if poll == 1 else POLL_INTERVAL
            remaining = deadline - time.monotonic()
            if remaining <= interval:
                return SolverResult("timeout", polls=poll - 1)
            time.sleep(interval)
            data, error = _post(
                session,
                "getTaskResult",
                {"clientKey": api_key, "taskId": task},
                deadline,
            )
            if error:
                return _failure(error, data, "task_result", poll)
            if "taskId" in data and data["taskId"] != task:
                return SolverResult("invalid_response", polls=poll)
            status = data.get("status")
            if status == "ready":
                cookie = _cookie(data.get("solution"), user_agent)
                return SolverResult(
                    "solved" if cookie else "invalid_cookie", cookie, poll
                )
            if status == "failed":
                return _failure("service_error", data, "task_result", poll)
            if status not in ("idle", "processing"):
                return SolverResult("invalid_response", polls=poll)
    return SolverResult("timeout", polls=MAX_POLLS)
