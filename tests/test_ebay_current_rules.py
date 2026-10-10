"""Queued and leased finds follow current rules; no live eBay or Telegram calls."""

import unittest
from contextlib import closing
from unittest.mock import AsyncMock, patch

from test_dashboard import photo_bytes
from test_ebay_monitor import EbayFixture, item
from test_photo_cards import bot

import alert_delivery
import alert_images
import ebay_alerts
import photo_cards
import search_settings


class CurrentRulesTests(EbayFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.search = self.enable()
        self.snapshot(self.search, [], 1000)

    def row(self, number):
        return next(row for row in self.outbox() if row["item_id"] == f"ebay:{number}")

    def queue(self, number, title="Hollister fur hood jacket"):
        self.assertEqual(
            self.snapshot(self.search, [item(number, title=title)], 1020), 1
        )

    def test_save_cancels_only_blocked_pending_and_preserves_seen_and_baseline(self):
        self.queue(123, "Hollister TEDDY-fur hood")
        self.queue(124)
        self.queue(126, "Hollister teddy jacket already sent")
        other = self.enable(2)
        self.snapshot(other, [], 1000)
        self.snapshot(other, [item(125, title="Hollister teddy fur hood")], 1020)
        self.batch(2, [300], title="Hollister teddy fur hood")
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE alert_outbox SET status='sent',telegram_message_id=42,sent_at=1030 WHERE item_id='ebay:126'"
            )
            seen = [tuple(row) for row in conn.execute("SELECT * FROM ebay_seen")]
            state = [tuple(row) for row in conn.execute("SELECT * FROM ebay_state")]
        changed = self.enable(exclusions="teddy", reminder="Updated notes")
        self.assertEqual(self.row(123)["status"], "cancelled")
        self.assertEqual(self.row(124)["status"], "pending")
        self.assertEqual(self.row(125)["status"], "pending")
        self.assertEqual(self.row(126)["status"], "sent")
        self.assertEqual(self.row(126)["telegram_message_id"], 42)
        self.assertEqual(
            next(row for row in self.outbox() if row["item_id"] == "300")["status"],
            "pending",
        )
        self.assertEqual(changed["ebay_generation"], self.search["ebay_generation"])
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                [tuple(row) for row in conn.execute("SELECT * FROM ebay_seen")], seen
            )
            self.assertEqual(
                [tuple(row) for row in conn.execute("SELECT * FROM ebay_state")], state
            )
        self.assertEqual(self.snapshot(changed, [item(123)], 1040), 0)

    def test_save_cancels_leased_job_and_stale_finish_cannot_revive_it(self):
        self.queue(123, "Hollister teddy fur hood")
        leased = alert_delivery.claim(now=1100, platform="ebay")
        self.assertTrue(ebay_alerts.prepare_listing(leased))
        self.enable(exclusions="teddy")
        self.assertFalse(ebay_alerts.prepare_listing(leased))
        self.assertEqual(self.row(123)["status"], "cancelled")
        self.assertIsNone(self.row(123)["lease_token"])
        alert_delivery.finish(leased, failure="Old Telegram failure", now=1101)
        self.assertEqual(self.row(123)["status"], "cancelled")

    def test_pre_send_reads_latest_exclusions_even_without_save_hook(self):
        self.queue(123, "Hollister teddy fur hood")
        leased = alert_delivery.claim(now=1100, platform="ebay")
        search_settings.update_search(1, "exclusions", "teddy")
        self.assertEqual(self.row(123)["status"], "pending")
        self.assertFalse(ebay_alerts.prepare_listing(leased))
        self.assertEqual(self.row(123)["status"], "cancelled")
        self.assertEqual(self.row(123)["error"], "Blocked by current exclusions")

    def test_matching_is_whole_words_and_notes_only_keep_pending_job(self):
        self.queue(123, "Hollister furniture-print fur hood")
        leased = alert_delivery.claim(now=1100, platform="ebay")
        self.enable(exclusions="teddy\nfurn", reminder="Resale aim £60; inspect cuffs")
        self.assertTrue(ebay_alerts.prepare_listing(leased))
        self.assertEqual(self.row(123)["status"], "pending")
        self.assertEqual(self.row(123)["lease_token"], leased["lease_token"])

    def test_budget_edit_cancellation_cannot_be_sent_by_old_lease(self):
        self.queue(123)
        leased = alert_delivery.claim(now=1100, platform="ebay")
        self.enable(ebay_max_price="10.00")
        self.assertEqual(self.row(123)["status"], "cancelled")
        self.assertFalse(ebay_alerts.prepare_listing(leased))

    def test_reclaimed_job_rejects_old_lease_without_cancelling_new_lease(self):
        self.queue(123)
        old = alert_delivery.claim(now=1100, platform="ebay")
        current = alert_delivery.claim(now=1221, platform="ebay")
        self.assertNotEqual(old["lease_token"], current["lease_token"])
        self.assertFalse(ebay_alerts.prepare_listing(old))
        self.assertTrue(ebay_alerts.prepare_listing(current))
        self.assertEqual(self.row(123)["status"], "pending")
        self.assertEqual(self.row(123)["lease_token"], current["lease_token"])

    def test_current_pause_is_rechecked_without_erasing_history(self):
        self.queue(123)
        leased = alert_delivery.claim(now=1100, platform="ebay")
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE search_dashboard SET paused=1 WHERE query_id=1")
        self.assertFalse(ebay_alerts.prepare_listing(leased))
        self.assertEqual(self.row(123)["status"], "cancelled")
        self.assertEqual(self.row(123)["error"], "Search no longer active for eBay")

    def test_sent_enrichment_keeps_history_and_duplicate_listing_is_refused(self):
        self.queue(123, "Hollister teddy fur hood")
        leased = alert_delivery.claim(now=1100, platform="ebay")
        alert_delivery.finish(leased, message_id=42, now=1101)
        self.enable(exclusions="teddy")
        self.assertFalse(ebay_alerts.prepare_listing(leased))
        self.assertTrue(ebay_alerts.prepare_listing(dict(leased, kind="photo")))
        self.assertEqual(self.row(123)["status"], "sent")
        self.assertEqual(self.row(123)["telegram_message_id"], 42)


class CurrentRulesDeliveryTests(EbayFixture, unittest.IsolatedAsyncioTestCase):
    async def test_exclusion_saved_during_photo_download_prevents_any_notification(
        self,
    ):
        photo_cards.enable()
        search = self.enable()
        self.snapshot(search, [], 1000)
        self.snapshot(search, [item(123, title="Hollister teddy fur hood")], 1020)
        fake_bot = bot()
        worker = alert_delivery.EbayPhotoDeliveryWorker(fake_bot, "123", bot_id="56789")
        worker.send_slot_delay = lambda: 0

        async def download(_photos):
            # A dashboard edit happens after the worker's first check and
            # before the collage/Telegram slot callback can start sending.
            self.enable(exclusions="teddy")
            return photo_bytes()

        try:
            with patch.object(
                alert_images, "listing_collage", new=AsyncMock(side_effect=download)
            ) as collage:
                self.assertTrue(await worker.tick(now=1100))
                collage.assert_awaited_once()
            fake_bot.send_photo.assert_not_awaited()
            fake_bot.send_message.assert_not_awaited()
            row = self.outbox()[0]
            self.assertEqual(row["status"], "cancelled")
            self.assertEqual(row["attempts"], 0)
            self.assertIsNone(row["telegram_message_id"])
            self.assertIsNone(row["lease_token"])
        finally:
            await worker.close()


if __name__ == "__main__":
    unittest.main()
