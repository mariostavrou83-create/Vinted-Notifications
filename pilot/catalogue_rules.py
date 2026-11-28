"""Match UK catalogue filters against published listing attributes inside Finder."""
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import parse_qsl, urlsplit

from .inputs import money, search_url

ATTRIBUTES = {"catalog": "category_ids", "catalog_id": "category_ids", "category_id": "category_ids",
              "brand_ids": "brand_ids", "size_ids": "size_ids", "status_ids": "status_ids",
              "color_ids": "color_ids", "material_ids": "material_ids"}
TRACKING = {"order", "sort", "page", "time", "search_id", "referrer", "disabled_personalization"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def words(text):
    return frozenset(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold()))


@dataclass(frozen=True)
class Filter:
    terms: frozenset
    attributes: tuple
    minimum: int
    maximum: int

    def accepts(self, tokens, facts, price):
        return (self.minimum <= price <= self.maximum and self.terms <= tokens
                and all(values & facts.get(field, frozenset()) for field, values in self.attributes))

    def anchors(self):
        # Any required word or attribute group can identify candidates for a full check.
        return [[("word", word)] for word in self.terms] + [
            [(field, value) for value in values] for field, values in self.attributes]


@lru_cache(maxsize=6000)
def catalogue_filter(url):
    url = urlsplit(search_url(url))
    category = re.fullmatch(r"/catalog(?:/([1-9][0-9]*)(?:-[^/%]*)?)?/?", url.path)
    if not category:
        raise ValueError("Use a UK Vinted catalogue link for this search.")
    attrs, terms, prices = defaultdict(set), set(), {}
    if category[1]:
        attrs["category_ids"].add(int(category[1]))
    for key, value in parse_qsl(url.query, keep_blank_values=True, max_num_fields=150):
        if key in TRACKING or key.startswith("utm_"):
            continue
        name = key.removesuffix("[]")
        if name in ATTRIBUTES and re.fullmatch(r"[1-9][0-9]{0,8}(?:,[1-9][0-9]{0,8})*", value):
            attrs[ATTRIBUTES[name]].update(int(n) for n in value.split(","))
        elif name in {"search_text", "query"} and len(value) <= 200:
            terms.update(words(value))
        elif name in {"price_from", "price_to"}:
            amount = money(value)
            if amount is None:
                raise ValueError("Enter a minimum or maximum item price.")
            if name in prices and prices[name] != amount:
                raise ValueError("Use a single minimum and maximum price.")
            prices[name] = amount
        elif name == "currency" and value == "GBP":
            continue
        else:
            raise ValueError("This UK catalogue filter is unsupported. Use category, brand, size, condition, colour, material, price or keywords.")
    if category[1] and len(attrs["category_ids"]) > 1:
        raise ValueError("Use the category path or a list of categories in the search link.")
    minimum, maximum = prices.get("price_from", 0), prices.get("price_to", 100_000_000)
    if minimum > maximum:
        raise ValueError("The minimum price exceeds the maximum.")
    normalized = sorted((name, sorted(values)) for name,values in attrs.items())
    spec = Filter(frozenset(terms), tuple((name,frozenset(values)) for name,values in normalized), minimum, maximum)
    return digest((sorted(terms),normalized,minimum,maximum)), spec


class CatalogueMatcher:
    def __init__(self, filters):
        self.filters = filters
        self.dimensions = {field for spec in filters.values() for field, _ in spec.attributes}
        self.index = defaultdict(set)
        self.all_items = set()
        choices = {key: spec.anchors() for key, spec in filters.items()}
        counts = Counter(anchor for groups in choices.values() for group in groups for anchor in group)
        for key, groups in choices.items():
            if groups:
                group = min(groups, key=lambda group: sum(counts[a] for a in group))
                for anchor in group:
                    self.index[anchor].add(key)
            else:
                self.all_items.add(key)

    def matched(self, listing):
        tokens = words(" ".join(listing[field] for field in ("title","description","brand")
                                if isinstance(listing.get(field), str)))
        facts = listing.get("attributes", {})
        if not isinstance(facts, dict):
            raise ValueError("Missing UK catalogue attributes.")
        fields = {}
        for dimension in self.dimensions:
            values = facts.get(dimension)
            if (not isinstance(values, list) or len(values) > 128
                    or any(type(v) is not int or v <= 0 for v in values)):
                raise ValueError("Incomplete UK catalogue attributes.")
            fields[dimension] = frozenset(values)
        candidates = self.all_items.copy()
        for token in tokens:
            candidates.update(self.index.get(("word", token), ()))
        for name, values in fields.items():
            for value in values:
                candidates.update(self.index.get((name,value), ()))
        return [key for key in candidates if self.filters[key].accepts(tokens, fields, listing["price_pence"])]
