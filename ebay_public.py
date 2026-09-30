"""Public eBay UK search collector. No login, API keys or identity rotation.

Only primary newly-listed results qualify. Unknown markup, challenges and
missing dates fail closed. Public dates have minute precision and cannot
prove a fifteen-second publication-to-notification deadline.
"""

import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

import requests
from lxml import etree, html

MAX_BYTES = 4_000_000
PAGE_SIZE = 60
UK = ZoneInfo("Europe/London")
MONTHS = {
    name: i
    for i, name in enumerate(
        (
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ),
        1,
    )
}
DATE = re.compile(r"\b(\d{1,2})-([A-Z][a-z]{2})\s+(\d{2}):(\d{2})\b")


def elements(node, class_name):
    return node.xpath(
        './/*[contains(concat(" ",normalize-space(@class)," "), $name)]',
        name=" " + class_name + " ",
    )


def text(node):
    return " ".join(" ".join(node.itertext()).split())


def first(node, *classes):
    for name in classes:
        matches = elements(node, name)
        if matches:
            return matches[0]
    return None


def gbp(value):
    match = re.fullmatch(r"\+?\s*£([\d,]+(?:\.\d{1,2})?)", value.strip())
    if not match:
        return None  # Ranges, foreign currencies and unknown prices are ambiguous.
    try:
        amount = Decimal(match[1].replace(",", ""))
        return {"value": str(amount), "currency": "GBP"}
    except InvalidOperation:
        return None


def listing_date(label, now):
    match = DATE.fullmatch(label)
    if not match or match[2] not in MONTHS:
        return None
    reference = datetime.fromtimestamp(now, UK)
    candidates = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            candidates.append(
                datetime(
                    year,
                    MONTHS[match[2]],
                    int(match[1]),
                    int(match[3]),
                    int(match[4]),
                    tzinfo=UK,
                )
            )
        except ValueError:
            continue
    return (
        min(candidates, key=lambda value: abs(value.timestamp() - now)).isoformat()
        if candidates
        else None
    )


def search_url(config):
    params = {"_nkw": config["keywords"], "_sop": "10", "_ipg": str(PAGE_SIZE)}
    if config["category"]:
        params["_sacat"] = config["category"]
    if config["buying"] == "fixed":
        params["LH_BIN"] = "1"
    elif config["buying"] == "auction":
        params["LH_Auction"] = "1"
    if config["uk_only"]:
        params["LH_PrefLoc"] = "1"
    if config["condition"] == "new":
        params["LH_ItemCondition"] = "1000|1500|1750"
    elif config["condition"] == "used":
        params["LH_ItemCondition"] = "3000"
    # Never send price limits: a price reduction must not create a new arrival.
    return "https://www.ebay.co.uk/sch/i.html?" + urlencode(params)


def parse_page(body, config, now=None):
    from ebay_monitor import EbayError

    now = time.time() if now is None else now
    if not body or len(body) > MAX_BYTES:
        raise EbayError("eBay public search was incomplete; no listings processed.", 30)
    lower = body.lower()
    if any(
        marker in lower
        for marker in (
            b"verify you are human",
            b"verifying your browser",
            b"pardon our interruption",
            b"complete the captcha",
            b"checking your browser",
        )
    ):
        raise EbayError(
            "eBay public search requires verification. Monitoring paused.",
            3600,
            True,
            halt=True,
        )
    try:
        doc = html.fromstring(body, parser=html.HTMLParser(encoding="utf-8"))
    except (etree.ParserError, ValueError):
        raise EbayError("eBay public search markup was unreadable.", 30) from None
    sort = first(doc, "srp-sort")
    if sort is None or not re.match(r"Sort:\s*Newly listed", text(sort)):
        raise EbayError(
            "eBay did not confirm Newly listed sorting; no listings processed.", 30
        )
    main = first(doc, "srp-results")
    page_text = text(doc)
    heading = first(doc, "srp-controls__count-heading")
    if heading is not None and re.match(r"0 results\b", text(heading)):
        return [], ""
    if main is None:
        if re.search(r"\b0 results\b|No exact matches found", page_text):
            return [], ""
        raise EbayError("eBay results layout changed; no listings processed.", 30)
    items, invalid = [], 0
    for card in main:
        if not isinstance(card.tag, str):
            continue
        card_text = text(card)
        if re.search(
            r"Results matching fewer words|Results matching fewer|No exact matches found|Fewer words",
            card_text,
            re.IGNORECASE,
        ):
            break  # Do not turn eBay's relaxed recommendations into matches.
        item_id = card.get("data-listingid", "")
        if not re.fullmatch(r"\d{9,15}", item_id):
            continue
        title_node = first(card, "s-card__title", "s-item__title")
        link = first(card, "s-card__link", "s-item__link")
        price_node = first(card, "s-card__price", "s-item__price")
        dates = DATE.findall(card_text)
        if title_node is None or link is None or price_node is None or not dates:
            invalid += 1
            continue
        title = re.sub(r"^New listing\s*", "", text(title_node), flags=re.IGNORECASE)
        url = urlparse(link.get("href", ""))
        url_id = re.search(r"/itm/(?:[^/]+/)?(\d+)(?:/|$)", url.path)
        price = gbp(text(price_node))
        if (
            title == "Shop on eBay"
            or not title
            or not price
            or not url_id
            or url_id[1] != item_id
            or url.hostname not in ("www.ebay.co.uk", "ebay.co.uk")
        ):
            invalid += 1
            continue
        day, month, hour, minute = dates[-1]
        label = f"{day}-{month} {hour}:{minute}"
        created = listing_date(label, now)
        if created is None:
            invalid += 1
            continue
        if config["uk_only"] and re.search(
            r"\bfrom (?!United Kingdom\b|UK\b)[A-Z]", card_text
        ):
            continue
        auction = (
            bool(re.search(r"\b\d+ bids?\b", card_text))
            or config["buying"] == "auction"
        )
        fixed = not auction or bool(re.search(r"Buy [Ii]t [Nn]ow", card_text))
        shipping = None
        if re.search(
            r"\bFree (?:delivery|postage|shipping)\b", card_text, re.IGNORECASE
        ):
            shipping = {"value": "0", "currency": "GBP"}
        else:
            postage = re.search(
                r"(\+?£[\d,]+(?:\.\d{1,2})?)\s+(?:delivery|postage|shipping)",
                card_text,
                re.IGNORECASE,
            )
            if postage:
                shipping = gbp(postage[1])
        condition = (
            "Pre-owned"
            if re.search(r"\bPre-owned\b|\bUsed\b", card_text, re.IGNORECASE)
            else (
                "New"
                if re.search(
                    r"\bBrand New\b|\bNew with\b|\bNew without\b|\bNew \(other\)",
                    card_text,
                    re.IGNORECASE,
                )
                else "Check listing"
            )
        )
        if config["condition"] == "new" and condition != "New":
            continue
        if config["condition"] == "used" and condition != "Pre-owned":
            continue
        image = first(card, "s-card__image", "s-item__image-img")
        options = (["FIXED_PRICE"] if fixed else []) + (["AUCTION"] if auction else [])
        items.append(
            {
                "legacyItemId": item_id,
                "itemId": f"v1|{item_id}|0",
                "title": title,
                "itemWebUrl": f"https://www.ebay.co.uk/itm/{item_id}",
                "itemCreationDate": created,
                "_dateSource": "publicSearchMinute",
                "_listedLabel": label,
                "buyingOptions": options,
                "price": price,
                "currentBidPrice": price if auction else None,
                "shippingOptions": [{"shippingCost": shipping}] if shipping else [],
                "condition": condition,
                "image": (
                    {"imageUrl": image.get("src") or image.get("data-defer-load")}
                    if image is not None
                    else {}
                ),
            }
        )
    cards = [c for c in main if isinstance(c.tag, str) and c.get("data-listingid")]
    if cards and not items and invalid:
        raise EbayError("eBay result fields changed; no listings processed.", 30)
    warning = (
        "Broad search: newest 60 public results only; narrow keywords if checks miss pages."
        if len(cards) >= PAGE_SIZE
        else ""
    )
    if invalid:
        warning = (
            f"Skipped {invalid} results with incomplete listing details. " + warning
        )
    return items, warning


class PublicClient:
    def __init__(self, config=None, session=None):
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "MSJ-Marketplace-Monitor/0.1 (public listing search)",
                "Accept-Language": "en-GB",
            }
        )

    def search(self, config):
        from ebay_monitor import EbayError, retry_delay

        started = time.monotonic()
        try:
            with self.session.get(
                search_url(config), timeout=(8, 5), stream=True
            ) as response:
                if response.status_code == 429:
                    raise EbayError(
                        "eBay public search rate limit; waiting before retrying.",
                        max(30, retry_delay(response.headers.get("Retry-After"))),
                        True,
                    )
                if response.status_code in (401, 403):
                    raise EbayError(
                        "eBay public search access denied. Monitoring paused.",
                        3600,
                        True,
                        halt=True,
                    )
                if response.status_code != 200:
                    raise EbayError(
                        f"eBay public search returned HTTP {response.status_code}.", 30
                    )
                if (
                    urlparse(response.url).hostname
                    not in ("www.ebay.co.uk", "ebay.co.uk")
                    or "/sch/" not in urlparse(response.url).path
                ):
                    raise EbayError(
                        "eBay redirected away from public search. Monitoring paused.",
                        3600,
                        True,
                        halt=True,
                    )
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > MAX_BYTES or time.monotonic() - started > 10:
                        raise EbayError(
                            "eBay public search exceeded its response budget; retrying.",
                            15,
                        )
                return parse_page(bytes(body), config)
        except requests.RequestException:
            raise EbayError(
                "eBay public search connection failed; will retry.", 15
            ) from None
