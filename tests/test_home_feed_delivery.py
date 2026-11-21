"""Home-feed freshness and atomic shared-outbox integration, fully offline."""

import json
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_finds_delivery import outbox
from test_search_controls import DatabaseFixture

import alert_delivery
import db
import search_settings
import vinted_alerts


def discovery(**changes):
    return dict(
        version=1,
        observed_at=1000.0,
        expires_at=1060.0,
        max_age_seconds=120,
        source_role="seller",
        womens_clothing=True,
        reason="brand_category_style",
        **changes,
    )


class FreshnessTests(unittest.TestCase):
    def test_three_minute_bound_is_listing_age_not_first_seen(self):
        evidence = {"home_discovery": discovery()}
        self.assertTrue(vinted_alerts.home_discovery_fresh(evidence, now=1000))
        self.assertTrue(vinted_alerts.home_discovery_fresh(evidence, now=1060))
        self.assertFalse(vinted_alerts.home_discovery_fresh(evidence, now=1060.01))
        self.assertFalse(vinted_alerts.home_discovery_fresh(evidence, now=999.9))

    def test_malformed_age_evidence_never_qualifies(self):
        cases = [None, {}, "fresh"]
        for key, values in {
            "version": [True, 0, 2, "1"],
            "observed_at": [None, True, "1000", float("nan"), float("inf"), 1001],
            "expires_at": [None, True, "1060", float("nan"), float("inf"), 999, 1180],
            "max_age_seconds": [-1, 180, 181, True, "120", 120.1],
            "source_role": [None, "unknown", "msj-studio", "buyer"],
        }.items():
            for value in values:
                cases.append(dict(discovery(), **{key: value}))
        for evidence in cases:
            with self.subTest(evidence=evidence):
                self.assertFalse(
                    vinted_alerts.home_discovery_fresh(
                        {"home_discovery": evidence},
                        now=1000,
                    )
                )

    def test_ordinary_search_alerts_keep_existing_timing(self):
        self.assertTrue(vinted_alerts.home_discovery_fresh({}, now=10**12))


class HomeOutboxTests(DatabaseFixture, unittest.TestCase):
    def home(self, item_id=110, query_id=1):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO parameters VALUES('home_monitor_enabled','1')"
            )
        details = {
            "version": 1,
            "name": "Vintage style",
            "brand": "Brand",
            "description": "Exact seller description <unchanged>",
            "description_pending": False,
            "photos": [],
            "home_discovery": discovery(),
        }
        alert = {
            "content": "Home feed find",
            "url": f"https://www.vinted.co.uk/items/{item_id}",
            "search_name": "Vintage style",
            "vinted_details": details,
        }
        return db.add_item_to_db(
            item_id,
            "Fresh vintage item",
            query_id,
            "8.00",
            1000,
            None,
            "GBP",
            alert,
            update_search_timestamp=False,
        )

    def test_discovery_keeps_search_watermark_and_global_duplicate_protection(self):
        self.assertTrue(self.home())
        self.assertEqual(search_settings.get_search(1)["last_item"], 100)
        self.assertFalse(self.home(query_id=2))
        self.assertFalse(db.add_item_to_db(110, "Same item", 2, "8", 1100, None, "GBP"))
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM items WHERE item=110").fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM alert_outbox WHERE item_id='110'"
                ).fetchone()[0],
                1,
            )
        self.assertTrue(
            db.add_item_to_db(111, "Ordinary item", 1, "8", 1100, None, "GBP")
        )
        self.assertEqual(search_settings.get_search(1)["last_item"], 1100)

    def test_discovery_expires_at_final_send_gate_even_without_total_budget(self):
        self.home()
        leased = alert_delivery.claim(now=1000)
        with patch("vinted_alerts.time.time", return_value=1059):
            self.assertTrue(vinted_alerts.prepare_listing(leased))
        with patch("vinted_alerts.time.time", return_value=1061):
            self.assertFalse(vinted_alerts.prepare_listing(leased))
        self.assertEqual(outbox(110)["status"], "cancelled")
        self.assertIn("three minutes", outbox(110)["error"])

    def test_stale_delivery_cannot_cancel_replacement_lease(self):
        self.home()
        first = alert_delivery.claim(now=1000)
        second = alert_delivery.claim(now=1121)
        with patch("vinted_alerts.time.time", return_value=1121):
            self.assertFalse(vinted_alerts.prepare_listing(first))
        self.assertEqual(outbox(110)["status"], "pending")
        self.assertEqual(outbox(110)["lease_token"], second["lease_token"])

    def test_current_pause_still_applies_to_home_find(self):
        self.home()
        leased = alert_delivery.claim(now=1000)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("INSERT INTO search_dashboard(query_id,paused) VALUES(1,1)")
        with patch("vinted_alerts.time.time", return_value=1020):
            self.assertFalse(vinted_alerts.prepare_listing(leased))
        self.assertEqual(outbox(110)["error"], "Search no longer active for Vinted")

    def test_pausing_discovery_stops_pending_home_but_not_ordinary_alert(self):
        self.home()
        self.batch(1, [111])
        home_lease = alert_delivery.claim(now=1000)
        ordinary_lease = alert_delivery.claim(now=1001)
        db.set_parameter("home_monitor_enabled", "0")
        with patch("vinted_alerts.time.time", return_value=1020):
            self.assertFalse(vinted_alerts.prepare_listing(home_lease))
            self.assertTrue(vinted_alerts.prepare_listing(ordinary_lease))
        self.assertEqual(outbox(110)["error"], "Home discovery is paused")
        self.assertEqual(outbox(111)["status"], "pending")

    def test_current_total_budget_still_applies_to_home_find(self):
        self.home()
        leased = alert_delivery.claim(now=1000)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT INTO vinted_search_budgets(query_id,max_total,postage_estimate) VALUES(1,500,220)"
            )
        with patch("vinted_alerts.time.time", return_value=1020):
            self.assertFalse(vinted_alerts.prepare_listing(leased))
        self.assertEqual(outbox(110)["error"], "Outside current estimated total budget")

    def test_heading_marks_discovery_and_preserves_seller_description(self):
        self.home()
        row = outbox(110)
        details = vinted_alerts.get_details(row)
        parts = vinted_alerts.sections(row, details)
        self.assertIn("Vinted Home · feed find", parts[0])
        self.assertIn("within 3 minutes", parts[2])
        self.assertIn("Brand, category and style", parts[2])
        self.assertIn("Exact seller description &lt;unchanged&gt;", parts[3])
        original = json.loads(json.dumps(details))
        vinted_alerts.sections(row, details)
        self.assertEqual(details, original)


class HomeFinalSendTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    home = HomeOutboxTests.home

    async def test_slow_photo_download_expires_before_telegram_send(self):
        self.home()
        row = alert_delivery.claim(now=1000)
        details = vinted_alerts.get_details(row)
        details["photo_card"] = True
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_alert_details SET payload=? WHERE item_id='110'",
                (json.dumps(details),),
            )
        bot = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
        worker = alert_delivery.VintedDeliveryWorker(bot, "123")
        worker.send_slot_delay = lambda: 0
        now = [1059]

        async def slow_photo(_details):
            now[0] = 1061
            return b"offline-photo"

        try:
            with patch("photo_cards.listing_photo", side_effect=slow_photo), patch(
                "vinted_alerts.time.time", side_effect=lambda: now[0]
            ):
                self.assertTrue(vinted_alerts.prepare_listing(row))
                with self.assertRaises(alert_delivery.ListingCancelled):
                    await worker.send_listing(row)
            bot.send_photo.assert_not_awaited()
            bot.send_message.assert_not_awaited()
            self.assertEqual(outbox(110)["status"], "cancelled")
        finally:
            await worker.close()


if __name__ == "__main__":
    unittest.main()
