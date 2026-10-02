"""One-message eBay photos, retries and account-closure redaction."""

import io
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image
from telegram import Bot
from telegram.error import BadRequest, NetworkError
from test_dashboard import photo_bytes
from test_ebay_monitor import EbayFixture, item
from test_vinted_native import NativeWireRequest

import alert_delivery
import alert_images
import ebay_alerts
import ebay_privacy
import search_settings
import vinted_native


class EbayNativeTests(EbayFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.search = self.enable(reminder="Check the back pockets", max_buy="15")
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT INTO dashboard_media VALUES ('reference',?,NULL,1)",
                (photo_bytes(),),
            )
        self.search["reference_id"] = "reference"
        self.snapshot(self.search, [], 1000)
        self.snapshot(
            self.search,
            [
                item(
                    123,
                    additionalImages=[
                        {"imageUrl": "https://i.ebayimg.com/images/b.jpg"}
                    ],
                )
            ],
            1020,
        )
        self.bot = SimpleNamespace(
            send_photo=AsyncMock(
                return_value=SimpleNamespace(
                    message_id=99, photo=[SimpleNamespace(file_id="photo")]
                )
            ),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
            edit_message_media=AsyncMock(
                return_value=SimpleNamespace(
                    message_id=99, photo=[SimpleNamespace(file_id="edited")]
                )
            ),
        )
        self.worker = alert_delivery.EbayPhotoDeliveryWorker(self.bot, "123", "456")
        self.download = patch.object(
            alert_images, "listing_collage", new=AsyncMock(return_value=photo_bytes())
        )
        self.download.start()
        self.addCleanup(self.download.stop)

    async def asyncTearDown(self):
        await self.worker.close()

    async def send(self, now=1100):
        with patch.object(self.worker, "send_slot_delay", return_value=0):
            await self.worker.tick(now=now)
            await self.worker.tick(now=now + 2)
            if self.worker.photo_task:
                await self.worker.photo_task

    async def test_native_photo_and_comparison_are_one_message(self):
        await self.send()
        self.bot.send_photo.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()
        self.bot.edit_message_media.assert_awaited_once()
        initial = self.bot.send_photo.call_args.kwargs
        self.assertTrue(initial["show_caption_above_media"])
        self.assertIn("Open eBay listing", initial["caption"])
        self.assertNotIn("Open Vinted", initial["caption"])
        edit = self.bot.edit_message_media.call_args.kwargs
        self.assertEqual(edit["message_id"], 99)
        self.assertEqual(self.outbox()[0]["photo_status"], "sent")
        details = ebay_alerts.get_details(self.outbox()[0])
        self.assertEqual(len(details["photos"]), 2)
        self.assertIn("back pockets", details["reminder"])
        self.assertIn("15.00", details["guide"])

    async def test_edit_retry_never_sends_a_second_notification(self):
        self.bot.edit_message_media.side_effect = NetworkError("offline")
        await self.send()
        row = self.outbox()[0]
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["photo_status"], "pending")
        self.bot.edit_message_media.side_effect = None
        await self.send(now=1110)
        self.bot.send_photo.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(self.outbox()[0]["photo_status"], "sent")

    async def test_missing_image_sends_only_one_text_then_edits_same_message(self):
        alert_images.listing_collage.return_value = None
        await self.send()
        self.bot.send_photo.assert_not_awaited()
        self.bot.send_message.assert_awaited_once()
        self.assertIn(
            "Check the back pockets", self.bot.send_message.call_args.kwargs["text"]
        )
        self.assertEqual(self.bot.edit_message_media.call_args.kwargs["message_id"], 99)
        self.assertEqual(self.outbox()[0]["status"], "sent")

    async def test_actual_telegram_wire_upload_contains_ebay_image_and_caption(self):
        request = NativeWireRequest()
        bot = Bot("123456:offline-token", request=request, get_updates_request=request)
        row = dict(self.outbox()[0], telegram_message_id=42)
        details = ebay_alerts.get_details(row)
        self.assertTrue(
            await vinted_native.enrich(
                bot, "123", row, details, AsyncMock(), persist=False
            )
        )
        import json

        media = json.loads(request.data.json_parameters["media"])
        self.assertIn("Open eBay listing", media["caption"])
        attachment = media["media"].removeprefix("attach://")
        raw = request.data.multipart_data[attachment][1]
        with Image.open(io.BytesIO(raw)) as image:
            self.assertGreater(image.height, image.width * 2)

    async def test_photo_redaction_removes_pixels_and_late_edit_requeues(self):
        with closing(search_settings.connection()) as conn, conn:
            ebay_privacy.queue_redaction(conn, 99)
        bot = AsyncMock()
        bot.edit_message_text.side_effect = BadRequest(
            "There is no text in the message to edit"
        )
        self.assertTrue(await ebay_privacy.redact_one(bot, "123"))
        bot.edit_message_media.assert_awaited_once()
        self.assertEqual(bot.edit_message_media.call_args.kwargs["message_id"], 99)
        self.assertNotIn(
            "jeans", bot.edit_message_media.call_args.kwargs["media"].caption
        )
        self.assertEqual(ebay_privacy.setup_values()["pending"], 0)
        row = dict(
            self.outbox()[0],
            kind="photo",
            telegram_message_id=99,
            lease_token="removed",
        )
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DELETE FROM alert_outbox WHERE item_id=?", (row["item_id"],))
        alert_delivery.finish(row)
        self.assertEqual(ebay_privacy.setup_values()["pending"], 1)
        self.assertIsNone(ebay_alerts.get_details(row))

    async def test_previews_are_redacted_with_the_listing(self):
        row = dict(self.outbox()[0], telegram_message_id=77)
        self.assertTrue(ebay_alerts.track_preview(row))
        with closing(search_settings.connection()) as conn, conn:
            ebay_privacy.purge(conn, ["deleted-seller"], redact=True)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM ebay_preview_messages").fetchone()[
                    0
                ],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT message_id FROM ebay_privacy_redactions"
                ).fetchone()[0],
                77,
            )
        self.assertFalse(ebay_alerts.track_preview(dict(row, telegram_message_id=78)))
        self.assertEqual(ebay_privacy.setup_values()["pending"], 2)


class EbayPhotoSafetyTests(unittest.TestCase):
    def test_ebay_cdn_only_and_no_lookalike_hosts(self):
        self.assertTrue(
            alert_images.safe_listing_photo("https://i.ebayimg.com/images/a.jpg")
        )
        for url in [
            "https://i.ebayimg.com.evil.test/a",
            "https://evil.test/a",
            "https://user@i.ebayimg.com/a",
            "http://i.ebayimg.com/a",
            "https://i.ebayimg.com:8080/a",
        ]:
            self.assertIsNone(alert_images.safe_listing_photo(url))
