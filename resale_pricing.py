"""Evidence for a quick UK resale, using independent active asking prices in pence."""

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from statistics import median
from unicodedata import normalize


# Multi-word and specific garment types take precedence over broader names.
GARMENTS = {
    "dress": ("dress", "dresses"), "jumpsuit": ("jumpsuit", "playsuit"),
    "blazer": ("blazer",), "jacket": ("jacket", "bomber", "biker", "shacket"),
    "coat": ("coat", "parka", "trench"), "cardigan": ("cardigan", "cardi"),
    "jumper": ("jumper", "sweater", "pullover"), "hoodie": ("hoodie", "hooded sweatshirt"),
    "waistcoat": ("waistcoat",), "blouse": ("blouse",), "shirt": ("shirt",),
    "top": ("top", "tops", "cami", "tee", "t shirt", "tshirt", "bodysuit", "corset"),
    "skirt": ("skirt", "skirts"), "shorts": ("shorts",),
    "jeans": ("jeans",), "trousers": ("trousers", "pants", "capris"),
}
FEATURES = {"leather", "suede", "silk", "wool", "cashmere", "linen", "mesh", "lace",
            "denim", "satin", "velvet", "bomber", "biker", "cropped", "quilted",
            "waxed", "fitted", "ribbed", "pinstripe", "cable", "maxi", "midi", "mini",
            "wrap", "frill", "beaded", "sequin", "embroidered", "shearling"}
COMMON = {"women", "womens", "ladies", "lady", "mens", "men", "size", "uk", "new",
          "very", "good", "condition", "vintage", "y2k", "style", "with", "and", "the",
          "black", "white", "blue", "green", "red", "brown", "pink", "grey", "khaki"}
UNBRANDED = {"", "unbranded", "no brand", "unknown", "not specified", "other"}
MAX_MARKET_AGE = 12 * 3600
PROFIT_TARGET = 1500


def words(value):
    return " ".join(re.findall(r"[a-z0-9]+", normalize("NFKD", str(value or "")).encode(
        "ascii", "ignore").decode().lower().replace("'", "").replace("’", "")))


def label(value):
    if isinstance(value, dict):
        value = value.get("title") or value.get("name") or value.get("label")
    return value.strip()[:160] if isinstance(value, str) else ""


def garment(title):
    text = " " + words(title).replace("t shirt", "tshirt") + " "
    return next((kind for kind, names in GARMENTS.items()
                 if any(" " + name + " " in text for name in names)), "")


def market_key(item):
    brand = words(label(item.get("brand")))
    kind = garment(item.get("title"))
    if not kind:
        return None
    if brand in UNBRANDED:
        traits = sorted(set(words(item.get("title")).split()) & FEATURES)
        return (" ".join(traits[:3]), kind) if traits else None
    return (brand, kind)


def pence(value):
    if isinstance(value, dict):
        if (value.get("currency_code") or value.get("currency") or "GBP") != "GBP":
            return None
        value = value.get("amount")
    try:
        money = Decimal(str(value))
        if money.is_finite() and 0 < money <= 10000 and money * 100 == int(money * 100):
            return int(money * 100)
    except (ValueError, TypeError, InvalidOperation):
        pass
    return None


def timestamp(value):
    if isinstance(value, (int, float)) and 0 < value < 1e11:
        return float(value)
    if isinstance(value, str):
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return date.timestamp() if date.tzinfo else None
        except (OverflowError, ValueError):
            pass
    return None


def market_listing(raw, currency):
    """Only Scavio's market results enter the cache. Keep no seller contact details."""
    if not isinstance(raw, dict) or raw.get("currency", currency) != "GBP":
        return None
    if any(raw.get(key) is True for key in ("is_sold", "is_reserved", "sold", "reserved", "is_hidden")):
        return None
    title, brand = label(raw.get("title")), label(raw.get("brand"))
    if not title or raw.get("id") is None:
        return None
    price = pence(raw.get("price"))
    discounted = pence(raw.get("price_with_discount"))
    if price is None:
        return None
    seller = raw.get("seller")
    seller_id = seller.get("id") if isinstance(seller, dict) else raw.get("seller_id")
    return dict(id=str(raw["id"]), title=title, brand=brand, size=label(raw.get("size")),
                condition=label(raw.get("condition")), price=min(price, discounted or price),
                seller=str(seller_id or ""), listed=timestamp(raw.get("listed_at") or raw.get("created_at")))


def distinctive(item):
    tokens = set(words(item.get("title")).split())
    ignore = set(words(item.get("brand")).split()) | COMMON | set(GARMENTS)
    return {s for s in tokens - ignore if len(s) > 2 and not s.isdigit()}


def fabric(text):
    if re.search(r"\b(faux|fake|vegan|imitation|synthetic|pu|pleather)\b", text):
        return "synthetic"
    if re.search(r"\b(real|genuine|lambskin|cowhide|sheepskin)\b", text):
        return "genuine"
    return ""


def shortlist(target, listings, now):
    """Filter before model ranking. Never show the model the alert price or asking prices."""
    key = market_key(target)
    if not key:
        return []
    target_words = distinctive(target)
    target_size = words(target.get("size"))
    target_condition = words(target.get("condition"))
    target_fabric = fabric(words(target.get("title")))
    ranked = []
    seen = set()
    for item in listings:
        if item["id"] == str(target["id"]) or item["id"] in seen or market_key(item) != key:
            continue
        title = words(item["title"])
        if re.search(r"\b(bundle|replica|wanted|iso|kids|childrens|boys|girls)\b", title):
            continue
        if re.search(r"\b(mens|men|man)\b", title) and not re.search(r"\b(men|mens)\b", words(target["title"])):
            continue
        observed = item.get("listed") or item.get("first_seen")
        if observed and (observed > now + 300 or now - observed > 30 * 86400):
            continue
        material = fabric(title)
        if target_fabric and material and target_fabric != material:
            continue
        traits = distinctive(item)
        specific = target_words & traits
        size_matches = bool(target_size and target_size == words(item.get("size")))
        condition_matches = bool(target_condition and target_condition == words(item.get("condition")))
        distinctive_match = bool(specific & FEATURES or specific - COMMON - FEATURES)
        same_material = not target_fabric or material == target_fabric
        days = max(0, int((now - observed) / 86400)) if observed else None
        weeks_old = days is not None and days >= 14
        age_band = (f"listed {days} days ago" if item.get("listed") else
                    f"seen active for at least {days} days" if days else "")
        score = len(specific) * 2 + len(specific & FEATURES) + size_matches + condition_matches - 2 * weeks_old
        ranked.append((score, item["id"], dict(item, strong=distinctive_match and same_material,
                                               long_listed=weeks_old, age_band=age_band)))
        seen.add(item["id"])
    return [item for _, _, item in sorted(ranked, key=lambda hit: (-hit[0], hit[1]))[:10]]


def percentile(prices, fraction):
    position = (len(prices) - 1) * fraction
    lower = int(position)
    return prices[lower] + (prices[min(lower + 1, len(prices) - 1)] - prices[lower]) * (position - lower)


def estimate(target, candidates, *, verified=None, approximate=()):
    """Undercut nearby active asks. A single seller cannot supply a confident consensus."""
    eligible = candidates if verified is None else [candidates[i] for i in (verified or approximate)]
    independent, sellers = [], set()
    for item in eligible:
        seller = item.get("seller")
        if seller and seller in sellers:
            continue
        independent.append(item)
        if seller:
            sellers.add(seller)
    strong = [item for item in independent if (item["strong"] if verified is None else bool(verified))]
    evidence = strong if strong else independent
    prices = sorted(item["price"] for item in evidence)
    result = dict(verdict="Not sure", resale_pence=None, profit_pence=None, comparisons=len(prices))
    if not prices:
        return result
    def undercut(value):
        return max(50, int((value * .95) // 50) * 50)
    low, high = undercut(percentile(prices, .25)), undercut(percentile(prices, .75))
    resale = undercut(percentile(prices, .4))
    result["resale_pence"] = resale
    result["verdict"] = "Not confident"
    cost = target.get("total_pence")
    if type(cost) is not int or cost <= 0:
        return result
    result["profit_pence"] = resale - cost
    confirmed_sellers = {item["seller"] for item in strong if item.get("seller") and not item.get("long_listed")}
    if len(confirmed_sellers) >= 3 and (high - low) <= median(prices) * .35:
        if low - cost > PROFIT_TARGET:
            result["verdict"] = "Yes"
        elif high - cost <= PROFIT_TARGET:
            result["verdict"] = "No"
    return result


def alert_line(result):
    resale, profit = result["resale_pence"], result["profit_pence"]
    if result["verdict"] == "Market unavailable":
        return "Comparison prices unavailable · no estimate yet"
    if result["verdict"] == "Review unavailable":
        return "AI review unavailable · no estimate yet"
    if resale is None:
        return "Not sure · no reliable resale estimate"
    def money(n):
        return ("−" if n < 0 else "") + f"£{abs(n) / 100:.2f}"
    gain = f"est. profit {money(profit)}" if profit is not None else "all-in cost unknown"
    verdict = result["verdict"]
    if verdict == "Not confident" and profit is not None:
        verdict += " (likely Yes)" if profit > PROFIT_TARGET else " (likely No)"
    return f"Over £15 profit? {verdict} · {gain} · est. resale {money(resale)}"
