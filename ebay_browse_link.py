"""Resolve public eBay browse links once, when the owner imports/saves a search.

Only eBay's bound, structured filter metadata is accepted. Persisted searches
remain explicit /sch/ links; polling never reads browse HTML or uses this cache.
"""

import json
import re
import threading
import time
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import requests

from ebay_search_link import ASPECTS, FIELDS, IGNORED, clean, parse_link

MAX_BYTES = 4 * 1024 * 1024
MAX_REDIRECTS = 3
DEADLINE = 20
CACHE_SECONDS = 600
CACHE_SIZE = 128
_cache = OrderedDict()
_cache_lock = threading.Lock()
_PAYLOAD = re.compile(
    r"\$brwweb_C\s*=\s*\(window\.\$brwweb_C\s*\|\|\s*\[\]\)\.concat\("
)
_SEARCH = re.compile(r"/sch/(?:(\d{1,10})/)?i\.html")


def _identity(path):
    pieces = path.strip("/").split("/")
    if not pieces or pieces.pop(0) != "b" or not 1 <= len(pieces) <= 3:
        return None
    if any(not re.fullmatch(r"[A-Za-z0-9%._~-]{1,200}", p) for p in pieces):
        return None
    if any(re.search(r"%(?:2f|5c|2e)", p, re.IGNORECASE) for p in pieces):
        return None
    node = re.fullmatch(r"bn_([1-9][0-9]{0,15})", pieces[-1])
    if node:
        category = pieces[-2] if len(pieces) > 1 and pieces[-2].isdigit() else None
        return "node", node[1], category
    if len(pieces) <= 2 and re.fullmatch(r"[1-9][0-9]{0,9}", pieces[-1]):
        return "category", pieces[-1], pieces[-1]
    return None


def _validated_url(link):
    if not isinstance(link, str) or len(link) > 6000:
        raise ValueError("Keep the eBay search link under 6,000 characters.")
    try:
        url = urlsplit(link.strip())
        valid = (
            url.scheme == "https"
            and url.hostname in {"ebay.co.uk", "www.ebay.co.uk"}
            and not url.username
            and not url.password
            and url.port in (None, 443)
            and not url.fragment
            and not any(ord(c) < 32 for c in link)
            and "\\" not in link
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Paste the full HTTPS eBay UK link from your address bar.")
    if not (_SEARCH.fullmatch(url.path) or _identity(url.path)):
        raise ValueError(
            "Use an eBay search or category results link, not an item or shop."
        )
    _query(url)
    return url


def _query(url):
    try:
        pairs = parse_qsl(url.query, keep_blank_values=True, max_num_fields=100)
    except ValueError:
        raise ValueError("The eBay link contains too many filters.") from None
    params = {}
    for key, value in pairs:
        key, value = clean(key), clean(value)
        if key in params and value != params[key]:
            raise ValueError("The eBay link repeats a filter with different values.")
        params[key] = value
    unsupported = sorted(set(params) - FIELDS - ASPECTS - IGNORED - {"mag"})
    if unsupported:
        raise ValueError(
            "Cannot import these filters safely: "
            + ", ".join(unsupported)
            + ". Nothing has been saved."
        )
    if params.get("mag", "") not in {"", "0", "1"}:
        raise ValueError("This eBay gallery setting cannot be imported safely.")
    return {key: value for key, value in params.items() if key not in IGNORED | {"mag"}}


def fetch_page(link):
    """Bound public GETs; no account cookies, credentials, proxy or API calls."""
    current = _validated_url(link).geturl()
    deadline = time.monotonic() + DEADLINE
    try:
        with requests.Session() as session:
            session.trust_env = False
            for hop in range(MAX_REDIRECTS + 1):
                if time.monotonic() >= deadline:
                    raise ValueError("eBay took too long to read this link. Try again.")
                session.cookies.clear()
                with session.get(
                    current,
                    headers={"Accept": "text/html", "Accept-Language": "en-GB"},
                    stream=True,
                    allow_redirects=False,
                    timeout=(3, min(8, max(0.1, deadline - time.monotonic()))),
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location or hop == MAX_REDIRECTS:
                            raise ValueError(
                                "eBay redirected this link too many times."
                            )
                        current = _validated_url(urljoin(current, location)).geturl()
                        continue
                    if response.status_code != 200:
                        raise ValueError(
                            "eBay could not provide this page's filters "
                            f"(HTTP {response.status_code}). Nothing has been saved. "
                            "Open the link on eBay to check it, then try again."
                        )
                    content_type = response.headers.get("Content-Type", "").lower()
                    if not any(
                        t in content_type
                        for t in ("text/html", "application/xhtml+xml")
                    ):
                        raise ValueError("eBay returned an unreadable filter page.")
                    chunks, size = [], 0
                    for chunk in response.iter_content(chunk_size=65536):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError(
                                "The eBay filter page was too large to read safely."
                            )
                        if time.monotonic() >= deadline:
                            raise ValueError(
                                "eBay took too long to read this link. Try again."
                            )
                        chunks.append(chunk)
                    return b"".join(chunks).decode("utf-8", errors="strict"), current
    except (requests.RequestException, UnicodeError):
        raise ValueError(
            "The eBay filter page could not be read. Nothing has been saved. Try again."
        ) from None
    raise ValueError("eBay did not provide readable filters for this link.")


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.in_script = False
        self.parts = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script, self.parts = True, []

    def handle_data(self, data):
        if self.in_script:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.in_script:
            self.scripts.append("".join(self.parts))
            self.in_script, self.parts = False, []


def _entity(entity, identity):
    if not isinstance(entity, dict):
        # Invalid provider data is an import error, handled by the form.
        raise ValueError("eBay's filter identity is missing.")  # noqa: TRY004
    kind, ident, _ = identity
    accepted = {"BrowseNode", "CategoryBrowseNode"} if kind == "node" else {"Category"}
    if entity.get("type") not in accepted or str(entity.get("id")) != ident:
        raise ValueError("eBay's filters do not belong to the pasted results page.")
    if entity.get("namespaceAttributes") not in (None, []):
        raise ValueError(
            "This eBay page has additional constraints that cannot be imported safely."
        )
    return _attributes(entity.get("attributes", []), aspects_only=True)


def _attributes(entries, *, aspects_only=False):
    if not isinstance(entries, list) or len(entries) > 100:
        raise ValueError("eBay's selected filters are unreadable.")
    result = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ValueError("eBay's selected filters are incomplete.")  # noqa: TRY004
        key, values = clean(entry["name"]), entry.get("value")
        if key not in (ASPECTS if aspects_only else ASPECTS | FIELDS):
            raise ValueError("Cannot import this selected eBay filter safely: " + key)
        if not isinstance(values, list) or not 1 <= len(values) <= 30:
            raise ValueError("eBay's selected filter values are incomplete.")
        strings = []
        for value in values:
            if type(value) not in (str, int, float):
                raise ValueError("eBay's selected filter values are unreadable.")
            value = clean(str(value))
            if (
                not value
                or "|" in value
                or (key in ASPECTS and re.search(r"[{},:\\\\\x00-\x1f]", value))
            ):
                raise ValueError("eBay's selected filter values are unreadable.")
            strings.append(value)
        result.setdefault(key, set()).update(strings)
        if key in FIELDS - {"LH_ItemCondition"} and len(result[key]) != 1:
            raise ValueError("eBay supplied conflicting values for " + key + ".")
    return result


def filters_from_html(html, final_url):
    """Read bound complete criteria and independently corroborate their identity."""
    url = _validated_url(final_url)
    identity = _identity(url.path)
    if (
        not identity
        or not isinstance(html, str)
        or len(html.encode("utf-8")) > MAX_BYTES
    ):
        raise ValueError("eBay did not provide a readable category filter page.")
    parser = _Scripts()
    contexts, corroboration = [], []
    try:
        parser.feed(html)
        decoder = json.JSONDecoder()
        for script in parser.scripts:
            for match in _PAYLOAD.finditer(script):
                root, _ = decoder.raw_decode(script[match.end() :].lstrip())
                objects = root.get("o", {}) if isinstance(root, dict) else {}
                widgets = objects.get("w", []) if isinstance(objects, dict) else []
                if not isinstance(widgets, list) or len(widgets) > 10000:
                    raise ValueError("eBay's filter metadata is unreadable.")
                for widget in widgets:
                    if (
                        not isinstance(widget, list)
                        or len(widget) < 3
                        or not isinstance(widget[2], dict)
                    ):
                        continue
                    props = widget[2]
                    context = props.get("context", {})
                    if isinstance(context, dict) and "browse" in context:
                        contexts.append(context["browse"])
                    if "pageContext" in props:
                        corroboration.append(props["pageContext"])
    except (ValueError, TypeError, RecursionError):
        raise ValueError(
            "eBay's filter metadata could not be read safely. Nothing has been saved."
        ) from None
    if not contexts or not corroboration:
        raise ValueError(
            "eBay did not provide its selected filter details. It may need verification. "
            "Nothing has been saved; open the page on eBay and try again."
        )
    projected = []
    for browse in contexts:
        if not isinstance(browse, dict) or not isinstance(browse.get("category"), dict):
            raise ValueError("eBay's category is missing.")  # noqa: TRY004
        category = str(browse["category"].get("id", ""))
        if not re.fullmatch(r"[1-9][0-9]{0,9}", category) or (
            identity[2] and identity[2] != category
        ):
            raise ValueError("eBay supplied conflicting categories for this page.")
        implicit = _entity(browse.get("entity"), identity)
        aspects = browse.get("aspects")
        if not isinstance(aspects, dict) or not {"local", "global"} <= aspects.keys():
            raise ValueError("eBay's selected filters are incomplete.")
        if not all(isinstance(aspects[key], list) for key in ("local", "global")):
            raise ValueError("eBay's selected filters are unreadable.")
        selected = _attributes(aspects["local"] + aspects["global"])
        if any(
            not values <= selected.get(key, set()) for key, values in implicit.items()
        ):
            raise ValueError(
                "eBay's implicit brand or other filters could not be confirmed."
            )
        for page in corroboration:
            if (
                not isinstance(page, dict)
                or str(page.get("categoryId")) != category
                or _entity(page.get("entity"), identity) != implicit
            ):
                raise ValueError("eBay supplied conflicting filter metadata.")
        selected["_sacat"] = {category}
        projected.append(
            {key: "|".join(sorted(values)) for key, values in selected.items()}
        )
    if any(params != projected[0] for params in projected[1:]):
        raise ValueError("eBay supplied conflicting selected filters.")
    return projected[0]


def _same_scalar(key, one, two):
    if key in {"_udlo", "_udhi"}:
        try:
            return Decimal(one) == Decimal(two)
        except InvalidOperation:
            return False
    return one == two


def _confirm_original_page(url, final_url, params):
    """A redirected node must retain the original node's implicit constraints."""
    original_identity = _identity(url.path)
    final_identity = _identity(final_url.path)
    if original_identity[2] and original_identity[2] != params.get("_sacat"):
        raise ValueError(
            "eBay's redirect changed the original category. Nothing has been saved."
        )
    if final_identity and final_identity[:2] == original_identity[:2]:
        return
    # A browse node can embed a brand without including it in its URL query.
    # Read its unfiltered base once rather than guessing from a slug or title.
    base_html, base_final = fetch_page(url._replace(query="").geturl())
    base_url = _validated_url(base_final)
    base_identity = _identity(base_url.path)
    if not base_identity or base_identity[:2] != original_identity[:2]:
        raise ValueError(
            "eBay redirected the original category too. Its filters could not be "
            "confirmed, so nothing has been saved. Copy a fresh filtered link."
        )
    original_params = filters_from_html(base_html, base_final)
    for key, value in original_params.items():
        if key in ASPECTS | {"LH_ItemCondition"}:
            retained = set(value.split("|")) <= set(params.get(key, "").split("|"))
        else:
            retained = key in params and _same_scalar(key, value, params[key])
        if not retained:
            raise ValueError(
                "eBay's redirect lost the original " + key + " filter. "
                "Nothing has been saved. Copy a fresh filtered link from eBay."
            )


def resolve_link(link):
    """Convert /b/ once; preserve existing /sch/ imports without any web read."""
    url = _validated_url(link)
    if _SEARCH.fullmatch(url.path):
        return url.geturl()
    cache_key = url.geturl()
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            _cache.move_to_end(cache_key)
            return cached[1]
    original = _query(url)
    html, final = fetch_page(url.geturl())
    final_url = _validated_url(final)
    if _SEARCH.fullmatch(final_url.path):
        final_config = parse_link(final)
        params = dict(parse_qsl(urlsplit(final_config["search_url"]).query))
    else:
        params = filters_from_html(html, final)
    _confirm_original_page(url, final_url, params)
    for submitted in (original, _query(final_url)):
        for key, value in submitted.items():
            if key in {"_sacat", "_dcat"}:
                if value not in {"", "0", params.get("_sacat")}:
                    raise ValueError("The link contains conflicting eBay categories.")
            elif key in ASPECTS | {"LH_ItemCondition"}:
                if (
                    not set(value.split("|")) <= set(params.get(key, "").split("|"))
                    or not value
                ):
                    raise ValueError(
                        "eBay did not confirm the selected " + key + " filter."
                    )
            elif key == "_nkw":
                if key in params and params[key] and value and params[key] != value:
                    raise ValueError("eBay supplied conflicting search text.")
                if value:
                    params[key] = value
            else:
                if key in params and not _same_scalar(key, value, params[key]):
                    raise ValueError(
                        "eBay did not confirm the selected " + key + " filter."
                    )
                params[key] = value
    converted = "https://www.ebay.co.uk/sch/i.html?" + urlencode(sorted(params.items()))
    # Validate every inferred aspect/value and global option with the unchanged
    # production importer before caching anything. Never cache HTML or sessions.
    converted = parse_link(converted)["search_url"]
    with _cache_lock:
        _cache[cache_key] = (time.monotonic() + CACHE_SECONDS, converted)
        _cache.move_to_end(cache_key)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return converted


def import_link(link, *, keywords=None, ignore_prices=False):
    return parse_link(
        resolve_link(link), keywords=keywords, ignore_prices=ignore_prices
    )
