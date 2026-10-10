"""Offline notification replies against saved alerts and the real buyer gates."""

import asyncio
import json
import os
import threading
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import NetworkError
from test_search_controls import DatabaseFixture
from test_vinted_buying import DEVICE, checkout

import photo_cards
import search_settings
import vinted_alerts
import vinted_buyer as buyer
import vinted_buying as buying
from vinted_buy_reply import reply_buy
from vinted_progress import STAGE_LABELS


class BuyReplyTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.review_mode = patch.dict(os.environ, {"MSJ_TELEGRAM_REVIEW_ONLY": "0"})
        self.review_mode.start()
        self.addCleanup(self.review_mode.stop)
        self.batch(1, [123])
        self.batch(2, [124])
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("INSERT INTO vinted_search_budgets VALUES (1,2000,350)")
            conn.execute(
                "UPDATE vinted_buyer SET session=?,verified_at=1,user_id='99',"
                "username='owner',enabled=1,browser_info=? WHERE id=1",
                (
                    buyer.encrypt({"cookies": {"access_token_web": "offline-token"}}),
                    json.dumps(DEVICE),
                ),
            )
            rows = [
                dict(row)
                for row in conn.execute("SELECT * FROM alert_outbox ORDER BY item_id")
            ]
        for row, message_id in zip(rows, (777, 778)):
            photo_cards.record(
                row,
                vinted_alerts.get_details(row),
                SimpleNamespace(message_id=message_id, photo=[]),
            )
        self.bot = SimpleNamespace(
            id=123456,
            edit_message_text=AsyncMock(),
            edit_message_caption=AsyncMock(),
        )
        self.source = SimpleNamespace(
            message_id=777,
            chat=SimpleNamespace(id=123, type="private"),
            from_user=SimpleNamespace(id=self.bot.id, is_bot=True),
            forward_origin=None,
        )
        self.message = SimpleNamespace(
            message_id=900,
            text="BUY",
            chat=self.source.chat,
            from_user=SimpleNamespace(id=123, is_bot=False),
            reply_to_message=self.source,
            reply_text=AsyncMock(),
        )
        self.final = checkout()
        self.payment = {"payment": {"status": "success"}}
        self.client = Mock()

        def response(method, path, body=None):
            if path == "/api/v2/items/123":
                return {
                    "item": {
                        "id": 123,
                        "user": {"id": 100},
                        "price": {"amount": "15.00", "currency_code": "GBP"},
                    }
                }
            if path == "/api/v2/conversations":
                return {"conversation": {"transaction": {"id": 456}}}
            if path in (
                "/api/v2/purchases/checkout/build",
                "/api/v2/purchases/checkout-123/checkout",
            ):
                return {"checkout": self.final}
            if path == "/api/v2/purchases/checkout-123/checkout/payment":
                if isinstance(self.payment, Exception):
                    raise self.payment
                return self.payment
            raise AssertionError((method, path))

        self.client.request.side_effect = response
        self.client.listing_page.side_effect = lambda url, item_id: response(
            "GET", "/api/v2/items/" + str(item_id)
        )
        connected = patch.object(buyer, "connected_client", return_value=self.client)
        connected.start()
        self.addCleanup(connected.stop)

    async def send_buy(self):
        await reply_buy(
            SimpleNamespace(message=self.message), SimpleNamespace(bot=self.bot)
        )

    def payments(self):
        return [
            call
            for call in self.client.request.call_args_list
            if call.args[1].endswith("/payment")
        ]

    async def test_buy_means_buy_for_original_item_without_second_approval(self):
        self.message.text = "  bUy\n"
        await self.send_buy()
        self.assertEqual(buying.result("123")["state"], "paid")
        self.assertIsNone(buying.result("124"))
        self.assertEqual(len(self.payments()), 1)
        self.assertIn("Paid £19.00", self.message.reply_text.call_args.args[0])
        self.assertEqual(self.bot.edit_message_text.call_args.kwargs["message_id"], 777)
        self.assertEqual(
            photo_cards.load("vinted", 777)[1]["buy_feedback"]["state"], "paid"
        )

    async def test_concurrent_buy_replies_submit_one_payment(self):
        await asyncio.gather(self.send_buy(), self.send_buy())
        self.assertEqual(len(self.payments()), 1)
        self.assertIn(
            "No payment was sent again", self.message.reply_text.call_args.args[0]
        )

    async def test_cancelled_final_reply_finishes_without_repeating_payment(self):
        final_started = asyncio.Event()
        release_final = asyncio.Event()
        delivered = []

        async def status(text, **kwargs):
            if "Paid" in text:
                final_started.set()
                await release_final.wait()
                delivered.append(text)

        self.message.reply_text.side_effect = status
        task = asyncio.create_task(self.send_buy())
        try:
            await asyncio.wait_for(final_started.wait(), 2)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            self.assertTrue(buying.purchase_lock(777).locked())
            self.assertEqual(buying.result("123")["state"], "paid")
        finally:
            release_final.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        await self.send_buy()
        self.assertEqual(len(self.payments()), 1)
        self.assertTrue(delivered)
        self.assertTrue(all("Paid" in text for text in delivered))
        self.assertEqual(
            photo_cards.load("vinted", 777)[1]["buy_feedback"]["state"], "paid"
        )

    async def test_mixed_autobuy_tap_and_buy_reply_share_one_purchase(self):
        loop = asyncio.get_running_loop()
        purchase_started = asyncio.Event()
        release_purchase = threading.Event()
        original_buy = buying.buy

        def purchase(row, *, progress=None):
            loop.call_soon_threadsafe(purchase_started.set)
            if not release_purchase.wait(2):
                raise AssertionError("Purchase was not released")
            return original_buy(row, progress=progress)

        query = SimpleNamespace(
            data="buy:click",
            answer=AsyncMock(),
            from_user=SimpleNamespace(id=123),
            message=self.source,
        )
        with patch.object(buying, "buy", side_effect=purchase) as buy:
            tap = asyncio.create_task(
                buying.callback(
                    SimpleNamespace(callback_query=query), SimpleNamespace(bot=self.bot)
                )
            )
            reply = None
            try:
                await asyncio.wait_for(purchase_started.wait(), 1)
                reply = asyncio.create_task(self.send_buy())
                await asyncio.sleep(0)
                buy.assert_called_once()
                self.message.reply_text.assert_not_awaited()
            finally:
                release_purchase.set()
                await asyncio.wait_for(
                    asyncio.gather(tap, *([reply] if reply else [])), 2
                )
        buy.assert_called_once()
        self.assertEqual(len(self.payments()), 1)
        self.assertEqual(buying.result("123")["state"], "paid")
        self.assertIsNone(buying.result("124"))
        self.assertEqual(
            photo_cards.load("vinted", 777)[1]["buy_feedback"]["state"], "paid"
        )
        self.assertIn(
            "No payment was sent again", self.message.reply_text.call_args.args[0]
        )

    async def test_buy_reply_pays_while_gallery_ui_lock_is_held(self):
        loop = asyncio.get_running_loop()
        payment_started = asyncio.Event()
        original = self.client.request.side_effect

        def response(method, path, body=None):
            if method == "POST" and path.endswith("/checkout/payment"):
                self.assertTrue(ui_lock.locked())
                self.assertEqual(buying.result("123")["state"], "paying")
                loop.call_soon_threadsafe(payment_started.set)
            return original(method, path, body)

        self.client.request.side_effect = response
        ui_lock = photo_cards.lock("vinted", 777)
        await ui_lock.acquire()
        task = asyncio.create_task(self.send_buy())
        try:
            await asyncio.wait_for(payment_started.wait(), 1)
            self.assertFalse(task.done())
            self.bot.edit_message_text.assert_not_awaited()
            self.assertEqual(len(self.payments()), 1)
        finally:
            ui_lock.release()
            await asyncio.wait_for(task, 2)
        self.assertEqual(buying.result("123")["state"], "paid")
        self.assertIn("Paid", self.message.reply_text.call_args.args[0])
        self.assertEqual(
            photo_cards.load("vinted", 777)[1]["buy_feedback"]["state"], "paid"
        )

    async def test_buy_reply_uses_live_stage_ui_without_waiting_to_submit_payment(self):
        loop = asyncio.get_running_loop()
        edit_started = threading.Event()
        release_edit = asyncio.Event()
        payment_started = asyncio.Event()
        original = self.client.request.side_effect

        async def edit(**kwargs):
            if STAGE_LABELS["loading_choices"] in kwargs["text"]:
                edit_started.set()
                await release_edit.wait()

        def response(method, path, body=None):
            if (
                method == "PUT"
                and path.endswith("/checkout")
                and not edit_started.wait(1)
            ):
                raise AssertionError("The live delivery stage did not start")
            if method == "POST" and path.endswith("/checkout/payment"):
                self.assertFalse(release_edit.is_set())
                self.assertEqual(buying.result("123")["state"], "paying")
                loop.call_soon_threadsafe(payment_started.set)
            return original(method, path, body)

        self.bot.edit_message_text.side_effect = edit
        self.client.request.side_effect = response
        # Disable the coalescing interval only for this deterministic race test.
        from vinted_telegram_progress import TelegramProgress

        def bridge(*args):
            return TelegramProgress(*args, interval=0)

        with patch("vinted_buy_reply.TelegramProgress", side_effect=bridge):
            task = asyncio.create_task(self.send_buy())
            try:
                await asyncio.wait_for(payment_started.wait(), 2)
                self.assertFalse(task.done())
            finally:
                release_edit.set()
                await asyncio.wait_for(task, 2)
        self.assertEqual(len(self.payments()), 1)
        self.assertEqual(buying.result("123")["state"], "paid")
        self.assertIn("Paid", self.message.reply_text.call_args.args[0])
        self.assertEqual(
            photo_cards.load("vinted", 777)[1]["buy_feedback"]["state"], "paid"
        )
        await self.send_buy()
        self.assertEqual(len(self.payments()), 1)

    async def test_slow_preparation_status_does_not_delay_the_single_payment(self):
        loop = asyncio.get_running_loop()
        preparation_started = asyncio.Event()
        release_preparation = asyncio.Event()
        payment_started = asyncio.Event()
        preparation_finished = asyncio.Event()

        async def status(text, **kwargs):
            if text == "Checking your buyer account…":
                preparation_started.set()
                await release_preparation.wait()
                preparation_finished.set()

        original = self.client.request.side_effect

        def response(method, path, body=None):
            if method == "POST" and path.endswith("/checkout/payment"):
                self.assertFalse(release_preparation.is_set())
                self.assertEqual(buying.result("123")["state"], "paying")
                loop.call_soon_threadsafe(payment_started.set)
            return original(method, path, body)

        self.message.reply_text.side_effect = status
        self.client.request.side_effect = response
        task = asyncio.create_task(self.send_buy())
        try:
            await asyncio.wait_for(preparation_started.wait(), 1)
            await asyncio.wait_for(payment_started.wait(), 1)
            await asyncio.wait_for(task, 2)
        finally:
            release_preparation.set()
            if not task.done():
                await asyncio.wait_for(task, 2)
        self.assertFalse(preparation_finished.is_set())
        self.assertEqual(buying.result("123")["state"], "paid")
        self.assertEqual(len(self.payments()), 1)
        self.assertEqual(
            photo_cards.load("vinted", 777)[1]["buy_feedback"]["state"], "paid"
        )
        self.assertIn("Paid", self.message.reply_text.call_args.args[0])
        await self.send_buy()
        self.assertEqual(len(self.payments()), 1)

    async def test_preparation_status_failure_cannot_change_or_repeat_payment(self):
        async def status(text, **kwargs):
            if text == "Checking your buyer account…":
                raise ValueError("Private Telegram error detail")

        self.message.reply_text.side_effect = status
        with self.assertLogs("vinted_buy_reply", level="WARNING") as logs:
            await self.send_buy()
        self.assertEqual(buying.result("123")["state"], "paid")
        self.assertEqual(len(self.payments()), 1)
        self.assertIn("ValueError", " ".join(logs.output))
        self.assertNotIn("Private Telegram error detail", " ".join(logs.output))
        self.assertIn("Paid", self.message.reply_text.call_args.args[0])
        await self.send_buy()
        self.assertEqual(len(self.payments()), 1)

    async def test_cancelled_reply_keeps_in_flight_marker_and_does_not_restart_buy(
        self,
    ):
        loop = asyncio.get_running_loop()
        purchase_started = asyncio.Event()
        purchase_finished = asyncio.Event()
        release_purchase = threading.Event()

        def purchase(row, *, progress=None):
            self.assertTrue(buying.claim(row))
            buying.record(row["item_id"], "paying", "Awaiting Vinted confirmation.")
            loop.call_soon_threadsafe(purchase_started.set)
            try:
                if not release_purchase.wait(2):
                    raise AssertionError("Purchase was not released")
                buying.record(row["item_id"], "paid", "Paid. Check Vinted.")
                return buying.result(row["item_id"])
            finally:
                loop.call_soon_threadsafe(purchase_finished.set)

        with patch.object(buying, "buy", side_effect=purchase) as buy:
            task = asyncio.create_task(self.send_buy())
            duplicate = None
            try:
                await asyncio.wait_for(purchase_started.wait(), 1)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertEqual(buying.result("123")["state"], "paying")
                duplicate = asyncio.create_task(self.send_buy())
                await asyncio.sleep(0)
                buy.assert_called_once()
                self.assertTrue(buying.purchase_lock(777).locked())
                status = SimpleNamespace(
                    data="buy:status",
                    answer=AsyncMock(),
                    from_user=SimpleNamespace(id=123),
                    message=self.source,
                )
                with patch.object(buying, "check_payment") as check:
                    await buying.callback(
                        SimpleNamespace(callback_query=status),
                        SimpleNamespace(bot=self.bot),
                    )
                check.assert_not_called()
                self.assertIn("payment", status.answer.call_args.args[0].lower())
            finally:
                release_purchase.set()
                await asyncio.wait_for(purchase_finished.wait(), 2)
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                if duplicate:
                    await asyncio.wait_for(duplicate, 2)
            self.assertIn(
                "No payment was sent again", self.message.reply_text.call_args.args[0]
            )
            await self.send_buy()
        buy.assert_called_once()
        self.assertEqual(buying.result("123")["state"], "paid")
        self.assertEqual(
            photo_cards.load("vinted", 777)[1]["buy_feedback"]["state"], "paid"
        )

    async def test_off_reply_never_connects_and_preserves_session_and_searches(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET enabled=0")
            session = conn.execute("SELECT session FROM vinted_buyer").fetchone()[0]
        await self.send_buy()
        self.client.request.assert_not_called()
        self.message.reply_text.assert_awaited_once()
        self.assertIn("Autobuy is off", self.message.reply_text.call_args.args[0])
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute("SELECT session FROM vinted_buyer").fetchone()[0], session
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0], 44
            )
            self.assertEqual(
                conn.execute("SELECT enabled FROM vinted_buyer").fetchone()[0], 0
            )

    async def test_only_owner_private_chat_can_trigger_buy(self):
        for field, value in (
            ("from_user", SimpleNamespace(id=999, is_bot=False)),
            ("from_user", SimpleNamespace(id=123, is_bot=True)),
            ("chat", SimpleNamespace(id=999, type="private")),
            ("chat", SimpleNamespace(id=123, type="group")),
        ):
            original = getattr(self.message, field)
            with self.subTest(field=field, value=value):
                setattr(self.message, field, value)
                await self.send_buy()
                self.client.request.assert_not_called()
                self.message.reply_text.assert_not_called()
            setattr(self.message, field, original)

    async def test_missing_forwarded_foreign_or_unknown_reply_never_uses_latest(self):
        for changes in (
            None,
            {"from_user": SimpleNamespace(id=444, is_bot=True)},
            {"forward_origin": object()},
            {"message_id": 779},
            {"chat": SimpleNamespace(id=999)},
        ):
            with self.subTest(changes=changes):
                self.message.reply_to_message = (
                    SimpleNamespace(**dict(vars(self.source), **changes))
                    if changes is not None
                    else None
                )
                await self.send_buy()
                self.client.request.assert_not_called()
                self.assertIn(
                    "No purchase was started", self.message.reply_text.call_args.args[0]
                )
        self.assertIsNone(buying.result("123"))
        self.assertIsNone(buying.result("124"))

    async def test_buy_text_must_be_exact(self):
        for text in ("BUY 124", "don't buy", "/buy", "BUY\nBUY"):
            self.message.text = text
            await self.send_buy()
        self.client.request.assert_not_called()
        self.message.reply_text.assert_not_called()

    async def test_live_total_over_search_limit_stops_before_payment(self):
        self.final = checkout("22.20")
        await self.send_buy()
        self.assertEqual(buying.result("123")["state"], "failed_before_payment")
        self.assertEqual(self.payments(), [])
        self.assertIn("£22.20", self.message.reply_text.call_args.args[0])

    async def test_existing_payment_states_do_not_send_another_request(self):
        row, _, _ = photo_cards.load("vinted", 777)
        self.assertTrue(buying.claim(row))
        for state in ("paying", "unknown", "needs_action", "paid", "payment_failed"):
            with self.subTest(state=state):
                buying.record("123", state, "Check Vinted")
                await self.send_buy()
                self.client.request.assert_not_called()
                self.assertIn(
                    "No payment was sent again",
                    self.message.reply_text.call_args.args[0],
                )

    async def test_missing_status_notification_does_not_repeat_uncertain_payment(self):
        self.message.reply_text.side_effect = NetworkError("Offline Telegram")
        self.payment = buyer.BuyerError("Offline timeout", reason="network")
        await self.send_buy()
        await self.send_buy()
        self.assertEqual(buying.result("123")["state"], "unknown")
        self.assertEqual(len(self.payments()), 1)
        self.assertEqual(
            photo_cards.load("vinted", 777)[1]["buy_feedback"]["state"], "unknown"
        )

    async def test_temporary_one_item_testing_gate_is_retained(self):
        with patch.dict(os.environ, {"MSJ_TELEGRAM_REVIEW_ONLY": "1"}):
            await self.send_buy()
        self.client.request.assert_not_called()
        self.assertIn("No payment was sent", self.message.reply_text.call_args.args[0])
        self.assertEqual(
            photo_cards.load("vinted", 777)[1]["buy_feedback"]["reason"],
            "item_approval_required",
        )
