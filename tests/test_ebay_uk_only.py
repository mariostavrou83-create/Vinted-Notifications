"""UK physical stock is required at import, discovery and final delivery."""

import json
import unittest
from contextlib import closing
from urllib.parse import parse_qs, urlsplit

from test_ebay_monitor import EbayFixture, item
from test_ebay_public import NOW, card, page

import alert_delivery
import ebay_alerts
import ebay_monitor as monitor
import ebay_public
import ebay_runtime_audit
import ebay_search_link
import ebay_store as store
import search_settings


class UKOnlyTests(EbayFixture, unittest.TestCase):
    def test_imported_and_manual_searches_always_require_uk_stock(self):
        for location in ("", "0", "1", "3"):
            with self.subTest(location=location):
                config = ebay_search_link.parse_link(
                    "https://www.ebay.co.uk/sch/i.html?_nkw=diesel"
                    "&_sacat=63862&Brand=Diesel&Size=S&_udhi=25&LH_PrefLoc=" + location
                )
                self.assertTrue(config["uk_only"])
                self.assertEqual(parse_qs(urlsplit(config["search_url"]).query)["LH_PrefLoc"], ["1"])
                self.assertEqual(config["aspects"], {"Brand": ["Diesel"], "Size": ["S"]})
                self.assertEqual(config["max_price"], 2500)
                self.assertIn("itemLocationCountry:GB", monitor.search_params(config)["filter"])
        self.assertTrue(self.enable(ebay_uk_only="")["ebay"]["uk_only"])

    def test_stale_worldwide_config_cannot_bypass_remote_filter(self):
        config = dict(store.DEFAULTS, keywords="diesel", uk_only=False)
        self.assertIn("itemLocationCountry:GB", monitor.search_params(config)["filter"])
        for url in (store.search_url(config), ebay_public.search_url(config)):
            self.assertEqual(parse_qs(urlsplit(url).query)["LH_PrefLoc"], ["1"])

    def test_uk_site_gbp_and_uk_seller_do_not_prove_item_location(self):
        config = dict(store.DEFAULTS, uk_only=False)
        for location in (None, {}, [], {"country": ""}, {"country": "US"}, {"country": "IT"}, {"country": "CN"}):
            with self.subTest(location=location):
                raw = item(123, itemLocation=location, seller={"country": "GB"})
                self.assertIsNone(monitor.parse_item(raw, config, 1020))
        raw = item(124, itemLocation={"country": "GB", "city": "Belfast"}, seller={"country": "US"})
        self.assertEqual(monitor.parse_item(raw, config, 1020)["item_location_country"], "GB")

    def test_mixed_snapshot_only_queues_uk_and_explains_rejected_locations(self):
        search = self.enable()
        self.snapshot(search, [], 1000)
        self.assertEqual(self.snapshot(search, [
            item(123), item(124, itemLocation={"country": "US"}), item(125, itemLocation={}),
        ], 1020), 1)
        self.assertEqual([r["item_id"] for r in self.outbox()], ["ebay:123"])
        details = ebay_alerts.get_details(self.outbox()[0])
        self.assertEqual(details["item_location_country"], "GB")
        report = ebay_runtime_audit.runtime_summary(store.configuration(), [[search]], 1030)
        self.assertEqual(report["filter_counts"]["1"]["item_location_not_uk"], 1)
        self.assertEqual(report["filter_counts"]["1"]["item_location_unknown"], 1)
        self.assertTrue(report["searches"][0]["uk_only"])

    def test_saved_search_upgrade_preserves_baselines_history_and_pacing(self):
        search = self.enable()
        self.snapshot(search, [], 1000)
        self.snapshot(search, [item(123)], 1020)
        legacy = dict(search["ebay"], uk_only=False,
            search_url="https://www.ebay.co.uk/sch/i.html?_nkw=hollister&LH_PrefLoc=3")
        tables = ("ebay_state", "ebay_seen", "ebay_call_buckets", "alert_outbox", "queries", "search_dashboard")
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE search_platforms SET ebay_config=? WHERE query_id=1", (json.dumps(legacy),))
            conn.execute("INSERT INTO ebay_call_buckets VALUES(10,123)")
            before = {t: [tuple(r) for r in conn.execute("SELECT * FROM " + t)] for t in tables}
        # Effective read rules protect delivery even before startup persists them.
        self.assertTrue(search_settings.get_search(1)["ebay"]["uk_only"])
        self.assertGreaterEqual(store.enforce_uk_only(), 1)
        self.assertEqual(store.enforce_uk_only(), 0)
        with closing(search_settings.connection()) as conn:
            saved = conn.execute("SELECT * FROM search_platforms WHERE query_id=1").fetchone()
            config = json.loads(saved["ebay_config"])
            self.assertTrue(config["uk_only"])
            self.assertEqual(parse_qs(urlsplit(config["search_url"]).query)["LH_PrefLoc"], ["1"])
            self.assertEqual(saved["ebay_generation"], search["ebay_generation"])
            for table in tables:
                self.assertEqual([tuple(r) for r in conn.execute("SELECT * FROM " + table)], before[table])

    def test_previously_queued_foreign_or_unverified_items_cannot_send(self):
        search = self.enable()
        self.snapshot(search, [], 1000)
        self.snapshot(search, [item(123), item(124), item(125), item(126)], 1020)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE ebay_alert_details SET payload=? WHERE item_id='ebay:123'", (json.dumps({"item_location_country": "US"}),))
            conn.execute("UPDATE ebay_alert_details SET payload='{}' WHERE item_id='ebay:124'")
            conn.execute("DELETE FROM ebay_alert_details WHERE item_id='ebay:125'")
        for number in (123, 124, 125):
            leased = alert_delivery.claim(now=1100, platform="ebay")
            self.assertEqual(leased["item_id"], f"ebay:{number}")
            self.assertFalse(ebay_alerts.prepare_listing(leased))
            row = next(r for r in self.outbox() if r["item_id"] == leased["item_id"])
            self.assertEqual(row["status"], "cancelled")
            self.assertEqual(row["error"], "Item location is not confirmed UK")
        leased = alert_delivery.claim(now=1100, platform="ebay")
        self.assertEqual(leased["item_id"], "ebay:126")
        self.assertTrue(ebay_alerts.prepare_listing(leased))

    def test_public_title_does_not_supply_missing_location(self):
        config = dict(store.DEFAULTS, keywords="diesel")
        results, _ = ebay_public.parse_page(page(card(title="Diesel from UK", location="")), config, NOW)
        self.assertEqual(len(results), 1)
        self.assertIsNone(monitor.parse_item(results[0], config, NOW))
        results, _ = ebay_public.parse_page(page(card(location="United Kingdom")), config, NOW)
        self.assertEqual(monitor.parse_item(results[0], config, NOW)["item_location_country"], "GB")
