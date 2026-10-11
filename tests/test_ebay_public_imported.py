"""Native filter fixtures and safety of the imported public-search path."""

import copy
import unittest
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

from test_ebay_monitor import EbayFixture

import ebay_monitor as monitor
import ebay_public as public
import ebay_public_filters as filters
import ebay_search_link as links
import ebay_store as store

FIXTURES = Path(__file__).parent / "fixtures/ebay_public_filters"
ZARA = "https://www.ebay.co.uk/sch/i.html?_sacat=11554&Brand=Zara&LH_BIN=1&LH_PrefLoc=1&_nkw=skinny"
FAM = ZARA.replace("Brand=Zara", "Brand=7+For+All+Mankind").replace("skinny", "jeans")


class ImportedPublicTests(unittest.TestCase):
    def setUp(self):
        self.config = links.parse_link(ZARA)

    def body(self, name):
        return (FIXTURES / (name + ".html")).read_bytes()

    def response(self, body, *, url=None, status=200):
        response = MagicMock()
        response.status_code = status
        response.url = url or public.search_url(self.config)
        response.history = []
        response.headers = {}
        response.iter_content.return_value = iter([body])
        response.__enter__.return_value = response
        session = MagicMock()
        session.get.return_value = response
        return public.PublicClient(session=session), session

    def test_multiword_brand_is_native_encoded_and_round_trips(self):
        config = links.parse_link(FAM)
        url = public.search_url(config)
        self.assertIn("Brand=7%2520For%2520All%2520Mankind", url)
        self.assertTrue(filters.same_remote_filters(config, url))

    def test_price_bounds_stay_local(self):
        config = links.parse_link(ZARA + "&_udlo=5&_udhi=25")
        params = parse_qs(urlsplit(public.search_url(config)).query)
        self.assertNotIn("_udlo", params)
        self.assertNotIn("_udhi", params)
        self.assertEqual(params["_sop"], ["10"])

    def test_blank_keywords_stop_before_network_without_global_halt(self):
        config = links.parse_link(ZARA.replace("_nkw=skinny", "_nkw="))
        session = MagicMock()
        with self.assertRaisesRegex(monitor.EbayError, "blank-keyword") as raised:
            public.PublicClient(session=session).search(config)
        session.get.assert_not_called()
        self.assertFalse(raised.exception.halt)
        self.assertFalse(raised.exception.global_cooldown)

    def test_stale_or_corrupt_filters_stop_before_network(self):
        for field, value in [
            ("aspects", {"Brand": ["Nike"]}),
            ("buying", "auction"),
            ("category", "63862"),
            ("keywords", "bootcut"),
        ]:
            config = copy.deepcopy(self.config)
            config[field] = value
            session = MagicMock()
            with self.subTest(field=field), self.assertRaises(monitor.EbayError):
                public.PublicClient(session=session).search(config)
            session.get.assert_not_called()

    def test_native_controls_confirm_brand(self):
        filters.require_selected_filters(
            self.body("7fam_native"), links.parse_link(FAM)
        )

    def test_extra_checked_brand_is_rejected_even_with_the_original_selected(self):
        body = self.body("zara_native").replace(
            b"</body>",
            b'<input name="su-selection-group__aspect-Brand" value="Nike" checked></body>',
        )
        with self.assertRaisesRegex(monitor.EbayError, "Brand"):
            filters.require_selected_filters(body, self.config)

    def test_extra_applied_size_is_rejected(self):
        config = links.parse_link(FAM + "&Size=8%257C10%257C12&LH_ItemCondition=3000")
        body = self.body("multi_size_native").replace(
            b"</body>",
            b'<div class="srp-multi-aspect__item--applied">Size: 14Remove filter</div></body>',
        )
        with self.assertRaisesRegex(monitor.EbayError, "Size"):
            filters.require_selected_filters(body, config)

    def test_extra_condition_is_rejected(self):
        config = links.parse_link(FAM + "&Size=8%257C10%257C12&LH_ItemCondition=3000")
        body = self.body("multi_size_native").replace(
            b"</body>",
            b'<input name="su-selection-group__condition" value="1000" checked></body>',
        )
        with self.assertRaisesRegex(monitor.EbayError, "condition"):
            filters.require_selected_filters(body, config)

    def test_same_url_does_not_hide_silently_ignored_brand(self):
        config = links.parse_link(FAM)
        with self.assertRaisesRegex(monitor.EbayError, "Brand"):
            public.parse_page(self.body("7fam_ignored"), config)

    def test_multi_size_condition_location_and_brand_are_confirmed(self):
        config = links.parse_link(FAM + "&Size=8%257C10%257C12&LH_ItemCondition=3000")
        filters.require_selected_filters(self.body("multi_size_native"), config)

    def test_auction_and_fixed_type_with_same_value_are_distinguished(self):
        config = links.parse_link(
            "https://www.ebay.co.uk/sch/i.html?_sacat=15662&Brand=Burberry&LH_Auction=1&LH_PrefLoc=1&LH_ItemCondition=3000&_nkw=tie"
        )
        body = self.body("auction_native")
        filters.require_selected_filters(body, config)
        config["buying"] = "fixed"
        with self.assertRaisesRegex(monitor.EbayError, "listing type"):
            filters.require_selected_filters(body, config)

    def test_unknown_free_postage_evidence_is_not_claimed_as_supported(self):
        config = dict(self.config, free_shipping=True)
        with self.assertRaisesRegex(monitor.EbayError, "free postage"):
            filters.require_selected_filters(self.body("zara_native"), config)

    def test_changed_landing_filters_reject_before_parse(self):
        client, session = self.response(
            self.body("zara_native"),
            url=public.search_url(self.config).replace("Brand=Zara", "Brand=Nike"),
        )
        with self.assertRaisesRegex(monitor.EbayError, "changed the saved"):
            client.search(self.config)
        session.get.assert_called_once()

    def test_confirmed_zero_results_can_complete(self):
        client, session = self.response(self.body("zara_native"))
        self.assertEqual(client.search(self.config), ([], ""))
        session.get.assert_called_once()

    def test_imported_public_denial_still_halts_without_retry(self):
        client, session = self.response(b"Access denied", status=403)
        with self.assertRaises(monitor.EbayError) as raised:
            client.search(self.config)
        self.assertTrue(raised.exception.halt)
        session.get.assert_called_once()


class PublicUnsupportedSearchIsolation(EbayFixture, unittest.TestCase):
    def test_unsupported_search_does_not_set_global_public_pause(self):
        search = self.enable()
        config = links.parse_link(ZARA.replace("_nkw=skinny", "_nkw="))
        with self.assertRaises(monitor.EbayError) as raised:
            public.search_url(config)
        monitor.record_failure(search, raised.exception, 1000)
        self.assertFalse(store.connection_summary()["public_paused"])
