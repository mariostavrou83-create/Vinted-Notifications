"""Count known Home page features without logging URLs, text or account data."""

import logging


def empty_page_shape(tree):
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
    counts.update(
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
