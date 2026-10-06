"""Working phone-photo transport, same-message controls and deletion races."""

import asyncio
import json
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest, NetworkError
from test_dashboard import photo_bytes
from test_ebay_monitor import EbayFixture, item
from test_finds_delivery import outbox
from test_search_controls import DatabaseFixture

import alert_delivery
import alert_images
import dashboard_store
import ebay_alerts
import ebay_privacy
import photo_cards
import search_settings
import vinted_alerts


def message(number=42, file_id="listing-photo"):
    return SimpleNamespace(message_id=number, photo=[SimpleNamespace(file_id=file_id)])


def bot():
    return SimpleNamespace(
        send_photo=AsyncMock(return_value=message()),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=42, photo=[])),
        edit_message_media=AsyncMock(return_value=message(file_id="example-photo")),
        edit_message_caption=AsyncMock(),
        edit_message_text=AsyncMock(),
        do_api_request=AsyncMock(),
    )


def query(data="card:examples", chat_id=123):
    return SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        message=SimpleNamespace(message_id=42, chat=SimpleNamespace(id=chat_id)),
    )


class PhotoCardTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        photo_cards.enable()
        dashboard_store.save_search(
            1,
            {
                "query_name": "Jeans",
                "query": search_settings.get_search(1)["query"],
                "revision": "0",
                "max_buy": "15",
                "reminder": "Check <label> & pockets",
            },
            photo_bytes(),
        )
        self.batch(1, [110])
        self.row = outbox(110)
        self.details = vinted_alerts.get_details(self.row)
        self.details["photos"] = [
            "https://images1.vinted.net/a.jpg",
            "https://images1.vinted.net/b.jpg",
        ]
        description = patch(
            "vinted_gallery.fetch_listing",
            return_value={
                "photos": [],
                "description": "Soft cotton with a small mark on one cuff.",
                "state": "ready",
            },
        )
        description.start()
        self.addCleanup(description.stop)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_alert_details SET payload=? WHERE item_id='110'",
                (json.dumps(self.details),),
            )
        self.bot = bot()
        self.worker = alert_delivery.VintedDeliveryWorker(self.bot, "123")
        self.worker.send_slot_delay = lambda: 0
        download = patch.object(
            alert_images, "listing_collage", new=AsyncMock(return_value=photo_bytes())
        )
        self.download = download.start()
        self.addCleanup(download.stop)

    async def asyncTearDown(self):
        await self.worker.close()

    async def send(self):
        await self.worker.tick(now=1000)
        await self.worker.tick(now=1002)
        if self.worker.photo_task:
            await self.worker.photo_task

    async def test_actual_worker_sends_photo_notes_and_controls_once_without_conversion(
        self,
    ):
        self.assertTrue(self.details["photo_card"])
        await self.send()
        self.bot.send_photo.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()
        self.bot.do_api_request.assert_not_awaited()
        self.bot.edit_message_media.assert_not_awaited()
        sent = self.bot.send_photo.call_args.kwargs
        self.assertIn("Check &lt;label&gt; &amp; pockets", sent["caption"])
        self.assertIn("£15.00", sent["caption"])
        self.assertTrue(sent["show_caption_above_media"])
        self.assertEqual(sent["photo"].input_file_content, photo_bytes())
        self.assertEqual(
            [b.text for r in sent["reply_markup"].inline_keyboard for b in r],
            ["Open Vinted listing ↗", "Autobuy", "Listing photos", "Your examples"],
        )
        self.download.assert_awaited_once_with(self.details["photos"])
        self.assertEqual(outbox(110)["photo_status"], "sent")

    async def test_description_edits_original_photo_caption_and_survives_examples(self):
        await self.send()
        self.assertIn(
            "Soft cotton with a small mark",
            self.bot.edit_message_caption.call_args.kwargs["caption"],
        )
        self.assertEqual(
            self.bot.edit_message_caption.call_args.kwargs["message_id"], 42
        )
        await photo_cards.handle_callback(self.bot, query(), "vinted", "123")
        self.assertIn(
            "Soft cotton with a small mark",
            self.bot.edit_message_media.call_args.kwargs["media"].caption,
        )
        self.bot.send_photo.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()

    async def test_failed_description_edit_retries_cached_text_without_losing_buy_status(
        self,
    ):
        await photo_cards.send_initial(
            self.bot, "123", self.row, self.details, AsyncMock()
        )
        row = dict(self.row, telegram_message_id=42)
        with closing(search_settings.connection()) as conn, conn:
            details = photo_cards.load("vinted", 42)[1]
            details["buy_feedback"] = {
                "state": "unknown",
                "message": "Payment unconfirmed. Check Vinted before retrying.",
            }
            conn.execute(
                "UPDATE telegram_photo_cards SET details=?", (json.dumps(details),)
            )
        self.bot.edit_message_caption.side_effect = [NetworkError("offline"), None]
        with self.assertRaises(NetworkError):
            await photo_cards.enrich(self.bot, "123", row, self.details, AsyncMock())
        self.assertTrue(
            await photo_cards.enrich(self.bot, "123", row, self.details, AsyncMock())
        )
        self.assertEqual(self.bot.edit_message_caption.await_count, 2)
        text = self.bot.edit_message_caption.call_args.kwargs["caption"]
        self.assertIn("Soft cotton", text)
        self.assertIn("Payment unconfirmed", text)
        self.bot.send_photo.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()

    async def test_examples_then_listing_change_only_same_message_and_reuse_files(self):
        await self.send()
        await photo_cards.handle_callback(self.bot, query(), "vinted", "123")
        first = self.bot.edit_message_media.call_args.kwargs
        self.assertEqual(first["message_id"], 42)
        self.assertIn("Check &lt;label&gt;", first["media"].caption)
        await photo_cards.handle_callback(
            self.bot, query("card:listing"), "vinted", "123"
        )
        self.assertEqual(
            self.bot.edit_message_media.call_args.kwargs["media"].media, "listing-photo"
        )
        await photo_cards.handle_callback(self.bot, query(), "vinted", "123")
        self.assertEqual(
            self.bot.edit_message_media.call_args.kwargs["media"].media, "example-photo"
        )
        self.bot.send_photo.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()
        self.download.assert_awaited_once()

    async def test_wrong_chat_cannot_read_example_or_change_message(self):
        await self.send()
        q = query(chat_id=999)
        await photo_cards.handle_callback(self.bot, q, "vinted", "123")
        self.bot.edit_message_media.assert_not_awaited()
        self.assertTrue(q.answer.call_args.kwargs["show_alert"])

    async def test_expired_callback_acknowledgement_does_not_prevent_photo_edit(self):
        await self.send()
        q = query()
        q.answer.side_effect = BadRequest("Query is too old")
        await photo_cards.dispatch_callback(self.bot, q, "vinted", "123")
        self.bot.edit_message_media.assert_awaited_once()

    async def test_example_file_cache_is_reused_between_alerts_but_never_bots(self):
        await self.send()
        await photo_cards.handle_callback(self.bot, query(), "vinted", "123")
        self.assertEqual(
            photo_cards.cached_example(self.bot, "vinted", self.row["reference_id"]),
            "example-photo",
        )
        other = SimpleNamespace(token="different-bot")
        self.assertIsNone(
            photo_cards.cached_example(other, "vinted", self.row["reference_id"])
        )
        self.assertIsNone(
            photo_cards.cached_example(self.bot, "ebay", self.row["reference_id"])
        )
        photo_cards.record(self.row, self.details, message(number=43))
        q = query()
        q.message.message_id = 43
        with patch.object(
            dashboard_store,
            "get_media",
            side_effect=AssertionError("cached examples must not reupload"),
        ):
            await photo_cards.handle_callback(self.bot, q, "vinted", "123")
        self.assertEqual(
            self.bot.edit_message_media.call_args.kwargs["media"].media, "example-photo"
        )

    async def test_lost_message_bookkeeping_recovers_existing_saved_alert(self):
        await self.send()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DELETE FROM telegram_photo_cards")
        q = query()
        q.message.photo = [SimpleNamespace(file_id="listing-photo")]
        await photo_cards.dispatch_callback(self.bot, q, "vinted", "123")
        self.bot.edit_message_media.assert_awaited_once()
        self.assertIsNotNone(photo_cards.load("vinted", 42))

    async def test_invalid_cached_file_id_recovers_from_saved_image(self):
        await self.send()
        photo_cards.cache_example(
            self.bot, "vinted", self.row["reference_id"], "expired-file"
        )
        self.bot.edit_message_media.side_effect = [
            BadRequest("Wrong file identifier"),
            message(file_id="fresh-file"),
        ]
        await photo_cards.dispatch_callback(self.bot, query(), "vinted", "123")
        self.assertEqual(self.bot.edit_message_media.await_count, 2)
        self.assertEqual(
            photo_cards.cached_example(self.bot, "vinted", self.row["reference_id"]),
            "fresh-file",
        )

    async def test_one_corrupt_image_does_not_kill_callback_listener(self):
        await self.send()
        self.bot.edit_message_media.side_effect = ValueError("bad image")
        q = query()
        await photo_cards.dispatch_callback(self.bot, q, "vinted", "123")
        self.assertIn("tap the button again", q.answer.call_args.args[0])
        self.bot.edit_message_media.side_effect = None
        await photo_cards.dispatch_callback(self.bot, query(), "vinted", "123")
        self.assertEqual(photo_cards.load("vinted", 42)[2]["view"], "examples")

    async def test_long_emoji_notes_fit_caption_and_all_pages_preserve_every_character(
        self,
    ):
        details = dict(
            self.details,
            name="🧥" * 100,
            brand="👖" * 120,
            reminder="🧵&lt;label&gt; " * 160,
            guide="Buy up to <b>£15</b>",
        )
        row = dict(self.row, title="😀" * 500)
        text, note_pages = photo_cards.captions(row, details)
        self.assertLessEqual(photo_cards.units(photo_cards.plain(text)), 1024)
        expected = "\n\n".join(
            photo_cards.plain(p) for p in vinted_alerts.sections(row, details)[3:] if p
        )
        self.assertEqual("".join(note_pages), expected)
        await photo_cards.send_initial(self.bot, "123", row, details, AsyncMock())
        for index in range(len(note_pages)):
            await photo_cards.handle_callback(
                self.bot, query(f"card:notes:{index}"), "vinted", "123"
            )
            args = self.bot.edit_message_caption.call_args.kwargs
            self.assertEqual(args["message_id"], 42)
            self.assertLessEqual(
                photo_cards.units(photo_cards.plain(args["caption"])), 1024
            )
            self.assertIn(note_pages[index], photo_cards.plain(args["caption"]))
        self.bot.send_photo.assert_awaited_once()

    async def test_missing_photo_fallback_and_retry_edit_never_send_second_notification(
        self,
    ):
        self.download.return_value = None
        await self.worker.tick(now=1000)
        self.bot.send_message.assert_awaited_once()
        self.download.return_value = photo_bytes()
        self.bot.edit_message_media.side_effect = NetworkError("offline")
        await self.worker.tick(now=1002)
        await self.worker.photo_task
        self.assertEqual(outbox(110)["photo_status"], "pending")
        self.bot.edit_message_media.side_effect = None
        await self.worker.tick(now=1020)
        await self.worker.photo_task
        self.assertEqual(outbox(110)["photo_status"], "sent")
        self.bot.send_message.assert_awaited_once()
        self.bot.send_photo.assert_not_awaited()
        self.assertEqual(self.bot.edit_message_media.call_args.kwargs["message_id"], 42)


class EbayPhotoCardTests(EbayFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        photo_cards.enable()
        self.search = self.enable(reminder="Check pockets")
        self.snapshot(self.search, [], 1000)
        self.snapshot(self.search, [item(123, seller={"username": "seller-one"})], 1020)
        self.row = self.outbox()[0]
        self.details = ebay_alerts.get_details(self.row)
        self.bot = bot()
        download = patch.object(
            alert_images, "listing_collage", new=AsyncMock(return_value=photo_bytes())
        )
        download.start()
        self.addCleanup(download.stop)

    def delete_seller(self):
        with closing(search_settings.connection()) as conn, conn:
            digest = conn.execute(
                "SELECT seller_hash FROM ebay_item_owners WHERE item_id=?",
                (self.row["item_id"],),
            ).fetchone()[0]
            ebay_privacy.purge(conn, {digest}, redact=True)

    async def test_actual_ebay_worker_uses_photo_in_first_delivery(self):
        worker = alert_delivery.EbayPhotoDeliveryWorker(self.bot, "123", "456")
        worker.send_slot_delay = lambda: 0
        try:
            await worker.tick(now=1100)
            await worker.tick(now=1102)
            await worker.photo_task
        finally:
            await worker.close()
        self.bot.send_photo.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()
        self.assertIn(
            "Open eBay listing", self.bot.send_photo.call_args.kwargs["caption"]
        )
        self.assertEqual(
            photo_cards.load("ebay", 42)[0]["item_id"], self.row["item_id"]
        )

    async def test_deletion_cascades_cached_photos_and_disables_controls(self):
        await photo_cards.send_initial(
            self.bot, "123", self.row, self.details, AsyncMock()
        )
        self.delete_seller()
        self.assertIsNone(photo_cards.load("ebay", 42))
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT message_id FROM ebay_privacy_redactions"
                ).fetchone()[0],
                42,
            )
        await photo_cards.handle_callback(
            self.bot, query("card:listing"), "ebay", "123"
        )
        self.bot.edit_message_media.assert_not_awaited()

    async def test_deletion_during_initial_send_tracks_message_for_redaction(self):
        async def racing_send(**kwargs):
            self.delete_seller()
            return message()

        self.bot.send_photo.side_effect = racing_send
        await photo_cards.send_initial(
            self.bot, "123", self.row, self.details, AsyncMock()
        )
        self.assertIsNone(photo_cards.load("ebay", 42))
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT message_id FROM ebay_privacy_redactions"
                ).fetchone()[0],
                42,
            )

    async def test_deletion_during_callback_requeues_redaction_after_its_edit(self):
        await photo_cards.send_initial(
            self.bot, "123", self.row, self.details, AsyncMock()
        )

        async def racing_edit(**kwargs):
            self.delete_seller()
            with closing(search_settings.connection()) as conn, conn:
                conn.execute("DELETE FROM ebay_privacy_redactions")
            return message()

        self.bot.edit_message_media.side_effect = racing_edit
        await photo_cards.handle_callback(
            self.bot, query("card:listing"), "ebay", "123"
        )
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT message_id FROM ebay_privacy_redactions"
                ).fetchone()[0],
                42,
            )

    async def test_telegram_callback_listener_passes_owner_chat_and_advances_offset(
        self,
    ):
        update = SimpleNamespace(update_id=9, callback_query=query("card:listing"))
        self.bot.get_updates = AsyncMock(
            side_effect=[[update], asyncio.CancelledError()]
        )
        with patch.object(photo_cards, "handle_callback", new=AsyncMock()) as handler:
            with self.assertRaises(asyncio.CancelledError):
                await photo_cards.poll_ebay_callbacks(self.bot, "123")
            handler.assert_awaited_once_with(
                self.bot, update.callback_query, "ebay", "123"
            )
        self.assertEqual(self.bot.get_updates.call_args.kwargs["offset"], 10)
