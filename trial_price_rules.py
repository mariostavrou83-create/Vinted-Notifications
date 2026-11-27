"""UK asking-price comparison for a quick resale. Values and margins are integer pence."""

import re
from collections import Counter
from datetime import datetime, timezone
from unicodedata import normalize
from urllib.parse import urlsplit

import requests


RECENT_SECONDS = 72 * 3600
PROFIT_TARGET = 1500
CONDITION = {
    "satisfactory": 1, "fair": 1, "acceptable": 1, "used acceptable": 1,
    "good": 2, "used good": 2,
    "very good": 3, "used very good": 3,
    "like new": 4, "new without tags": 4, "new without box": 4,
    "new": 5, "new with tags": 5, "new with box": 5, "brand new": 5, "sealed": 5,
}
MATCH_FIELDS = ("brand", "subtype", "model", "material", "size", "edition", "capacity", "format", "variant")
GENERIC = set("a an and in for to the with of by at on new good very condition nice used great size uk "
              "unisex mens womens ladies black blue grey white red green pink vintage original authentic".split())
RISK_MARKERS = {
    "damaged": re.compile(r"\b(faulty|spares|for (?:parts|repair)|broken|untested|water damage|screen cracked|cracked screen)\b"),
    "imitation": re.compile(r"\b(replica|imitation|lookalike|dupe)\b"),
    "bundle": re.compile(r"\b(bundle|multipack|lot of|set of|pack of)\b"),
    "accessory only": re.compile(r"\b((?:box|case|cover|sleeve|manual|disc|strap) only|empty box)\b"),
    "accessory for another item": re.compile(r"\b(?:case|cover|skin|sleeve|strap|charger|adapter|replacement) for\b"),
    "faux material": re.compile(r"\b(faux leather|fake leather|synthetic leather|vegan leather|leatherette|pleather|pu leather)\b"),
}


def words(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    return " ".join(re.findall(r"[^\W_]+", normalize("NFKC", str(value)).casefold(), re.UNICODE))


def timestamp(value):
    if isinstance(value, datetime):
        return value.timestamp() if value.tzinfo else None
    if type(value) in (int, float):
        return value if 0 < value < 10**11 else None
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return stamp.timestamp() if stamp.tzinfo else None
        except (ValueError, OverflowError):
            pass
    return None


def listing_url(item):
    raw = item.get("url") or item.get("listing_url")
    if not isinstance(raw, str):
        return None
    try:
        link = urlsplit(raw)
        match = re.fullmatch(r"/items/([0-9]+)(?:-[^/]*)?/?", link.path)
        if match and link.scheme == "https" and link.netloc.lower() in {"vinted.co.uk", "www.vinted.co.uk"}:
            return f"https://www.vinted.co.uk/items/{match[1]}"
    except ValueError:
        pass
    return None


def grade(item):
    return CONDITION.get(words(item.get("condition")))


# These are observed UK Vinted apparel labels. Unknown sizing and numeric-only sizes
# keep their own exact keys, including retailer-specific bands.
APPAREL_SIZES = {
    "xxs": ("extra extra small", "2"), "xs": ("extra small", "4 6"),
    "s": ("small", "8 10"), "m": ("medium", "12 14"), "l": ("large", "16 18"),
}
SIZE_SPELLINGS = {}
for _tag, (_long, _range) in APPAREL_SIZES.items():
    SIZE_SPELLINGS[_tag] = tuple(dict.fromkeys((
        _tag, _long, f"{_tag} uk {_range}", f"{_long} uk {_range}",
        f"{_tag} gb {_range}", f"{_long} gb {_range}")))
SIZE_KEYS = {spelling: tag for tag, spellings in SIZE_SPELLINGS.items() for spelling in spellings}


def size_lookup_keys(item):
    key = field(item, "size")
    return SIZE_SPELLINGS.get(key, (key,))


def field(item, key):
    value = words(item.get(key))
    if value in {"unknown", "not specified", "n a"}:
        return ""
    if key == "material":
        return {
            "real leather": "leather", "genuine leather": "leather",
            "fake leather": "faux leather", "vegan leather": "faux leather", "pu leather": "faux leather",
            "leatherette": "faux leather", "pleather": "faux leather", "synthetic leather": "faux leather",
        }.get(value, value)
    if key == "size":
        return SIZE_KEYS.get(value, re.sub(r"^uk (\d+)$", r"\1", value))
    return value


def pence(value):
    return type(value) is int and value >= 0


def markers(item):
    text = words(item.get("title")) + " " + words(item.get("material"))
    return {key for key, pattern in RISK_MARKERS.items() if pattern.search(text)}


APPAREL_ERA = re.compile(r"\b(y2k|00s|2000[’']?s|90s|1990[’']?s|80s|1980[’']?s)\b", re.I)
MARKED_SIZE = re.compile(
    r"\b(?:tagged\s+)?(?:size\s+(?:uk\s*)?|uk\s*)"
    r"(\d{1,2}(?:\s*[-–—/]\s*\d{1,2})?|xxs|xs|s|m|l|extra small|small|medium|large)\b", re.I)


def _apparel_title(item):
    raw = str(item.get("title") or "")
    structured = field(item, "size")
    aliases = SIZE_SPELLINGS.get(structured, (structured,))
    # Only an explicitly supplied numeric size band can corroborate a number
    # in the title. An unqualified alpha label alone cannot verify UK sizing.
    supplied = words(item.get("size"))
    allowed_numbers = set(re.findall(r"\d+", supplied))
    actual = set()

    def verified(match):
        label = words(match[1])
        numbers = set(re.findall(r"\d+", label))
        if numbers and numbers <= allowed_numbers:
            actual.update(numbers)
            return " "
        if not numbers and label in aliases:
            return " "
        return match[0]

    raw = MARKED_SIZE.sub(verified, raw)
    eras = {("2000" if m[0].lower() in {"y2k", "00s"} or m[0].startswith("2000")
             else "1990" if "9" in m[0] else "1980") for m in APPAREL_ERA.finditer(raw)}
    return words(APPAREL_ERA.sub(" ", raw)), eras, actual


_OUTERWEAR = re.compile(r"\b(?:coat|jacket|puffer|parka|bomber|anorak)\b")
_FUR_TRIM = re.compile(r"\bfur (?:trim(?:med)?|collar(?:ed)?|cuffs?|hood(?:ed)?)\b")
_FUR_BODY = re.compile(r"\bfur (?:coats?|jackets?|parkas?)\b")


def _fur_outerwear(title):
    if not _OUTERWEAR.search(title):
        return None
    trim, body = bool(_FUR_TRIM.search(title)), bool(_FUR_BODY.search(title))
    if trim != body:
        return "trim" if trim else "body"
    return None


def product_mismatch(target, other, *, rank_style=False):
    a, b = words(target.get("title")), words(other.get("title"))
    if not a or not b:
        return "Missing item title"
    from trial_style_evidence import family_mismatch
    wrong_family = family_mismatch(target, other)
    if wrong_family:
        return wrong_family
    for key, mismatch in (("vinted_catalog_id", "Different product category"),
                          ("vinted_brand_id", "Different brand")):
        left, right = field(target, key), field(other, key)
        if left and right and left != right:
            return mismatch
    fur_a, fur_b = _fur_outerwear(a), _fur_outerwear(b)
    if fur_a and fur_b and fur_a != fur_b:
        return "Fur trim and a fur outer shell are different constructions"
    category_a, category_b = field(target, "category"), field(other, "category")
    if category_a and category_b and category_a != category_b:
        return "Different product category"
    if markers(target) != markers(other):
        return "Different contents, material or working condition"
    for key in MATCH_FIELDS:
        left, right = field(target, key), field(other, key)
        if left == right:
            continue
        # A clearly named material, model or brand can be present in only one structured record.
        if key in ("model", "brand", "material", "subtype") and (not left or not right):
            if f" {left or right} " in f" {b if left else a} ":
                continue
        return f"Different or unconfirmed {key}"
    specs = target.get("attributes")
    other_specs = other.get("attributes")
    if isinstance(specs, dict):
        for key, value in specs.items():
            if words(value) and (not isinstance(other_specs, dict) or field(other_specs, key) != words(value)):
                return "Different or unconfirmed product specification"

    units = r"\b(\d+)\s*(gb|tb|ml|litres?|cm|mm|inch|oz|pieces?)\b"
    if set(re.findall(units, a)) != set(re.findall(units, b)):
        return "Different or unconfirmed capacity, size or quantity"
    if field(target, "model") and field(other, "model"):
        return None
    # A UK size or era tag in a garment title is not a model identifier.
    from resale_pricing import garment
    if garment(a) and garment(b) and field(target, "size") and field(other, "size"):
        a, era_a, title_sizes_a = _apparel_title(target)
        b, era_b, title_sizes_b = _apparel_title(other)
        if era_a and era_b and era_a != era_b:
            return "Different garment era"
        if title_sizes_a and title_sizes_b and title_sizes_a.isdisjoint(title_sizes_b):
            return "Different stated UK sizes"
    # Brand and colour on their own do not identify a comparable product.
    excluded = GENERIC | set((field(target, "brand") + " " + field(other, "brand")).split())
    excluded.update("navy beige cream brown silver gold ivory multicolour multicolor charcoal tan khaki".split())
    # The brand label may be abbreviated to Morgan even when sellers write its full name.
    if re.search(r"\bmorgan de toi\b", a + " " + b):
        excluded.update(("morgan", "de", "toi"))
    left, right = set(a.split()) - excluded, set(b.split()) - excluded
    numbers_a = {s for s in left if any(c.isdigit() for c in s)}
    numbers_b = {s for s in right if any(c.isdigit() for c in s)}
    if numbers_a != numbers_b:
        return "Different or unconfirmed model variant"
    common = left & right
    if a != b and (len(common) < 2 or len(common) / max(len(left), len(right), 1) < .5):
        if rank_style:
            from trial_style_evidence import strong_style_match
            if strong_style_match(target, other):
                return None
        return "Insufficient matching product details"
    if not common:
        return "Insufficient matching product details"
    return None


def new_result():
    return {"status": "insufficient", "resale_pence": None, "profit_pence": None, "meets_target": None,
            "evidence": [], "exclusions": {}, "explanation": "", "review_status": "rules"}


def estimate_resale(target, comparables, *, now=None, review=None, rank_style=False):
    """A cheaper superior listing caps the price. No assumed material/condition premium."""
    result = new_result()
    instant = timestamp(now) if now is not None else datetime.now(timezone.utc).timestamp()
    if instant is None:
        raise ValueError("now must be a timezone-aware datetime or Unix timestamp")
    target_condition = grade(target)
    if not target_condition or target.get("currency", "GBP") != "GBP":
        result["explanation"] = "A UK listing with a recognised condition is needed to compare prices."
        return result
    if not str(target.get("seller_id") or "").strip() or not (target.get("id") or listing_url(target)):
        result["explanation"] = "Listing and seller identifiers are needed to exclude the item and its seller."
        return result
    skipped = Counter()
    latest = {}
    for other in comparables:
        if not isinstance(other, dict):
            continue
        url, when = listing_url(other), timestamp(other.get("observed_at"))
        if not url or when is None or not 0 <= instant - when <= RECENT_SECONDS:
            skipped["Missing or expired UK pricing evidence"] += 1
            continue
        if url not in latest or when > latest[url][0]:
            latest[url] = when, other
    candidates, context, match_notes = [], [], {}
    for url, (when, item) in latest.items():
        item_id = url.rsplit("/", 1)[1]
        seller = str(item.get("seller_id") or "").strip()
        amount = item.get("ask_pence")
        if "id" in item and str(item["id"]) != item_id:
            reason = "Listing identifier does not match its link"
        elif not seller or seller == str(target.get("seller_id")) or url == listing_url(target) or item_id == str(target.get("id")):
            reason = "Same listing, same seller or unidentified seller"
        elif (item.get("currency", "GBP") != "GBP" or words(item.get("country", "UK")) not in {"uk", "gb", "united kingdom"}
              or not pence(amount) or not amount
              or item.get("active", True) is not True or words(item.get("status", "active")) not in {"active", "available", "listed"}):
            reason = "Not an active GBP asking price"
        else:
            reason = product_mismatch(target, item, rank_style=rank_style)
            level = grade(item)
            if not reason and level and rank_style:
                from trial_style_evidence import assess
                tier, note = assess(target, item, target_condition, level)
                if tier == "context":
                    context.append((url, item, note))
                    continue
                if tier == "different":
                    reason = note
                else:
                    match_notes[url] = note
            if not reason and (not level or level < target_condition):
                reason = "Lower or unconfirmed condition"
        if reason:
            skipped[reason] += 1
        else:
            candidates.append((url, item))

    if rank_style and len(candidates) >= 2:
        from trial_style_evidence import fully_described_match
        precise = [(url, row) for url, row in candidates if fully_described_match(target, row)]
        if len({row["seller_id"] for _, row in precise}) >= 2:
            selected_urls = {url for url, _ in precise}
            context.extend((url, row, match_notes[url]) for url, row in candidates if url not in selected_urls)
            candidates = precise

    if review and len({str(i["seller_id"]) for _, i in candidates}) >= 2:
        try:
            # Passing positions keeps prices, sellers, account data and URLs out of the model prompt.
            keep = review(target, [i for _, i in candidates])
            if not isinstance(keep, (list, set)) or any(type(n) is not int or not 0 <= n < len(candidates) for n in keep):
                raise ValueError("invalid model selections")
            keep = set(keep)
            skipped["Different item on detailed review"] += len(candidates) - len(keep)
            candidates = [pair for n, pair in enumerate(candidates) if n in keep]
            result["review_status"] = "ai_reviewed"
        except (ValueError, KeyError, TypeError, IndexError, requests.RequestException):
            result["review_status"] = "ai_unavailable"

    if context:
        # Reference listings are similar in some respects but cannot set the price.
        seen_sellers = set()
        result["context_evidence"] = []
        for url, item, note in sorted(context, key=lambda choice: (-float(timestamp(choice[1]["observed_at"])), choice[0])):
            if item["seller_id"] in seen_sellers:
                continue
            seen_sellers.add(item["seller_id"])
            result["context_evidence"].append({"id": item["id"], "url": url, "title": item["title"],
                                                "ask_pence": item["ask_pence"], "condition": item["condition"], "reason": note})
            if len(seen_sellers) == 5:
                break
    by_seller = {}
    # Count each seller once. For a seller's multiple listings, use the cheapest eligible alternative.
    for url, item in sorted(candidates, key=lambda pair: (pair[1]["ask_pence"], pair[0])):
        by_seller.setdefault(str(item["seller_id"]).strip(), (url, item))
    available = list(by_seller.values())
    result["evidence"] = [
        {"id": url.rsplit("/", 1)[1], "title": item["title"], "url": url,
         "ask_pence": item["ask_pence"], "condition": item["condition"],
         "reason": match_notes.get(url) or ("Same condition" if grade(item) == target_condition else "Higher stated condition"),
         "observed_at": item["observed_at"].isoformat() if isinstance(item["observed_at"], datetime) else item["observed_at"]}
        for url, item in available[:5]
    ]
    result["exclusions"] = {reason: count for reason, count in sorted(skipped.items()) if count}
    if len(available) < 2 or not any(grade(i) == target_condition for _, i in candidates):
        result["explanation"] = (
            "The other listings differ in documented style or wear. They do not establish a price for this item."
            if rank_style and context else "Need two independent comparable sellers and at least one match in the same condition.")
        return result
    cheapest = available[0][1]["ask_pence"]
    if len(available) >= 2 and available[1][1]["ask_pence"] > cheapest * 2:
        result["explanation"] = "The cheapest comparable is less than half the next asking price. Check both listings before pricing."
        return result
    if cheapest <= 100:
        result["explanation"] = "The closest listing is already at or below £1. A lower quick-sale ask is unsupported."
        return result
    price = max(100, ((cheapest - 1) // 50) * 50)
    cost, selling_cost = target.get("all_in_pence"), target.get("selling_cost_pence", 0)
    result.update(status="estimated", resale_pence=price,
                  explanation="Quick-sale asking price set just below the cheapest comparable item in the same or better condition.")
    if pence(cost) and pence(selling_cost):
        profit = price - cost - selling_cost
        result.update(profit_pence=profit, meets_target=profit >= PROFIT_TARGET)
    return result


def _public_description(item):
    return {
        field: str(item[field])[:150]
        for field in ("title", "category", "condition", *MATCH_FIELDS)
        if isinstance(item.get(field), (str, int))
    }
