"""Live native photo dispatch, durable retries, and complete one-image comparisons."""

import asyncio
import io
import json
import re
import unittest
from contextlib import closing
from html import unescape
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image
from telegram import Bot
from telegram.error import NetworkError
from telegram.request import BaseRequest
from test_dashboard import photo_bytes
from test_finds_delivery import outbox
from test_search_controls import DatabaseFixture

import alert_delivery
import alert_images
import dashboard_store
import db
import search_settings
import vinted_alerts
import vinted_gallery
import vinted_native


class NativeWireRequest(BaseRequest):
    read_timeout = 8

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        self.data = request_data
        result = {
            "message_id": 42,
            "date": 0,
            "chat": {"id": 123, "type": "private"},
            "photo": [
                {
                    "file_id": "offline",
                    "file_unique_id": "offline",
                    "width": 100,
                    "height": 100,
                }
            ],
        }
        return 200, json.dumps({"ok": True, "result": result}).encode()


class NativeWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_comparison_edit_uploads_the_referenced_multipart_image(self):
        request = NativeWireRequest()
        bot = Bot(
            "123456:offline-test-token", request=request, get_updates_request=request
        )
        row = {
            "query_id": 50,
            "item_id": "123",
            "telegram_message_id": 42,
            "reference_id": None,
            "search_name": "Jeans",
            "url": "https://www.vinted.co.uk/items/123",
            "title": "Jeans",
            "price": "15",
            "currency": "GBP",
        }
        details = {
            "name": "Jeans",
            "brand": "7 for all mankind",
            "photos": ["https://images1.vinted.net/a.jpg"],
            "reminder": "Check pockets",
        }
        with patch.object(
            vinted_gallery, "resolve", new=AsyncMock(return_value=details["photos"])
        ), patch.object(
            alert_images, "listing_collage", new=AsyncMock(return_value=photo_bytes())
        ):
            self.assertTrue(
                await vinted_native.enrich(
                    bot, "123", row, details, AsyncMock(), persist=False
                )
            )
        media = json.loads(request.data.json_parameters["media"])
        self.assertTrue(media["media"].startswith("attach://"))
        attachment = media["media"].removeprefix("attach://")
        self.assertIn(attachment, request.data.multipart_data)
        filename, raw, mime = request.data.multipart_data[attachment]
        self.assertEqual(filename, "vinted-comparison.jpg")
        self.assertTrue(raw.startswith(b"\xff\xd8"))
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(request.data.json_parameters["message_id"], "42")


class NativeImageTests(unittest.TestCase):
    def test_caption_fits_telegram_with_long_emoji_and_no_broken_checkout_link(self):
        row = {
            "query_id": 50,
            "search_name": "name",
            "url": "https://www.vinted.co.uk/items/123",
            "title": "🧥" * 500,
            "price": "15",
            "currency": "GBP",
        }
        details = {"name": "👖" * 100, "brand": "🧵" * 120, "guide": "", "reminder": ""}
        caption = vinted_native.caption(row, details)
        plain = unescape(re.sub(r"<[^>]*>", "", caption))
        self.assertLessEqual(len(plain.encode("utf-16-le")) // 2, 1024)
        self.assertNotIn("transaction/buy", caption)
        self.assertIn("Open Vinted listing", caption)

    def test_comparison_keeps_both_square_panels_and_complete_notes_below(self):
        def colour(value):
            image = Image.new("RGB", (1280, 1280), value)
            stream = io.BytesIO()
            image.save(stream, "PNG")
            return stream.getvalue()

        notes = {
            "guide": "Buy up to <b>£15</b>",
            "reminder": "Check &lt;label&gt; " * 40,
        }
        with patch.object(
            vinted_native.ImageDraw.ImageDraw, "text", autospec=True
        ) as draw:
            raw = vinted_native.comparison_image(colour("red"), colour("blue"), notes)
        text = " ".join(call.args[2] for call in draw.call_args_list)
        self.assertIn("Buy up to £15", text)
        self.assertEqual(text.count("Check <label>"), 40)
        with Image.open(io.BytesIO(raw)) as image:
            self.assertGreater(image.height, 2 * 1280)
            self.assertLessEqual(image.width + image.height, 10000)
            self.assertGreater(image.getpixel((640, 700))[0], 240)
            self.assertGreater(image.getpixel((640, 2050))[2], 240)


class NativeDeliveryTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        vinted_native.enable()
        dashboard_store.save_search(
            1,
            {
                "query_name": "Jeans",
                "query": db.get_queries()[0][1],
                "revision": "0",
                "reminder": "Check pockets",
                "max_buy": "15",
            },
            photo_bytes(),
        )
        self.bot = SimpleNamespace(
            send_photo=AsyncMock(
                return_value=SimpleNamespace(message_id=42, photo=[1])
            ),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
            edit_message_media=AsyncMock(
                return_value=SimpleNamespace(message_id=42, photo=[1])
            ),
            do_api_request=AsyncMock(),
        )
        self.worker = alert_delivery.VintedDeliveryWorker(self.bot, "123")
        for target, name, value in (
            (self.worker, "send_slot_delay", lambda: 0),
            (vinted_gallery, "fetch_gallery", lambda _: ([], "unavailable")),
            (alert_images, "listing_collage", AsyncMock(return_value=photo_bytes())),
        ):
            mock = patch.object(target, name, new=value)
            mock.start()
            self.addCleanup(mock.stop)

    async def asyncTearDown(self):
        await self.worker.close()

    def enqueue(self, query_id=1, item_id=110):
        self.batch(query_id, [item_id])
        details = vinted_alerts.get_details(outbox(item_id))
        details["photos"] = ["https://images1.vinted.net/a.jpg"]
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_alert_details SET payload=? WHERE item_id=?",
                (json.dumps(details), str(item_id)),
            )
            conn.execute(
                "UPDATE alert_outbox SET photo_status='pending' WHERE item_id=?",
                (str(item_id),),
            )

    async def test_live_alert_starts_with_photo_and_edits_that_message_only(self):
        self.enqueue()
        self.assertTrue(vinted_alerts.get_details(outbox(110))["native_photo"])
        await self.worker.tick(now=1000)
        self.bot.send_photo.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()
        self.assertTrue(
            self.bot.send_photo.await_args.kwargs["show_caption_above_media"]
        )
        await self.worker.tick(now=1002)
        await self.worker.photo_task
        self.assertEqual(outbox(110)["photo_status"], "sent")
        self.assertEqual(
            self.bot.edit_message_media.await_args.kwargs["message_id"], 42
        )
        self.bot.send_photo.assert_awaited_once()
        self.bot.do_api_request.assert_not_awaited()

    async def test_edit_retry_does_not_resend_notification_and_survives_restart(self):
        self.enqueue()
        await self.worker.tick(now=1000)
        self.bot.edit_message_media.side_effect = NetworkError("offline")
        await self.worker.tick(now=1002)
        await self.worker.photo_task
        self.assertEqual(outbox(110)["photo_status"], "pending")
        await self.worker.close()
        self.worker = alert_delivery.VintedDeliveryWorker(self.bot, "123")
        self.worker.send_slot_delay = lambda: 0
        self.bot.edit_message_media.side_effect = None
        await self.worker.tick(now=1020)
        await self.worker.photo_task
        self.assertEqual(outbox(110)["photo_status"], "sent")
        self.bot.send_photo.assert_awaited_once()

    async def test_slow_comparison_does_not_hold_up_another_search(self):
        self.enqueue()
        await self.worker.tick(now=1000)
        started, gate = asyncio.Event(), asyncio.Event()

        async def slow(*args, **kwargs):
            started.set()
            await gate.wait()
            return True

        with patch.object(vinted_native, "enrich", new=slow):
            await self.worker.tick(now=1002)
            await asyncio.wait_for(started.wait(), 1)
            self.enqueue(query_id=2, item_id=111)
            await asyncio.wait_for(self.worker.tick(now=1004), 1)
            self.assertEqual(outbox(111)["status"], "sent")
            self.assertEqual(self.bot.send_photo.await_count, 2)
            gate.set()
            await self.worker.photo_task

    async def test_missing_photo_falls_back_once_then_edits_without_new_notification(
        self,
    ):
        self.enqueue()
        with patch.object(
            alert_images, "listing_collage", new=AsyncMock(return_value=None)
        ):
            await self.worker.tick(now=1000)
        self.bot.send_message.assert_awaited_once()
        self.assertIn("Check pockets", self.bot.send_message.await_args.kwargs["text"])
        await self.worker.tick(now=1002)
        await self.worker.photo_task
        self.assertEqual(outbox(110)["photo_status"], "sent")
        self.bot.send_photo.assert_not_awaited()
        self.assertEqual(
            self.bot.edit_message_media.await_args.kwargs["message_id"], 42
        )

    async def test_initial_photo_failure_retries_the_durable_listing_job(self):
        self.enqueue()
        self.bot.send_photo.side_effect = NetworkError("offline")
        await self.worker.tick(now=1000)
        self.assertEqual(outbox(110)["status"], "pending")
        self.bot.send_photo.side_effect = None
        await self.worker.tick(now=1010)
        self.assertEqual(outbox(110)["status"], "sent")
        self.bot.send_message.assert_not_awaited()
