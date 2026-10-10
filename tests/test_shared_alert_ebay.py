"""Offline shared eBay criteria, complete-budget estimates and legacy isolation."""

import unittest
from contextlib import closing
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from test_ebay_monitor import EbayFixture, item
from test_search_controls import DatabaseFixture

import ebay_alerts
import ebay_monitor as monitor
import ebay_search_link as links
import ebay_store as store
import search_settings
import vinted_alerts

FILTER_LINK = (
    "https://www.ebay.co.uk/sch/57988/i.html?_nkw=discard+these+words"
    "&Brand=Hollister&Size=S%257CM&LH_ItemCondition=3000"
    "&LH_BIN=1&LH_PrefLoc=1&_udlo=80&_udhi=1"
)


def shared_config(maximum=2000, **changes):
    return dict(
        store.DEFAULTS,
        shared_alert_version=1,
        shared_keywords=["fur", "sherpa", "fur hood"],
        max_price=maximum,
        include_shipping=True,
        **changes,
    )


class SharedKeywordTests(unittest.TestCase):
    def test_words_and_multiword_alternatives_use_one_query(self):
        self.assertEqual(
            links.shared_keyword_query(["fur", "sherpa", "fur hood"]),
            '(fur,sherpa,"fur hood")',
        )
        self.assertEqual(links.shared_keyword_query(["fur hood"]), '"fur hood"')
        self.assertEqual(links.shared_keyword_query(["sherpa"]), "sherpa")
        self.assertEqual(links.shared_keyword_query([]), "")

    def test_exact_limit_is_accepted_and_overlong_serialization_is_rejected(self):
        self.assertEqual(len(links.shared_keyword_query(["a" * 100])), 100)
        with self.assertRaisesRegex(ValueError, "100-character"):
            links.shared_keyword_query(["a" * 97, "b"])
        with self.assertRaisesRegex(ValueError, "100-character"):
            links.shared_keyword_query(["a " + "b" * 98])

    def test_reserved_syntax_cannot_change_the_query(self):
        for value in [
            '"fur"',
            "(fur)",
            "fur*",
            "-teddy",
            "fur -hood",
            "brand:fur",
            "fur|hood",
            "fur\\hood",
            "fur[hood]",
            "fur\x00hood",
        ]:
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "plain words"
            ):
                links.shared_keyword_query([value])


class SharedFormTests(unittest.TestCase):
    def parse(self, **changes):
        form = {
            "query": "https://www.vinted.co.uk/catalog?brand_ids[]=179",
            "ebay_search_url": FILTER_LINK,
            "shared_keywords": "fur, sherpa\nfur hood, Fur",
        }
        form.update(changes)
        with patch.object(store, "configuration", return_value={"source": "browse"}):
            return store.parse_shared_form(form, maximum=2500)

    def test_new_form_replaces_url_words_and_prices_preserving_filters(self):
        vinted, ebay, config = self.parse()
        self.assertEqual((vinted, ebay), (1, 1))
        self.assertEqual(config["shared_alert_version"], 1)
        self.assertEqual(config["shared_keywords"], ["fur", "sherpa", "fur hood"])
        self.assertEqual(config["keywords"], '(fur,sherpa,"fur hood")')
        self.assertEqual((config["min_price"], config["max_price"]), (None, 2500))
        self.assertTrue(config["include_shipping"])
        params = monitor.search_params(config)
        self.assertEqual(params["q"], '(fur,sherpa,"fur hood")')
        self.assertEqual(params["category_ids"], "57988")
        self.assertEqual(
            params["aspect_filter"], "categoryId:57988,Brand:{Hollister},Size:{M|S}"
        )
        self.assertIn("itemLocationCountry:GB", params["filter"])
        self.assertIn("conditionIds:{3000}", params["filter"])
        self.assertIn("buyingOptions:{FIXED_PRICE}", params["filter"])
        canonical = parse_qs(urlsplit(config["search_url"]).query)
        self.assertNotIn("_udlo", canonical)
        self.assertNotIn("_udhi", canonical)
        self.assertEqual(canonical["_nkw"], ['(fur,sherpa,"fur hood")'])

    def test_empty_url_text_is_valid_when_shared_keywords_supply_criteria(self):
        _, _, config = self.parse(
            ebay_search_url="https://www.ebay.co.uk/sch/i.html?_nkw=&LH_BIN=1"
        )
        self.assertEqual(config["keywords"], '(fur,sherpa,"fur hood")')

    def test_blank_shared_keywords_clear_url_text_and_retain_category(self):
        _, _, config = self.parse(shared_keywords="")
        self.assertEqual(config["keywords"], "")
        self.assertNotIn("q", monitor.search_params(config))
        self.assertEqual(config["shared_keywords"], [])
        with self.assertRaisesRegex(ValueError, "keywords or a selected"):
            self.parse(
                shared_keywords="",
                ebay_search_url="https://www.ebay.co.uk/sch/i.html?_nkw=jeans",
            )

    def test_vinted_only_does_not_require_or_change_ebay_source(self):
        with patch.object(
            store,
            "configuration",
            side_effect=AssertionError("No eBay configuration read"),
        ):
            vinted, ebay, config = store.parse_shared_form(
                {
                    "query": "https://www.vinted.co.uk/catalog",
                    "shared_keywords": "fur, sherpa",
                },
                maximum=2500,
            )
        self.assertEqual((vinted, ebay), (1, 0))
        self.assertEqual(config["shared_keywords"], ["fur", "sherpa"])

    def test_ebay_only_is_inferred_from_url_and_keeps_shared_fields(self):
        vinted, ebay, config = self.parse(query="")
        self.assertEqual((vinted, ebay), (0, 1))
        self.assertEqual(config["max_price"], 2500)

    def test_non_browse_source_is_rejected_without_switching_it(self):
        with patch.object(
            store, "configuration", return_value={"source": "public"}
        ), self.assertRaisesRegex(ValueError, "Browse API"):
            store.parse_shared_form(
                {"ebay_search_url": FILTER_LINK, "shared_keywords": "fur"}, maximum=2500
            )

    def test_unsafe_url_filters_still_fail_before_save(self):
        for url in [
            FILTER_LINK + "&LH_Sold=1",
            FILTER_LINK.replace("www.ebay.co.uk", "evil.example"),
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.parse(ebay_search_url=url)

    def test_one_required_dashboard_budget_and_bounded_query(self):
        for maximum in [None, 99, 100001, "2500", True]:
            with self.subTest(maximum=maximum), self.assertRaisesRegex(
                ValueError, "maximum total"
            ):
                store.parse_shared_form({"platform_mode": "vinted"}, maximum=maximum)
        with self.assertRaisesRegex(ValueError, "100-character"):
            self.parse(shared_keywords="a" * 97 + ",b")

    def test_local_budget_and_notes_do_not_create_additional_fetch_groups(self):
        _, _, one = self.parse()
        two = dict(one, max_price=3500)
        groups = monitor.grouped_searches(
            [
                {"ebay": one, "exclusions": ["teddy"], "reminder": "First"},
                {"ebay": two, "exclusions": [], "reminder": "Second"},
            ]
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)


class SharedBudgetTests(unittest.TestCase):
    def test_official_fee_examples_and_cap(self):
        expected = {
            0: 10,
            500: 45,
            2000: 150,
            2100: 154,
            30000: 1270,
            150000: 3670,
            400000: 8670,
            750000: 8670,
        }
        for price, fee in expected.items():
            with self.subTest(price=price):
                self.assertEqual(monitor.buyer_protection_allowance({}, price), fee)

    def test_rounding_is_conservative_at_each_fee_boundary(self):
        for price, fee in [
            (1, 11),
            (1999, 150),
            (2001, 151),
            (30001, 1271),
            (400001, 8670),
        ]:
            with self.subTest(price=price):
                self.assertEqual(monitor.buyer_protection_allowance({}, price), fee)

    def test_business_and_fee_inclusive_public_prices_need_no_allowance(self):
        self.assertEqual(
            monitor.buyer_protection_allowance(
                {"seller": {"sellerAccountType": "BUSINESS"}}, 2000
            ),
            0,
        )
        self.assertEqual(monitor.buyer_protection_allowance({}, 2150, public=True), 0)

    def test_item_location_and_seller_contact_country_are_not_registration(self):
        for country in ["GB", "UK", "US", "ZZ", None]:
            raw = {
                "itemLocation": {"country": country},
                "seller": {
                    "sellerAccountType": "INDIVIDUAL",
                    "sellerLegalInfo": {
                        "sellerProvidedLegalAddress": {"country": country}
                    },
                },
            }
            with self.subTest(country=country):
                self.assertEqual(monitor.buyer_protection_allowance(raw, 2000), 150)

    def test_supplied_price_shipping_and_fee_are_included_in_budget(self):
        parsed = monitor.parse_item(item(123), shared_config(1915), 1020)
        self.assertEqual(parsed["estimated_total"], 1915)
        self.assertEqual(parsed["buyer_fee_estimate"], 115)
        self.assertEqual(parsed["price"], 1500)
        self.assertEqual(parsed["shipping"], 300)
        self.assertIsNone(monitor.parse_item(item(123), shared_config(1914), 1020))

    def test_business_price_and_shipping_boundary(self):
        raw = item(123, seller={"sellerAccountType": "BUSINESS"})
        parsed = monitor.parse_item(raw, shared_config(1800), 1020)
        self.assertEqual(parsed["estimated_total"], 1800)
        self.assertEqual(parsed["buyer_fee_estimate"], 0)
        self.assertIsNone(monitor.parse_item(raw, shared_config(1799), 1020))

    def test_unknown_invalid_or_foreign_currency_shipping_never_becomes_free(self):
        for options in [
            [],
            None,
            [{"shippingCost": {"value": "1", "currency": "USD"}}],
            [{"shippingCost": {"value": "NaN", "currency": "GBP"}}],
        ]:
            with self.subTest(options=options):
                self.assertIsNone(
                    monitor.parse_item(
                        item(123, shippingOptions=options), shared_config(2500), 1020
                    )
                )
                # Even a corrupted shared flag cannot bypass the shipping guard.
                config = shared_config(2500)
                config["include_shipping"] = False
                self.assertIsNone(
                    monitor.parse_item(item(123, shippingOptions=options), config, 1020)
                )

    def test_supplied_free_shipping_is_accepted(self):
        raw = item(
            123, shippingOptions=[{"shippingCost": {"value": "0", "currency": "GBP"}}]
        )
        self.assertEqual(
            monitor.parse_item(raw, shared_config(1615), 1020)["estimated_total"], 1615
        )

    def test_public_displayed_price_does_not_add_a_second_fee(self):
        raw = item(
            123, price="21.50", _dateSource="publicSearchMinute", _listedLabel="Today"
        )
        parsed = monitor.parse_item(raw, shared_config(2450), 1020)
        self.assertEqual(parsed["estimated_total"], 2450)
        self.assertEqual(parsed["buyer_fee_estimate"], 0)

    def test_unmarked_legacy_config_keeps_item_only_price_matching(self):
        config = dict(store.DEFAULTS, max_price=1500)
        parsed = monitor.parse_item(item(123, shippingOptions=[]), config, 1020)
        self.assertIsNotNone(parsed)
        self.assertNotIn("estimated_total", parsed)
        self.assertNotIn("shared_alert_version", parsed)
        config["include_shipping"] = True
        config["max_price"] = 1800
        self.assertIsNotNone(monitor.parse_item(item(123), config, 1020))


class SharedPersistenceTests(DatabaseFixture, unittest.TestCase):
    def test_shared_marker_keywords_and_budget_round_trip_without_legacy_marker(self):
        legacy = store.platform_details(2)
        self.assertEqual(legacy["shared_alert_version"], 0)
        self.assertNotIn("shared_alert_version", legacy["ebay"])
        config = shared_config(2500)
        with closing(search_settings.connection()) as conn, conn:
            store.save_platforms(conn, 1, 1, 1, config)
        saved = store.platform_details(1)
        self.assertEqual(saved["shared_alert_version"], 1)
        self.assertEqual(saved["shared_keywords"], ["fur", "sherpa", "fur hood"])
        self.assertEqual(saved["ebay"]["max_price"], 2500)
        self.assertEqual(store.platform_details(2), legacy)

    def test_shared_snapshot_and_both_caption_paths_label_estimate(self):
        search = search_settings.get_search(1)
        search["reminder"] = "Resale £40; inspect <hood>"
        parsed = monitor.parse_item(item(123), shared_config(2500), 1020)
        details = ebay_alerts.snapshot(parsed, search)
        self.assertEqual(details["estimated_total"], 1915)
        self.assertEqual(details["buyer_fee_estimate"], 115)
        row = {
            "query_id": 1,
            "search_name": "Fur",
            "url": parsed["url"],
            "title": parsed["title"],
            "price": "15.00",
            "currency": "GBP",
        }
        rendered = "\n".join(vinted_alerts.sections(row, details))
        for text in [rendered, monitor.format_alert(parsed, search)]:
            self.assertIn("Estimated total", text)
            self.assertIn("£19.15", text)
            self.assertIn("conservative buyer-fee allowance", text)
            self.assertIn("&lt;hood&gt;", text)


class SharedDeliveryTests(EbayFixture, unittest.TestCase):
    def test_one_quiet_baseline_then_matching_items_once_and_exclusions_locally(self):
        with closing(search_settings.connection()) as conn, conn:
            store.save_platforms(conn, 1, 1, 1, shared_config(1915))
        search = search_settings.get_search(1)
        search["exclusions"] = ["teddy"]
        self.assertEqual(self.snapshot(search, [item(1)], 1000), 0)
        self.assertEqual(
            self.snapshot(
                search,
                [
                    item(2),
                    item(3, price="15.01"),
                    item(4, title="Hollister teddy fur hood"),
                ],
                1020,
            ),
            1,
        )
        self.assertEqual(self.snapshot(search, [item(2)], 1030), 0)
        rows = self.outbox()
        self.assertEqual([row["item_id"] for row in rows], ["ebay:2"])
        details = ebay_alerts.get_details(rows[0])
        self.assertEqual(details["estimated_total"], 1915)


if __name__ == "__main__":
    unittest.main()
