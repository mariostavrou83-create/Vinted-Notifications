"""Offline regressions using UK DOM selectors observed 20 November 2026."""

import json
import unittest

import vinted_home_feed as feed


def native(**changes):
    item = {
        "id": "123",
        "seller_id": "456",
        "title": "Fitted top",
        "price": {"amount": "5.00", "currency_code": "GBP"},
        "can_buy": True,
        "is_reserved": False,
        "is_hidden": False,
        "brand_id": 88,
        "catalog_id": 222,
        "size_id": 3,
        "status_id": 2,
        "color1_id": 1,
        "photos": [{"url": "https://images1.vinted.net/t/01_test/f800/image.webp"}],
        "description": "Fitted cotton top in very good condition.",
        **changes,
    }
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(item)
        + "</script>"
    )


def listing(age="1 min ago", *, identity="123", summary=None, extra="", **changes):
    return f"""<html><head><link rel="canonical" href="https://www.vinted.co.uk/items/{identity}-top"></head><body>
    <main><ul><li><a href="/catalog/1904-women?referrer=item-crumbs">Women</a></li>
    <li><a href="/catalog/222-tops?referrer=item-crumbs">Tops</a></li></ul>
    <aside id="sidebar"><div class="item-page-sidebar-content">
    <div data-testid="item-page-summary-plugin"><h1>Fitted top</h1><span>Uploaded {summary or age}</span></div>
    <div data-testid="item-price">£5.00</div><div itemprop="upload_date">{age}</div>
    <a href="/brand/88-ralph-lauren">Ralph Lauren</a><div itemprop="size">S / UK 8-10</div>
    <div itemprop="description">Fitted cotton top in very good condition.</div>
    <div itemprop="condition">Very good</div>{extra}</div></aside></main>
    {native(**changes)}</body></html>"""


def card(identity="123", **changes):
    values = {
        "href": f"/items/{identity}-fitted-top?homepage_session_id=private-telemetry",
        "title": "Fitted top, brand: Ralph Lauren, condition: Very good, size: S / UK 8-10, 5.00 £, 5.95 £ Includes Vinted fee",
        "price": "£5.00",
        **changes,
    }
    return f"""<div data-testid="feed-item"><div>
    <img data-testid="feed-item--image--img" src="https://images1.vinted.net/t/01_test/f800/image.webp">
    <a data-testid="feed-item--overlay-link" href="{values['href']}" title="{values['title']}"></a></div>
    <div data-testid="feed-item--description-title">Ralph Lauren</div>
    <div data-testid="feed-item--description-subtitle">S / UK 8-10 · Very good</div>
    <div data-testid="feed-item--price-text">{values['price']}</div>
    <div data-testid="total-combined-price">£5.95</div></div>"""


def home(body, navigation='<a href="/inbox">Inbox</a>'):
    return f"<html><header>{navigation}</header><main>{body}</main></html>"


class HomeFeedTests(unittest.TestCase):
    def test_visible_home_cards_only_and_no_telemetry_returned(self):
        result = feed.parse_home(home(card() + native(id="999")))
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["reason"], "ok")
        self.assertEqual([x["id"] for x in result["items"]], ["123"])
        item = result["items"][0]
        self.assertEqual(item["title"], "Fitted top")
        self.assertEqual(item["price"]["amount"], "5.00")
        self.assertEqual(item["size_title"], "S / UK 8-10")
        self.assertEqual(item["total_item_price"]["amount"], "5.95")
        self.assertNotIn("private-telemetry", json.dumps(result))
        self.assertFalse(item["description_complete"])

    def test_marked_home_link_survives_card_selector_changes(self):
        markup = card().replace("feed-item", "different-card")
        result = feed.parse_home(home(markup + native(id="999")).replace("<main>", '<div role="main">').replace("</main>", "</div>"))
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual((item["id"], item["title"], item["brand_title"], item["size_title"]),
                         ("123", "Fitted top", "Ralph Lauren", "S / UK 8-10"))
        self.assertEqual((item["price"]["amount"], item["total_item_price"]["amount"]),
                         ("5.00", "5.95"))
        self.assertNotIn("private-telemetry", json.dumps(result))
        self.assertNotIn("home_created_at", item)
        self.assertNotIn("category_ids", item)
        # Matching native metadata can still corroborate freshness for this visible ID.
        enriched = feed.parse_home(home(markup + native(created_at_ts=1795443600)))["items"][0]
        self.assertEqual(enriched["home_created_at"], 1795443600)

    def test_caption_fallback_needs_authenticated_visible_attributed_home_link(self):
        markup = card().replace("feed-item", "different-card")
        for body in (
            home(markup, '<a href="/member/login">Login</a>'),
            home(markup.replace("homepage_session_id", "other")),
            home(markup.replace("/items/123", "https://evil.invalid/items/123")),
            home(markup.replace('£ Includes Vinted fee', 'EUR Includes Vinted fee')),
            home(markup.replace("5.95 £", "4.95 £")),
            home("<template>" + markup + "</template>"),
            home('<div aria-hidden="true">' + markup + "</div>"),
            home("").replace("</body>", markup + "</body>"),
            home(card(price="£17.50").replace("feed-item--overlay-link", "other")),
        ):
            with self.subTest(body=body[:80]):
                self.assertEqual(feed.parse_home(body)["items"], [])

    def test_anonymous_or_unknown_home_cannot_supply_recommendations(self):
        for links, reason in [
            (
                '<a href="/member/signup/select_type">Sign up | Log in</a>',
                "home_not_authenticated",
            ),
            ("", "home_auth_unverified"),
        ]:
            result = feed.parse_home(home(card(), links))
            self.assertEqual(result["items"], [])
            self.assertEqual(result["reason"], reason)

    def test_duplicate_external_bad_prices_and_oversize_fail_closed(self):
        body = (
            card()
            + card()
            + card("2", href="https://evil.invalid/items/2")
            + card("3", price="£NaN")
        )
        self.assertEqual(len(feed.parse_home(home(body))["items"]), 1)
        self.assertEqual(feed.parse_home("x" * (feed.MAX_HTML + 1))["items"], [])
        self.assertEqual(feed.parse_home(home(native()))["items"], [])

    def test_non_listing_and_credential_urls_rejected(self):
        for url in [
            "https://user:secret@www.vinted.co.uk/items/123",
            "//evil.invalid/items/123",
            "https://www.vinted.co.uk:444/items/123",
            "/member/123",
            "javascript:alert(1)",
        ]:
            self.assertIsNone(feed.item_url(url))

    def test_current_age_abbreviations_use_conservative_upper_bound(self):
        for text, expected in [
            ("just now", (0, 60)),
            ("0 min ago", (0, 60)),
            ("1 min ago", (60, 120)),
            ("2 minutes ago", (120, 180)),
            ("3 min ago", (180, 240)),
            ("Uploaded 10 min ago", (600, 660)),
            ("a day ago", (86400, 172800)),
            ("59 sec ago", (59, 60)),
        ]:
            self.assertEqual(feed.age_bounds(text), expected)
        for text in [
            "today",
            "3",
            "3 mins",
            "-1 min ago",
            "Last seen 1 min ago",
            "Updated 1 min ago",
            "a few minutes ago",
        ]:
            self.assertIsNone(feed.age_bounds(text))

    def test_freshness_is_not_first_seen_and_three_rounded_minutes_rejected(self):
        fresh = feed.listing_age(listing(), "123", 1000)
        self.assertTrue(fresh["verified"])
        self.assertTrue(fresh["fresh"])
        self.assertEqual(fresh["max_age_seconds"], 120)
        old = feed.listing_age(listing("3 min ago"), "123", 1000)
        self.assertTrue(old["verified"])
        self.assertFalse(old["fresh"])
        self.assertEqual(old["max_age_seconds"], 240)
        self.assertFalse(
            feed.listing_age(native(created_at_ts=999, first_seen=999), "123", 1000)[
                "verified"
            ]
        )
        for minutes, maximum, accepted in [
            (0, 60, True),
            (1, 120, True),
            (2, 180, True),
            (3, 240, False),
        ]:
            age = feed.listing_age(listing(f"{minutes} min ago"), "123", 1000)
            self.assertEqual(age["max_age_seconds"], maximum)
            self.assertEqual(age["fresh"], accepted)

    def test_home_creation_time_requires_matching_listing_and_consistent_age_evidence(self):
        now = 1795443600
        target = {"id": "123", "home_created_at": now - 151}
        for body, home_card, upper in [
            (listing("2 min ago"), target, 151),
            (listing("2 min ago"), dict(target, id="124"), 180),
            (listing("2 min ago"), {"id": "123", "first_seen": now - 151}, 180),
            (listing("2 min ago"), dict(target, home_created_at=now + 2), 180),
            (listing("2 min ago"), dict(target, home_created_at=now - 180), 180),
            (listing("2 min ago", created_at_ts=now-123), target, 180),
            (listing("3 min ago"), target, 240),
        ]:
            with self.subTest(card=home_card, upper=upper):
                result = feed.parse_listing(body, "123", now, home_card=home_card)
                self.assertEqual(result["age"]["max_age_seconds"], upper)

    def test_wrong_item_conflicting_missing_or_duplicate_age_is_unverified(self):
        for body in [
            listing(identity="999"),
            listing(summary="2 min ago"),
            listing().replace('itemprop="upload_date"', 'itemprop="last_seen"'),
            listing(extra='<span itemprop="upload_date">1 min ago</span>'),
            listing().replace("www.vinted.co.uk", "evil.invalid"),
            listing("unknown"),
        ]:
            self.assertFalse(feed.listing_age(body, "123", 1000)["verified"])
        for now in [True, float("nan"), float("inf"), -1]:
            self.assertFalse(feed.listing_age(listing(), "123", now)["verified"])

    def test_listing_details_bound_to_same_item_and_structural_ids(self):
        result = feed.parse_listing(listing(), "123", 1000)
        self.assertEqual(result["reason"], "ok")
        item = result["item"]
        self.assertEqual(item["id"], "123")
        self.assertEqual(item["seller_id"], "456")
        self.assertEqual(item["brand_id"], "88")
        self.assertEqual(item["size_id"], "3")
        self.assertEqual(item["category_ids"], ["1904", "222"])
        self.assertEqual(item["condition_id"], "2")
        self.assertEqual(item["color_ids"], ["1"])
        self.assertTrue(item["description_complete"])
        self.assertEqual(
            item["description"], "Fitted cotton top in very good condition."
        )

    def test_unavailable_price_conflict_brand_conflict_and_wrong_native_identity_reject(
        self,
    ):
        cases = [
            {"id": "999"},
            {"is_reserved": True},
            {"is_sold": True},
            {"can_buy": False},
            {"price": {"amount": "6.00", "currency_code": "GBP"}},
            {"brand_id": 99},
        ]
        for change in cases:
            self.assertIsNone(
                feed.parse_listing(listing(**change), "123", 1000)["item"]
            )

    def test_neighbor_metadata_is_not_borrowed_and_incomplete_description_excluded(
        self,
    ):
        body = listing().replace(
            native(), native(brand_id=88, size_id=None) + native(id="999", size_id=7)
        )
        item = feed.parse_listing(body, "123", 1000)["item"]
        self.assertNotIn("size_id", item)
        long = "x" * 6001
        body = listing().replace("Fitted cotton top in very good condition.", long)
        self.assertFalse(
            feed.parse_listing(body, "123", 1000)["item"]["description_complete"]
        )

    def test_all_description_text_is_retained_for_exclusions_and_total_is_observed(
        self,
    ):
        description = "Cotton " * 700 + "damaged hem"
        body = listing(extra='<span data-testid="total-combined-price">£5.95</span>')
        body = body.replace("Fitted cotton top in very good condition.", description)
        item = feed.parse_listing(body, "123", 1000)["item"]
        self.assertTrue(item["description_complete"])
        self.assertTrue(item["description"].endswith("damaged hem"))
        self.assertEqual(item["total_item_price"]["amount"], "5.95")


if __name__ == "__main__":
    unittest.main()
