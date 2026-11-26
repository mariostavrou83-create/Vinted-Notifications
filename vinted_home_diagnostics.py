"""Count known Home page features without logging URLs, text or account data."""

import logging
from urllib.parse import parse_qs, urlsplit


def empty_page_shape(tree):
    from vinted_home_feed import MAX_ITEMS, _money, _text, item_url

    anchors = tree.xpath('//a[@href]')
    item_links = [a for a in anchors if "/items/" in a.get("href")]
    home_links = [a for a in item_links if "homepage_session_id=" in a.get("href")]
    scripts = [node.text or "" for node in tree.iter("script")]
    selectors = {
        "main": "//main | //*[@role='main']",
        "card": "//*[@data-testid='feed-item']",
        "overlay": "//*[@data-testid='feed-item--overlay-link']",
        "price": "//*[@data-testid='feed-item--price-text']",
        "grid_item": "//*[@data-testid='grid-item']",
        "item_box": "//*[contains(@class,'new-item-box')]",
    }
    counts = {key: len(tree.xpath(selector)) for key, selector in selectors.items()}
    native = tree.xpath(selectors["card"])[:MAX_ITEMS]
    counts.update(dict.fromkeys((
        "cards_with_uk_link_and_price", "home_cards_with_uk_link_and_price", "native_in_main",
        "native_in_body", "native_in_secondary_region", "native_aria_hidden", "overlay_aria_hidden",
        "native_html_hidden", "native_inert", "native_in_template", "native_stream_segment",
    ), 0))
    for card in native:
        overlays = card.xpath('.//a[@data-testid="feed-item--overlay-link"]/@href')
        prices = card.xpath('.//*[@data-testid="feed-item--price-text"]')
        if len(overlays) == len(prices) == 1 and item_url(overlays[0]) and _money(_text(prices[0])):
            counts["cards_with_uk_link_and_price"] += 1
            if parse_qs(urlsplit(overlays[0]).query).get("homepage_session_id"):
                counts["home_cards_with_uk_link_and_price"] += 1
        for key, predicate in (
            ("native_in_main", 'ancestor::*[self::main or @role="main"]'),
            ("native_in_body", 'ancestor::body'),
            ("native_in_secondary_region", 'ancestor::*[self::aside or self::nav or self::footer or self::header]'),
            ("native_aria_hidden", 'ancestor-or-self::*[@aria-hidden="true"]'),
            ("overlay_aria_hidden", './/a[@data-testid="feed-item--overlay-link"]/ancestor-or-self::*[@aria-hidden="true"]'),
            ("native_html_hidden", 'ancestor-or-self::*[@hidden]'),
            ("native_inert", 'ancestor-or-self::*[@inert]'),
            ("native_in_template", 'ancestor::*[self::template or self::noscript]'),
            ("native_stream_segment", 'ancestor::*[@hidden and starts-with(@id,"S:")]'),
        ):
            counts[key] += int(card.xpath(f"boolean({predicate})"))
    counts.update(
        stream_placeholders=len(tree.xpath('//template[starts-with(@id,"B:")]')),
        completion_scripts=sum("$RC(" in script for script in scripts),
        item_links=len(item_links),
        home_links=len(home_links),
        home_captions=sum(bool(a.get("aria-label") or a.get("title")) for a in home_links),
        scripts_with_home_links=sum("homepage_session_id" in script for script in scripts),
        scripts_with_items=sum("/items/" in script for script in scripts),
        scripts_with_feed_cards=sum("feed-item" in script for script in scripts),
        scripts_with_recommendations=sum("recommend" in script.lower() for script in scripts),
    )
    # Fixed schema names only. Values, paths and script contents stay private.
    for key in ("recommendedItems", "recommended_items", "homepageItems",
                "homepage_items", "feedItems", "feed_items", "homepageFeed",
                "itemBox", "catalog_id", "catalogId", "feed_item", "grid-item"):
        counts[key] = sum(key in script for script in scripts)
    logging.getLogger(__name__).info("MSJ Home page shape: %s", " ".join(
        f"{key}={value}" for key, value in counts.items()
    ))
    return counts
