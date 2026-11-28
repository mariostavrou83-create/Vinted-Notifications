"""Bounded read-only Home reads on an already verified account client."""

import math
import re
import time
from contextlib import closing
from email.utils import parsedate_to_datetime
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

import requests

import vinted_buyer as buyer
import vinted_seller as seller
from search_settings import connection
from vinted_accounts import account_scope, client_account, current_role
from vinted_buyer_priority import yield_to_purchase
from vinted_http import NAVIGATION_HEADERS

MAX_HTML_BYTES = 4 * 1024 * 1024
VERIFY_SECONDS = 15


class HomeReader:
    """Reuse a recently verified connection only for the same selected Home account."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.client = None
        self.verify_at = 0

    def close(self):
        if self.client is not None:
            self.client.session.close()
            self.client = None

    def current_session(self):
        if self.client is None or self.client._verified_session is None:
            return False
        user_id, seal, _ = self.client._verified_session
        role = self.client.account_role
        if role not in {"seller", "buyer"}:
            return False
        with closing(connection()) as conn:
            return conn.execute(
                f"SELECT 1 FROM vinted_{role} WHERE id=1 AND user_id=? AND session=? AND verified_at>0",
                (user_id, seal),
            ).fetchone() is not None

    def fetch(self, role, url):
        from vinted_home_controls import source_selected
        if not source_selected(role):
            raise buyer.BuyerError("This Home feed is not selected.", reason="account_changed")
        account = seller if role == "seller" else buyer
        with account_scope(role), account.exclusive():
            if current_role() != role:
                self.close()
                raise buyer.BuyerError("Home account did not match.", reason="account_changed")
            yield_to_purchase()
            try:
                if self.clock() >= self.verify_at or self.client is None or self.client.account_role != role or not self.current_session():
                    self.close()
                    self.client = account.connected_client(solve_challenges=False, allow_refresh=False)
                    self.verify_at = self.clock() + VERIFY_SECONDS
                    if self.client.account_role != role or not self.current_session():
                        raise buyer.BuyerError("Home account did not match.", reason="account_changed")
                result = read_html(self.client, url)
                if not self.current_session():
                    raise buyer.BuyerError("The saved Home connection changed.", reason="saved_session")
                return result
            except Exception:
                # An auth rejection, replacement session or broken connection
                # must pass a fresh seller identity read before it is reused.
                self.close()
                raise


def _incomplete_read(response, stage):
    # A broken stream can still carry a status and a mandatory Retry-After.
    # Classify headers without attempting to read the failed body a second time.
    if response is not None:
        if response.status_code != 200 or "Retry-After" in response.headers:
            headers_only = SimpleNamespace(status_code=response.status_code, headers=response.headers, text="")
            return buyer.response_error(headers_only, None, "request")
        # An incomplete response cannot establish whether the account was challenged.
        stage = "request"
    return buyer.BuyerError("Home discovery read did not complete.", reason="network", stage=stage)


def same_item_redirect(response, requested_url):
    """Recognize a canonical item redirect without following or exposing it."""
    if response.status_code not in (301, 302, 303, 307, 308):
        return False
    location = response.headers.get("Location", "")
    if (
        not isinstance(location, str)
        or not location
        or len(location) > 4096
        or any(ord(char) <= 32 for char in location)
    ):
        return False
    try:
        source = urlsplit(requested_url)
        target = urlsplit(urljoin(requested_url, location))
        original = re.fullmatch(r"/items/([0-9]{1,20})(?:-[\w-]+)?", source.path)
        redirected = re.fullmatch(r"/items/([0-9]{1,20})(?:-[\w-]+)?/?", target.path)
        return bool(
            original
            and redirected
            and original[1] == redirected[1]
            and target.scheme == "https"
            and target.netloc == "www.vinted.co.uk"
            and not target.query
            and not target.fragment
        )
    except ValueError:
        return False


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
        # RFC 9111 §4.2.3 takes the greater of apparent age and corrected
        # Age. Expressed as an origin time, that is the earlier timestamp.
        # Using request start here also accounts for time spent in transit.
        return min(generated, started - int(age))
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
    page_stage = "listing_page" if parts.path.startswith("/items/") else "request"
    reading_page = False
    try:
        with client.accepted_response_cookies("home_discovery"):
            reading_page = True
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
            if response.status_code == 429:
                # The status and headers are sufficient. A stalled error body
                # must never postpone recording the provider's cooldown.
                raise _incomplete_read(response, "request")
            body = bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > MAX_HTML_BYTES:
                    if response.status_code != 200 or "Retry-After" in response.headers:
                        raise _incomplete_read(response, "request")
                    raise buyer.BuyerError(
                        "Home discovery page exceeded its bound.",
                        reason="response_too_large",
                    )
            html = body.decode("utf-8", errors="strict")
            # Use a bounded response for the existing error classifier only.
            response._content = bytes(body)
            response._content_consumed = True
            reading_page = False
            data = buyer.homepage_data(response)
            if response.status_code != 200 or buyer.security_challenge(response, data):
                stage = "request" if "Retry-After" in response.headers else page_stage
                error = buyer.response_error(response, data, stage)
                if (error.reason == "redirect" and "Retry-After" not in response.headers
                        and same_item_redirect(response, url)):
                    raise buyer.BuyerError(
                        "Home listing was redirected and skipped.",
                        response.status_code,
                        reason="listing_redirect",
                    )
                raise error
            client.update_tokens(response, {})
        token = buyer.csrf_from_html(html)
        if token:
            client.csrf = token
            client.headers()
        client.persist_session()
        observed_at = response_time(response.headers, started)
        return html, observed_at
    except (requests.RequestException, UnicodeError) as exc:
        failed_response = response if response is not None else getattr(exc, "response", None)
        # Identity and session maintenance errors never acquire listing scope.
        raise _incomplete_read(failed_response, page_stage if reading_page else "request") from None
    finally:
        if response is not None:
            response.close()
