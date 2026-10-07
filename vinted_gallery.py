"""Read public listing gallery markup, never recommendations or account data.

One bounded background page request per listing. Challenges and rate limits
are reported, not worked around; the fast alert retains its catalogue photo.
"""

import re
import time
from contextlib import closing, contextmanager
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlparse

import requests

from alert_images import safe_listing_photo
from listing_text import clean_description
from logger import get_logger
from search_settings import connection
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
    if (
        not isinstance(value, str)
        or len(value) > 4096
        or re.search(r"[\x00-\x20\x7f]", value)
    ):
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if (
        parsed.scheme == "https"
        and parsed.netloc in ("www.vinted.co.uk", "vinted.co.uk")
        and re.fullmatch(r"/items/[0-9]{1,20}(?:-[\w-]+)?/?", unquote(parsed.path))
    ):
        return "https://www.vinted.co.uk" + parsed.path
    return None


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
    VOID_TAGS = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
    )
    HIDDEN_TAGS = frozenset(
        {"script", "style", "noscript", "iframe", "template", "button"}
    )

    def __init__(self):
        super().__init__()
        self.photos = {}
        self.stack = []
        self.descriptions = []

    @property
    def description(self):
        # The outer item-description can include controls and a collapsed copy.
        # Prefer the actual text anchor, while retaining the older outer markup.
        for testid in ("item-description-text", "item-description"):
            for candidate in self.descriptions:
                if candidate["done"] and candidate["testid"] == testid:
                    text = clean_description("".join(candidate["parts"]))
                    if text:
                        return text
        return ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = (
            bool(self.stack and self.stack[-1][1])
            or tag in self.HIDDEN_TAGS
            or "hidden" in attrs
            or attrs.get("aria-hidden", "").lower() == "true"
        )
        if not hidden and attrs.get("data-testid") in (
            "item-description",
            "item-description-text",
        ):
            self.descriptions.append(
                {
                    "testid": attrs["data-testid"],
                    "depth": len(self.stack) + 1,
                    "parts": [],
                    "done": tag in self.VOID_TAGS,
                }
            )
        if not hidden and tag in ("br", "p", "div", "li"):
            for candidate in self.descriptions:
                if not candidate["done"]:
                    candidate["parts"].append("\n")
        if tag not in self.VOID_TAGS:
            self.stack.append((tag, hidden))
        match = re.fullmatch(r"item-photo-(\d+)--img", attrs.get("data-testid", ""))
        if tag == "img" and match and safe_listing_photo(attrs.get("src")):
            index = int(match[1])
            if 1 <= index <= 20:
                self.photos.setdefault(index, attrs["src"])

    def handle_endtag(self, tag):
        if tag in self.VOID_TAGS:
            return
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                if not self.stack[-1][1] and tag in ("p", "div", "li"):
                    for candidate in self.descriptions:
                        if not candidate["done"]:
                            candidate["parts"].append("\n")
                del self.stack[index:]
                for candidate in self.descriptions:
                    if candidate["depth"] > len(self.stack):
                        candidate["done"] = True
                return

    def handle_data(self, value):
        if not self.stack or not self.stack[-1][1]:
            for candidate in self.descriptions:
                if not candidate["done"]:
                    candidate["parts"].append(value)


def parse_gallery(html):
    parser = GalleryParser()
    parser.feed(html)
    return distinct_photos([url for _, url in sorted(parser.photos.items())])


def parse_listing(html, url):
    url = listing_url(url)
    if not url or not isinstance(html, str) or len(html) > 4 * 1024 * 1024:
        return {"photos": [], "description": ""}
    parser = GalleryParser()
    parser.feed(html)
    item_id = re.search(r"/items/(\d+)", url).group(1)
    page = parse_page_data(html, item_id)
    return {
        "photos": distinct_photos(
            [url for _, url in sorted(parser.photos.items())] + page["photos"]
        ),
        "description": parser.description or page["description"],
    }


def _pause(seconds):
    with closing(connection()) as conn, conn:
        conn.execute(
            """INSERT INTO delivery_runtime VALUES ('vinted_gallery_after', ?)
            ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)""",
            (time.time() + seconds,),
        )
        return conn.execute(
            "SELECT value FROM delivery_runtime WHERE key='vinted_gallery_after'"
        ).fetchone()[0]


def retry_delay(response):
    """Keep the shared five-minute minimum and honour a longer server pause."""
    value = response.headers.get("Retry-After")
    if not isinstance(value, str) or len(value) > 100:
        return 300
    value = value.strip()
    if re.fullmatch(r"[0-9]{1,10}", value):
        return max(300, int(value))
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is not None:
            return max(300, date.timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        pass
    return 300


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


def canonical_redirect(url, location):
    """Accept only a canonical UK item URL for this same numeric listing."""
    if (
        not isinstance(location, str)
        or len(location) > 4096
        or re.search(r"[\x00-\x20\x7f]", location)
    ):
        return None
    url = listing_url(url)
    if not url:
        return None
    try:
        destination = urljoin(url, location)
        parsed = urlparse(destination)
    except ValueError:
        return None
    # A canonical item redirect needs no query or fragment. This also excludes
    # authentication/challenge parameters without copying them into diagnostics.
    if parsed.query or parsed.fragment:
        return None
    target = listing_url(destination)
    if not target or target == url:
        return None
    old_id = re.match(r"/items/([0-9]+)", urlparse(url).path)[1]
    new_id = re.match(r"/items/([0-9]+)", urlparse(target).path)[1]
    return target if old_id == new_id else None


@contextmanager
def listing_response(url):
    """Reuse the owner's saved session for one read, without renewing or buying.

    Public clients send a normal Vinted session cookie on item-page reads. Keep
    our encrypted connection inside the server. One verified redirect to the
    same UK item can canonicalise its slug. No renewal, checkout, challenge
    solving or cross-origin redirect happens in this read-only path.
    """
    import vinted_buyer as buyer

    with closing(connection()) as conn:
        saved = conn.execute("SELECT session FROM vinted_buyer WHERE id=1").fetchone()
    client = None
    if saved and saved[0]:
        try:
            client = buyer.Client(buyer.decrypt(saved[0]))
        except (buyer.BuyerError, OSError):
            logger.info("Vinted listing detail: saved_session_unavailable")
    get = client.session.get if client else requests.get
    try:
        for attempt in range(2):
            with get(
                url,
                headers={"Accept": "text/html,application/xhtml+xml"},
                stream=True,
                timeout=(2, 4),
                allow_redirects=False,
            ) as response:
                target = (
                    canonical_redirect(url, response.headers.get("Location"))
                    if not attempt and response.status_code in (301, 302, 307, 308)
                    else None
                )
                if target:
                    logger.info("Vinted listing detail: same_item_redirect")
                    url = target
                    continue
                yield response
                return
    finally:
        if client:
            client.session.close()


def fetch_listing(url):
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
    started = time.monotonic()
    try:
        with listing_response(url) as response:
            # Numeric status distinguishes denied access from rate limiting.
            # Never retain response bodies, cookies or redirect query strings.
            logger.info("Vinted listing detail: http=%s", response.status_code)
            if response.status_code in (401, 403, 429):
                retry_after = _pause(retry_delay(response))
                # Challenges may arrive with HTTP 403 rather than HTTP 200.
                # Inspect a bounded prefix before deciding a denial is retryable.
                prefix = bytearray()
                try:
                    for chunk in response.iter_content(8192):
                        prefix.extend(chunk[: 65536 - len(prefix)])
                        if len(prefix) >= 65536 or time.monotonic() - started > 6:
                            break
                except requests.RequestException:
                    pass
                state = (
                    "challenge"
                    if challenge_html(prefix.decode("utf-8", errors="replace"))
                    else "access_limited"
                )
                return dict(empty, state=state, retry_after=retry_after)
            if response.status_code != 200:
                return dict(empty, state="http_" + str(response.status_code))
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 4 * 1024 * 1024 or time.monotonic() - started > 6:
                    return dict(empty, state="download_limit")
                chunks.append(chunk)
            html = b"".join(chunks).decode("utf-8", errors="replace")
    except requests.RequestException:
        return dict(empty, state="network_error")
    if challenge_html(html):
        _pause(300)
        return dict(empty, state="challenge")
    data = parse_listing(html, url)
    return dict(
        data,
        state=(
            "ready" if data["photos"] or data["description"] else "gallery_unavailable"
        ),
    )


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
