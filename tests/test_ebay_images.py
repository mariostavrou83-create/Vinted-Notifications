import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_dashboard import photo_bytes
from test_ebay_monitor import EbayFixture, item
from test_photo_cards import bot

import ebay_alerts
import ebay_images
import ebay_monitor
import ebay_store
import photo_cards
import search_settings


class EbayImageTests(EbayFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        photo_cards.enable()
        search = self.enable()
        self.snapshot(search, [], 1000)
        self.snapshot(search, [item(123, image={})], 1020)
        self.row = self.outbox()[0]
        self.details = ebay_alerts.get_details(self.row)
        self.config = dict(
            ebay_store.configuration(),
            client_id="test",
            client_secret="test",
            source="browse",
        )

    def test_thumbnail_fallback_is_saved_for_dashboard_and_telegram(self):
        photo = "https://i.ebayimg.com/images/thumbnail.jpg"
        raw = item(456, image={}, thumbnailImages=[{"imageUrl": photo}])
        parsed = ebay_monitor.parse_item(
            raw, search_settings.get_search(1)["ebay"], 1020
        )
        self.assertEqual(parsed["photo_url"], photo)
        self.assertEqual(parsed["photos"], [photo])
        self.assertEqual(
            ebay_images.extract(
                dict(raw, image={"imageUrl": "https://i.ebayimg.com/primary.jpg"})
            ),
            ["https://i.ebayimg.com/primary.jpg"],
        )
        self.assertEqual(
            ebay_images.extract({"image": {"imageUrl": "https://evil.test/picture"}}),
            [],
        )

    async def test_missing_search_photo_is_recovered_before_initial_native_send(self):
        photo = "https://i.ebayimg.com/full.jpg"
        api = SimpleNamespace(
            status_code=200,
            json=lambda: {"legacyItemId": "123", "image": {"imageUrl": photo}},
        )
        client = Mock(
            token="offline", session=SimpleNamespace(get=Mock(return_value=api))
        )
        telegram = bot()
        with patch("ebay_store.configuration", return_value=self.config), patch(
            "ebay_monitor.BrowseClient", return_value=client
        ), patch(
            "alert_images.listing_collage", new=AsyncMock(return_value=photo_bytes())
        ) as download:
            await photo_cards.send_initial(
                telegram, "123", self.row, self.details, AsyncMock()
            )
        telegram.send_photo.assert_awaited_once()
        telegram.send_message.assert_not_awaited()
        download.assert_awaited_once_with([photo])
        self.assertEqual(self.outbox()[0]["photo_url"], photo)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute("SELECT SUM(calls) FROM ebay_call_buckets").fetchone()[0],
                1,
            )

    async def test_existing_photo_uses_no_extra_ebay_call(self):
        self.row["photo_url"] = "https://i.ebayimg.com/row.jpg"
        with patch.object(ebay_images, "fetch_missing") as fetch:
            self.assertEqual(
                await ebay_images.resolve(self.row, self.details),
                [self.row["photo_url"]],
            )
        fetch.assert_not_called()

    async def test_deletion_during_recovery_does_not_restore_item_data(self):
        def deletion(row):
            with closing(search_settings.connection()) as conn, conn:
                conn.execute(
                    "DELETE FROM alert_outbox WHERE item_id=?", (row["item_id"],)
                )
            return ["https://i.ebayimg.com/row.jpg"]

        with patch.object(ebay_images, "fetch_missing", side_effect=deletion):
            self.assertEqual(await ebay_images.resolve(self.row, self.details), [])
        self.assertEqual(self.outbox(), [])

    def test_exhausted_budget_and_cooldown_never_call_item_api(self):
        with patch("ebay_store.configuration", return_value=self.config), patch(
            "ebay_store.reserve_call", return_value=9999999999
        ), patch("ebay_monitor.BrowseClient") as client:
            self.assertEqual(ebay_images.fetch_missing(self.row), [])
        client.assert_not_called()
