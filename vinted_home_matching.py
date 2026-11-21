"""Conservative, side-effect-free relevance checks for Home discovery.

Home recommendations are candidate discovery, never relevance proof. A candidate
must satisfy all saved structural filters and a finite price cap. Keyword
alternatives may use the small, explicit spelling/synonym vocabulary below; an
photo evidence is restricted to explicitly profiled open-label style searches.
The caller independently verifies listing age, availability, identity and dedup.
"""

import re
import unicodedata
from functools import lru_cache
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import vinted_budget
import vinted_scraper_styles as scraper_styles

MAX_TEXT = 6000
MAX_SEARCHES = 500
_FILTERS = {
    "brand": ("brand_ids[]", "brand_id"),
    "catalog": ("catalog[]", "category_ids"),
    "size": ("size_ids[]", "size_id"),
    "color": ("color_ids[]", "color_ids"),
    "material": ("material_ids[]", "material_ids"),
    "status": ("status_ids[]", "condition_id"),
    "country": ("country_ids[]", "country_id"),
    "city": ("city_ids[]", "city_id"),
    "video_game_platform": ("video_game_platform_ids[]", "video_game_platform_ids"),
}
_IGNORED_PARAMS = {
    "search_text",
    "price_to",
    "price_from",
    "currency",
    "order",
    "page",
    "per_page",
    "time",
    "search_id",
    "disabled_personalization",
    "referrer",
    "catalog_from",
}
# Equivalent garment descriptors only. In particular, vintage/Y2K, authenticity,
# condition and material are not inferred or removed to manufacture a match.
_SYNONYMS = (
    ("zip up", "zip front", "full zip"),
    ("fitted", "slim fit", "slim fitting"),
    ("cable knit", "cable knitted"),
    ("roll neck", "turtle neck", "turtleneck"),
    ("v neck", "v necked"),
    ("long sleeve", "long sleeved"),
    ("short sleeve", "short sleeved"),
    ("fur trim", "fur trimmed"),
)
_NEGATIONS = {"no", "not", "without", "never", "non"}


def _words(value):
    return tuple(
        re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold())
    )


@lru_cache(maxsize=640)
def _canonical_words(value):
    words = list(_words(value))
    for index, phrases in enumerate(_SYNONYMS):
        replacement = f"style{index}"
        for phrase in sorted(phrases, key=len, reverse=True):
            target = list(_words(phrase))
            at = 0
            while at <= len(words) - len(target):
                if words[at : at + len(target)] == target:
                    words[at : at + len(target)] = [replacement]
                at += 1
    return tuple(words)


def _phrase_in(phrase, fields, *, synonyms=False, positive=False):
    tokenize = _canonical_words if synonyms else _words
    needle = tokenize(phrase)
    if not needle:
        return False
    for field in fields:
        words = field if isinstance(field, tuple) else tokenize(field)
        for at in range(len(words) - len(needle) + 1):
            if words[at : at + len(needle)] == needle:
                # A seller's "not fitted" is not positive style evidence.
                if (synonyms or positive) and set(words[max(0, at - 3) : at]) & _NEGATIONS:
                    continue
                return True
    return False


def _ids(value):
    values = value if isinstance(value, (list, tuple, set)) else [value]
    if len(values) > 100:
        return None
    result = set()
    for part in values:
        if isinstance(part, bool) or not isinstance(part, (int, str)):
            return None
        for identity in str(part).split(","):
            identity = identity.strip()
            if not re.fullmatch(r"[0-9]{1,20}", identity):
                return None
            result.add(identity)
    return result


@lru_cache(maxsize=1024)
def _filters(query):
    if not isinstance(query, str) or len(query) > 12000:
        return None
    try:
        parts = urlsplit(query)
        values = parse_qs(parts.query, keep_blank_values=True, max_num_fields=1000)
        if (
            parts.scheme != "https"
            or parts.netloc not in {"www.vinted.co.uk", "vinted.co.uk"}
            or parts.fragment
            or not re.fullmatch(r"/catalog(?:/[0-9]+(?:-[\w-]+)?)?/?", parts.path)
            or values.get("currency", ["GBP"]) != ["GBP"]
        ):
            return None
    except (ValueError, TypeError):
        return None
    allowed = set(_IGNORED_PARAMS)
    structural = {}
    for attribute, (legacy, field) in _FILTERS.items():
        direct = f"attribute_ids[{attribute}]"
        allowed.update((legacy, direct))
        raw = values.get(legacy, []) + values.get(direct, [])
        if raw:
            identities = _ids(raw)
            if not identities:
                return None
            structural[field] = identities
    category = re.match(r"/catalog/([0-9]+)", parts.path)
    if category:
        if (
            structural.get("category_ids")
            and category[1] not in structural["category_ids"]
        ):
            return None
        structural.setdefault("category_ids", set()).add(category[1])
    if set(values) - allowed:
        return None
    if not (structural.get("brand_id") or structural.get("category_ids")):
        return None
    return values, structural


def _price_limits(item, search, values):
    if not isinstance(item.get("price"), dict) or "amount" not in item["price"]:
        return None
    price = vinted_budget.money(item.get("price"))
    if price is None or price <= 0:
        return None
    caps = []
    for key, minimum in (("price_to", False), ("price_from", True)):
        if key not in values:
            continue
        raw = values[key]
        if len(raw) != 1:
            return None
        amount = vinted_budget.money({"amount": raw[0], "currency_code": "GBP"})
        if amount is None or (price < amount if minimum else price > amount):
            return None
        if not minimum:
            caps.append(amount)
    maximum = search.get("max_buy")
    if maximum is not None:
        if type(maximum) is not int or maximum <= 0 or price > maximum:
            return None
        caps.append(maximum)
    total = search.get("vinted_max_total")
    if total is not None:
        if type(total) is not int or not 100 <= total <= 100000:
            return None
        caps.append(total)
    if not caps:
        return None
    postage = search.get("vinted_postage_estimate", vinted_budget.DEFAULT_POSTAGE)
    if type(postage) is not int or not 0 <= postage <= 10000:
        return None
    budget = vinted_budget.estimate(
        SimpleNamespace(
            price=item["price"]["amount"],
            currency="GBP",
            raw_data={"total_item_price": item.get("total_item_price")},
        ),
        search,
        display=True,
    )
    return budget if budget and budget["within_budget"] else None


# Verified women's clothing labels/IDs used by this Finder. A visible size
# can supply missing metadata, but never override a conflicting native size.
_WOMEN_HALVES = {
    "xs": {"2"}, "extra small": {"2"}, "s": {"3"}, "small": {"3"},
    "m": {"4"}, "medium": {"4"},
}
_WOMEN_CLOTHING = {"1037", "12", "13", "194", "196", "11", "9", "10", "2524", "572"}
_WOMEN_CRUMBS = {
    "jackets & coats": "1037", "outerwear": "1037", "coats & jackets": "1037",
    "jackets": "1037", "coats": "1037", "tops & t-shirts": "12", "tops": "12",
    "jumpers & sweaters": "13", "jumpers & knitwear": "13", "cardigans": "194",
    "hoodies & sweatshirts": "196", "skirts": "11", "trousers": "9", "dresses": "10",
}


def _item_scope(item):
    observed = {field: _ids(item.get(field)) for _, field in _FILTERS.values()}
    categories = observed["category_ids"] or set()
    labels = set(item.get("category_labels", []))
    if labels & {"women", "women's clothing", "women’s clothing"}:
        categories = categories | {_WOMEN_CRUMBS[n] for n in labels if n in _WOMEN_CRUMBS}
    observed["category_ids"] = categories
    if not categories.intersection(_WOMEN_CLOTHING) or "size_id" in item.get("conflicting_fields", ()):
        return observed
    size = item.get("size_title", "")
    if not isinstance(size, str):
        return observed
    label = size.strip().casefold().replace("–", "-").replace(" / ", "/")
    match = re.fullmatch(r"(extra small|small|medium|xs|s|m)(?:/(?:uk |eu )?\d+(?:\s*-\s*\d+)?)*", label)
    if not match:
        return observed
    visible = _WOMEN_HALVES[match[1]]
    if item.get("size_id") is None:
        observed["size_id"] = visible
    elif not visible.intersection(observed["size_id"] or ()):
        observed["size_id"] = None
    return observed


def _match(item, search, fields, styles, observed, photo_styles, eligible, diag):
    if (
        not isinstance(search, dict)
        or type(search.get("id")) is not int
        or search["id"] <= 0
    ):
        return None
    if (
        search.get("paused")
        or search.get("archived")
        or not search.get("vinted_enabled", True)
    ):
        return None
    parsed = _filters(search.get("query"))
    if parsed is None:
        return None
    values, structural = parsed
    for field, allowed in structural.items():
        field_ids = observed[field]
        if not field_ids or not field_ids & allowed:
            return None
    diag.add("scope")
    exclusions = search.get("exclusions", [])
    if (
        not isinstance(exclusions, list)
        or len(exclusions) > 60
        or any(not isinstance(word, str) or len(word) > 60 for word in exclusions)
        or any(_phrase_in(word, fields) for word in exclusions)
    ):
        return None
    must_have = search.get("must_have", "")
    if not isinstance(must_have, str) or len(must_have) > 400:
        return None
    if must_have and not (_phrase_in(must_have, fields, positive=True) or _phrase_in(must_have, styles, synonyms=True)):
        return None
    words = (
        search.get("shared_keywords")
        if search.get("shared_alert_version") == 1
        else search.get("vinted_keywords")
    )
    words = words or values.get("search_text", [])
    if not isinstance(words, list) or len(words) > 100:
        return None
    if any(not isinstance(word, str) or len(word) > 400 for word in words):
        return None
    words = [word.strip() for word in words if word.strip()]
    budget = _price_limits(item, search, values)
    if budget is None or (search.get("style_alert_cap") and budget["total"] > search["style_alert_cap"]):
        return None
    diag.add("budget")
    literal = any(_phrase_in(word, fields, positive=True) for word in words)
    equivalent = bool(words and not literal and any(_phrase_in(word, styles, synonyms=True) for word in words))
    profile = scraper_styles.profile_for(words, structural)
    style_evidence = "literal" if literal else "equivalent_words" if equivalent else "not_required"
    quality = scraper_styles.garment_quality(profile, item["title"], item["description"], item.get("brand_title")) if profile else None
    if quality in {"blocked", "weak_high_street"}:
        return None
    if words and (not (literal or equivalent) or quality == "unconfirmed"):
        if not profile:
            return None
        guide = scraper_styles.STYLES[profile]
        if quality == "distinctive" and any(scraper_styles.evidence(item[f], guide) for f in ("title", "description")):
            style_evidence = "scraper_style"
        else:
            # An image may only supply a style after the same search's price,
            # exclusions, category, size and any explicit must-have have passed.
            eligible.add(profile)
            if profile not in photo_styles:
                return None
            style_evidence = "scraper_photo"
    scope = (
        "brand_category"
        if "brand_id" in structural and "category_ids" in structural
        else "brand" if "brand_id" in structural else "category"
    )
    return {
        "query_id": search["id"],
        "reason": style_evidence if style_evidence.startswith("scraper_") else scope + "_style" if words or must_have else "saved_filters",
        "style_evidence": style_evidence,
        "budget": budget,
    }


def match_candidate(item, searches, *, photo_styles=(), eligible=None, diagnostics=None):
    """Return one deterministic saved target, or None; never modify either input.

    Required candidate fields: title, complete description, GBP price, and each
    structural ID required by a target's URL. Category IDs may include observed
    ancestors. Known women’s garment sizes may use an unambiguous displayed size.
    """
    if not isinstance(item, dict) or item.get("description_complete") is not True:
        return None
    if any(
        not isinstance(item.get(key), str) or len(item[key]) > MAX_TEXT
        for key in ("title", "description")
    ):
        return None
    if not isinstance(searches, (list, tuple)) or len(searches) > MAX_SEARCHES:
        return None
    fields = tuple(_words(item[k]) for k in ("title", "description"))
    styles = tuple(_canonical_words(item[k]) for k in ("title", "description"))
    observed = _item_scope(item)
    eligible = eligible if eligible is not None else set()
    diag = diagnostics if diagnostics is not None else set()
    matches = [result for search in searches if (result := _match(
        item, search, fields, styles, observed, photo_styles, eligible, diag
    ))]
    return min(matches, key=lambda result: result["query_id"]) if matches else None
