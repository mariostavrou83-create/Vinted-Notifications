"""Autobuy taps report blockers and outcomes on the existing Telegram alert."""

import asyncio
import json
import threading
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest, NetworkError
from test_search_controls import DatabaseFixture

import photo_cards
import search_settings
import vinted_alerts
import vinted_buyer as buyer
import vinted_buying as buying


class FeedbackTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.batch(1, [110])
        with closing(search_settings.connection()) as conn, conn:
            self.row = dict(
                conn.execute(
                    "SELECT * FROM alert_outbox WHERE item_id='110'"
                ).fetchone()
            )
            conn.execute(
                "UPDATE vinted_buyer SET session=?,verified_at=1,user_id='99',enabled=0",
                (
                    buyer.encrypt(
                        {"cookies": {"access_token_web": "offline-test-token"}}
                    ),
                ),
            )
        self.details = vinted_alerts.get_details(self.row)
        self.query = SimpleNamespace(
            data="buy:click",
            answer=AsyncMock(),
            from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(
                message_id=42,
                chat=SimpleNamespace(id=123),
                photo=[SimpleNamespace(file_id="listing-photo")],
            ),
        )
        photo_cards.record(self.row, self.details, self.query.message)
        self.bot = SimpleNamespace(
            edit_message_caption=AsyncMock(),
            edit_message_text=AsyncMock(),
            send_message=AsyncMock(),
        )
        self.context = SimpleNamespace(bot=self.bot)
        env = patch.dict(
            "os.environ", {"DASHBOARD_URL": "https://dashboard.example.test"}
        )
        env.start()
        self.addCleanup(env.stop)

    async def tap(self):
        await buying.callback(SimpleNamespace(callback_query=self.query), self.context)

    def buttons(self):
        return [
            b
            for row in self.bot.edit_message_caption.call_args.kwargs[
                "reply_markup"
            ].inline_keyboard
            for b in row
        ]

    def enable(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET enabled=1")
            conn.execute("INSERT INTO vinted_search_budgets VALUES (1,2000,350)")

    async def test_off_answers_once_with_reason_and_persists_setup_links(self):
        with patch.object(buying, "buy") as buy:
            await self.tap()
        buy.assert_not_called()
        self.query.answer.assert_awaited_once()
        self.assertIn("Autobuy is off", self.query.answer.call_args.args[0])
        self.assertTrue(self.query.answer.call_args.kwargs["show_alert"])
        buttons = self.buttons()
        self.assertIn(
            "https://dashboard.example.test/search/1", [b.url for b in buttons]
        )
        self.assertIn(
            "https://dashboard.example.test/connections#vinted-buying",
            [b.url for b in buttons],
        )
        self.assertIn("buy:status", [b.callback_data for b in buttons])
        self.assertIn("buy:click", [b.callback_data for b in buttons])
        self.assertIn(
            "Autobuy is off", self.bot.edit_message_caption.call_args.kwargs["caption"]
        )
        self.bot.send_message.assert_not_awaited()
        self.assertIsNone(buying.result("110"))
        row, details, _ = photo_cards.load("vinted", 42)
        self.assertEqual(details["buy_feedback"]["state"], "setup_required")
        self.assertIn(
            "buy:status",
            [
                b.callback_data
                for r in photo_cards.markup(row, details).inline_keyboard
                for b in r
            ],
        )

    async def test_expired_popup_does_not_hide_the_persistent_reason(self):
        self.query.answer.side_effect = BadRequest("Query is too old")
        await self.tap()
        self.bot.edit_message_caption.assert_awaited_once()
        self.assertIn(
            "Autobuy is off",
            photo_cards.load("vinted", 42)[1]["buy_feedback"]["message"],
        )

    async def test_invalid_url_limit_stops_before_any_purchase(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET enabled=1")
            conn.execute(
                "UPDATE queries SET query='https://www.vinted.co.uk/catalog?price_to=NaN' WHERE id=1"
            )
        with patch.object(buying, "buy") as buy:
            await self.tap()
        buy.assert_not_called()
        self.assertIn("Search #1", self.query.answer.call_args.args[0])
        self.assertEqual(
            photo_cards.load("vinted", 42)[1]["buy_feedback"]["reason"],
            "url_limit_invalid",
        )

    async def test_checkout_failure_uses_persistent_status_not_a_second_answer(self):
        self.enable()
        outcome = {
            "state": "failed_before_payment",
            "message": "Vinted needs delivery details.",
        }
        with patch.object(buying, "buy", return_value=outcome) as buy:
            await self.tap()
        buy.assert_called_once()
        self.query.answer.assert_awaited_once()
        self.assertEqual(
            photo_cards.load("vinted", 42)[1]["buy_feedback"]["message"],
            outcome["message"],
        )
        self.query.data = "buy:status"
        self.query.answer.reset_mock()
        with patch.object(buying, "buy") as buy:
            await self.tap()
        buy.assert_not_called()
        self.query.answer.assert_awaited_once()
        self.assertEqual(self.query.answer.call_args.args[0], outcome["message"])

    async def test_slow_acknowledgement_does_not_gate_purchase_or_paid_feedback(self):
        self.enable()
        loop = asyncio.get_running_loop()
        acknowledgement_started = asyncio.Event()
        release_acknowledgement = asyncio.Event()
        purchase_started = asyncio.Event()
        feedback_saved = asyncio.Event()

        async def slow_answer(*args, **kwargs):
            acknowledgement_started.set()
            await release_acknowledgement.wait()

        def purchase(row):
            loop.call_soon_threadsafe(purchase_started.set)
            self.assertFalse(release_acknowledgement.is_set())
            self.assertTrue(buying.claim(row))
            buying.record(row["item_id"], "paid", "Paid. Check Vinted.")
            return buying.result(row["item_id"])

        async def feedback(**kwargs):
            feedback_saved.set()

        self.query.answer.side_effect = slow_answer
        self.bot.edit_message_caption.side_effect = feedback
        with patch.object(buying, "buy", side_effect=purchase) as buy:
            task = asyncio.create_task(self.tap())
            try:
                await asyncio.wait_for(acknowledgement_started.wait(), 1)
                await asyncio.wait_for(purchase_started.wait(), 1)
                await asyncio.wait_for(feedback_saved.wait(), 1)
                self.assertFalse(release_acknowledgement.is_set())
                self.assertEqual(buying.result("110")["state"], "paid")
                self.assertEqual(
                    photo_cards.load("vinted", 42)[1]["buy_feedback"]["state"],
                    "paid",
                )
            finally:
                release_acknowledgement.set()
                await asyncio.wait_for(task, 2)
            await self.tap()
        buy.assert_called_once()

    async def test_failed_acknowledgement_cannot_replay_a_saved_uncertain_result(self):
        self.enable()
        self.query.answer.side_effect = NetworkError("Offline Telegram")

        def purchase(row):
            self.assertTrue(buying.claim(row))
            buying.record(row["item_id"], "unknown", "Check Vinted before retrying.")
            return buying.result(row["item_id"])

        with patch.object(buying, "buy", side_effect=purchase) as buy:
            await self.tap()
            await self.tap()
        buy.assert_called_once()
        self.assertEqual(buying.result("110")["state"], "unknown")
        self.assertEqual(
            photo_cards.load("vinted", 42)[1]["buy_feedback"]["state"], "unknown"
        )

    async def test_cancelled_popup_wait_preserves_paid_result_without_another_buy(self):
        self.enable()
        release_acknowledgement = asyncio.Event()
        feedback_saved = asyncio.Event()

        async def slow_answer(*args, **kwargs):
            await release_acknowledgement.wait()

        async def feedback(**kwargs):
            feedback_saved.set()

        def purchase(row):
            self.assertTrue(buying.claim(row))
            buying.record(row["item_id"], "paid", "Paid. Check Vinted.")
            return buying.result(row["item_id"])

        self.query.answer.side_effect = slow_answer
        self.bot.edit_message_caption.side_effect = feedback
        with patch.object(buying, "buy", side_effect=purchase) as buy:
            task = asyncio.create_task(self.tap())
            try:
                await asyncio.wait_for(feedback_saved.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release_acknowledgement.set()
                if not task.done():
                    await asyncio.wait_for(task, 2)
            await self.tap()
        buy.assert_called_once()
        self.assertEqual(buying.result("110")["state"], "paid")
        self.assertEqual(
            photo_cards.load("vinted", 42)[1]["buy_feedback"]["state"], "paid"
        )

    async def test_queued_duplicate_tap_reads_saved_result_under_same_alert_lock(self):
        self.enable()
        loop = asyncio.get_running_loop()
        purchase_started = asyncio.Event()
        release_purchase = threading.Event()

        def purchase(row):
            loop.call_soon_threadsafe(purchase_started.set)
            if not release_purchase.wait(2):
                raise AssertionError("Purchase was not released")
            self.assertTrue(buying.claim(row))
            buying.record(row["item_id"], "paid", "Paid. Check Vinted.")
            return buying.result(row["item_id"])

        with patch.object(buying, "buy", side_effect=purchase) as buy:
            first = asyncio.create_task(self.tap())
            second = None
            try:
                await asyncio.wait_for(purchase_started.wait(), 1)
                second = asyncio.create_task(self.tap())
                await asyncio.sleep(0)
                buy.assert_called_once()
            finally:
                release_purchase.set()
                await asyncio.wait_for(
                    asyncio.gather(first, *([second] if second else [])), 2
                )
        buy.assert_called_once()
        self.query.answer.assert_awaited()
        self.assertEqual(self.query.answer.await_count, 2)
        self.assertIn("Paid", self.query.answer.call_args.args[0])
        self.assertEqual(buying.result("110")["state"], "paid")

    async def test_gallery_image_wait_does_not_delay_purchase_or_acknowledgement(self):
        self.enable()
        loop = asyncio.get_running_loop()
        image_started = asyncio.Event()
        release_image = asyncio.Event()
        purchase_saved = asyncio.Event()
        acknowledgement_started = asyncio.Event()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE telegram_photo_cards SET details=? WHERE message_id=42",
                (json.dumps(dict(self.details, rendered_listing_photos=[])),),
            )

        async def resolve(row, details, **kwargs):
            details.update(
                description="Fresh description from this listing.",
                description_checked=True,
                gallery_checked=True,
                photos=["https://images1.vinted.net/offline-test.jpg"],
            )

        async def image(details):
            self.assertTrue(photo_cards.lock("vinted", 42).locked())
            image_started.set()
            await release_image.wait()
            return b"offline-test-photo"

        async def acknowledgement(*args, **kwargs):
            acknowledgement_started.set()

        def purchase(row):
            self.assertFalse(release_image.is_set())
            self.assertTrue(buying.claim(row))
            buying.record("110", "paid", "Paid. Check Vinted.")
            loop.call_soon_threadsafe(purchase_saved.set)
            return buying.result("110")

        self.query.answer.side_effect = acknowledgement
        self.bot.edit_message_media = AsyncMock(
            return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="fresh-photo")])
        )
        with patch("vinted_gallery.resolve", side_effect=resolve), patch.object(
            photo_cards, "listing_photo", side_effect=image
        ), patch.object(buying, "buy", side_effect=purchase) as buy:
            gallery = asyncio.create_task(
                photo_cards.enrich(
                    self.bot,
                    "123",
                    dict(self.row, telegram_message_id=42),
                    self.details,
                    AsyncMock(),
                )
            )
            tap = None
            try:
                await asyncio.wait_for(image_started.wait(), 1)
                tap = asyncio.create_task(self.tap())
                await asyncio.wait_for(acknowledgement_started.wait(), 1)
                await asyncio.wait_for(purchase_saved.wait(), 1)
                self.assertFalse(tap.done())
                self.bot.edit_message_caption.assert_not_awaited()
                self.assertEqual(buying.result("110")["state"], "paid")
            finally:
                release_image.set()
                await asyncio.wait_for(
                    asyncio.gather(gallery, *([tap] if tap else [])), 2
                )
        buy.assert_called_once()
        _, details, card = photo_cards.load("vinted", 42)
        self.assertEqual(details["description"], "Fresh description from this listing.")
        self.assertEqual(details["buy_feedback"]["state"], "paid")
        self.assertEqual(details["rendered_listing_photos"], details["photos"])
        self.assertEqual(card["listing_file_id"], "fresh-photo")
        self.assertEqual(card["view"], "listing")
        self.assertIn(
            "Paid. Check Vinted.",
            self.bot.edit_message_caption.call_args.kwargs["caption"],
        )

    async def test_queued_purchase_feedback_keeps_newer_reconciled_paid_state(self):
        self.assertTrue(buying.claim(self.row))
        with patch.object(buying.time, "time", return_value=100):
            buying.record("110", "unknown", "Earlier unverified response.")
        old = dict(buying.result("110"), reason="old_reason")
        ui_lock = photo_cards.lock("vinted", 42)
        await ui_lock.acquire()
        task = asyncio.create_task(
            buying.show_purchase_feedback(self.bot, self.query.message, old)
        )
        try:
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            with patch.object(buying.time, "time", return_value=101):
                buying.record(
                    "110", "paid", "Paid and independently checked.", total=1884
                )
        finally:
            ui_lock.release()
        selected = await asyncio.wait_for(task, 1)
        self.assertEqual(selected["state"], "paid")
        feedback = photo_cards.load("vinted", 42)[1]["buy_feedback"]
        self.assertEqual(feedback["state"], "paid")
        self.assertEqual(feedback["total"], 1884)
        self.assertIsNone(feedback["reason"])
        self.assertNotIn("buy:click", [b.callback_data for b in self.buttons()])
        self.assertIn(
            "Paid and independently checked.",
            self.bot.edit_message_caption.call_args.kwargs["caption"],
        )

    async def test_unchanged_purchase_feedback_keeps_specific_failure_reason(self):
        self.assertTrue(buying.claim(self.row))
        buying.record("110", "failed_before_payment", "The total exceeds your limit.")
        outcome = dict(buying.result("110"), reason="total_over_budget")
        selected = await buying.show_purchase_feedback(
            self.bot, self.query.message, outcome
        )
        self.assertEqual(selected["reason"], "total_over_budget")
        self.assertEqual(
            photo_cards.load("vinted", 42)[1]["buy_feedback"]["reason"],
            "total_over_budget",
        )

    async def test_paid_or_uncertain_purchase_never_gets_a_retry_payment_button(self):
        self.enable()
        buying.claim(self.row)
        for state in ("paid", "unknown", "needs_action", "paying"):
            with self.subTest(state=state):
                buying.record(
                    "110",
                    state,
                    "Check the result in Vinted.",
                    checkout_id="checkout-123",
                )
                with patch.object(buying, "buy") as buy:
                    await self.tap()
                buy.assert_not_called()
                self.assertNotIn("buy:click", [b.callback_data for b in self.buttons()])
                self.assertIn("buy:status", [b.callback_data for b in self.buttons()])

    async def test_telegram_edit_failure_does_not_repeat_or_lose_purchase_result(self):
        self.enable()
        self.bot.edit_message_caption.side_effect = NetworkError("offline")
        outcome = {"state": "unknown", "message": "Check Vinted before retrying."}
        with patch.object(buying, "buy", return_value=outcome) as buy:
            await self.tap()
        buy.assert_called_once()
        self.assertEqual(
            photo_cards.load("vinted", 42)[1]["buy_feedback"]["state"], "unknown"
        )
        self.bot.send_message.assert_not_awaited()

    async def test_another_user_cannot_view_purchase_status(self):
        self.query.data = "buy:status"
        self.query.from_user.id = 999
        await self.tap()
        self.assertIn("Only your private", self.query.answer.call_args.args[0])
        self.bot.edit_message_caption.assert_not_awaited()

    async def test_status_rechecks_existing_payment_and_updates_same_alert_without_buying(
        self,
    ):
        buying.claim(self.row)
        buying.record(
            "110", "unknown", "Check Vinted", checkout_id="checkout-123", total=1884
        )
        self.query.data = "buy:status"
        outcome = {
            "state": "paid",
            "message": "Paid £18.84",
            "checkout_id": "checkout-123",
            "total": 1884,
        }
        with patch.object(
            buying, "check_payment", return_value=outcome
        ) as check, patch.object(buying, "buy") as buy:
            await self.tap()
        check.assert_called_once_with("110")
        buy.assert_not_called()
        self.assertEqual(
            photo_cards.load("vinted", 42)[1]["buy_feedback"]["state"], "paid"
        )
        self.assertNotIn("buy:click", [b.callback_data for b in self.buttons()])
        self.bot.send_message.assert_not_awaited()

    async def test_repeated_identical_status_is_success_not_a_telegram_error(self):
        self.bot.edit_message_caption.side_effect = BadRequest(
            "Message is not modified"
        )
        await self.tap()
        with closing(search_settings.connection()) as conn:
            row = conn.execute(
                "SELECT error FROM telegram_control_health WHERE platform='vinted'"
            ).fetchone()
        self.assertEqual(row[0], "")

    async def test_specific_reason_survives_photo_switching_and_long_note_pages(self):
        self.enable()
        long_details = dict(
            self.details,
            name="🧥" * 100,
            brand="🧥" * 120,
            guide="Buy under the total budget",
            reminder="🧵&lt;label&gt; " * 160,
            budget={
                "max_total": 1500,
                "total": 1680,
                "buyer_protection": 130,
                "buyer_protection_estimated": True,
                "postage_estimate": 350,
            },
        )
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE telegram_photo_cards SET details=?", (json.dumps(long_details),)
            )
        outcome = {
            "state": "failed_before_payment",
            "reason": "total_over_budget",
            "message": "Over budget: £16.80 including fees and delivery; maximum £15.00. No payment was sent.",
        }
        with patch.object(buying, "buy", return_value=outcome):
            await self.tap()
        row, details, _ = photo_cards.load("vinted", 42)
        row["title"] = "😀" * 500
        caption, notes = photo_cards.captions(row, details)
        self.assertIn(outcome["message"], caption)
        self.assertLessEqual(photo_cards.units(photo_cards.plain(caption)), 1024)
        self.assertIn(
            "Over budget with fees & delivery · details",
            [b.text for b in self.buttons()],
        )
        for index in range(len(notes)):
            text = photo_cards.notes_caption(details, index, notes)
            self.assertIn(outcome["message"], text)
            self.assertLessEqual(photo_cards.units(photo_cards.plain(text)), 1024)
        expected = "\n\n".join(
            photo_cards.plain(p) for p in vinted_alerts.sections(row, details)[3:] if p
        )
        self.assertEqual("".join(notes), expected)

    async def test_sold_message_removes_retry_and_does_not_resend_photo(self):
        self.enable()
        outcome = {
            "state": "failed_before_payment",
            "reason": "item_sold",
            "message": "This item is already sold on Vinted. No payment was sent.",
        }
        with patch.object(buying, "buy", return_value=outcome):
            await self.tap()
        self.assertIn(
            "already sold", self.bot.edit_message_caption.call_args.kwargs["caption"]
        )
        self.assertNotIn("buy:click", [b.callback_data for b in self.buttons()])
        self.assertEqual(
            self.bot.edit_message_caption.call_args.kwargs["message_id"], 42
        )
        self.bot.send_message.assert_not_awaited()

    async def test_text_only_legacy_alert_gets_reason_in_same_message(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE telegram_photo_cards SET listing_file_id=NULL")
        await self.tap()
        self.bot.edit_message_caption.assert_not_awaited()
        self.assertIn(
            "Autobuy is off", self.bot.edit_message_text.call_args.kwargs["text"]
        )
        self.assertEqual(self.bot.edit_message_text.call_args.kwargs["message_id"], 42)
