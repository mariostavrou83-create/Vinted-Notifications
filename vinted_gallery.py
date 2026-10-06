"""Read public listing gallery markup, never recommendations or account data.

One bounded background page request per listing. Challenges and rate limits
are reported, not worked around; the fast alert retains its catalogue photo.
"""

import json
import re
import time
from contextlib import closing
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

from alert_images import safe_listing_photo
from listing_text import clean_description
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
        self.description_parts = []
        self.description_depth = 0
        self.description_done = False
        self.structured = []
        self.script_parts = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.script_parts = []
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
        if tag == "script" and self.script_parts is not None:
            try:
                self.structured.append(json.loads("".join(self.script_parts)))
            except (ValueError, TypeError):
                pass
            self.script_parts = None
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
        if self.script_parts is not None:
            self.script_parts.append(value)
        elif self.description_depth:
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
    pending = list(parser.structured)
    while pending and not description:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            if isinstance(value.get("@graph"), list):
                pending.extend(value["@graph"])
            identity = re.search(
                r"/items/(\d+)", str(value.get("url") or value.get("@id") or "")
            )
            if value.get("@type") == "Product" and identity and identity[1] == item_id:
                description = clean_description(value.get("description"), html=True)
    return {
        "photos": distinct_photos([url for _, url in sorted(parser.photos.items())]),
        "description": description,
    }


def _pause(seconds):
    with closing(connection()) as conn, conn:
        conn.execute(
            """INSERT INTO delivery_runtime VALUES ('vinted_gallery_after', ?)
            ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)""",
            (time.time() + seconds,),
        )


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
        return dict(empty, state="cooldown")
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
                return dict(empty, state="access_limited")
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

    if details.get("gallery_checked") and (
        not include_description or details.get("description_checked")
    ):
        return details.get("photos", [])
    existing = distinct_photos(details.get("photos", []))
    if include_description and not details.get("description"):
        data = await asyncio.to_thread(fetch_listing, row["url"])
        photos, state = distinct_photos(data["photos"] + existing), data["state"]
        details.update(
            description=data["description"],
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
