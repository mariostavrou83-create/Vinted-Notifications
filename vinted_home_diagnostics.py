"""Count known Home page features without logging URLs, text or account data."""

import logging
import re
from urllib.parse import parse_qs, urlsplit


def empty_page_shape(tree):
    from vinted_home_feed import MAX_ITEMS, _money, _react_moves, _streamed_home_segments, _text, item_url

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
    # Count only structural React markers. Never log an identifier or statement.
    boundaries = tree.xpath('//template[starts-with(@id,"B:")]')
    pending_markers = {"$?", "$~", "$", "$!"}
    segment_ids = {node.get("id") for node in tree.xpath('//div[@hidden and starts-with(@id,"S:")]')}
    card_segments = {node.get("id") for card in native for node in card.iterancestors()
                     if node.get("id") in segment_ids}
    calls = re.findall(r"""\$RC\(\s*["'](B:[\w:-]+)["']\s*,\s*["'](S:[\w:-]+)["']""", "\n".join(scripts))
    inserts = re.findall(r"""\$RS\(\s*["'](S:[\w:-]+)["']\s*,\s*["'](P:[\w:-]+)["']""", "\n".join(scripts))
    counts.update(
        stream_segments=len(segment_ids), card_segments=len(card_segments),
        rc_pairs=len(calls), rc_card_pairs=sum(seg in card_segments for _, seg in calls),
        rs_pairs=len(inserts), rs_card_pairs=sum(seg in card_segments for seg, _ in inserts),
        insert_placeholders=len(tree.xpath('//template[starts-with(@id,"P:")]')),
        rc_referenced_boundaries=sum(any(node.get("id") == bid for node in boundaries) for bid, _ in calls),
        nested_boundaries=sum(bool(node.xpath('ancestor::*[@hidden]')) for node in boundaries),
        pending_boundaries=sum(getattr(node.getprevious(), "text", "") in pending_markers for node in boundaries),
    )
    parsed_moves = {pair for script in scripts for pair in _react_moves(script)}
    resolved = _streamed_home_segments(tree)
    known_targets = {src: dest for dest, src in calls}
    known_targets.update(inserts)
    counts.update(parsed_moves=len(parsed_moves),
                  parsed_card_moves=sum(src in card_segments for _, src in parsed_moves),
                  resolved_segments=len(resolved),
                  resolved_card_segments=sum(node.get("id") in card_segments for node in resolved))
    for segment_id in card_segments:
        visited = set()
        while segment_id in known_targets and segment_id not in visited:
            visited.add(segment_id)
            dest = known_targets[segment_id]
            matches = tree.xpath('//*[@id=$target]', target=dest)
            if len(matches) != 1:
                break
            slot = matches[0]
            counts["card_target_is_template"] = int(slot.tag == "template")
            hosts = [ancestor for ancestor in slot.iterancestors() if "hidden" in ancestor.attrib]
            if not hosts:
                counts["card_chain_to_body"] = int(bool(slot.xpath("ancestor::body")))
                counts["card_chain_to_main"] = int(bool(slot.xpath('ancestor::*[self::main or @role="main"]')))
                break
            segment_id = hosts[0].get("id")
        counts["card_chain_hops"] = len(visited)
    # Fixed schema names only. Values, paths and script contents stay private.
    for key in ("recommendedItems", "recommended_items", "homepageItems",
                "homepage_items", "feedItems", "feed_items", "homepageFeed",
                "itemBox", "catalog_id", "catalogId", "feed_item", "grid-item"):
        counts[key] = sum(key in script for script in scripts)
    logging.getLogger(__name__).info("MSJ Home page shape: %s", " ".join(
        f"{key}={value}" for key, value in counts.items()
    ))
    return counts
