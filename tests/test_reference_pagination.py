"""Eight saved references toggle in-place without touching listing/buy actions."""

import asyncio
import io
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image
from telegram.error import BadRequest, NetworkError, RetryAfter
from test_ebay_monitor import EbayFixture, item
from test_finds_delivery import outbox
from test_photo_cards import bot, message, query
from test_search_controls import DatabaseFixture

import alert_images
import dashboard_store
import ebay_alerts
import ebay_privacy
import photo_cards
import search_settings
import vinted_alerts


def image_bytes(index):
    stream = io.BytesIO()
    Image.new("RGB", (120, 180), (index * 27, 220 - index * 19, index * 11)).save(
        stream, "PNG"
    )
    return dashboard_store.normalize_photo(io.BytesIO(stream.getvalue()))


def media_bytes(media):
    return media.media.input_file_content


def labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


class ReferencePaginationTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        photo_cards.enable()
        self.originals = [image_bytes(index) for index in range(8)]
        dashboard_store.save_search(
            1,
            {
                "query_name": "Eight examples",
                "query": search_settings.get_search(1)["query"],
                "revision": "0",
                "max_buy": "15",
                "reminder": "Check seams",
            },
            photos=self.originals,
        )
        self.batch(1, [110])
        self.row = outbox(110)
        self.details = dict(
            vinted_alerts.get_details(self.row),
            photos=["https://images1.vinted.net/listing.jpg"],
            description="Seller description",
            description_checked=True,
        )
        self.references = dashboard_store.get_reference_pages(self.row["reference_id"])
        self.bot = bot()
        download = patch.object(
            alert_images, "listing_collage", new=AsyncMock(return_value=image_bytes(0))
        )
        self.download = download.start()
        self.addCleanup(download.stop)

    async def send(self):
        await photo_cards.send_initial(
            self.bot, "123", self.row, self.details, AsyncMock()
        )

    async def tap(self, data):
        await photo_cards.dispatch_callback(self.bot, query(data), "vinted", "123")

    async def test_initial_controls_and_first_four_preserve_listing_and_autobuy(self):
        await self.send()
        initial = self.bot.send_photo.call_args.kwargs
        self.assertEqual(
            labels(initial["reply_markup"]),
            [
                "Open Vinted listing ↗",
                "Autobuy",
                "Show additional reference pictures",
                "Listing photos",
                "Your examples",
            ],
        )
        self.bot.edit_message_media.return_value = message(file_id="first-four")
        await self.tap("card:examples")
        edit = self.bot.edit_message_media.call_args.kwargs
        self.assertEqual(edit["message_id"], 42)
        self.assertEqual(
            media_bytes(edit["media"]), alert_images.collage(self.originals[:4])
        )
        self.assertIn("Autobuy", labels(edit["reply_markup"]))
        self.assertEqual(edit["media"].caption, initial["caption"])
        self.bot.send_photo.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()

    async def test_next_four_and_back_use_same_message_and_independent_cached_files(
        self,
    ):
        await self.send()
        self.bot.edit_message_media.side_effect = [
            message(file_id="second-four"),
            message(file_id="first-four"),
            message(file_id="second-four"),
        ]
        await self.tap("card:references:1")
        edit = self.bot.edit_message_media.call_args.kwargs
        self.assertEqual(
            media_bytes(edit["media"]), alert_images.collage(self.originals[4:])
        )
        self.assertIn("Show initial reference pictures", labels(edit["reply_markup"]))
        self.assertEqual(photo_cards.load("vinted", 42)[2]["view"], "examples:1")
        await self.tap("card:references:0")
        self.assertEqual(
            media_bytes(self.bot.edit_message_media.call_args.kwargs["media"]),
            alert_images.collage(self.originals[:4]),
        )
        self.assertIn(
            "Show additional reference pictures",
            labels(self.bot.edit_message_media.call_args.kwargs["reply_markup"]),
        )
        await self.tap("card:references:1")
        self.assertEqual(
            self.bot.edit_message_media.call_args.kwargs["media"].media, "second-four"
        )
        self.assertEqual(self.bot.edit_message_media.call_args.kwargs["message_id"], 42)
        self.assertEqual(
            photo_cards.cached_example(self.bot, "vinted", self.references[0]["id"]),
            "first-four",
        )
        self.assertEqual(
            photo_cards.cached_example(self.bot, "vinted", self.references[1]["id"]),
            "second-four",
        )
        self.bot.send_photo.assert_awaited_once()

    async def test_double_tap_is_idempotent_and_does_not_flip_back(self):
        await self.send()
        await asyncio.gather(
            self.tap("card:references:1"), self.tap("card:references:1")
        )
        self.bot.edit_message_media.assert_awaited_once()
        self.assertEqual(photo_cards.load("vinted", 42)[2]["view"], "examples:1")

    async def test_editing_search_does_not_change_delivered_reference_snapshot(self):
        await self.send()
        current = search_settings.get_search(1)
        dashboard_store.save_search(
            1,
            {
                "query_name": "Changed examples",
                "query": current["query"],
                "revision": str(current["revision"]),
                "remove_photo": "yes",
            },
            photos=self.originals[:4] + [image_bytes(8)] * 4,
        )
        self.assertNotEqual(
            search_settings.get_search(1)["reference_id"], self.row["reference_id"]
        )
        await self.tap("card:references:1")
        self.assertEqual(
            media_bytes(self.bot.edit_message_media.call_args.kwargs["media"]),
            self.references[1]["image"],
        )
        self.assertEqual(
            photo_cards.load("vinted", 42)[1]["reference_page_ids"],
            [photo["id"] for photo in self.references],
        )

    async def test_wrong_sender_and_inaccessible_message_do_not_read_references(self):
        await self.send()
        for inaccessible in (False, True):
            q = query("card:references:1")
            if inaccessible:
                q.message.date = 0
            else:
                q.from_user.id = 999
            with patch.object(
                dashboard_store,
                "get_media",
                side_effect=AssertionError("Unauthorized photo read"),
            ):
                await photo_cards.dispatch_callback(self.bot, q, "vinted", "123")
            self.assertTrue(q.answer.call_args.kwargs["show_alert"])
        self.bot.edit_message_media.assert_not_awaited()

    async def test_rejected_or_timed_out_edit_keeps_previous_page_and_actions(self):
        await self.send()
        self.bot.edit_message_media.side_effect = BadRequest("Cannot edit message")
        q = query("card:references:1")
        await photo_cards.dispatch_callback(self.bot, q, "vinted", "123")
        self.assertEqual(photo_cards.load("vinted", 42)[2]["view"], "listing")
        self.assertIn("tap the button again", q.answer.call_args.args[0])
        self.bot.edit_message_media.side_effect = NetworkError("offline")
        await self.tap("card:references:1")
        self.assertEqual(photo_cards.load("vinted", 42)[2]["view"], "listing")
        self.bot.send_photo.assert_awaited_once()

    async def test_invalid_second_page_file_id_reuploads_only_that_page(self):
        await self.send()
        photo_cards.cache_example(
            self.bot, "vinted", self.references[1]["id"], "expired-second"
        )
        self.bot.edit_message_media.side_effect = [
            BadRequest("Wrong file identifier"),
            message(file_id="new-second"),
        ]
        await self.tap("card:references:1")
        self.assertEqual(self.bot.edit_message_media.await_count, 2)
        self.assertEqual(
            media_bytes(self.bot.edit_message_media.call_args.kwargs["media"]),
            self.references[1]["image"],
        )
        self.assertEqual(
            photo_cards.cached_example(self.bot, "vinted", self.references[1]["id"]),
            "new-second",
        )
        self.assertIsNone(
            photo_cards.cached_example(self.bot, "vinted", self.references[0]["id"])
        )

    async def test_caption_enrichment_and_notes_preserve_second_page_button(self):
        self.details["reminder"] = "Long reminder " * 120
        await self.send()
        await self.tap("card:references:1")
        await self.tap("card:notes:0")
        args = self.bot.edit_message_caption.call_args.kwargs
        self.assertIn("Show initial reference pictures", labels(args["reply_markup"]))
        await photo_cards.enrich(
            self.bot,
            "123",
            dict(self.row, telegram_message_id=42),
            self.details,
            AsyncMock(),
        )
        args = self.bot.edit_message_caption.call_args.kwargs
        self.assertIn("Show initial reference pictures", labels(args["reply_markup"]))
        self.assertTrue(photo_cards.load("vinted", 42)[2]["view"].startswith("notes:"))

    async def test_text_fallback_can_show_second_page_then_notes_and_enrichment(self):
        self.download.return_value = None
        self.details["reminder"] = "Long reminder " * 120
        await self.send()
        self.bot.send_message.assert_awaited_once()
        await self.tap("card:references:1")
        saved = photo_cards.load("vinted", 42)
        self.assertIsNotNone(saved[2]["example_file_id"])
        await self.tap("card:notes:0")
        self.bot.edit_message_text.assert_not_awaited()
        await photo_cards.enrich(
            self.bot,
            "123",
            dict(self.row, telegram_message_id=42),
            self.details,
            AsyncMock(),
        )
        self.bot.edit_message_text.assert_not_awaited()
        self.assertIn(
            "Show initial reference pictures",
            labels(self.bot.edit_message_caption.call_args.kwargs["reply_markup"]),
        )

    async def test_lost_accepted_edit_response_still_records_photo_presence(self):
        self.download.return_value = None
        self.details["reminder"] = "Long reminder " * 120
        await self.send()
        self.bot.edit_message_media.side_effect = [
            NetworkError("accepted response lost"),
            BadRequest("message is not modified"),
        ]
        await self.tap("card:references:1")
        saved = photo_cards.load("vinted", 42)
        self.assertEqual(saved[2]["view"], "examples:1")
        self.assertTrue(photo_cards.has_photo(saved[2], saved[1]))
        self.assertIsNone(saved[2]["example_file_id"])
        await self.tap("card:notes:0")
        await photo_cards.enrich(
            self.bot,
            "123",
            dict(self.row, telegram_message_id=42),
            self.details,
            AsyncMock(),
        )
        self.bot.edit_message_text.assert_not_awaited()
        self.assertIn(
            "Show initial reference pictures",
            labels(self.bot.edit_message_caption.call_args.kwargs["reply_markup"]),
        )

    async def test_owner_requested_control_diagnostic_exercises_both_pages_and_back(
        self,
    ):
        self.bot.send_photo.return_value.chat = SimpleNamespace(id=123)
        manager = AsyncMock()
        manager.__aenter__.return_value = self.bot
        self.bot.edit_message_media.side_effect = [
            message(file_id="diagnostic-first"),
            message(file_id="diagnostic-second"),
            message(file_id="diagnostic-first"),
            message(file_id="listing-photo"),
        ]
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE parameters SET value='123456:offline-test' WHERE key='telegram_token'"
            )
        with patch("telegram.Bot", return_value=manager), patch.object(
            photo_cards, "enrich", new=AsyncMock(return_value=True)
        ), patch("asyncio.sleep", new=AsyncMock()):
            result = await photo_cards.test_controls("vinted")
        self.assertIn("additional references", result)
        self.assertEqual(self.bot.edit_message_media.await_count, 4)
        self.assertEqual(photo_cards.load("vinted", 42)[2]["view"], "listing")
        for photo in self.references:
            self.assertIsNotNone(
                photo_cards.cached_example(self.bot, "vinted", photo["id"])
            )

    async def test_forged_additional_page_on_old_one_page_alert_is_rejected(self):
        row = dict(self.row, reference_id=self.references[0]["id"])
        details = dict(self.details, reference_page_ids=[self.references[0]["id"]])
        photo_cards.record(row, details, message())
        q = query("card:references:1")
        await photo_cards.dispatch_callback(self.bot, q, "vinted", "123")
        self.bot.edit_message_media.assert_not_awaited()
        self.assertIn("no additional", q.answer.call_args.args[0])

    async def test_telegram_rate_limit_keeps_same_page_without_resending(self):
        await self.send()
        self.bot.edit_message_media.side_effect = RetryAfter(30)
        await self.tap("card:references:1")
        self.assertEqual(photo_cards.load("vinted", 42)[2]["view"], "listing")
        self.bot.edit_message_media.assert_awaited_once()
        self.bot.send_photo.assert_awaited_once()


class EbayReferencePaginationTests(EbayFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        photo_cards.enable()
        dashboard_store.save_search(
            1,
            {
                "query_name": "Eight eBay examples",
                "query": search_settings.get_search(1)["query"],
                "revision": "0",
            },
            photos=[image_bytes(index) for index in range(8)],
        )
        self.search = self.enable(reminder="Check pockets")
        self.snapshot(self.search, [], 1000)
        self.snapshot(self.search, [item(123, seller={"username": "seller-one"})], 1020)
        self.row = self.outbox()[0]
        self.details = ebay_alerts.get_details(self.row)
        self.references = dashboard_store.get_reference_pages(self.row["reference_id"])
        self.bot = bot()
        download = patch.object(
            alert_images, "listing_collage", new=AsyncMock(return_value=image_bytes(0))
        )
        download.start()
        self.addCleanup(download.stop)

    async def test_ebay_next_and_initial_pages_keep_listing_controls_and_snapshot(self):
        await photo_cards.send_initial(
            self.bot, "123", self.row, self.details, AsyncMock()
        )
        initial = self.bot.send_photo.call_args.kwargs
        self.assertIn(
            "Show additional reference pictures", labels(initial["reply_markup"])
        )
        self.assertNotIn("Autobuy", labels(initial["reply_markup"]))
        await photo_cards.dispatch_callback(
            self.bot, query("card:references:1"), "ebay", "123"
        )
        edit = self.bot.edit_message_media.call_args.kwargs
        self.assertEqual(edit["message_id"], 42)
        self.assertEqual(media_bytes(edit["media"]), self.references[1]["image"])
        self.assertIn("Show initial reference pictures", labels(edit["reply_markup"]))
        self.assertIn("Open eBay listing ↗", labels(edit["reply_markup"]))
        self.assertNotIn("Autobuy", labels(edit["reply_markup"]))
        await photo_cards.dispatch_callback(
            self.bot, query("card:references:0"), "ebay", "123"
        )
        self.assertEqual(
            media_bytes(self.bot.edit_message_media.call_args.kwargs["media"]),
            self.references[0]["image"],
        )
        saved = photo_cards.load("ebay", 42)
        self.assertEqual(
            saved[1]["reference_page_ids"], [photo["id"] for photo in self.references]
        )
        self.assertEqual(saved[2]["view"], "examples")
        self.bot.send_photo.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()

    async def test_ebay_deletion_during_second_page_edit_requeues_redaction(self):
        await photo_cards.send_initial(
            self.bot, "123", self.row, self.details, AsyncMock()
        )

        async def racing_edit(**kwargs):
            with closing(search_settings.connection()) as conn, conn:
                digest = conn.execute(
                    "SELECT seller_hash FROM ebay_item_owners WHERE item_id=?",
                    (self.row["item_id"],),
                ).fetchone()[0]
                ebay_privacy.purge(conn, {digest}, redact=True)
                conn.execute("DELETE FROM ebay_privacy_redactions")
            return message()

        self.bot.edit_message_media.side_effect = racing_edit
        await photo_cards.dispatch_callback(
            self.bot, query("card:references:1"), "ebay", "123"
        )
        self.assertIsNone(photo_cards.load("ebay", 42))
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT message_id FROM ebay_privacy_redactions"
                ).fetchone()[0],
                42,
            )
