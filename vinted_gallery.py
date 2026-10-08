"""Read scoped listing details with bounded browser requests and optional solving.

The fast alert retains its catalogue photo while detail requests run separately.
Only a configured, supported security check allows one additional listing read.
"""

import re
import time
from contextlib import closing, contextmanager
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

from alert_images import safe_listing_photo
from listing_text import clean_description
from logger import get_logger
from search_settings import connection
from vinted_http import NAVIGATION_HEADERS
from vinted_page_data import parse_page_data

logger = get_logger(__name__)
RETRY_STATES = {
    "cooldown",
    "access_limited",
    "network_error",
    "http_500",
    "http_502",
    "http_503",
    "http_504",
}


def retry_pending(details):
    return (
        details.get("description_state") in RETRY_STATES
        and not details.get("description")
        and details.get("listing_attempts", 0) < 3
        and time.time() < details.get("listing_retry_until", 0)
    )


def listing_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if (
        parsed.scheme == "https"
        and parsed.netloc in ("www.vinted.co.uk", "vinted.co.uk")
        and re.fullmatch(r"/items/\d+(?:-[\w-]+)?/?", parsed.path)
    ):
        return "https://www.vinted.co.uk" + parsed.path
    return None


def canonical_listing_url(value, current):
    """Allow one clean canonical redirect within the same UK listing identity."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
    ):
        return None
    try:
        target = urljoin(current, value)
        parsed = urlparse(target)
    except ValueError:
        return None
    accepted = listing_url(target)
    if (
        not accepted
        or parsed.netloc != "www.vinted.co.uk"
        or parsed.query
        or parsed.fragment
        or target == current
        or re.search(r"/items/(\d+)", target).group(1)
        != re.search(r"/items/(\d+)", current).group(1)
    ):
        return None
    return accepted


def photo_identity(url):
    # CDN hosts, size variants and signatures may differ for the same photo.
    path = urlparse(url).path
    match = re.match(r"/t/([^/]+)/", path)
    return match[1] if match else url


def distinct_photos(urls):
    seen, result = set(), []
    for url in urls:
        if safe_listing_photo(url):
            identity = photo_identity(url)
            if identity not in seen:
                seen.add(identity)
                result.append(url)
    return result[:4]


class GalleryParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.photos = {}
        self.description_parts = []
        self.description_depth = 0
        self.description_done = False
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self.in_script = True
        if (
            not self.description_done
            and not self.description_depth
            and attrs.get("data-testid")
            in ("item-description", "item-description-text")
        ):
            self.description_depth = 1
        elif self.description_depth:
            if tag not in ("br", "img", "hr", "input", "meta", "link"):
                self.description_depth += 1
            if tag in ("br", "p", "div", "li"):
                self.description_parts.append("\n")
        match = re.fullmatch(r"item-photo-(\d+)--img", attrs.get("data-testid", ""))
        if tag == "img" and match and safe_listing_photo(attrs.get("src")):
            index = int(match[1])
            if 1 <= index <= 20:
                self.photos.setdefault(index, attrs["src"])

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False
        if self.description_depth and tag not in (
            "br",
            "img",
            "hr",
            "input",
            "meta",
            "link",
        ):
            self.description_depth -= 1
            if not self.description_depth:
                self.description_done = True

    def handle_data(self, value):
        if not self.in_script and self.description_depth:
            self.description_parts.append(value)


def parse_gallery(html):
    parser = GalleryParser()
    parser.feed(html)
    return distinct_photos([url for _, url in sorted(parser.photos.items())])


def parse_listing(html, url):
    parser = GalleryParser()
    parser.feed(html)
    description = clean_description("".join(parser.description_parts))
    item_id = re.search(r"/items/(\d+)", url).group(1)
    page = parse_page_data(html, item_id)
    return {
        "photos": distinct_photos(
            [url for _, url in sorted(parser.photos.items())] + page["photos"]
        ),
        "description": description or page["description"],
    }


def _pause(seconds):
    with closing(connection()) as conn, conn:
        conn.execute(
            """INSERT INTO delivery_runtime VALUES ('vinted_gallery_after', ?)
            ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)""",
            (time.time() + seconds,),
        )


def challenge_html(html):
    return any(
        marker in html.lower()
        for marker in (
            "client challenge",
            "verify you are human",
            "captcha-delivery.com",
            "checking your browser",
        )
    )


@contextmanager
def listing_client(client=None):
    """Use the configured browser transport, with available saved buyer cookies.

    An unreadable saved session can fall back to anonymous cookies. Invalid private
    network settings fail closed rather than sending the request without its proxy.
    This path never renews account credentials. Its caller validates canonical
    redirects before making any additional same-item read.
    """
    import vinted_buyer as buyer

    if client is not None:
        yield client
        return
    with closing(connection()) as conn:
        saved = conn.execute("SELECT session FROM vinted_buyer WHERE id=1").fetchone()
    cookies = None
    if saved and saved[0]:
        try:
            cookies = buyer.decrypt(saved[0])
        except (buyer.BuyerError, OSError):
            logger.info("Vinted listing detail: saved_session_unavailable")
    client = buyer.Client(cookies) if cookies is not None else buyer.Client()
    try:
        yield client
    finally:
        client.session.close()


@contextmanager
def listing_response(url, client=None):
    """Stream one HTML read; close the client only when this helper owns it."""
    import vinted_buyer as buyer

    url = listing_url(url)
    if not url:
        raise buyer.BuyerError("Unsupported Vinted listing URL.")
    with listing_client(client) as reader, reader.session.get(
        url,
        headers={
            **NAVIGATION_HEADERS,
            "Origin": None,
            "Content-Type": None,
            "Sec-Fetch-Site": "same-origin",
            "Referer": buyer.BASE + "/",
        },
        stream=True,
        timeout=(2, 4),
        allow_redirects=False,
    ) as response:
        yield response


def response_snapshot(response, body, url):
    """Expose only the bounded bytes already read to challenge classification."""
    snapshot = requests.Response()
    snapshot.status_code = response.status_code
    snapshot.headers.update(response.headers)
    snapshot.url = url
    snapshot.encoding = "utf-8"
    snapshot._content = body
    return snapshot


def bounded_body(response, started):
    """Return listing bytes, or a denied response prefix, within the read limits."""
    if response.status_code in (401, 403, 429):
        prefix = bytearray()
        try:
            for chunk in response.iter_content(8192):
                prefix.extend(chunk[: 65536 - len(prefix)])
                if len(prefix) >= 65536 or time.monotonic() - started > 6:
                    break
        except requests.RequestException:
            pass
        return bytes(prefix), "access_limited"
    if response.status_code != 200:
        return b"", "http_" + str(response.status_code)
    chunks, size = [], 0
    for chunk in response.iter_content(65536):
        size += len(chunk)
        if size > 4 * 1024 * 1024 or time.monotonic() - started > 6:
            return b"", "download_limit"
        chunks.append(chunk)
    return b"".join(chunks), "ready"


def fetch_listing(url, *, client=None, parser=parse_listing):
    """Fetch bounded same-item HTML, optionally through a caller-owned client.

    The parser runs only after a successful, fully bounded HTML response. Extra
    parsed fields are returned to the caller and are not written to alert data.
    """
    import vinted_buyer as buyer

    empty = {"photos": [], "description": ""}
    url = listing_url(url)
    if not url:
        return dict(empty, state="invalid_url")
    with closing(connection()) as conn:
        paused = conn.execute(
            "SELECT value FROM delivery_runtime WHERE key='vinted_gallery_after'"
        ).fetchone()
    if paused and paused[0] > time.time():
        return dict(empty, state="cooldown", retry_after=paused[0])
    try:
        with listing_client(client) as reader:
            current_url = url
            canonical_used = False
            solver_used = False
            for attempt in range(3):
                # A solver wait does not consume the next listing's six-second
                # download budget. Both downloads still have independent caps.
                started = time.monotonic()
                with listing_response(current_url, reader) as response:
                    # Retain only fixed diagnostics, never cookies or bodies.
                    logger.info("Vinted listing detail: http=%s", response.status_code)
                    body, state = bounded_body(response, started)
                    snapshot = response_snapshot(response, body, current_url)
                if snapshot.status_code in (301, 302, 303, 307, 308):
                    canonical = canonical_listing_url(
                        snapshot.headers.get("Location"), current_url
                    )
                    if attempt < 2 and not canonical_used and canonical:
                        canonical_used = True
                        current_url = canonical
                        continue
                    if buyer.redirect_reason(snapshot) != "security_challenge":
                        return dict(empty, state=state)
                html = body.decode("utf-8", errors="replace")
                try:
                    challenge_data = snapshot.json()
                except (ValueError, RecursionError):
                    challenge_data = None
                challenge = (
                    buyer.redirect_reason(snapshot) == "security_challenge"
                    or challenge_html(html)
                    or buyer.security_challenge(snapshot, challenge_data)
                )
                if challenge:
                    if attempt < 2 and not solver_used:
                        solver_used = True
                        if reader.solve_challenge(snapshot, challenge_data):
                            continue
                    _pause(300)
                    return dict(empty, state="challenge")
                if state == "access_limited":
                    _pause(300)
                if state != "ready":
                    return dict(empty, state=state)
                data = parser(html, url)
                if not isinstance(data, dict):
                    return dict(empty, state="gallery_unavailable")
                data = dict(empty, **data)
                return dict(
                    data,
                    state=(
                        "ready"
                        if data["photos"] or data["description"] or data.get("item")
                        else "gallery_unavailable"
                    ),
                )
    except requests.RequestException:
        return dict(empty, state="network_error")
    except (buyer.BuyerError, OSError):
        logger.info("Vinted listing detail: configuration_unavailable")
        return dict(empty, state="configuration_error")


def fetch_gallery(url):
    data = fetch_listing(url)
    return data["photos"], data["state"]


async def resolve(row, details, *, persist=True, include_description=False):
    """Called only after the initial notification, or for an explicit preview.

    Cache the result with the queued snapshot so an edit retry cannot repeatedly
    hit the listing page. Nothing touches catalogue polling or its request budget.
    """
    import asyncio
    import json

    retry = include_description and retry_pending(details)
    if retry and time.time() < details.get("listing_retry_after", 0):
        return details.get("photos", [])
    if (
        not retry
        and details.get("gallery_checked")
        and (not include_description or details.get("description_checked"))
    ):
        return details.get("photos", [])
    existing = distinct_photos(details.get("photos", []))
    if include_description and (
        not details.get("description")
        or (len(existing) < 4 and not details.get("gallery_checked"))
    ):
        data = await asyncio.to_thread(fetch_listing, row["url"])
        photos, state = distinct_photos(data["photos"] + existing), data["state"]
        now = time.time()
        details.setdefault("listing_retry_until", now + 900)
        details["listing_attempts"] = details.get("listing_attempts", 0) + (
            state != "cooldown"
        )
        details["listing_retry_after"] = max(
            now + 1,
            data.get(
                "retry_after",
                now + (300 if state in {"cooldown", "access_limited"} else 30),
            ),
        )
        details.update(
            description=data["description"] or details.get("description", ""),
            description_checked=True,
            description_state=state,
        )
    elif len(existing) == 4 or include_description:
        photos, state = existing, "catalogue"
        if include_description:
            details["description_checked"] = True
    else:
        gallery, state = await asyncio.to_thread(fetch_gallery, row["url"])
        photos = distinct_photos(gallery + existing)
    details.update(photos=photos, gallery_checked=True, gallery_state=state)
    if persist:
        with closing(connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_alert_details SET payload=? WHERE item_id=?",
                (json.dumps(details, ensure_ascii=False), row["item_id"]),
            )
    logger.info(
        "Vinted gallery item=%s photos=%s source=%s", row["item_id"], len(photos), state
    )
    return photos
