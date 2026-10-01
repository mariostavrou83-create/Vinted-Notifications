"""Real storage and Telegram wire serialization; no live notifications."""

import asyncio
import io
import json
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_dashboard as dashboard_tests
from PIL import Image
from telegram import Bot
from telegram.error import BadRequest, NetworkError
from telegram.request import BaseRequest
from test_dashboard import photo_bytes
from test_finds_delivery import outbox
from test_search_controls import DatabaseFixture

import alert_delivery as delivery
import alert_images
import dashboard_store as store
import db
import search_settings as settings
import vinted_alerts
import vinted_gallery


class CollageTests(unittest.TestCase):
    def test_one_to_four_squares_keep_all_images_and_reject_five(self):
        colours = [(220, 10, 10), (10, 220, 10), (10, 10, 220), (220, 220, 10)]
        raw = []
        for colour in colours:
            stream = io.BytesIO()
            Image.new("RGB", (100, 400), colour).save(stream, "PNG")
            raw.append(stream.getvalue())
        for count in range(1, 5):
            result = alert_images.reference_collage(
                [io.BytesIO(x) for x in raw[:count]]
            )
            with Image.open(io.BytesIO(result)) as image:
                self.assertEqual(image.size, (1280, 1280))
                for colour, (x, y, right, bottom) in zip(
                    colours, alert_images.cells(count)
                ):
                    actual = image.getpixel(((x + right) // 2, (y + bottom) // 2))
                    self.assertTrue(all(abs(a - b) < 5 for a, b in zip(actual, colour)))
        with self.assertRaises(ValueError):
            alert_images.reference_collage([io.BytesIO(raw[0])] * 5)

    def test_only_deduplicated_vinted_photos_are_retained(self):
        root = "https://images1.vinted.net/"
        item = SimpleNamespace(
            photo=root + "a.jpg",
            raw_data={
                "photos": [
                    {"url": root + "a.jpg"},
                    {"url": root + "b.jpg"},
                    {"url": "https://127.0.0.1/private"},
                    {"url": root + "c.jpg"},
                    {"url": root + "d.jpg"},
                    {"url": root + "e.jpg"},
                ]
            },
        )
        self.assertEqual(
            alert_images.photo_urls(item), [root + c + ".jpg" for c in "abcd"]
        )
        for url in (
            "https://vinted.net.evil.test/x",
            "http://vinted.net/x",
            "https://secret@vinted.net/x",
            "https://vinted.net:123/x",
        ):
            self.assertIsNone(alert_images.safe_listing_photo(url))


class UploadTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from web_ui_plugin.web_ui import create_app

        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()

    owner = dashboard_tests.DashboardTests.owner
    form = dashboard_tests.DashboardTests.form

    def add_photos(self, count=1, **changes):
        form = self.form(
            photo=[(io.BytesIO(photo_bytes()), f"{i}.png") for i in range(count)]
        )
        form["revision"] = str(settings.get_search(1)["revision"])
        form.update(changes)
        return self.client.post("/search/1", data=form)

    def test_add_later_keeps_original_and_remove_only_selected_slot(self):
        self.owner()
        before = [r[:3] for r in db.get_queries()]
        self.assertEqual(self.add_photos().status_code, 302)
        first = store.reference_photos(1)[0]["media_id"]
        for count in (2, 3, 4):
            self.assertEqual(self.add_photos().status_code, 302)
            slots = store.reference_photos(1)
            self.assertEqual(len(slots), count)
            self.assertEqual(slots[0]["media_id"], first)
        original_row = settings.get_search(1)
        self.assertEqual(self.add_photos().status_code, 200)
        self.assertEqual(settings.get_search(1), original_row)
        # Make room and append in one atomic save, retaining the other originals.
        self.assertEqual(self.add_photos(remove_reference=["1", "2"]).status_code, 302)
        self.assertEqual(len(store.reference_photos(1)), 3)
        self.assertEqual(store.reference_photos(1)[0]["media_id"], first)
        self.assertEqual([r[:3] for r in db.get_queries()], before)
        self.assertEqual(settings.get_search(1)["rebaseline"], 0)
        self.assertEqual(
            self.add_photos(0, remove_reference=["0", "1", "2"]).status_code, 302
        )
        self.assertEqual(store.reference_photos(1), [])
        self.assertIsNone(settings.get_search(1)["reference_id"])

    def test_stale_addition_and_invalid_removal_are_atomic(self):
        self.owner()
        self.add_photos()
        before = settings.get_search(1), store.reference_photos(1)
        self.assertEqual(self.add_photos(revision="0").status_code, 200)
        self.assertEqual(self.add_photos(remove_reference=["3"]).status_code, 200)
        self.assertEqual((settings.get_search(1), store.reference_photos(1)), before)

    def test_migration_preserves_existing_flattened_collage_and_poll_interval(self):
        self.owner()
        self.add_photos(2)
        before = settings.get_search(1)
        with closing(settings.connection()) as conn, conn:
            conn.execute("DROP TABLE search_reference_photos")
            conn.execute(
                "UPDATE parameters SET value='8' WHERE key='msj_search_schema'"
            )
            conn.execute(
                "UPDATE parameters SET value='3' WHERE key='query_refresh_delay'"
            )
        settings.ensure_schema()
        self.assertEqual(settings.get_search(1), before)
        self.assertEqual(
            store.reference_photos(1),
            [{"position": 0, "media_id": before["reference_id"]}],
        )
        self.assertEqual(db.get_parameter("query_refresh_delay"), "3")
        with closing(settings.connection()) as conn, conn:
            conn.execute("UPDATE dashboard_media SET created=0")
        self.assertEqual(self.add_photos().status_code, 302)
        self.assertEqual(len(store.reference_photos(1)), 2)
        self.assertIsNotNone(store.get_media(before["reference_id"]))

    def test_batch_upload_preview_route_auth_and_no_search_reset(self):
        self.assertEqual(
            self.client.post("/search/1/preview-notification").status_code, 400
        )
        self.owner()
        before = db.get_queries()
        form = self.form(
            photo=[(io.BytesIO(photo_bytes()), f"{i}.png") for i in range(4)]
        )
        self.assertEqual(self.client.post("/search/1", data=form).status_code, 302)
        row = settings.get_search(1)
        self.assertEqual([r[:3] for r in before], [r[:3] for r in db.get_queries()])
        self.assertEqual(row["rebaseline"], 0)
        with Image.open(
            io.BytesIO(store.get_media(row["reference_id"])["image"])
        ) as image:
            self.assertEqual(image.size, (1280, 1280))
        invalid = self.form(
            photo=[(io.BytesIO(photo_bytes()), f"{i}.png") for i in range(5)],
        )
        invalid["revision"] = str(row["revision"])
        self.assertEqual(self.client.post("/search/1", data=invalid).status_code, 200)
        self.assertEqual(settings.get_search(1), row)
        response = self.client.post(
            "/search/1/preview-notification", data={"csrf": "offline-csrf"}
        )
        self.assertEqual(response.location, "/search/1")
        self.assertFalse(vinted_alerts.enabled())


class RichWorkerTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        gallery = patch.object(
            vinted_gallery, "fetch_gallery", return_value=([], "unavailable")
        )
        gallery.start()
        self.addCleanup(gallery.stop)
        db.set_parameter("vinted_single_message_alerts", "1")
        store.save_search(
            1,
            {
                "query_name": "Fur & cuffs",
                "query": db.get_queries()[0][1],
                "revision": "0",
                "max_buy": "12",
                "reminder": "Check <label>",
            },
            photo_bytes(),
        )
        self.bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
            send_photo=AsyncMock(),
            do_api_request=AsyncMock(
                return_value={"message_id": 42, "rich_message": {"blocks": []}}
            ),
        )
        self.worker = delivery.VintedDeliveryWorker(self.bot, "123")

    async def asyncTearDown(self):
        await self.worker.close()

    async def test_slow_photo_never_blocks_new_link_and_edit_keeps_message_id(self):
        self.batch(1, [110])
        gate, started = asyncio.Event(), asyncio.Event()

        async def slow(urls):
            started.set()
            await gate.wait()
            return photo_bytes()

        with patch.object(self.worker, "send_slot_delay", return_value=0), patch.object(
            alert_images, "listing_collage", side_effect=slow
        ):
            await self.worker.tick(now=1000)
            await self.worker.tick(now=1002)
            await asyncio.wait_for(started.wait(), 1)
            self.batch(2, [111])
            await asyncio.wait_for(self.worker.tick(now=1004), 0.5)
            self.assertEqual(outbox(111)["status"], "sent")
            self.assertFalse(self.worker.photo_task.done())
            gate.set()
            await self.worker.photo_task
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.bot.send_photo.assert_not_awaited()
        call = self.bot.do_api_request.await_args
        self.assertEqual(call.args, ("editMessageText",))
        self.assertEqual(call.kwargs["api_kwargs"]["message_id"], 42)
        html = call.kwargs["api_kwargs"]["rich_message"]["html"]
        ordered = [
            "#1 · Fur &amp; cuffs",
            "Open Vinted listing",
            "Hollister fur jacket",
            "Price:",
            "Brand:",
            "id=listing",
            "id=reference",
            "Your buying guide",
            "Your buying reminder",
        ]
        self.assertEqual(
            [html.index(x) for x in ordered], sorted(html.index(x) for x in ordered)
        )
        self.assertIn("Check &lt;label&gt;", html)
        self.assertEqual(outbox(110)["photo_status"], "sent")

    async def test_failed_edit_retries_without_resending_original_notification(self):
        self.batch(1, [110])
        with patch.object(self.worker, "send_slot_delay", return_value=0), patch.object(
            alert_images, "listing_collage", new=AsyncMock(return_value=photo_bytes())
        ):
            await self.worker.tick(now=1000)
            self.bot.do_api_request.side_effect = NetworkError("offline")
            await self.worker.tick(now=1002)
            await self.worker.photo_task
            self.assertEqual(outbox(110)["photo_status"], "pending")
            self.bot.do_api_request.side_effect = BadRequest("Message is not modified")
            await self.worker.tick(now=1005)
            await self.worker.photo_task
        self.bot.send_message.assert_awaited_once()
        self.bot.send_photo.assert_not_awaited()
        self.assertEqual(outbox(110)["photo_status"], "sent")

    async def test_queued_guide_is_a_snapshot_and_fast_link_is_above_details(self):
        self.batch(1, [110])
        row = outbox(110)
        settings.update_search(1, "reminder", "New reminder")
        details = vinted_alerts.get_details(row)
        self.assertEqual(details["reminder"], "Check &lt;label&gt;")
        self.assertLess(
            row["content"].index("Open Vinted"),
            row["content"].index("Hollister fur jacket"),
        )
        await self.worker.tick(now=1000)
        self.assertTrue(
            self.bot.send_message.await_args.kwargs["link_preview_options"].is_disabled
        )

    async def test_preview_enables_native_delivery_only_after_confirmed_media_edit(
        self,
    ):
        self.batch(1, [110])
        db.set_parameter("vinted_single_message_alerts", "0")
        db.set_parameter("telegram_token", "123456:offline-test-token")
        with closing(settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE alert_outbox SET photo_url='https://images1.vinted.net/a.jpg'"
            )
        context = AsyncMock()
        context.__aenter__.return_value = self.bot
        self.bot.send_photo.return_value = SimpleNamespace(message_id=42, photo=[1])
        self.bot.edit_message_media = AsyncMock(
            return_value=SimpleNamespace(message_id=42, photo=[1])
        )
        with patch("telegram.Bot", return_value=context), patch.object(
            alert_images, "listing_collage", new=AsyncMock(return_value=photo_bytes())
        ):
            self.bot.edit_message_media.side_effect = BadRequest("Invalid media")
            with self.assertRaises(BadRequest):
                await vinted_alerts.preview_and_enable(1)
            self.assertFalse(vinted_alerts.enabled())
            self.assertNotEqual(db.get_parameter("vinted_native_photo_alerts"), "1")
            self.bot.edit_message_media.side_effect = None
            await vinted_alerts.preview_and_enable(1)
            self.assertTrue(vinted_alerts.enabled())
            self.assertEqual(db.get_parameter("vinted_native_photo_alerts"), "1")
        self.bot.send_message.assert_not_awaited()
        self.bot.do_api_request.assert_not_awaited()
        self.assertEqual(
            self.bot.edit_message_media.await_args.kwargs["message_id"], 42
        )

    async def test_phone_preview_uses_native_photo_without_changing_live_mode(
        self,
    ):
        self.batch(1, [110])
        db.set_parameter("vinted_single_message_alerts", "0")
        db.set_parameter("telegram_token", "123456:offline-test-token")
        with closing(settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE alert_outbox SET photo_url='https://images1.vinted.net/a.jpg'"
            )
        context = AsyncMock()
        context.__aenter__.return_value = self.bot
        self.bot.send_photo.return_value = SimpleNamespace(message_id=42, photo=[1])
        with patch("telegram.Bot", return_value=context), patch.object(
            alert_images, "listing_collage", new=AsyncMock(return_value=photo_bytes())
        ) as collage, patch.object(
            vinted_gallery, "resolve", new=AsyncMock()
        ) as gallery, patch.object(
            store, "get_media"
        ) as reference:
            for enabled in ("0", "1"):
                db.set_parameter("vinted_single_message_alerts", enabled)
                result = await vinted_alerts.preview_and_enable(1, photo_first=True)
                self.assertEqual(
                    db.get_parameter("vinted_single_message_alerts"), enabled
                )
                self.assertEqual(
                    result, {"photo_count": 1, "gallery_state": "catalogue"}
                )
            gallery.assert_not_awaited()
            reference.assert_not_called()
            self.assertEqual(len(collage.await_args.args[0]), 1)
        self.bot.send_message.assert_not_awaited()
        self.bot.do_api_request.assert_not_awaited()
        data = self.bot.send_photo.await_args.kwargs
        self.assertIn("STANDARD PHOTO TEST", data["caption"])
        self.assertLess(
            data["caption"].index("Open Vinted"),
            data["caption"].index("Hollister fur jacket"),
        )
        self.assertTrue(data["show_caption_above_media"])
        self.assertEqual(data["parse_mode"], "HTML")
        self.assertEqual(data["photo"].filename, "vinted-listing.jpg")


class WireRequest(BaseRequest):
    read_timeout = 8

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        self.data = request_data
        return 200, json.dumps({"ok": True, "result": {"message_id": 42}}).encode()


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_uploads_both_images_in_one_edit_request(self):
        request = WireRequest()
        bot = Bot(
            "123456:offline-test-token", request=request, get_updates_request=request
        )
        row = {
            "query_id": 50,
            "search_name": "Jeans",
            "url": "https://www.vinted.co.uk/items/110",
            "title": "Jeans",
            "price": "15",
            "currency": "GBP",
            "telegram_message_id": 42,
        }
        details = {
            "name": "Jeans",
            "brand": "7 for all mankind",
            "guide": "Buy up to £15",
            "reminder": "Check pockets",
        }
        data = vinted_alerts.rich_request(row, details, photo_bytes(), photo_bytes())
        data["chat_id"] = "123"
        await bot.do_api_request("editMessageText", api_kwargs=data)
        payload = request.data.json_parameters
        rich = json.loads(payload["rich_message"])
        self.assertEqual(len(rich["media"]), 2)
        self.assertEqual(set(request.data.multipart_data), {"listing", "reference"})
        self.assertEqual(rich["media"][0]["media"]["media"], "attach://listing")


if __name__ == "__main__":
    unittest.main()
