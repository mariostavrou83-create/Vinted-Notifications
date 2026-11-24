"""Offline checks for the private two-person group and shared purchase safety."""

import asyncio
import threading
import time
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import alert_controls
import alert_delivery
import telegram_shared_alerts as shared
import telegram_shared_buy as shared_buy
import vinted_buyer
import vinted_buying
from search_settings import connection
from test_search_controls import DatabaseFixture
from vinted_purchase_control import PurchaseControl, bind_control, payment_gate


def person(user_id):
    return SimpleNamespace(id=user_id, is_bot=False, first_name="Test buyer")


class SharedAlertTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.chat = SimpleNamespace(id=-321, type="group", username=None)
        self.bot = SimpleNamespace(
            get_chat=AsyncMock(return_value=self.chat),
            get_chat_member_count=AsyncMock(return_value=3),
            send_photo=AsyncMock(return_value=SimpleNamespace(message_id=101)),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=101)),
            edit_message_reply_markup=AsyncMock(),
            username="TestFinderBot",
        )
        self.context = SimpleNamespace(bot=self.bot)

    async def activate(self, owner_id=123, partner_id=456):
        message = SimpleNamespace(
            chat=self.chat, reply_text=AsyncMock(), text="/share_alerts@TestFinderBot",
            new_chat_members=(), left_chat_member=None, migrate_to_chat_id=None,
            reply_to_message=SimpleNamespace(
                from_user=person(partner_id), chat=self.chat, forward_origin=None, sender_chat=None,
            ),
        )
        update = SimpleNamespace(effective_chat=self.chat, effective_user=person(owner_id),
                                 message=message, callback_query=None)
        await shared.share_command(update, self.context)
        return shared.current()

    def accepted(self, item=800):
        self.batch(1, [item])
        row = alert_delivery.claim()
        receipt = SimpleNamespace(message_id=99, photo=[SimpleNamespace(file_id="offline-file-id")],
                                  caption_html="<b>Vinted listing</b>", text_html="")
        alert_delivery.finish(row, message_id=receipt.message_id, listing_message=receipt)

    async def ready(self):
        group = await self.activate()
        self.accepted()
        job = shared.claim(group, time.time())
        await shared.send(self.bot, group, job)
        return group

    def tap(self, user_id, action="shared:buy", message_id=101):
        query = SimpleNamespace(data=action, message=SimpleNamespace(chat=self.chat, message_id=message_id),
                                from_user=person(user_id), answer=AsyncMock(), id="offline-query")
        return SimpleNamespace(callback_query=query), query

    async def test_only_owner_links_a_private_group_and_selects_the_actual_reply_author(self):
        self.assertIsNone(await self.activate(owner_id=789))
        self.bot.get_chat.assert_not_awaited()
        self.bot.get_chat_member_count.return_value = 4
        self.assertIsNone(await self.activate())
        self.bot.get_chat_member_count.return_value = 3
        self.chat.username = "test_public"
        self.assertIsNone(await self.activate())
        self.chat.username = None
        group = await self.activate()
        self.assertEqual((group.owner, group.partner), (123, 456))
        self.assertFalse(shared.member(group, self.chat, person(789)))

    async def test_shared_alert_reuses_the_photo_and_uses_only_group_buttons(self):
        group = await self.ready()
        send = self.bot.send_photo.await_args.kwargs
        self.assertEqual(send["photo"], "offline-file-id")
        self.assertEqual(send["caption"], "<b>Vinted listing</b>")
        callbacks = [button.callback_data for row in send["reply_markup"].inline_keyboard for button in row]
        self.assertIn("shared:buy", callbacks)
        self.assertNotIn("buy:click", callbacks)
        self.assertEqual(shared.lookup(group, 101)["item_id"], "800")
        self.assertIsNone(shared.claim(group, time.time()))
        with closing(connection()) as conn:
            self.assertEqual(conn.execute("SELECT status FROM alert_outbox WHERE item_id='800'").fetchone()[0], "sent")

    async def test_group_respects_saved_alert_pause_after_private_delivery(self):
        group = await self.activate()
        self.accepted()
        alert_controls.set_paused("search", True)
        await shared.send(self.bot, group, shared.claim(group, time.time()))
        self.bot.send_photo.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_stranger_stale_message_and_unlinked_group_cannot_invoke_autobuy(self):
        group = await self.ready()
        with patch.object(vinted_buying, "buy", side_effect=AssertionError("No purchase")) as buy:
            await shared_buy.callback(self.tap(789)[0], self.context)
            await shared_buy.callback(self.tap(456, message_id=100)[0], self.context)
            shared.unlink(group.version)
            await shared_buy.callback(self.tap(456)[0], self.context)
        buy.assert_not_called()

    async def test_two_approved_accounts_share_one_purchase_attempt(self):
        await self.ready()
        start = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def fake_buy(row, *, progress):
            item = row["item_id"]
            self.assertTrue(vinted_buying.claim(row))
            progress("waiting")
            loop.call_soon_threadsafe(start.set)
            self.assertTrue(release.wait(timeout=5))
            vinted_buying.record(item, "paid", "Test purchase completed")
            return vinted_buying.result(item)

        with patch.object(vinted_buying, "buy", side_effect=fake_buy) as buy:
            first = asyncio.create_task(shared_buy.callback(self.tap(456)[0], self.context))
            try:
                await asyncio.wait_for(start.wait(), 2)
                second, query = self.tap(123)
                await shared_buy.callback(second, self.context)
                self.assertIn("already running", query.answer.await_args.args[0])
            finally:
                release.set()
                await first
            await shared_buy.callback(self.tap(123)[0], self.context)
            buy.assert_called_once()
        self.assertEqual(vinted_buying.result("800")["state"], "paid")
        self.assertIn("Bought", self.bot.edit_message_reply_markup.await_args.kwargs[
            "reply_markup"].inline_keyboard[1][0].text)

    async def test_unlink_checks_permission_again_at_payment_gate(self):
        group = await self.activate()
        control = PurchaseControl("800", authorise=lambda: shared.current() == group)
        shared.unlink(group.version)
        with bind_control(control), self.assertRaises(vinted_buyer.BuyerError):
            with payment_gate():
                self.fail("Payment gate accepted a revoked group")
