"""Read public listing gallery markup, never recommendations or account data.

One bounded background page request per listing. Challenges and rate limits
are reported, not worked around; the fast alert retains its catalogue photo.
"""

import re
import time
from contextlib import closing
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

from alert_images import safe_listing_photo
from logger import get_logger
from search_settings import connection

logger = get_logger(__name__)


def listing_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    parsed = urlparse(value)
    if (
        parsed.scheme == "https"
        and parsed.netloc in ("www.vinted.co.uk", "vinted.co.uk")
        and re.fullmatch(r"/items/\d+(?:-[\w-]+)?/?", parsed.path)
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
    def __init__(self):
        super().__init__()
        self.photos = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        match = re.fullmatch(r"item-photo-(\d+)--img", attrs.get("data-testid", ""))
        if tag == "img" and match and safe_listing_photo(attrs.get("src")):
            index = int(match[1])
            if 1 <= index <= 20:
                self.photos.setdefault(index, attrs["src"])


def parse_gallery(html):
    parser = GalleryParser()
    parser.feed(html)
    return distinct_photos([url for _, url in sorted(parser.photos.items())])


def _pause(seconds):
    with closing(connection()) as conn, conn:
        conn.execute(
            """INSERT INTO delivery_runtime VALUES ('vinted_gallery_after', ?)
            ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)""",
            (time.time() + seconds,),
        )


def fetch_gallery(url):
    url = listing_url(url)
    if not url:
        return [], "invalid_url"
    with closing(connection()) as conn:
        paused = conn.execute(
            "SELECT value FROM delivery_runtime WHERE key='vinted_gallery_after'"
        ).fetchone()
    if paused and paused[0] > time.time():
        return [], "cooldown"
    started = time.monotonic()
    try:
        with requests.get(
            url,
            stream=True,
            timeout=(2, 4),
            allow_redirects=False,
        ) as response:
            if response.status_code in (401, 403, 429):
                _pause(300)
                return [], "access_limited"
            if response.status_code != 200:
                return [], "http_" + str(response.status_code)
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 4 * 1024 * 1024 or time.monotonic() - started > 6:
                    return [], "download_limit"
                chunks.append(chunk)
            html = b"".join(chunks).decode("utf-8", errors="replace")
    except requests.RequestException:
        return [], "network_error"
    photos = parse_gallery(html)
    if photos:
        return photos, "ready"
    if any(
        marker in html.lower()
        for marker in (
            "client challenge",
            "verify you are human",
            "captcha-delivery.com",
            "checking your browser",
        )
    ):
        _pause(300)
        return [], "challenge"
    return [], "gallery_unavailable"


async def resolve(row, details, *, persist=True):
    """Called only after the initial notification, or for an explicit preview.

    Cache the result with the queued snapshot so an edit retry cannot repeatedly
    hit the listing page. Nothing touches catalogue polling or its request budget.
    """
    import asyncio
    import json

    if details.get("gallery_checked"):
        return details.get("photos", [])
    existing = distinct_photos(details.get("photos", []))
    if len(existing) == 4:
        photos, state = existing, "catalogue"
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
