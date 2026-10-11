"""Validate imported filters for an explicitly permitted public-search source.

Website access and the dashboard save gate are deliberately separate. These
helpers do not grant access, change settings, retry denials, or fetch pages.
"""

import json
import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

from lxml import etree, html

REMOTE_FIELDS = (
    "keywords",
    "category",
    "buying",
    "uk_only",
    "condition",
    "condition_ids",
    "aspects",
    "free_shipping",
)


def imported_search_url(config, page_size=60):
    from ebay_monitor import EbayError
    from ebay_search_link import parse_link

    try:
        validated = parse_link(config.get("search_url", ""))
    except ValueError:
        raise EbayError(
            "Saved eBay link needs review; no public fetch made.", 300
        ) from None
    if any(validated.get(key) != config.get(key) for key in REMOTE_FIELDS):
        raise EbayError(
            "Saved eBay link and filters differ; no public fetch made.", 300
        )
    if not validated["keywords"]:
        raise EbayError(
            "This blank-keyword search needs Browse API. eBay's public category "
            "page has no supported newest-first inventory; other searches can continue.",
            300,
        )
    params = dict(parse_qsl(urlsplit(validated["search_url"]).query))
    params.pop("_udlo", None)
    params.pop("_udhi", None)
    for key in validated.get("aspects", {}):
        value = params.pop(key)
        params[quote(key, safe="")] = quote(value, safe="")
    params.update(_sop="10", _ipg=str(page_size))
    return "https://www.ebay.co.uk/sch/i.html?" + urlencode(sorted(params.items()))


def same_remote_filters(config, landed_url):
    from ebay_search_link import parse_link

    try:
        landed = parse_link(landed_url, ignore_prices=True)
    except ValueError:
        return False
    return all(landed.get(key) == config.get(key) for key in REMOTE_FIELDS)


def decoded(value):
    for _ in range(3):
        new = unquote(value)
        if new == value:
            break
        value = new
    return value


def selected_filters(body, config):
    """Confirm page controls; an unchanged URL alone does not prove filtering."""
    doc = html.fromstring(bytes(body), parser=html.HTMLParser(encoding="utf-8"))
    selections, selected_formats = {}, set()
    for node in doc.xpath("//input[@checked]"):
        name, value = decoded(node.get("name", "")), decoded(node.get("value", ""))
        selections.setdefault(name, set()).add(value)
        if name == "su-selection-group__format":
            links = node.xpath("ancestor::a[@data-track][1]")
            if links:
                try:
                    track = json.loads(links[0].get("data-track"))
                    detail = decoded(
                        track.get("eventProperty", {}).get("moduledtl", "")
                    )
                    match = re.search(r"(?:^|\|)aspectURLParam:([^|]+)", detail)
                    if match:
                        selected_formats.add(match.group(1))
                except (ValueError, TypeError, AttributeError):
                    pass
    chips = {
        " ".join(node.text_content().split()).replace("Remove filter", "").strip()
        for node in doc.xpath(
            "//*[contains(concat(' ',normalize-space(@class),' '),"
            "' srp-multi-aspect__item--applied ')]"
        )
    }
    missing = []
    for name, values in (config.get("aspects") or {}).items():
        actual = selections.get("su-selection-group__aspect-" + name, set())
        if not set(values).issubset(actual) and not all(
            name + ": " + value in chips for value in values
        ):
            missing.append(name)
    conditions = set(config.get("condition_ids") or [])
    if conditions and not conditions.issubset(
        selections.get("su-selection-group__condition", set())
    ):
        missing.append("condition")
    if config.get("uk_only") and "1" not in selections.get(
        "su-selection-group__location", set()
    ):
        missing.append("UK location")
    expected = {"fixed": "LH_BIN", "auction": "LH_Auction", "both": "LH_All"}.get(
        config.get("buying")
    )
    if expected and expected not in selected_formats:
        missing.append("listing type")
    if config.get("free_shipping"):
        missing.append("free postage (page evidence not qualified)")
    return {"confirmed": not missing, "missing": missing}


def require_selected_filters(body, config):
    from ebay_monitor import EbayError

    try:
        evidence = selected_filters(body, config)
    except (ValueError, etree.ParserError):
        raise EbayError("Saved eBay page filters could not be read.", 30) from None
    if not evidence["confirmed"]:
        raise EbayError(
            "eBay did not confirm the saved "
            + ", ".join(evidence["missing"])
            + " filters; no listings processed.",
            300,
        )
