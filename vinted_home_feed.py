"""Bounded, read-only Home cards and target listing freshness.

Selectors are from the UK Home and listing DOM inspected on 20 November 2026.
Home card IDs are the only candidates; arbitrary recommendations in page scripts
are never added. Upload age is the target sidebar's native upload_date field,
not first-seen time, item ordering, photo timestamps or seller activity.
"""

import math
import re
from collections import deque
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlsplit

from lxml import html as lhtml

from alert_images import safe_listing_photo
from listing_text import clean_description
from vinted_page_data import (
    MAX_DEPTH,
    MAX_HTML,
    _page_records,
    _parse_transaction_item,
    _resolve,
    parse_page_data,
)

MAX_NODES = 20000
MAX_ITEMS = 96
MAX_AGE_SECONDS = 180


def _text(node):
    return " ".join(node.text_content().split())


def _identity(value):
    return (
        str(value)
        if type(value) in (str, int) and re.fullmatch(r"[0-9]{1,20}", str(value))
        else None
    )


def item_url(value, identity=None):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.netloc and (
            parsed.scheme != "https"
            or parsed.hostname not in {"www.vinted.co.uk", "vinted.co.uk"}
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
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
        tree = lhtml.fromstring(
            body, parser=lhtml.HTMLParser(no_network=True, huge_tree=False)
        )
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
    match = re.fullmatch(
        r"(a|an|[0-9]{1,6}) (sec(?:ond)?s?|min(?:ute)?s?|hours?|days?|weeks?|months?|years?) ago",
        text,
    )
    if not match:
        return None
    count = 1 if match[1] in {"a", "an"} else int(match[1])
    unit = match[2]
    multiplier = (
        1
        if unit.startswith("sec")
        else (
            60
            if unit.startswith("min")
            else (
                3600
                if unit.startswith("hour")
                else (
                    86400
                    if unit.startswith("day")
                    else (
                        604800
                        if unit.startswith("week")
                        else 2678400 if unit.startswith("month") else 31622400
                    )
                )
            )
        )
    )
    return count * multiplier, (count + 1) * multiplier


def _home_uploads(body, cards):
    """Use only same-item listing creation fields already supplied with visible cards."""
    roots, refs, _ = _page_records(body)
    listed = {card["id"]: card for card in cards}
    stamps, category_ids, available, unavailable = {}, {}, set(), set()
    missing_titles = {}
    queue, visited = deque(roots), 0
    while queue:
        record = queue.popleft()
        visited += 1
        if visited + len(queue) > MAX_NODES:
            return  # Incomplete pages cannot establish consistent timestamps.
        if isinstance(record, list):
            queue.extend(child for child in record if isinstance(child, (dict, list)))
            continue
        if not isinstance(record, dict):
            continue
        queue.extend(child for child in record.values() if isinstance(child, (dict, list)))
        identity = _identity(record.get("id"))
        card = listed.get(identity)
        if not card or not (_identity(record.get("seller_id")) or _identity(record.get("user_id"))):
            continue
        if record.get("item_id") is not None and _identity(record.get("item_id")) != identity:
            continue
        title, price = _resolve(record.get("title"), refs), _resolve(record.get("price"), refs)
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            continue
        if card["title"] and title != card["title"]:
            continue
        if not isinstance(price, dict) or price.get("currency_code") != "GBP":
            continue
        if _money("£" + str(price.get("amount", ""))) != card["price"]:
            continue
        if not card["title"]:
            missing_titles.setdefault(identity, set()).add(title.strip())
        category = _identity(_resolve(record.get("catalog_id"), refs))
        if category:
            category_ids.setdefault(identity,set()).add(category)
        for key in ("created_at_ts", "created_at"):
            stamp = _upload_time(_resolve(record.get(key), refs))
            if stamp is not None:
                stamps.setdefault(identity, set()).add(stamp)
        can_buy = _resolve(record.get("can_buy"), refs)
        if can_buy is True:
            available.add(identity)
        if can_buy is False or any(_resolve(record.get(k), refs) is True
                                   for k in ("is_reserved", "is_hidden", "is_sold", "is_closed")):
            unavailable.add(identity)
    # A titleless card is still a visible listing. Only an unambiguous native
    # title with the same item ID and displayed price can supply its fast path.
    # Otherwise leave its age and category for the exact listing detail read.
    for identity, titles in missing_titles.items():
        if len(titles) == 1:
            listed[identity]["title"] = next(iter(titles))
    for identity, values in category_ids.items():
        if len(values) == 1 and listed[identity]["title"]:
            listed[identity]["category_ids"] = list(values)
    for identity in unavailable:
        listed[identity]["home_available"] = False
    for identity, values in stamps.items():
        if max(values) - min(values) <= 1 and listed[identity]["title"]:
            card = listed[identity]
            card["home_created_at"] = min(values)
            if identity in available and identity not in unavailable:
                card["home_available"] = True


# Home's accessible listing caption carries separate item and fee-inclusive prices.
# This representation also exists when the surrounding visual card test IDs change.
_CAPTION_AMOUNT = r"(?:£\s*[0-9][0-9,.]*|[0-9][0-9,.]*\s*£)"
_HOME_CAPTION = re.compile(
    r"(?P<title>.{1,500}?), (?:brand: (?P<brand>[^,]{1,120}), )?"
    r"condition: (?P<condition>[^,]{1,80}), (?:size: (?P<size>[^,]{1,80}), )?"
    rf"(?P<item>{_CAPTION_AMOUNT}), (?P<total>{_CAPTION_AMOUNT}) Includes Vinted fee",
    re.IGNORECASE,
)


def _caption_price(text):
    return _money("£" + text.strip("£ ").strip())


_NON_FEED_TAGS = {"header", "nav", "footer", "aside", "template", "noscript"}
_NON_FEED_ROLES = {"banner", "navigation", "contentinfo", "complementary"}
_HIDDEN_STYLE = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!important\s*)?(?:;|$)", re.I)
_REACT_COMPLETE = re.compile(r"""\$(RC|RS)\(\s*(["'])([BPS]:[A-Za-z0-9_:-]{1,80})\2\s*,\s*(["'])([BPS]:[A-Za-z0-9_:-]{1,80})\4\s*\)""")
_JS_FENCES = re.compile(r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|//[^\n\r]*|/\*[\s\S]*?\*/|[()\[\]{};])""")
_FRAME_CALLBACK = re.compile(r"""requestAnimationFrame\(\s*(?:function\s*\(\s*\)|\(\s*\)\s*=>)\s*\{([\s\S]*)\}\s*\)""")
_FRAME_ARROW = re.compile(r"""requestAnimationFrame\(\s*\(\s*\)\s*=>\s*([\s\S]*)\)""")


def _visible_body(node, streamed=(), card=None):
    # An image click overlay can be aria-hidden while its containing card is
    # displayed. Respect aria-hidden on the card and every ancestor above it.
    card_reached = card is None
    while node is not None:
        if node is card:
            card_reached = True
        if (node.tag in _NON_FEED_TAGS or node.get("role") in _NON_FEED_ROLES
                or "inert" in node.attrib or _HIDDEN_STYLE.search(node.get("style", ""))
                or (card_reached and node.get("aria-hidden", "").lower() == "true")
                or ("hidden" in node.attrib and node not in streamed)):
            return False
        if node.tag == "body":
            return True
        node = node.getparent()
    return False


def _react_moves(program, level=0):
    # Inspect only complete executable statements of a small inline script.
    # Quotes, comments and bodies of unrelated callbacks cannot emit a move.
    if len(program) > 65536 or level > 2:
        return
    start, depth = 0, 0
    parts = []
    for token in _JS_FENCES.finditer(program):
        symbol = token.group()
        if symbol in ("(", "[", "{"):
            depth += 1
        elif symbol in (")", "]", "}"):
            depth -= 1
            if depth < 0:
                return
        elif symbol == ";" and not depth:
            parts.append(program[start:token.start()].strip())
            start = token.end()
    if depth:
        return
    parts.append(program[start:].strip())
    for part in parts:
        if not part:
            continue
        frame = _FRAME_CALLBACK.fullmatch(part) or _FRAME_ARROW.fullmatch(part)
        if frame:
            yield from _react_moves(frame[1], level + 1)
            continue
        # React may sequence several immediate completion calls with a comma.
        pos = 0
        moves = []
        while match := _REACT_COMPLETE.match(part, pos):
            command, first, second = match[1], match[3], match[5]
            if command == "RC" and first.startswith("B:") and second.startswith("S:"):
                moves.append((first, second))
            elif command == "RS" and first.startswith("S:") and second.startswith("P:"):
                moves.append((second, first))
            pos = match.end()
            remainder = part[pos:]
            if not remainder.strip():
                yield from moves
                break
            comma = re.match(r"\s*,\s*", remainder)
            if not comma:
                break
            pos += comma.end()


def _concrete_stream_moves(program):
    """Read literal React move statements without depending on its bootstrap syntax."""
    if len(program) > 131072:
        return
    fences = iter(_JS_FENCES.finditer(program))
    fence = next(fences, None)
    for match in _REACT_COMPLETE.finditer(program):
        while fence is not None and fence.end() <= match.start():
            fence = next(fences, None)
        if fence is not None and fence.start() <= match.start():
            continue  # A string, template literal or comment contains this spelling.
        prefix = program[max(0, match.start() - 128):match.start()].rstrip()
        if prefix and prefix[-1] not in ";},":
            continue
        cmd, first, second = match[1], match[3], match[5]
        if cmd == "RS" and first.startswith("S:") and second.startswith("P:"):
            yield second, first
        elif cmd == "RC" and first.startswith("B:") and second.startswith("S:"):
            yield first, second


def _verified_slot_segments(tree):
    """Target-detail candidates from a concrete S-to-P move in the signed-in Home."""
    scripts = tree.xpath('//script[not(@src) and (contains(text(),"$RS(") or contains(text(),"$RC("))]')
    if not scripts:
        return set()
    nodes = {}
    for node in tree.xpath('//*[starts-with(@id,"B:") or starts-with(@id,"S:") or starts-with(@id,"P:")]'):
        identity = node.get("id")
        nodes[identity] = None if identity in nodes else node
    targets = {}
    slots = set()
    for script in scripts:
        if script.get("type", "").strip().lower() not in ("", "text/javascript", "application/javascript"):
            continue
        for dest, source in _concrete_stream_moves(script.text or ""):
            placeholder, segment = nodes.get(dest), nodes.get(source)
            if (placeholder is None or segment is None or placeholder.tag != "template"
                    or segment.tag != "div" or "hidden" not in segment.attrib):
                continue
            if segment in targets and targets[segment] is not placeholder:
                targets[segment] = None
                continue
            targets[segment] = placeholder
            if dest.startswith("P:"):
                slots.add(segment)
    # A still-pending parent can contain a genuine completed child slot. Its
    # matching React boundary ties that parent to the page even before reveal.
    for identity, segment in nodes.items():
        if not identity.startswith("S:") or segment is None or segment in targets:
            continue
        boundary = nodes.get("B:" + identity[2:])
        if boundary is None or boundary.tag != "template" or segment.tag != "div" or "hidden" not in segment.attrib:
            continue
        marker = boundary.getprevious()
        if marker is not None and marker.tag is lhtml.etree.Comment and marker.text in ("$?", "$~"):
            targets[segment] = boundary
    placed = set()
    for _ in range(MAX_DEPTH):
        before = len(placed)
        for segment, target in targets.items():
            if target is not None and segment not in placed and _visible_body(target.getparent(), placed):
                placed.add(segment)
        if len(placed) == before:
            break
    return slots & placed


def _streamed_home_segments(tree):
    # A React completion places S: contents at a B: boundary or a P: slot.
    # Either destination can itself be waiting inside another streamed segment.
    scripts = tree.xpath('//script[not(@src) and (contains(text(),"$RC(") or contains(text(),"$RS("))]')
    if not scripts:
        return {}
    nodes = {}
    for node in tree.xpath('//*[starts-with(@id,"B:") or starts-with(@id,"S:") or starts-with(@id,"P:")]'):
        key = node.get("id")
        nodes[key] = None if key in nodes else node
    targets = {}
    for script in scripts:
        if script.get("type", "").strip().lower() not in ("", "text/javascript", "application/javascript"):
            continue
        for dest, source in _react_moves(script.text or ""):
            boundary, segment = nodes.get(dest), nodes.get(source)
            if (boundary is None or segment is None or boundary.tag != "template"
                    or segment.tag != "div" or "hidden" not in segment.attrib):
                continue
            if dest.startswith("B:"):
                marker = boundary.getprevious()
                if marker is None or marker.tag is not lhtml.etree.Comment or marker.text != "$?":
                    continue
            if segment not in targets or targets[segment] is boundary:
                targets[segment] = boundary
            else:
                targets[segment] = None
    revealed = {}
    for _ in range(MAX_DEPTH):
        before = len(revealed)
        for segment, boundary in targets.items():
            if (segment not in revealed and boundary is not None
                    and _visible_body(boundary.getparent(), revealed)):
                revealed[segment] = boundary
        if len(revealed) == before:
            break
    return revealed


def _attributed_native_link(link, streamed, all_panels):
    query = parse_qs(urlsplit(link.get("href")).query, keep_blank_values=True)
    if "homepage_session_id" not in query:
        return False
    if any(query["homepage_session_id"]):
        return True
    # A blank attribution is sufficient within the main Home listing surface.
    # For streamed cards evaluate the position after their placeholder resolves.
    node = link.getparent()
    while node is not None:
        if node in streamed:
            node = streamed[node].getparent()
        elif node.tag == "main" or node.get("role") == "main" or node in all_panels:
            return True
        else:
            node = node.getparent()
    return False


def _selected_all_panels(tree):
    panels = {}
    for panel in tree.xpath('//*[@role="tabpanel" and @id]'):
        identity = panel.get("id")
        panels[identity] = None if identity in panels else panel
    selected = set()
    for tab in tree.xpath('//*[@role="tab" and @aria-selected="true" and @aria-controls]'):
        label = (tab.get("aria-label") or _text(tab)).strip().casefold()
        panel = panels.get(tab.get("aria-controls"))
        if panel is not None and label in {"all", "all items"} and _visible_body(tab):
            selected.add(panel)
    return selected


def _visible_home_links(tree, seen):
    links = tree.xpath(
        '//*[self::main or @role="main"]//a[contains(@href,"homepage_session_id=")]'
        '[not(ancestor-or-self::*[@hidden or @inert or @aria-hidden="true" or self::template or self::noscript])]'
    )
    for link in links[:MAX_ITEMS * 3]:
        if not _visible_body(link):
            continue
        href = link.get("href")
        url = item_url(href)
        if not url or not parse_qs(urlsplit(href).query).get("homepage_session_id"):
            continue
        identity = re.search(r"/items/([0-9]+)", url)[1]
        if identity in seen:
            continue
        caption = link.get("title") or link.get("aria-label") or ""
        if len(caption) > 1024:
            continue
        match = _HOME_CAPTION.fullmatch(" ".join(caption.split()))
        if not match:
            continue
        price, total = _caption_price(match["item"]), _caption_price(match["total"])
        if not price or not total or Decimal(total["amount"]) < Decimal(price["amount"]):
            continue
        # A renamed link or missing price test ID is fine. An invalid or
        # conflicting price still displayed by the known card must fail closed.
        prices = link.xpath('ancestor::*[@data-testid="feed-item"][1]//*[@data-testid="feed-item--price-text"]')
        if prices and (len(prices) != 1 or _money(_text(prices[0])) != price):
            continue
        yield {
            "id": identity, "url": url, "title": match["title"].strip(),
            "brand_title": match["brand"] or "", "size_title": match["size"] or "",
            "condition": match["condition"], "price": price, "total_item_price": total,
            "photo": {}, "description_complete": False,
        }


def parse_home(body):
    """Return only visible Home feed cards with a valid UK item URL and GBP price."""
    tree = _tree(body)
    if tree is None:
        return {"items": [], "reason": "invalid_document"}
    navigation = tree.xpath('//header//a/@href | //*[@role="banner"]//a/@href')
    if any(
        urlsplit(href).path.startswith(("/member/signup", "/member/login"))
        for href in navigation
    ):
        return {"items": [], "reason": "home_not_authenticated", "authenticated": False}
    if not any(
        href.startswith("/inbox")
        and not href.startswith("//")
        and urlsplit(href).path.rstrip("/") == "/inbox"
        for href in navigation
    ):
        return {"items": [], "reason": "home_auth_unverified", "authenticated": False}
    cards = tree.xpath('//*[@data-testid="feed-item"]')
    if len(cards) > MAX_ITEMS:
        cards = cards[:MAX_ITEMS]
    items, seen = [], set()
    streamed = _streamed_home_segments(tree) if cards else {}
    all_panels = _selected_all_panels(tree) if cards else set()
    slot_segments, detail_only = None, set()
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
        if not _visible_body(links[0], streamed, card) or not _visible_body(prices[0], streamed, card):
            if not any(parse_qs(urlsplit(links[0].get("href")).query).get("homepage_session_id", ())):
                continue
            if slot_segments is None:
                slot_segments = _verified_slot_segments(tree)
            # Only a specific native S-to-P slot can provide a candidate. The
            # card, price and click target themselves must remain unobstructed.
            if (not any(node in slot_segments for node in card.iterancestors())
                    or not _visible_body(links[0], slot_segments, card)
                    or not _visible_body(prices[0], slot_segments, card)):
                continue
            detail_only.add(identity)
        caption = links[0].get("title") or links[0].get("aria-label") or ""
        title = re.split(r", (?:brand|condition): ", caption, maxsplit=1)[0].strip()
        if not title or len(title) > 500:
            if not _attributed_native_link(links[0], streamed, all_panels):
                continue
            title = ""
            # The card's description title is the brand. A dedicated item-title
            # node is safe. Generic image alt text cannot establish a listing title.
            names = card.xpath('.//*[@data-testid="feed-item--title"]')
            if len(names) == 1:
                title = _text(names[0])
        if len(title) > 500:
            title = ""
        images = card.xpath('.//img[@data-testid="feed-item--image--img"]/@src')
        brand = card.xpath('.//*[@data-testid="feed-item--description-title"]')
        subtitle = card.xpath('.//*[@data-testid="feed-item--description-subtitle"]')
        details = _text(subtitle[0]).split(" · ") if len(subtitle) == 1 else []
        combined = card.xpath('.//*[@data-testid="total-combined-price"]')
        item = {
            "id": identity,
            "url": url,
            "title": title,
            "price": price,
            "brand_title": _text(brand[0])[:120] if len(brand) == 1 else "",
            "size_title": details[0][:80] if len(details) > 1 else "",
            "condition": details[-1][:80] if details else "",
            "photo": (
                {"url": images[0]}
                if len(images) == 1 and safe_listing_photo(images[0])
                else {}
            ),
            "description_complete": False,
        }
        if len(combined) == 1 and (total := _money(_text(combined[0]))):
            item["total_item_price"] = total
        seen.add(identity)
        items.append(item)
    if len(items) < MAX_ITEMS:
        for candidate in _visible_home_links(tree, seen):
            seen.add(candidate["id"])
            items.append(candidate)
            if len(items) == MAX_ITEMS:
                break
    if items:
        visible = [item for item in items if item["id"] not in detail_only]
        if visible:
            _home_uploads(body, visible)
        return {"items": items, "reason": "ok", "authenticated": True}
    from vinted_home_diagnostics import empty_page_shape

    return {"items": [], "reason": "home_cards_unavailable", "authenticated": True,
            "shape": empty_page_shape(tree)}


def _sidebar(tree, identity):
    canonical = tree.xpath('//link[@rel="canonical"]/@href')
    if len(canonical) != 1 or not item_url(canonical[0], identity):
        return None
    summaries = tree.xpath('//*[@data-testid="item-page-summary-plugin"]')
    if len(summaries) != 1:
        return None
    side = summaries[0].xpath(
        'ancestor::div[contains(@class,"item-page-sidebar-content")][1]'
    )
    return side[0] if len(side) == 1 else None


def _upload_time(value):
    if type(value) is int or (isinstance(value, str) and re.fullmatch(r"\d{1,13}", value)):
        stamp = int(value)
        return stamp / 1000 if 10**12 <= stamp < 10**13 else stamp
    if isinstance(value, str) and len(value) <= 50 and "T" in value:
        try:
            date = datetime.fromisoformat(value)
            return date.timestamp() if date.tzinfo is not None else None
        except (ValueError, OSError, OverflowError):
            return None
    return None


def listing_age(body, item_id, now, *, side=None, attrs=None, home_card=None):
    """Age at request start; caller must add elapsed/cache age before alerting."""
    result = {
        "verified": False,
        "max_age_seconds": None,
        "checked_at": now,
        "evidence": "upload_age_unverified",
    }
    identity = _identity(item_id)
    if (
        not identity
        or type(now) not in (int, float)
        or not math.isfinite(now)
        or now <= 0
    ):
        return result
    if side is None:
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
    result.update(
        verified=True,
        min_age_seconds=bounds[0],
        max_age_seconds=bounds[1],
        evidence="target_upload_date",
        fresh=bounds[1] <= MAX_AGE_SECONDS,
    )
    if bounds[0] < MAX_AGE_SECONDS:
        stamps = []
        for v in ages[0].xpath('./@datetime | ./@content | .//time/@datetime'):
            if (stamp := _upload_time(v)) is not None:
                stamps.append(stamp)
        if attrs is None and bounds[1] == MAX_AGE_SECONDS:
            attrs = _native_attributes(body, identity)
        if attrs and attrs.get("created_stamp") is not None:
            stamps.append(attrs["created_stamp"])
        # Home can expose the creation time where the same listing only shows
        # rounded minutes. Require agreement with the target's displayed age
        # and any exact listing evidence before narrowing the upper bound.
        if isinstance(home_card, dict) and _identity(home_card.get("id")) == identity:
            stamp = home_card.get("home_created_at")
            if type(stamp) in (int, float) and 0 < stamp <= now:
                stamps.append(stamp)
        if stamps and not (attrs or {}).get("conflicting_creation_time") and max(stamps) - min(stamps) <= 1:
            elapsed = now - min(stamps)
            if bounds[0] <= elapsed < bounds[1]:
                result.update(max_age_seconds=math.ceil(elapsed), min_age_seconds=math.floor(elapsed),
                              evidence="target_upload_timestamp")
    return result


def _native_attributes(body, identity, page_records=None):
    """Optional exact attributes from complete same-item records, never neighbors."""
    roots, rows, _ = _page_records(body) if page_records is None else page_records
    pending = deque((root, 0) for root in roots)
    fields, conflicts, visited = {}, set(), 0
    creation_times = set()
    keys = (
        "brand_id",
        "catalog_id",
        "size_id",
        "status_id",
        "color1_id",
        "color2_id",
        "material_id",
    )
    while pending and visited < MAX_NODES:
        value, depth = pending.popleft()
        visited += 1
        if depth > MAX_DEPTH:
            return {}
        if isinstance(value, dict):
            same_item = _identity(value.get("id")) == identity
            if same_item and any(
                (key in value and _resolve(value[key], rows) is blocked)
                for key, blocked in (("can_buy", False), ("is_sold", True), ("is_reserved", True),
                                     ("is_hidden", True), ("is_closed", True))
            ):
                fields["unavailable"] = True
            if same_item and "seller_id" in value:
                if "item_id" in value and _identity(value["item_id"]) != identity:
                    return {}
                if "catalog_id" in value or "price" in value:
                    for key in ("created_at_ts", "created_at"):
                        stamp = _upload_time(_resolve(value.get(key), rows))
                        if stamp is not None:
                            creation_times.add(stamp)
                user = _resolve(value.get("user"), rows)
                country = value.get("seller_country_code")
                if isinstance(user, dict) and _identity(user.get("id")) == _identity(value.get("seller_id")):
                    country = country or user.get("country_iso_code") or user.get("country_code")
                if isinstance(country, str) and re.fullmatch(r"[A-Za-z]{2}", country) and "seller_country" not in conflicts:
                    country = country.upper()
                    if fields.get("seller_country", country) == country:
                        fields["seller_country"] = country
                    else:
                        fields.pop("seller_country", None)
                        conflicts.add("seller_country")
                for key in keys:
                    if key not in value or key in conflicts:
                        continue
                    raw = _resolve(value[key], rows)
                    if raw is None:
                        continue  # Optional colours and materials are commonly null.
                    found = _identity(raw)
                    if found is None or (key in fields and fields[key] != found):
                        conflicts.add(key)
                        fields.pop(key, None)
                    else:
                        fields[key] = found
            children = [x for x in value.values() if isinstance(x, (dict, list))]
        elif isinstance(value, list):
            children = [x for x in value if isinstance(x, (dict, list))]
        else:
            children = []
        if len(children) + len(pending) + visited > MAX_NODES:
            return {}
        pending.extend((child, depth + 1) for child in children)
    if creation_times:
        if max(creation_times) - min(creation_times) <= 1:
            fields["created_stamp"] = min(creation_times)
        else:
            fields["conflicting_creation_time"] = True
    return {**fields, "conflicting_fields": sorted(conflicts)} if not pending else {}


def _catalog_evidence(tree):
    """Read same-site links in the target breadcrumb, including absolute URLs."""
    containers = tree.xpath(
        '//main//*[self::ul or self::ol][.//a[contains(@href,"referrer=item-crumbs")]]'
        ' | //main//nav[@aria-label="Breadcrumb" or @aria-label="Breadcrumbs"]'
        ' | //main//*[@data-testid="breadcrumbs" or @data-testid="item-breadcrumbs"]'
    )
    ids, labels = set(), set()
    for container in containers:
        for a in container.xpath(".//a[@href]"):
            try:
                url = urlsplit(a.get("href"))
                if url.netloc and (url.scheme != "https" or url.netloc not in {"vinted.co.uk", "www.vinted.co.uk"}):
                    continue
                if not url.netloc and url.scheme:
                    continue
                match = re.fullmatch(r"/catalog/(\d{1,20})(?:-[\w-]+)?/?", url.path)
                category = {match[1]} if match else set()
                if url.path.rstrip("/") == "/catalog":
                    category.update(v for v in parse_qs(url.query).get("catalog[]", []) if _identity(v))
                if category:
                    ids.update(category)
                    labels.add(_text(a).casefold())
            except (TypeError, ValueError):
                continue
    return ids, labels


def _visible_stock(side):
    """Return True, False or None for target-scoped public availability."""
    if side.xpath('.//*[@data-testid="item-status--sold" or @data-testid="item-status--reserved" or @data-testid="item-status--unavailable"]'):
        return False
    values = {str(v).strip().rstrip("/").rsplit("/", 1)[-1].casefold()
              for node in side.xpath('.//*[@itemprop="availability"]')
              for v in [node.get("href") or node.get("content") or _text(node)] if v}
    if values:
        return values == {"instock"}
    controls = side.xpath('.//button[not(@disabled) and not(@aria-disabled="true") and not(@hidden)]')
    return True if any(_text(button).casefold() == "buy now" for button in controls) else None


def parse_listing(body, item_id, now, *, home_card=None):
    """Return fresh target details; no credentials, writes, or purchase operations."""
    identity = str(item_id)
    tree = _tree(body)
    side = _sidebar(tree, identity) if tree is not None else None
    records = _page_records(body) if side is not None else None
    attrs = _native_attributes(body, identity, records) if side is not None else {}
    age = listing_age(body, item_id, now, side=side, attrs=attrs, home_card=home_card)
    result = {"age": age, "item": None, "reason": "listing_unverified"}
    if not age["verified"]:
        return result
    diagnostic = {}
    native = _parse_transaction_item(body, identity, diagnostics=diagnostic, require_price=True, page_records=records)
    public_stock = _visible_stock(side)
    if attrs.get("unavailable") or public_stock is False:
        result["reason"] = "unavailable"
        return result
    if native:
        if native.get("can_buy") is not True or any(native.get(k) is True for k in ("is_reserved", "is_hidden", "is_sold", "is_closed")):
            result["reason"] = "unavailable"
            return result
    elif diagnostic.get("reason") not in {"no_target_record", "incomplete_record"} or public_stock is not True:
        result["reason"] = "availability_unverified"
        return result
    titles = side.xpath('.//*[@data-testid="item-page-summary-plugin"]//h1')
    prices = side.xpath('.//*[@data-testid="item-price"]')
    title = _text(titles[0]) if len(titles) == 1 else ""
    if not title or len(title) > 500:
        result["reason"] = "title_unverified"
        return result
    if len(prices) != 1:
        return result
    price = _money(_text(prices[0]))
    native_price = (native or {}).get("price")
    if not price:
        return result
    if native_price:
        try:
            if native_price.get("currency_code") != "GBP" or Decimal(str(native_price.get("amount"))) != Decimal(price["amount"]):
                return result
        except InvalidOperation:
            return result
    details = parse_page_data(body, identity, full_description=True, page_records=records)
    descriptions = side.xpath('.//*[@itemprop="description"]')
    full_text = (
        _text(descriptions[0])
        if len(descriptions) == 1
        else details.get("description", "")
    )
    complete = (
        (bool(full_text) or len(descriptions) == 1)
        and len(full_text) <= 6000
        and "[Description shortened" not in full_text
    )
    description = clean_description(full_text)
    brand_links = side.xpath('.//a[starts-with(@href,"/brand/")]')
    brands = {
        m[1]
        for a in brand_links
        if (m := re.match(r"^/brand/([0-9]{1,20})(?:-|$)", a.get("href", "")))
    }
    sizes = side.xpath('.//*[@itemprop="size"]')
    conditions = side.xpath('.//*[@itemprop="status" or @itemprop="condition"]')
    canonical = tree.xpath('//link[@rel="canonical"]/@href')[0]
    if len(brands) == 1:
        if "brand_id" in attrs.get("conflicting_fields", ()):
            return result
        brand_id = next(iter(brands))
        if attrs.get("brand_id") and attrs["brand_id"] != brand_id:
            return result
        attrs["brand_id"] = brand_id
    item = {
        "id": identity,
        "url": item_url(canonical, identity),
        "title": title,
        "price": price,
        "seller_id": (native or {}).get("user_id"),
        "description": description,
        "description_complete": complete,
        "photos": details.get("photos", []),
        "brand_title": _text(brand_links[0])[:120] if len(brands) == 1 else "",
        "size_title": _text(sizes[0])[:80] if len(sizes) == 1 else "",
        "condition": _text(conditions[0])[:80] if len(conditions) == 1 else "",
        **attrs,
    }
    categories, labels = _catalog_evidence(tree)
    item["category_labels"] = sorted(labels)
    if attrs.get("catalog_id"):
        categories.add(attrs["catalog_id"])
    item["category_ids"] = sorted(categories)
    if attrs.get("status_id"):
        item["condition_id"] = attrs["status_id"]
    item["color_ids"] = [attrs[k] for k in ("color1_id", "color2_id") if attrs.get(k)]
    item["material_ids"] = [attrs["material_id"]] if attrs.get("material_id") else []
    combined = side.xpath('.//*[@data-testid="total-combined-price"]')
    if (
        len(combined) == 1
        and (total := _money(_text(combined[0])))
        and Decimal(total["amount"]) >= Decimal(price["amount"])
    ):
        item["total_item_price"] = total
    result.update(item=item, reason="ok")
    return result
