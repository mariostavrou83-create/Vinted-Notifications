"""Bounded, read-only Home cards and target listing freshness.

Selectors are from the UK Home and listing DOM inspected on 20 November 2026.
Home card IDs are the only candidates; arbitrary recommendations in page scripts
are never added. Upload age is the target sidebar's native upload_date field,
not first-seen time, item ordering, photo timestamps or seller activity.
"""

import math
import re
from collections import deque
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from lxml import html as lhtml

from alert_images import safe_listing_photo
from listing_text import clean_description
from vinted_page_data import (
    MAX_DEPTH,
    MAX_HTML,
    _Scripts,
    _records,
    _resolve,
    parse_page_data,
    parse_purchase_item,
)

MAX_NODES = 20000
MAX_ITEMS = 96
MAX_AGE_SECONDS = 180


def _text(node):
    return " ".join(node.text_content().split())


def _identity(value):
    return str(value) if type(value) in (str, int) and re.fullmatch(r"[0-9]{1,20}", str(value)) else None


def item_url(value, identity=None):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.netloc and (
            parsed.scheme != "https"
            or parsed.hostname not in {"www.vinted.co.uk", "vinted.co.uk"}
            or parsed.port not in (None, 443)
            or parsed.username or parsed.password
        ):
            return None
        if not parsed.netloc and (parsed.scheme or not value.startswith("/")):
            return None
        match = re.fullmatch(r"/items/([0-9]{1,20})(?:-[\w-]+)?/?", parsed.path)
        if not match or (identity is not None and match[1] != str(identity)):
            return None
        return "https://www.vinted.co.uk" + parsed.path
    except ValueError:
        return None


def _tree(body):
    if not isinstance(body, str) or not body or len(body) > MAX_HTML:
        return None
    try:
        tree = lhtml.fromstring(body, parser=lhtml.HTMLParser(no_network=True, huge_tree=False))
        if sum(1 for _ in tree.iter()) > MAX_NODES:
            return None
        return tree
    except (ValueError, lhtml.etree.ParserError):
        return None


def _money(value):
    if not isinstance(value, str) or len(value) > 32:
        return None
    match = re.fullmatch(r"£\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)", value)
    if not match:
        return None
    try:
        amount = Decimal(match[1].replace(",", ""))
        if not amount.is_finite() or not 0 < amount <= 1000000:
            return None
        return {"amount": f"{amount:.2f}", "currency_code": "GBP"}
    except InvalidOperation:
        return None


def age_bounds(value):
    """Return conservative seconds bounds for the displayed UK upload age."""
    if not isinstance(value, str) or len(value) > 80:
        return None
    text = " ".join(value.casefold().split()).removeprefix("uploaded ")
    if text in {"just now", "a moment ago", "less than a minute ago"}:
        return 0, 60
    match = re.fullmatch(r"(a|an|[0-9]{1,6}) (sec(?:ond)?s?|min(?:ute)?s?|hours?|days?|weeks?|months?|years?) ago", text)
    if not match:
        return None
    count = 1 if match[1] in {"a", "an"} else int(match[1])
    unit = match[2]
    multiplier = (1 if unit.startswith("sec") else 60 if unit.startswith("min")
                  else 3600 if unit.startswith("hour") else 86400 if unit.startswith("day")
                  else 604800 if unit.startswith("week") else 2678400 if unit.startswith("month") else 31622400)
    return count * multiplier, (count + 1) * multiplier


def parse_home(body):
    """Return only visible Home feed cards with a valid UK item URL and GBP price."""
    tree = _tree(body)
    if tree is None:
        return {"items": [], "reason": "invalid_document"}
    navigation = tree.xpath('//header//a/@href | //*[@role="banner"]//a/@href')
    if any(urlsplit(href).path.startswith(("/member/signup", "/member/login")) for href in navigation):
        return {"items": [], "reason": "home_not_authenticated", "authenticated": False}
    if not any(href.startswith("/inbox") and not href.startswith("//")
               and urlsplit(href).path.rstrip("/") == "/inbox" for href in navigation):
        return {"items": [], "reason": "home_auth_unverified", "authenticated": False}
    cards = tree.xpath('//*[@data-testid="feed-item"]')
    if len(cards) > MAX_ITEMS:
        cards = cards[:MAX_ITEMS]
    items, seen = [], set()
    for card in cards:
        links = card.xpath('.//a[@data-testid="feed-item--overlay-link"]')
        prices = card.xpath('.//*[@data-testid="feed-item--price-text"]')
        if len(links) != 1 or len(prices) != 1:
            continue
        url = item_url(links[0].get("href"))
        price = _money(_text(prices[0]))
        if not url or not price:
            continue
        identity = re.search(r"/items/([0-9]+)", url)[1]
        if identity in seen:
            continue
        caption = links[0].get("title") or links[0].get("aria-label") or ""
        title = re.split(r", (?:brand|condition): ", caption, maxsplit=1)[0].strip()
        if not title or len(title) > 500 or title == caption:
            continue
        images = card.xpath('.//img[@data-testid="feed-item--image--img"]/@src')
        brand = card.xpath('.//*[@data-testid="feed-item--description-title"]')
        subtitle = card.xpath('.//*[@data-testid="feed-item--description-subtitle"]')
        details = _text(subtitle[0]).split(" · ") if len(subtitle) == 1 else []
        combined = card.xpath('.//*[@data-testid="total-combined-price"]')
        item = {"id": identity, "url": url, "title": title, "price": price,
                "brand_title": _text(brand[0])[:120] if len(brand) == 1 else "",
                "size_title": details[0][:80] if len(details) > 1 else "",
                "condition": details[-1][:80] if details else "",
                "photo": {"url": images[0]} if len(images) == 1 and safe_listing_photo(images[0]) else {},
                "description_complete": False}
        if len(combined) == 1 and (total := _money(_text(combined[0]))):
            item["total_item_price"] = total
        seen.add(identity)
        items.append(item)
    return {"items": items, "reason": "ok" if items else "home_cards_unavailable", "authenticated": True}


def _sidebar(tree, identity):
    canonical = tree.xpath('//link[@rel="canonical"]/@href')
    if len(canonical) != 1 or not item_url(canonical[0], identity):
        return None
    summaries = tree.xpath('//*[@data-testid="item-page-summary-plugin"]')
    if len(summaries) != 1:
        return None
    side = summaries[0].xpath('ancestor::div[contains(@class,"item-page-sidebar-content")][1]')
    return side[0] if len(side) == 1 else None


def listing_age(body, item_id, now):
    """Age at request start; caller must add elapsed/cache age before alerting."""
    result = {"verified": False, "max_age_seconds": None, "checked_at": now,
              "evidence": "upload_age_unverified"}
    identity = _identity(item_id)
    if not identity or type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
        return result
    tree = _tree(body)
    side = _sidebar(tree, identity) if tree is not None else None
    if side is None:
        return result
    ages = side.xpath('.//*[@itemprop="upload_date"]')
    if len(ages) != 1 or (bounds := age_bounds(_text(ages[0]))) is None:
        return result
    # A second displayed summary must agree, never rescue a missing upload field.
    summaries = side.xpath('.//*[@data-testid="item-page-summary-plugin"]')
    for node in summaries[0].iter() if summaries else ():
        if len(node) == 0 and _text(node).startswith("Uploaded "):
            other = age_bounds(_text(node))
            if other is None or other != bounds:
                return result
    result.update(verified=True, min_age_seconds=bounds[0], max_age_seconds=bounds[1],
                  evidence="target_upload_date", fresh=bounds[1] <= MAX_AGE_SECONDS)
    return result


def _native_attributes(body, identity):
    """Optional exact attributes from complete same-item records, never neighbors."""
    scripts = _Scripts()
    scripts.feed(body)
    roots, rows = _records(scripts.scripts)
    pending = deque((root, 0) for root in roots)
    fields, visited = {}, 0
    keys = ("brand_id", "catalog_id", "size_id", "status_id", "color1_id", "color2_id", "material_id")
    while pending and visited < MAX_NODES:
        value, depth = pending.popleft()
        visited += 1
        if depth > MAX_DEPTH:
            return {}
        if isinstance(value, dict):
            if _identity(value.get("id")) == identity and "seller_id" in value:
                if "item_id" in value and _identity(value["item_id"]) != identity:
                    return {}
                for key in keys:
                    if key in value:
                        found = _identity(_resolve(value[key], rows))
                        if found is None or (key in fields and fields[key] != found):
                            return {}
                        fields[key] = found
            children = [x for x in value.values() if isinstance(x, (dict, list))]
        elif isinstance(value, list):
            children = [x for x in value if isinstance(x, (dict, list))]
        else:
            children = []
        if len(children) + len(pending) + visited > MAX_NODES:
            return {}
        pending.extend((child, depth + 1) for child in children)
    return fields if not pending else {}


def parse_listing(body, item_id, now):
    """Return fresh target details; no credentials, writes, or purchase operations."""
    age = listing_age(body, item_id, now)
    result = {"age": age, "item": None, "reason": "listing_unverified"}
    if not age["verified"]:
        return result
    tree = _tree(body)
    identity = str(item_id)
    side = _sidebar(tree, identity)
    native = parse_purchase_item(body, identity)
    if not native or native.get("can_buy") is not True or any(native.get(k) is True for k in ("is_reserved", "is_hidden", "is_sold", "is_closed")):
        return result
    titles = side.xpath('.//*[@data-testid="item-page-summary-plugin"]//h1')
    prices = side.xpath('.//*[@data-testid="item-price"]')
    if len(titles) != 1 or len(prices) != 1:
        return result
    price = _money(_text(prices[0]))
    native_price = native.get("price") or {}
    if not price or native_price.get("currency_code") != "GBP":
        return result
    try:
        if Decimal(str(native_price.get("amount"))) != Decimal(price["amount"]):
            return result
    except InvalidOperation:
        return result
    details = parse_page_data(body, identity, full_description=True)
    descriptions = side.xpath('.//*[@itemprop="description"]')
    full_text = _text(descriptions[0]) if len(descriptions) == 1 else details.get("description", "")
    complete = bool(full_text) and len(full_text) <= 6000 and "[Description shortened" not in full_text
    description = clean_description(full_text)
    brand_links = side.xpath('.//a[starts-with(@href,"/brand/")]')
    brands = {m[1] for a in brand_links if (m := re.match(r"^/brand/([0-9]{1,20})(?:-|$)", a.get("href", "")))}
    sizes = side.xpath('.//*[@itemprop="size"]')
    conditions = side.xpath('.//*[@itemprop="status" or @itemprop="condition"]')
    canonical = tree.xpath('//link[@rel="canonical"]/@href')[0]
    attrs = _native_attributes(body, identity)
    if len(brands) == 1:
        brand_id = next(iter(brands))
        if attrs.get("brand_id") and attrs["brand_id"] != brand_id:
            return result
        attrs["brand_id"] = brand_id
    item = {"id": identity, "url": item_url(canonical, identity), "title": _text(titles[0])[:500],
            "price": price, "seller_id": native["user_id"], "description": description,
            "description_complete": complete, "photos": details.get("photos", []),
            "brand_title": _text(brand_links[0])[:120] if len(brands) == 1 else "",
            "size_title": _text(sizes[0])[:80] if len(sizes) == 1 else "",
            "condition": _text(conditions[0])[:80] if len(conditions) == 1 else "", **attrs}
    # Breadcrumb list is observed before the sidebar, not category links in similar items.
    breadcrumbs = tree.xpath('//main//ul[.//a[contains(@href,"referrer=item-crumbs")]]')
    categories = set()
    if len(breadcrumbs) == 1:
        for href in breadcrumbs[0].xpath('.//a/@href'):
            if match := re.match(r"^/catalog/([0-9]{1,20})(?:-|[?]|$)", href):
                categories.add(match[1])
    if attrs.get("catalog_id"):
        categories.add(attrs["catalog_id"])
    item["category_ids"] = sorted(categories)
    if attrs.get("status_id"):
        item["condition_id"] = attrs["status_id"]
    item["color_ids"] = [attrs[k] for k in ("color1_id", "color2_id") if attrs.get(k)]
    item["material_ids"] = [attrs["material_id"]] if attrs.get("material_id") else []
    combined = side.xpath('.//*[@data-testid="total-combined-price"]')
    if len(combined) == 1 and (total := _money(_text(combined[0]))):
        if Decimal(total["amount"]) >= Decimal(price["amount"]):
            item["total_item_price"] = total
    result.update(item=item, reason="ok")
    return result
