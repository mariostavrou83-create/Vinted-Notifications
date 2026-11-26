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
    return f"<html><body><header>{navigation}</header><main>{body}</main></body></html>"


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

    def test_titleless_personalised_card_preserves_id_for_exact_detail(self):
        markup = card(title="").replace('title=""', "").replace("<img ", '<img alt="Product image" ')
        items = feed.parse_home(home(markup + native(id="999")))["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["id"], items[0]["title"]), ("123", ""))
        self.assertNotIn("home_available", items[0])
        self.assertNotIn("home_created_at", items[0])
        self.assertNotIn("category_ids", items[0])
        self.assertEqual(feed.parse_home(home(markup.replace("homepage_session_id", "other")))["items"], [])
        for wrapper in ('<div hidden>{}</div>', '<div inert>{}</div>', '<span aria-hidden="true">{}</span>',
                        '<template>{}</template>', '<noscript>{}</noscript>'):
            self.assertEqual(feed.parse_home(home(wrapper.format(markup) + native()))["items"], [])
        outside_main = home("").replace("</body>", markup + native() + "</body>")
        self.assertEqual(feed.parse_home(outside_main)["items"][0]["id"], "123")

    def test_native_home_card_with_accessibility_hidden_click_layer_outside_main(self):
        markup = card(title="").replace(
            '<a data-testid="feed-item--overlay-link"', '<a aria-hidden="true" data-testid="feed-item--overlay-link"'
        )
        for rendered in (markup, markup.replace('<div>\n    <img', '<div aria-hidden="true">\n    <img')):
            page = home("").replace("</body>", rendered + native() + "</body>")
            self.assertEqual(feed.parse_home(page)["items"][0]["title"], "Fitted top")
        for blocked in ('<header>{}</header>', '<footer>{}</footer>', '<nav>{}</nav>', '<aside>{}</aside>',
                        '<div role="navigation">{}</div>', '<div style="display: none">{}</div>'):
            with self.subTest(blocked=blocked):
                self.assertEqual(feed.parse_home(home(blocked.format(markup)))["items"], [])
        for attr in ("hidden", "inert"):
            self.assertEqual(feed.parse_home(home(markup.replace('<a aria-hidden=', f'<a {attr} aria-hidden=')))["items"], [])

    def test_blank_native_home_attribution_requires_main_or_visible_selected_all_tab(self):
        markup = card(title="", href="/items/123-fitted-top?homepage_session_id=")
        tab = '<button role="tab" aria-selected="true" aria-controls="products">All items</button>'
        panel = '<div role="tabpanel" id="products">' + markup + '</div>'
        self.assertEqual(feed.parse_home(home(markup))["items"][0]["id"], "123")
        page = home("").replace("</body>", tab + panel + "</body>")
        self.assertEqual(feed.parse_home(page)["items"][0]["id"], "123")
        for rejected in (page.replace(tab, ""), page.replace("All items", "Menswear"),
                         page.replace('aria-selected="true"', 'aria-selected="false"'),
                         page.replace("homepage_session_id=", "")):
            self.assertEqual(feed.parse_home(rejected)["items"], [])

    def test_react_pending_boundary_must_explicitly_reveal_exact_native_card_segment(self):
        anchor = '<!--$?--><template id="B:1"></template><!--/$-->'
        markup = '<div hidden id="S:1">' + card(title="") + '</div>'
        instruction = '<script>$RC("B:1","S:1")</script>'
        def rendered(boundary, content, call):
            return home(boundary).replace("</body>", content + call + "</body>")
        for call in (instruction, '<script>$RC=function(a,b){f(a,b)};$RC("B:1","S:1");</script>'):
            self.assertEqual(feed.parse_home(rendered(anchor, markup, call))["items"][0]["id"], "123")
        for page in (
            rendered(anchor, markup, ""),
            rendered(anchor + anchor, markup, instruction),
            rendered(anchor, markup + markup, instruction),
            rendered(anchor, markup, instruction.replace("S:1", "S:2")),
            rendered(anchor.replace("<!--$?-->", ""), markup, instruction),
            rendered("<nav>" + anchor + "</nav>", markup, instruction),
            rendered(anchor, markup.replace('<div data-testid="feed-item"', '<div hidden data-testid="feed-item"'), instruction),
            rendered(anchor, markup, instruction.replace("<script>", '<script type="application/json">')),
            rendered(anchor, markup, '<script>if(false) $RC("B:1","S:1")</script>'),
            rendered(anchor, markup, '<script>//};$RC("B:1","S:1")</script>'),
        ):
            with self.subTest(page=page[-80:]):
                self.assertEqual(feed.parse_home(page)["items"], [])

    def test_nested_stream_segments_project_card_into_home_across_suspended_slots(self):
        anchor = '<!--$?--><template id="B:0"></template><!--/$-->'
        branch = '<div hidden id="S:0"><section><template id="P:1"></template></section></div>'
        markup = card(title="", href="/items/123-fitted-top?homepage_session_id=")
        cards = '<div hidden id="S:2">' + markup + '</div>'
        calls = '<script>var $RB=[],$RC=function(a,b){f(a,b)};requestAnimationFrame(()=>{$RS("S:2","P:1")});$RC("B:0","S:0");after()</script>'
        def render(boundary=anchor, wrapper=branch, script=calls):
            return home(boundary).replace("</body>", wrapper + cards + script + "</body>")
        item = feed.parse_home(render())["items"][0]
        self.assertEqual((item["id"], item["title"]), ("123", ""))
        for page in (
            render(wrapper=branch.replace("P:1", "P:9")),
            render(script=calls.replace('$RC("B:0","S:0");', "")),
            render(boundary="<aside>" + anchor + "</aside>"),
            render(wrapper=branch.replace("<section>", "<section hidden>")),
            render(script=calls.replace('requestAnimationFrame(()=>{', 'registerCallback(()=>{')),
            render(script=calls.replace('requestAnimationFrame(()=>{', 'if(false) requestAnimationFrame(()=>{')),
        ):
            self.assertEqual(feed.parse_home(page)["items"], [])

    def test_react_19_runtime_and_direct_and_frame_completions(self):
        strings = [
            'var $RB=[],$RV=function(q){work(q)},$RC=function(a,b){if(b){next(a,b)}};$RC("B:0","S:0")',
            'requestAnimationFrame(function(){$RC("B:0","S:0")})',
            "requestAnimationFrame(()=>requestAnimationFrame(()=>$RC('B:0','S:0')))",
            '$RC("B:0","S:0"),$RC("B:1","S:1")',
        ]
        for code in strings:
            self.assertIn(("B:0", "S:0"), list(feed._react_moves(code)))
        for code in ("""let s='$RC("B:0","S:0")'""", 'if(false){$RC("B:0","S:0")}',
                     '/*$RC("B:0","S:0")*/', 'log($RC("B:0","S:0"))'):
            self.assertEqual(list(feed._react_moves(code)), [])

    def test_titleless_card_gets_only_unambiguous_same_item_same_price_native_title(self):
        stamp = 1795443600
        markup = card(title="")
        for payload, expected in (
            (native(created_at_ts=stamp), "Fitted top"),
            (native(title=" ") + native(id="999"), ""),
            (native(created_at_ts=stamp, price={"amount":"9.00","currency_code":"GBP"}), ""),
            (native(created_at_ts=stamp, item_id="999"), ""),
            (native(created_at_ts=stamp) + native(title="Other top", created_at_ts=stamp), ""),
        ):
            with self.subTest(expected=expected, payload=payload[:100]):
                result = feed.parse_home(home(markup + payload))["items"][0]
                self.assertEqual(result["title"], expected)
                if expected:
                    self.assertEqual(result["home_created_at"], stamp)
                    self.assertTrue(result["home_available"])
                else:
                    self.assertNotIn("home_created_at", result)
                    self.assertNotIn("category_ids", result)

    def test_exact_listing_requires_one_nonempty_title(self):
        for heading in ("<h1></h1>", "<h1>  </h1>", "<h1>A</h1><h1>B</h1>", "<h1>" + "x" * 501 + "</h1>"):
            result = feed.parse_listing(listing().replace("<h1>Fitted top</h1>", heading), "123", 1000)
            self.assertIsNone(result["item"])
            self.assertEqual(result["reason"], "title_unverified")

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
