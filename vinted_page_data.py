"""Read identity-scoped seller data from server-rendered listing scripts.

Schema references reviewed 7 October 2026; a current UK listing page also
confirmed the anchored Flight description plugin on that date:
https://github.com/teddy-vltn/vinted-discord-bot/blob/main/src/api/fetchItemDetail.js
(Unlicense) reads descriptions from the rendered payload after detail API removal.
https://github.com/ScrapeUnblocker/vinted-scraper/blob/main/src/scrapeunblocker_vinted/parsing.py
and its tests/conftest.py (MIT) describe Next.js Flight chunks, item IDs, photo
records, description plugins and JSON-LD offer URLs. The latter fixtures are
synthetic. This is an original parser, with no dependency on either transport.
Length-framed Flight text records follow React's MIT-licensed protocol sources:
https://github.com/facebook/react/blob/main/packages/react-server/src/ReactFlightServer.js
(emitTextChunk) and ReactServerStreamConfigNode.js (UTF-8 byte lengths), alongside
packages/react-client/src/ReactFlightClient.js (row framing and string references).

Only JSON is decoded. Item identity must belong to the same record as its
description/photos, or explicitly anchor its plugin list/reference. Unrelated
recommendations, account data, executable JavaScript and global first-description
matches are excluded. Unsupported Flight record types fail closed.
"""

import json
import re
from collections import deque
from html.parser import HTMLParser
from itertools import islice
from urllib.parse import unquote, urlparse

from alert_images import safe_listing_photo
from listing_text import clean_description

MAX_HTML = 4 * 1024 * 1024
MAX_SCRIPTS = 512
MAX_DEPTH = 64
MAX_NODES = 10000
MAX_PURCHASE_NODES = 50000
MAX_ROWS = 2048
_PUSH = "self.__next_f.push("
_ROW = re.compile(rb"([0-9a-fA-F]{0,16}):")
_LENGTH = re.compile(rb"([0-9a-fA-F]{1,8}),")
# React's byte-framed text/typed-array tags. Only T is decoded as seller text;
# the remaining binary records are consumed by length and never inspected.
_LENGTH_TAGS = b"TAOobUSsLlGgMmV"


class _RawText(str):
    """Raw T records are literal text, even when they begin with '$' or JSON."""


def _empty():
    return {"photos": [], "description": ""}


def _item_id(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    value = str(value)
    return value if re.fullmatch(r"[0-9]{1,20}", value) else None


def _url_id(value):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parsed = urlparse(value)
        if parsed.netloc and (
            parsed.scheme != "https"
            or parsed.netloc not in ("www.vinted.co.uk", "vinted.co.uk")
        ):
            return None
        if not parsed.netloc and parsed.scheme:
            return None
        match = re.fullmatch(
            r"/items/([0-9]{1,20})(?:-[\w-]+)?/?", unquote(parsed.path)
        )
        return match[1] if match else None
    except ValueError:
        return None


def _loads(text):
    """Check nesting before passing bounded text to the recursive JSON decoder."""
    if len(text) > MAX_HTML:
        return None
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                return None
        elif char in "]}":
            depth -= 1
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "script" and len(self.scripts) < MAX_SCRIPTS:
            self.current = (dict(attrs), [])

    def handle_data(self, value):
        if self.current is not None:
            self.current[1].append(value)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            attrs, parts = self.current
            self.scripts.append((attrs, "".join(parts)))
            self.current = None


def _flight_rows(payload):
    try:
        data = payload.encode("utf-8")
    except UnicodeError:
        return {}
    if len(data) > MAX_HTML:
        return {}
    rows, position, count = {}, 0, 0
    while position < len(data) and count < MAX_ROWS:
        count += 1
        match = _ROW.match(data, position)
        if not match or match.end() >= len(data):
            return {}
        row = match[1].decode("ascii").lower()
        start = match.end()
        tag = data[start : start + 1]
        if tag[0] in _LENGTH_TAGS:
            length = _LENGTH.match(data, start + 1)
            if not length:
                return {}
            size = int(length[1], 16)
            end = length.end() + size
            if size > MAX_HTML or end > len(data):
                return {}
            value = None
            if tag == b"T":
                try:
                    value = _RawText(data[length.end() : end].decode("utf-8"))
                except UnicodeError:
                    return {}
            # Length records have no trailing delimiter. A newline inside their
            # raw body cannot introduce another record, and Unicode counts bytes.
            position = end
        else:
            end = data.find(b"\n", start)
            if end < 0:
                return {}
            value = None
            if not (65 <= tag[0] <= 90 or tag in (b"#", b"r", b"x")):
                try:
                    text = data[start:end].decode("utf-8")
                except UnicodeError:
                    return {}
                value = _loads(text)
                if value is None and text.strip() != "null":
                    return {}
            position = end + 1
        if row and value is not None:
            # Import/debug metadata can share IDs with model records, but duplicate
            # model/text definitions cannot establish a unique reference target.
            rows[row] = None if row in rows else value
    return rows if position == len(data) else {}


def _records(scripts):
    roots, chunks = [], []
    for attrs, text in scripts:
        if attrs.get("id") == "__NEXT_DATA__" or attrs.get("type") == (
            "application/ld+json"
        ):
            value = _loads(text)
            if value is not None:
                roots.append(value)
            continue
        # A standalone call is accepted; expressions inside JS strings or other
        # executable statements are not searched or evaluated.
        text = text.strip().removesuffix(";").rstrip()
        if not text.startswith(_PUSH) or not text.endswith(")"):
            continue
        value = _loads(text[len(_PUSH) : -1])
        if (
            isinstance(value, list)
            and len(value) == 2
            and value[0] == 1
            and not isinstance(value[0], bool)
            and isinstance(value[1], str)
        ):
            chunks.append(value[1])
    payload = "".join(chunks)
    rows = _flight_rows(payload)
    roots.extend(value for value in rows.values() if value is not None)
    return roots, rows


def _resolve(value, rows):
    seen = set()
    for _ in range(8):
        if isinstance(value, _RawText):
            return str(value)
        if not isinstance(value, str) or not value.startswith("$"):
            return value
        if value.startswith("$$"):
            return value[1:]
        if len(value) > 512 or value in seen:
            return None
        seen.add(value)
        pieces = value[1:].split(":")
        if len(pieces) > 12 or not re.fullmatch(r"[0-9a-fA-F]+", pieces[0]):
            return None
        value = rows.get(pieces[0].lower())
        for key in pieces[1:]:
            if (
                key == "props"
                and isinstance(value, list)
                and len(value) == 4
                and value[0] == "$"
            ):
                value = value[3]
            elif isinstance(value, dict):
                value = value.get(key)
            else:
                return None
    return None


def _description(value, rows, *, html=False):
    if value == "$undefined":
        return ""
    if isinstance(value, str) and re.fullmatch(r"\$[0-9a-fA-F]+(?::[\w]+)*", value):
        value = _resolve(value, rows)
    return clean_description(value, html=html)


def _plugins(value, item_id, *, scoped, rows):
    value = _resolve(value, rows)
    if not isinstance(value, list) or len(value) > 100:
        return ""
    plugins = []
    identities = []
    for raw in value:
        plugin = _resolve(raw, rows)
        if not isinstance(plugin, dict):
            continue
        data = _resolve(plugin.get("data"), rows)
        if not isinstance(data, dict):
            continue
        plugins.append((plugin.get("name"), data))
        # An explicit identity anywhere in this plugin list must agree, including
        # the description itself, not only the summary/shipping plugins.
        if "item_id" in data:
            identities.append(_item_id(data["item_id"]))
    if identities and any(identity != item_id for identity in identities):
        return ""
    if not scoped and not identities:
        return ""
    for name, data in plugins:
        if name == "description":
            return _description(data.get("description"), rows)
    return ""


def _product_identity(value, item_id):
    offers = value.get("offers")
    offers = offers if isinstance(offers, list) else [offers]
    urls = [value.get("url"), value.get("@id")]
    urls.extend(offer.get("url") for offer in offers if isinstance(offer, dict))
    identities = [_url_id(url) for url in urls if url is not None]
    return bool(identities) and all(identity == item_id for identity in identities)


def _add_photos(result, values):
    if not isinstance(values, list):
        values = [values]
    seen = {re.sub(r"(/t/[^/]+/).*", r"\1", urlparse(url).path) for url in result}
    for value in values[:20]:
        url = value.get("url") if isinstance(value, dict) else value
        if not safe_listing_photo(url):
            continue
        identity = re.sub(r"(/t/[^/]+/).*", r"\1", urlparse(url).path)
        if identity not in seen and len(result) < 4:
            result.append(url)
            seen.add(identity)


def parse_purchase_item(html, item_id):
    """Read current buy eligibility from one consistent, identity-scoped item."""
    item_id = _item_id(item_id)
    if not item_id or not isinstance(html, str) or len(html) > MAX_HTML:
        return None
    parser = _Scripts()
    parser.feed(html)
    roots, rows = _records(parser.scripts)
    pending = deque((root, 0) for root in roots)
    candidates = []
    visited = 0
    required = ("seller_id", "price", "can_buy", "is_reserved", "is_hidden")
    while pending and visited < MAX_PURCHASE_NODES:
        value, depth = pending.popleft()
        visited += 1
        if depth > MAX_DEPTH:
            return None
        remaining = max(0, MAX_PURCHASE_NODES - visited - len(pending))
        if isinstance(value, list):
            children = [child for child in value if isinstance(child, (dict, list))]
            if len(children) > remaining:
                return None
            pending.extend((child, depth + 1) for child in children)
            continue
        if not isinstance(value, dict):
            continue
        identities = [_item_id(value[key]) for key in ("id", "item_id") if key in value]
        # Recommendations and independent status/description plugins cannot
        # supply a target item's price or seller. Every field comes from the
        # same complete item record, including resolved Flight references.
        if _item_id(value.get("id")) == item_id and (
            "price" in value or "can_buy" in value
        ):
            if any(identity != item_id for identity in identities) or not all(
                key in value for key in required
            ):
                return None
            if "url" in value and _url_id(value["url"]) != item_id:
                return None
            price = _resolve(value["price"], rows)
            seller = _item_id(_resolve(value["seller_id"], rows))
            flags = {
                key: _resolve(value[key], rows)
                for key in (
                    "can_buy",
                    "is_reserved",
                    "is_hidden",
                    "is_sold",
                    "is_closed",
                )
                if key in value
            }
            if (
                not seller
                or not isinstance(price, dict)
                or not isinstance(price.get("currency_code"), str)
                or "amount" not in price
                or any(type(flag) is not bool for flag in flags.values())
            ):
                return None
            candidate = {
                "id": item_id,
                "user_id": seller,
                "price": {
                    "amount": price["amount"],
                    "currency_code": price["currency_code"],
                },
                **flags,
            }
            # Sold/closed can explain an otherwise generic can_buy=False, but
            # only an explicit boolean in this same complete target record is
            # evidence. Optional status must also agree across duplicate records.
            if candidates and candidate != candidates[0]:
                return None
            candidates.append(candidate)
        children = [
            child for child in value.values() if isinstance(child, (dict, list))
        ]
        if len(children) > remaining:
            return None
        pending.extend((child, depth + 1) for child in children)
    # A truncated traversal cannot rule out a later conflicting target record.
    return candidates[0] if candidates and not pending else None


def parse_page_data(html, item_id):
    """Return only target-listing data from supported JSON and Flight records."""
    item_id = _item_id(item_id)
    if not item_id or not isinstance(html, str) or len(html) > MAX_HTML:
        return _empty()
    parser = _Scripts()
    parser.feed(html)
    roots, rows = _records(parser.scripts)
    # Give each Flight record a turn before descending into a large bootstrap
    # record. Current pages put the listing after thousands of translation and
    # configuration values; depth-first traversal exhausts the bound first.
    pending = deque((root, 0) for root in roots)
    result = _empty()
    visited = 0
    while pending and visited < MAX_NODES:
        value, depth = pending.popleft()
        visited += 1
        if depth > MAX_DEPTH:
            continue
        remaining = max(0, MAX_NODES - visited - len(pending))
        if isinstance(value, list):
            pending.extend(
                (child, depth + 1)
                for child in islice(
                    (child for child in value if isinstance(child, (dict, list))),
                    remaining,
                )
            )
            continue
        if not isinstance(value, dict):
            continue
        product = value.get("@type") == "Product"
        explicit_ids = [
            _item_id(value[key]) for key in ("id", "item_id") if key in value
        ]
        identity_matches = bool(explicit_ids) and all(
            identity == item_id for identity in explicit_ids
        )
        matched = (
            _product_identity(value, item_id)
            if product
            else (
                identity_matches
                and (
                    isinstance(_resolve(value.get("photos"), rows), list)
                    or "seller_id" in value
                    or "plugins" in value
                    or (
                        isinstance(value.get("title"), str)
                        and ("description" in value or "plugins" in value)
                    )
                )
                and all(
                    _url_id(value[key]) == item_id for key in ("url",) if key in value
                )
            )
        )
        if matched:
            if not result["description"]:
                result["description"] = _description(
                    value.get("description"), rows, html=product
                ) or _plugins(
                    _resolve(value.get("plugins"), rows),
                    item_id,
                    scoped=True,
                    rows=rows,
                )
            _add_photos(
                result["photos"],
                _resolve(value.get("image") if product else value.get("photos"), rows),
            )
        elif (
            not result["description"]
            and "plugins" in value
            and not product
            and not explicit_ids
        ):
            result["description"] = _plugins(
                _resolve(value["plugins"], rows), item_id, scoped=False, rows=rows
            )
        # Inspect child records individually, never inherit identity to arbitrary
        # descendants (which can contain recommendations or seller profiles).
        pending.extend(
            (child, depth + 1)
            for child in islice(
                (child for child in value.values() if isinstance(child, (dict, list))),
                remaining,
            )
        )
    return result
