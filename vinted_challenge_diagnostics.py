"""Fixed refusal evidence; never solve, retry or expose a challenge URL."""

import logging
from collections.abc import Mapping
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import requests

from vinted_timing import OPERATIONS

logger = logging.getLogger(__name__)

# These are exact diagnostic labels, not a mapping from numeric API codes to
# causes. In particular, code 106 alone does not establish any of these reasons.
_MESSAGE_CODES = frozenset({
    "access_denied", "request_forbidden", "permission_denied", "forbidden",
    "unauthorized", "unauthenticated", "authentication_required",
    "invalid_authentication_token", "invalid_authentification_token",
    "invalid_token", "invalid_grant", "invalid_refresh_token",
    "refresh_token_expired", "session_expired", "invalid_csrf_token",
    "csrf_token_invalid", "csrf_error", "captcha_required", "verification_required",
    "rate_limited", "rate_limit_exceeded", "request_rate_limit_exceeded",
    "too_many_requests", "action_denied", "action_not_allowed", "not_allowed",
    "operation_not_allowed", "user_blocked", "account_blocked", "account_restricted",
    "bad_request", "not_found", "item_not_found", "item_sold", "item_reserved",
})


def _message_code_evidence(data):
    if not isinstance(data, dict) or "message_code" not in data:
        return "missing", "none"
    code = data["message_code"]
    if not isinstance(code, str):
        return "non_string", "none"
    # Never output an unknown upstream identifier, even if it looks like a
    # machine code: credentials and private text can also have that shape.
    return "string", code if len(code) <= 64 and code in _MESSAGE_CODES else "other"


def _retry_after_evidence(headers):
    value = headers.get("Retry-After", headers.get("retry-after"))
    if value is None:
        return False, "missing", None
    if not isinstance(value, str):
        return True, "non_string", None
    if len(value) > 6 or not value.isascii() or not value.isdigit():
        # HTTP dates can also be valid Retry-After values. Do not echo them,
        # parse private strings or label every non-numeric value invalid.
        return True, "non_numeric", None
    seconds = int(value)
    if seconds > 86400:
        return True, "outside_bound", None
    return True, "seconds", seconds


def _candidate_evidence(response, data):
    """Describe rejected URL formats without widening solver validation."""
    from vinted_captcha import CHALLENGE_HOSTS, MAX_RESPONSE

    result = dict.fromkeys(("source", "host", "path", "marker", "referer"), "none")
    candidates = []
    headers = getattr(response, "headers", {})
    if getattr(response, "status_code", None) in (301, 302, 303, 307, 308):
        candidates.append((headers.get("Location"), "redirect"))
    if isinstance(data, dict):
        candidates.extend((data.get(key), "json") for key in ("url", "captcha_url", "challenge_url"))
    body = getattr(response, "text", "")
    if isinstance(body, str) and len(body) <= MAX_RESPONSE and "<" in body:
        class Links(HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag in ("iframe", "a", "form", "script") and len(candidates) < 20:
                    attrs = dict(attrs)
                    candidate = attrs.get("src") or attrs.get("href") or attrs.get("action")
                    try:
                        if isinstance(candidate, str) and urlsplit(candidate).hostname in CHALLENGE_HOSTS:
                            candidates.append((candidate, "html"))
                    except ValueError:
                        pass
        Links().feed(body)
    for value, source in candidates:
        if not isinstance(value, str) or not 0 < len(value) <= 8192:
            continue
        result["source"] = source
        try:
            parts = urlsplit(value)
            result["host"] = {"geo.captcha-delivery.com": "geo", "ct.captcha-delivery.com": "ct"}.get(parts.hostname, "other")
            result["path"] = {"/captcha": "captcha", "/interstitial": "interstitial"}.get(parts.path.rstrip("/"), "other")
            query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=64)
            marker = query.get("t")
            result["marker"] = (
                "missing" if marker is None else "multiple" if len(marker) != 1
                else marker[0] if marker[0] in ("fe", "bv", "it") else "other"
            )
            referer = query.get("referer")
            result["referer"] = "missing" if referer is None else "other"
            if referer and len(referer) == 1:
                result["referer"] = {
                    "www.vinted.co.uk": "www", "vinted.co.uk": "apex", "api.vinted.co.uk": "api",
                }.get(urlsplit(referer[0]).hostname, "other")
        except (ValueError, UnicodeError):
            pass
        break
    return {"challenge_candidate_" + key: value for key, value in result.items()}


def refusal_evidence(response, data, *, operation, challenged):
    """Describe header presence, validated URL structure and credential counts.

    Header presence is evidence, not an authentication or payment-readiness
    decision. Query markers describe the received format; they do not establish
    why the provider challenged this connection or that an IP was banned.
    """
    from vinted_captcha import extract_challenge

    operation = (
        operation if isinstance(operation, str) and operation in OPERATIONS else "buyer_request"
    )
    status = getattr(response, "status_code", None)
    status = status if type(status) is int and 100 <= status <= 599 else None
    headers = getattr(response, "headers", {})
    message_code_type, message_code = _message_code_evidence(data)
    retry_after_present, retry_after_kind, retry_after_seconds = _retry_after_evidence(headers)

    def present(name):
        return headers.get(name) is not None or headers.get(name.lower()) is not None

    url = extract_challenge(response, data)
    kind, marker, source = ("unrecognised" if challenged else "none"), "none", "none"
    if url:
        parts = urlsplit(url)
        kind = "captcha" if parts.path.rstrip("/") == "/captcha" else "interstitial"
        query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=64)
        marker = query["t"][0]
        source = (
            "redirect" if status in (301, 302, 303, 307, 308)
            else "json" if isinstance(data, dict) and any(
                data.get(key) == url for key in ("url", "captcha_url", "challenge_url")
            ) else "html"
        )
    cookies = getattr(response, "cookies", None)
    security_cookie = isinstance(cookies, requests.cookies.RequestsCookieJar) and any(
        cookie.name == "datadome" for cookie in cookies
    )
    prepared_headers = getattr(getattr(response, "request", None), "headers", None)
    cookie_evidence, access_count, security_count, csrf_present = False, None, None, None
    if isinstance(prepared_headers, Mapping):
        csrf_present = bool(prepared_headers.get(
            "X-CSRF-Token", prepared_headers.get("x-csrf-token")
        ))
        cookie = prepared_headers.get("Cookie", prepared_headers.get("cookie", ""))
        if isinstance(cookie, str) and len(cookie) <= 65536:
            parts = cookie.split(";", 1000)
            if len(parts) <= 1000:
                names = [part.split("=", 1)[0].strip() for part in parts]
                cookie_evidence = True
                access_count = names.count("access_token_web")
                security_count = names.count("datadome")
    return {
        "operation": operation, "http": status,
        "challenged": challenged is True,
        "x_datadome": present("X-DataDome"),
        "x_dd_b": present("X-DD-B"),
        "x_set_cookie": present("X-Set-Cookie"),
        "datadome_set_cookie": security_cookie,
        "challenge_url_extractable": bool(url),
        "challenge_url_kind": kind,
        "challenge_url_marker": marker,
        "challenge_url_source": source,
        "prepared_cookie_evidence": cookie_evidence,
        "prepared_access_cookies": access_count,
        "prepared_datadome_cookies": security_count,
        "prepared_csrf_present": csrf_present,
        "message_code_type": message_code_type,
        "message_code": message_code,
        "retry_after_present": retry_after_present,
        "retry_after_kind": retry_after_kind,
        "retry_after_seconds": retry_after_seconds,
        **_candidate_evidence(response, data),
    }


def log_refusal_evidence(response, data, *, operation, challenged):
    """Optional diagnostics cannot change a refusal or replay any request."""
    try:
        evidence = refusal_evidence(
            response, data, operation=operation, challenged=challenged
        )
        logger.info(
            "Vinted refusal evidence: operation=%s http=%s challenged=%s "
            "x_datadome=%s x_dd_b=%s x_set_cookie=%s datadome_set_cookie=%s "
            "challenge_url_extractable=%s challenge_url_kind=%s "
            "challenge_url_marker=%s challenge_url_source=%s "
            "prepared_cookie_evidence=%s prepared_access_cookies=%s "
            "prepared_datadome_cookies=%s prepared_csrf_present=%s "
            "message_code_type=%s message_code=%s retry_after_present=%s "
            "retry_after_kind=%s retry_after_seconds=%s "
            "challenge_candidate_source=%s challenge_candidate_host=%s "
            "challenge_candidate_path=%s challenge_candidate_marker=%s "
            "challenge_candidate_referer=%s",
            *(evidence[key] for key in (
                "operation", "http", "challenged", "x_datadome", "x_dd_b",
                "x_set_cookie", "datadome_set_cookie", "challenge_url_extractable",
                "challenge_url_kind", "challenge_url_marker", "challenge_url_source",
                "prepared_cookie_evidence", "prepared_access_cookies",
                "prepared_datadome_cookies", "prepared_csrf_present",
                "message_code_type", "message_code", "retry_after_present",
                "retry_after_kind", "retry_after_seconds",
                "challenge_candidate_source", "challenge_candidate_host",
                "challenge_candidate_path", "challenge_candidate_marker", "challenge_candidate_referer",
            )),
        )
    except Exception:  # noqa: BLE001,S110 -- evidence cannot affect payments
        pass
