"""Translate explicit eBay UK search URL filters, without fetching the URL.

Unknown filters fail closed. Website navigation/tracking parameters are discarded;
item aspects are sent to Browse's category-scoped aspect_filter, never to q.
"""

import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

ASPECTS = {
    "Brand",
    "Size",
    "Size Type",
    "Style",
    "Colour",
    "Color",
    "Department",
    "Fit",
    "Material",
    "Fabric Type",
    "Features",
    "Accents",
    "Theme",
    "Vintage",
    "Pattern",
    "Inside Leg",
    "Rise",
    "Closure",
    "Waist Size",
    "Model",
    "Season",
    "Sleeve Length",
    "Neckline",
    "Type",
    "Occasion",
    "Character",
    "Product Line",
    "Dress Length",
    "Skirt Length",
    "Garment Care",
    "Country/Region of Manufacture",
}
IGNORED = {
    "_from",
    "_trksid",
    "_trkparms",
    "_sop",
    "_ipg",
    "_pgn",
    "_dmd",
    "_oac",
    "_odkw",
    "_osacat",
    "_fsrp",
    "rt",
    "mkcid",
    "mkrid",
    "campid",
    "toolid",
    "customid",
    "mkevt",
    "siteid",
    "norover",
    "mkscid",
}
FIELDS = {
    "_nkw",
    "_sacat",
    "_dcat",
    "_udlo",
    "_udhi",
    "LH_BIN",
    "LH_Auction",
    "LH_All",
    "LH_ItemCondition",
    "LH_PrefLoc",
    "LH_FS",
    "_sacurrency",
}


def clean(value):
    # eBay commonly double-encodes aspect spaces (%2520) and separators (%257C).
    value = unquote(value).strip()
    if len(value) > 500 or any(ord(c) < 32 for c in value):
        raise ValueError("An eBay filter is too long or contains invalid characters.")
    return value


def aspect_filter(config):
    aspects = config.get("aspects") or {}
    if not aspects:
        return ""
    category = config.get("category", "")
    if not re.fullmatch(r"[1-9][0-9]{0,9}", category):
        raise ValueError("Choose an eBay category before filtering by brand or size.")
    parts = ["categoryId:" + category]
    for key, values in sorted(aspects.items()):
        if (
            key not in ASPECTS
            or not isinstance(values, list)
            or not 1 <= len(values) <= 30
        ):
            raise ValueError("This eBay item-specific filter is not supported.")
        for value in values:
            if (
                not isinstance(value, str)
                or not value
                or re.search(r"[{},:|\\\x00-\x1f]", value)
            ):
                raise ValueError(
                    "An eBay item-specific value contains unsupported punctuation."
                )
        parts.append(key + ":{" + "|".join(values) + "}")
    return ",".join(parts)


def parse_link(link, *, keywords=None, ignore_prices=False):
    """Import supported filters; explicit shared-form overrides leave legacy intact."""
    from dashboard_store import parse_money
    from ebay_store import DEFAULTS

    link = link.strip()
    if len(link) > 6000:
        raise ValueError("Keep the eBay search link under 6,000 characters.")
    try:
        url = urlsplit(link)
        valid = (
            url.scheme == "https"
            and url.hostname in {"ebay.co.uk", "www.ebay.co.uk"}
            and not url.username
            and not url.password
            and url.port in (None, 443)
            and not url.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            "Paste a full https://www.ebay.co.uk search-results link, not a short/share link."
        )
    path = re.fullmatch(r"/sch/(?:(\d{1,10})/)?i\.html", url.path)
    if not path:
        raise ValueError(
            "Use an eBay UK search-results link (/sch/…/i.html), not an item, shop or /b/ page."
        )
    params = {}
    for key, value in parse_qsl(url.query, keep_blank_values=True, max_num_fields=100):
        key = clean(key)
        value = clean(value)
        if key in params and value != params[key]:
            raise ValueError(
                "The link repeats a filter with different values. Copy a fresh eBay search link."
            )
        params[key] = value
    unsupported = sorted(set(params) - FIELDS - ASPECTS - IGNORED)
    if unsupported:
        raise ValueError(
            "Cannot import these filters safely: "
            + ", ".join(unsupported)
            + ". Remove them on eBay and copy the updated link. Nothing has been saved."
        )
    if keywords is not None:
        params["_nkw"] = keywords
    if ignore_prices:
        # The dashboard's complete budget replaces both URL item-price limits.
        params.pop("_udlo", None)
        params.pop("_udhi", None)
    config = dict(DEFAULTS, filter_mode="url", buying="both", uk_only=False)
    config["keywords"] = params.get("_nkw", "")
    if len(config["keywords"]) > 100:
        raise ValueError("Use eBay search keywords up to 100 characters.")
    categories = {
        c
        for c in [path[1], params.get("_sacat"), params.get("_dcat")]
        if c and c != "0"
    }
    if len(categories) > 1:
        raise ValueError(
            "The link contains conflicting categories. Choose one category on eBay and copy it again."
        )
    category = next(iter(categories), "")
    if category and not re.fullmatch(r"[1-9][0-9]{0,9}", category):
        raise ValueError("Choose a single numeric eBay category.")
    config["category"] = category
    if not config["keywords"] and not category:
        raise ValueError("The link needs keywords or a selected eBay category.")
    if params.get("_sacurrency", "GBP") != "GBP":
        raise ValueError("Choose GBP prices on eBay UK before copying the link.")
    config["min_price"] = parse_money(params.get("_udlo", ""))
    config["max_price"] = parse_money(params.get("_udhi", ""))
    if (
        config["min_price"] is not None
        and config["max_price"] is not None
        and config["min_price"] > config["max_price"]
    ):
        raise ValueError("The eBay price range must start with the lower price.")
    for key in ("LH_BIN", "LH_Auction", "LH_All", "LH_FS"):
        if params.get(key, "") not in {"", "0", "1"}:
            raise ValueError(
                "Unsupported value for " + key + ". Copy a fresh filtered search link."
            )
    selected = [
        key for key in ("LH_BIN", "LH_Auction", "LH_All") if params.get(key) == "1"
    ]
    if len(selected) > 1:
        raise ValueError(
            "Choose one listing type on eBay before copying its search link."
        )
    config["buying"] = {"LH_BIN": "fixed", "LH_Auction": "auction"}.get(
        next(iter(selected), ""), "both"
    )
    location = params.get("LH_PrefLoc", "")
    if location not in {"", "0", "1", "3"}:
        raise ValueError(
            "This location filter cannot be imported. Choose UK only or Worldwide on eBay."
        )
    config["uk_only"] = location == "1"
    config["free_shipping"] = params.get("LH_FS") == "1"
    conditions = sorted(set(params.get("LH_ItemCondition", "").split("|")) - {""})
    if any(
        c
        not in {
            "1000",
            "1500",
            "1750",
            "2000",
            "2010",
            "2020",
            "2030",
            "2500",
            "2750",
            "3000",
            "4000",
            "5000",
            "6000",
            "7000",
        }
        for c in conditions
    ):
        raise ValueError("This eBay condition filter is not supported.")
    config["condition_ids"] = conditions
    config["aspects"] = {
        key: sorted(set(value.split("|")))
        for key, value in params.items()
        if key in ASPECTS
    }
    aspect_filter(config)  # Validate category and delimiters before saving anything.
    canonical = {k: v for k, v in params.items() if k not in IGNORED and k != "_dcat"}
    if category:
        canonical["_sacat"] = category
    canonical["_sop"] = "10"
    config["search_url"] = "https://www.ebay.co.uk/sch/i.html?" + urlencode(
        sorted(canonical.items())
    )
    return config


def shared_keyword_query(words):
    """One bounded OR query, with each multiword alternative kept as a phrase.

    Browse silently truncates q after 100 characters. Reject rather than lose a
    saved alternative, and prohibit user input from introducing query operators.
    """
    if not words:
        return ""
    for word in words:
        if (
            re.search(r'[()\[\]{}*"\\:<>|]', word)
            or any(part.startswith("-") for part in word.split())
            or any(ord(char) < 32 for char in word)
        ):
            raise ValueError(
                "Use plain words or phrases for shared keywords, without eBay search operators."
            )
    terms = ['"' + word + '"' if " " in word else word for word in words]
    query = terms[0] if len(terms) == 1 else "(" + ",".join(terms) + ")"
    if len(query) > 100:
        raise ValueError(
            "These alternatives exceed eBay's 100-character search limit. Shorten them or split them into another alert."
        )
    return query


def describe(config):
    lines = ["Keywords: " + (config["keywords"] or "Any within the selected category")]
    lines.append("Category: " + (config["category"] or "All categories"))
    for name, values in sorted((config.get("aspects") or {}).items()):
        lines.append(name + ": " + " or ".join(values))
    if not config.get("aspects", {}).get("Brand"):
        lines.append("Brand: no separate brand filter")
    if not any(
        "Size" in key or key == "Inside Leg" for key in config.get("aspects", {})
    ):
        lines.append("Size: no size filter")
    lo, hi = config["min_price"], config["max_price"]
    lines.append(
        "Item price: "
        + (f"£{lo / 100:.2f}" if lo is not None else "£0")
        + " to "
        + (f"£{hi / 100:.2f}" if hi is not None else "no maximum")
        + " (postage excluded)"
    )
    lines.append(
        "Listing type: "
        + {
            "fixed": "Buy it now",
            "auction": "Auctions",
            "both": "Buy it now + auctions",
        }[config["buying"]]
    )
    lines.append(
        "Condition IDs: "
        + (", ".join(config.get("condition_ids", [])) or "Any condition")
    )
    lines.append(
        "Location: "
        + ("UK only" if config["uk_only"] else "Any location; must ship to the UK")
    )
    if config.get("free_shipping"):
        lines.append("Postage: free only")
    lines.append("Monitoring: newest first, regardless of the link's display order")
    return lines
