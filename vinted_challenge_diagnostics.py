"""Fixed refusal evidence; never solve, retry or expose a challenge URL."""

import logging
from urllib.parse import parse_qs, urlsplit

import requests

from vinted_timing import OPERATIONS

logger = logging.getLogger(__name__)


def refusal_evidence(response, data, *, operation, challenged):
    """Describe only documented header presence and validated URL structure.

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

    def present(name):
        return headers.get(name) is not None or headers.get(name.lower()) is not None

    url = extract_challenge(response, data)
    kind, marker, source = ("unrecognised" if challenged else "none"), "none", "none"
    if url:
        parts = urlsplit(url)
        kind = "captcha" if parts.path.rstrip("/") == "/captcha" else "interstitial"
        query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=64)
        marker = "bv" if query.get("t") == ["bv"] else "fe"
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
            "challenge_url_marker=%s challenge_url_source=%s",
            *(evidence[key] for key in (
                "operation", "http", "challenged", "x_datadome", "x_dd_b",
                "x_set_cookie", "datadome_set_cookie", "challenge_url_extractable",
                "challenge_url_kind", "challenge_url_marker", "challenge_url_source",
            )),
        )
    except Exception:  # noqa: BLE001,S110 -- evidence cannot affect payments
        pass
