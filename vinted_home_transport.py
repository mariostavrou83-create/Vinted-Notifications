"""Bounded read-only Home reads on an already verified account client."""

import math
import re
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

import vinted_buyer as buyer
from vinted_accounts import client_account
from vinted_buyer_priority import yield_to_purchase
from vinted_http import NAVIGATION_HEADERS

MAX_HTML_BYTES = 4 * 1024 * 1024


def response_time(headers, started):
    """Age is evaluated from the response generation time, never receipt time."""
    try:
        stamp = parsedate_to_datetime(headers.get("Date", ""))
        if stamp.tzinfo is None:
            raise ValueError
        generated = stamp.timestamp()
        age = headers.get("Age", "0")
        if not re.fullmatch(r"[0-9]{1,8}", age):
            raise ValueError
        if not math.isfinite(generated) or generated > started + 5:
            raise ValueError
        return min(started, generated) - int(age)
    except (ValueError, TypeError, OverflowError):
        raise buyer.BuyerError(
            "Home discovery could not verify response freshness.",
            reason="response_time_unverified",
        ) from None


@client_account
def read_html(client, url):
    """One GET, no redirects, renewal, solver tasks, checkout or payment."""
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != "www.vinted.co.uk"
        or parts.query
        or parts.fragment
        or not (
            parts.path == "/"
            or re.fullmatch(r"/items/[0-9]{1,20}(?:-[\w-]+)?", parts.path)
        )
    ):
        raise buyer.BuyerError("Invalid Home discovery URL.", reason="home_url")
    if client._verified_session is None:
        raise buyer.BuyerError(
            "Home discovery requires a verified account.", reason="home_identity"
        )
    yield_to_purchase()
    started = time.time()
    response = None
    try:
        with client.accepted_response_cookies("home_discovery"):
            response = client.session.get(
                url,
                headers={
                    **NAVIGATION_HEADERS,
                    "Content-Type": None,
                    "Origin": None,
                    "Cache-Control": "no-cache",
                },
                timeout=(4, 8),
                allow_redirects=False,
                stream=True,
            )
            body = bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > MAX_HTML_BYTES:
                    raise buyer.BuyerError(
                        "Home discovery page exceeded its bound.",
                        reason="response_too_large",
                    )
            html = body.decode("utf-8", errors="strict")
            # Use a bounded response for the existing error classifier only.
            response._content = bytes(body)
            response._content_consumed = True
            data = buyer.homepage_data(response)
            if response.status_code != 200 or buyer.security_challenge(response, data):
                raise buyer.response_error(response, data, "request")
            client.update_tokens(response, {})
        token = buyer.csrf_from_html(html)
        if token:
            client.csrf = token
            client.headers()
        client.persist_session()
        observed_at = response_time(response.headers, started)
        return html, observed_at
    except (requests.RequestException, UnicodeError):
        raise buyer.BuyerError(
            "Home discovery read did not complete.", reason="network"
        ) from None
    finally:
        if response is not None:
            response.close()
