"""Real SQLite rollback checks; HTTP, Telegram and purchasing stay fake."""

import io
import unittest
from contextlib import closing
from unittest.mock import patch

from PIL import Image
from test_ebay_monitor import EbayFixture, item

import dashboard_store
import ebay_connections
import ebay_monitor as monitor
import ebay_public
import ebay_store as store
import search_settings


class PublicRollbackTests(EbayFixture, unittest.TestCase):
    def runtime(self):
        with closing(search_settings.connection()) as conn:
            return dict(conn.execute("SELECT key,value FROM delivery_runtime"))

    def public_failure(self, search, *, halt=True, retry_after=3600):
        poller = monitor.Poller(workers=1)
        try:
            with (
                patch.object(monitor.time, "time", return_value=1000),
                patch.object(
                    ebay_public.PublicClient,
                    "search",
                    side_effect=monitor.EbayError(
                        "Public access refused", retry_after, True, halt=halt
                    ),
                ) as fetch,
            ):
                poller.fetch_group(
                    [search],
                    {"source": "public", "client_id": "", "client_secret": ""},
                    1000,
                    30,
                )
                fetch.assert_called_once()
        finally:
            poller.close()

    def test_public_403_does_not_delay_rollback_to_browse(self):
        search = self.enable()
        self.public_failure(search)
        self.assertEqual(self.runtime()["ebay_public_paused"], 1)
        self.assertEqual(self.runtime()["ebay_public_cooldown"], 4600)
        self.assertNotIn("ebay_api_cooldown", self.runtime())
        self.assertIsNone(
            store.reserve_call({"source": "browse", "daily_budget": 5000}, 1001)
        )
        self.assertGreater(
            store.reserve_call({"source": "public", "daily_budget": 5000}, 9000),
            9000,
        )

    def test_public_rate_limit_waits_without_holding_browse_requests(self):
        self.public_failure(self.enable(), halt=False, retry_after=60)
        self.assertEqual(self.runtime()["ebay_public_cooldown"], 1060)
        self.assertNotIn("ebay_public_paused", self.runtime())
        self.assertEqual(
            store.reserve_call({"source": "public", "daily_budget": 5000}, 1001),
            1060,
        )
        self.assertIsNone(
            store.reserve_call({"source": "browse", "daily_budget": 5000}, 1001)
        )

    def test_public_connection_denial_does_not_pause_browse(self):
        with (
            patch.object(
                ebay_connections, "configuration", return_value={"source": "public"}
            ),
            patch.object(monitor.time, "time", return_value=1000),
            patch.object(
                ebay_public.PublicClient,
                "search",
                side_effect=monitor.EbayError("HTTP 403", 3600, True, halt=True),
            ),
            self.assertRaises(ValueError),
        ):
            ebay_connections.test_connection("ebay")
        self.assertEqual(self.runtime()["ebay_public_cooldown"], 4600)
        self.assertNotIn("ebay_api_cooldown", self.runtime())
        self.assertIsNone(
            store.reserve_call({"source": "browse", "daily_budget": 5000}, 1001)
        )

    def test_public_success_cannot_clear_an_existing_browse_rate_limit(self):
        search = self.enable()
        monitor.record_failure(
            search, monitor.EbayError("API rate limit", 600, True), 1000
        )
        with closing(search_settings.connection()) as conn, conn:
            conn.executemany(
                "INSERT OR REPLACE INTO delivery_runtime VALUES (?,?)",
                [("ebay_public_paused", 1), ("ebay_public_cooldown", 4600)],
            )
        with (
            patch.object(
                ebay_connections, "configuration", return_value={"source": "public"}
            ),
            patch.object(ebay_public.PublicClient, "search", return_value=([], "")),
        ):
            ebay_connections.test_connection("ebay")
        self.assertEqual(self.runtime()["ebay_api_cooldown"], 1600)
        self.assertNotIn("ebay_public_cooldown", self.runtime())
        self.assertNotIn("ebay_public_paused", self.runtime())
        store.save_configuration({"source": "public"})
        store.save_configuration({"source": "browse"})
        self.assertEqual(
            store.reserve_call({"source": "browse", "daily_budget": 5000}, 1001),
            1600,
        )

    def test_round_trip_preserves_shared_search_and_rejects_stale_results(self):
        photo = io.BytesIO()
        Image.new("RGB", (20, 20), "blue").save(photo, format="JPEG")
        identity = dashboard_store.save_search(
            None,
            {
                "shared_alert_version": "1",
                "query_name": "Future fur jackets",
                "platform_mode": "both",
                "query": "https://www.vinted.co.uk/catalog?brand_ids[]=111&catalog[]=2050&size_ids[]=4",
                "ebay_search_url": "https://www.ebay.co.uk/sch/i.html?_sacat=57988&Brand=Hollister&LH_BIN=1&LH_PrefLoc=1",
                "shared_keywords": "fur, sherpa",
                "vinted_max_total": "25.00",
                "exclusions": "ripped",
                "reminder": "Check the hood and cuffs. Resale aim £60. " * 30,
            },
            photos=[photo.getvalue()],
        )
        store.save_live_selection([identity])
        original = search_settings.get_search(identity)
        photos = dashboard_store.reference_photos(identity)
        with closing(search_settings.connection()) as conn:
            buyer = list(conn.execute("SELECT * FROM vinted_buyer"))
            attempts = list(conn.execute("SELECT * FROM vinted_buy_attempts"))
            variants = list(conn.execute("SELECT * FROM vinted_keyword_variants"))
            original_queries = list(conn.execute("SELECT * FROM queries"))
        records = [item(101, categories=[{"categoryId": "57988"}])]
        self.snapshot(original, [], 1000)
        self.assertEqual(self.snapshot(original, records, 1020), 1)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE alert_outbox SET status='sent',sent_at=1021")
        store.save_configuration({"source": "public"})
        self.assertEqual(self.snapshot(original, [item(102)], 1030), 0)
        public_search = search_settings.get_search(identity)
        with patch.object(monitor.time, "time", return_value=1030):
            self.assertEqual(
                monitor.record_snapshot(public_search, records, 1030, source="public"),
                0,
            )
        self.public_failure(public_search)
        store.save_configuration({"source": "browse"})
        self.assertEqual(self.snapshot(public_search, [item(103)], 1040), 0)
        restored = search_settings.get_search(identity)
        self.assertEqual(self.snapshot(restored, records, 1040), 0)
        self.assertEqual(
            self.snapshot(
                restored,
                records
                + [item(104, created=1045, categories=[{"categoryId": "57988"}])],
                1050,
            ),
            1,
        )
        self.assertEqual(
            store.reserve_call({"source": "browse", "daily_budget": 5000}, 1051),
            None,
        )
        for key in (
            "query_name",
            "query",
            "platform_mode",
            "shared_keywords",
            "reminder",
            "exclusions",
            "vinted_max_total",
            "reference_id",
        ):
            self.assertEqual(restored[key], original[key], key)
        self.assertEqual(restored["ebay"], original["ebay"])
        self.assertEqual(dashboard_store.reference_photos(identity), photos)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(set(store.live_ids(conn)), {identity})
            self.assertEqual(list(conn.execute("SELECT * FROM vinted_buyer")), buyer)
            self.assertEqual(
                list(conn.execute("SELECT * FROM vinted_buy_attempts")), attempts
            )
            self.assertEqual(
                list(conn.execute("SELECT * FROM vinted_keyword_variants")), variants
            )
            self.assertEqual(
                list(conn.execute("SELECT * FROM queries")), original_queries
            )
        self.assertEqual(len(self.outbox()), 2)
        self.assertEqual(
            {row["item_id"] for row in self.outbox()}, {"ebay:101", "ebay:104"}
        )


if __name__ == "__main__":
    unittest.main()
