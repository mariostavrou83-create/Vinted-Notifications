"""Offline checks for the private two-person group and shared purchase safety."""

import asyncio
import threading
import time
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import BotCommandScopeChatMember

import alert_controls
import alert_delivery
import telegram_shared_alerts as shared
import telegram_shared_buy as shared_buy
import vinted_buyer
import vinted_buying
from search_settings import connection
from test_search_controls import DatabaseFixture
from vinted_progress import STAGE_LABELS
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
            edit_message_caption=AsyncMock(),
            edit_message_text=AsyncMock(),
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

    async def test_shared_worker_installs_both_menus_and_removes_them_after_unlink(self):
        group = await self.activate()
        installed, removed = asyncio.Event(), asyncio.Event()
        self.bot.bot = SimpleNamespace(can_join_groups=True)

        def finish_menu(command, event):
            if command.await_count == 2:
                event.set()
            return True

        self.bot.set_my_commands = AsyncMock(side_effect=lambda *args, **kwargs: finish_menu(self.bot.set_my_commands, installed))
        self.bot.delete_my_commands = AsyncMock(side_effect=lambda **kwargs: finish_menu(self.bot.delete_my_commands, removed))
        scopes = [BotCommandScopeChatMember(group.chat, user) for user in (group.owner, group.partner)]
        worker = asyncio.create_task(shared.run(self.bot))
        try:
            await asyncio.wait_for(installed.wait(), 2)
            installs = self.bot.set_my_commands.await_args_list
            self.assertCountEqual([call.kwargs["scope"] for call in installs], scopes)
            self.assertTrue(all(call.args[0] == shared.MENU_COMMANDS for call in installs))
            shared.unlink(group.version)
            await asyncio.wait_for(removed.wait(), 2)
            self.assertCountEqual(
                [call.kwargs["scope"] for call in self.bot.delete_my_commands.await_args_list], scopes
            )
        finally:
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker

    async def test_shared_alert_reuses_the_photo_and_uses_only_group_buttons(self):
        group = await self.ready()
        send = self.bot.send_photo.await_args.kwargs
        self.assertEqual(send["photo"], "offline-file-id")
        self.assertEqual(send["caption"], "<b>Vinted listing</b>")
        self.assertIs(send["show_caption_above_media"], True)
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

    async def test_same_shared_photo_alert_shows_live_stages_and_then_the_result(self):
        await self.ready()
        details = "<b>Vinted listing</b>\n\nSeller's description"
        recap = "<b>Test jacket</b>\nBrand: Test\nEst. total incl. shipping: £20.00"
        caption = f"{details}\n\n{recap}"
        checking, paying = asyncio.Event(), asyncio.Event()
        submit, finish = threading.Event(), threading.Event()

        def purchase(row, *, progress):
            self.assertTrue(vinted_buying.claim(row))
            progress("checking_item")
            self.assertTrue(submit.wait(6))
            with payment_gate():
                vinted_buying.record(row["item_id"], "paying", "Waiting for result")
            progress("submitting_payment")
            self.assertTrue(finish.wait(6))
            vinted_buying.record(row["item_id"], "paid", "Purchase completed")
            return vinted_buying.result(row["item_id"])

        async def edit_caption(**kwargs):
            if STAGE_LABELS["checking_item"] in kwargs["caption"]:
                checking.set()
            if STAGE_LABELS["submitting_payment"] in kwargs["caption"]:
                paying.set()

        self.bot.edit_message_caption.side_effect = edit_caption
        update, query = self.tap(456)
        query.message.photo = [SimpleNamespace(file_id="offline-file-id")]
        query.message.caption_html = caption
        with patch.object(vinted_buying, "buy", side_effect=purchase) as buy:
            task = asyncio.create_task(shared_buy.callback(update, self.context))
            try:
                await asyncio.wait_for(checking.wait(), 3)
                first = self.bot.edit_message_caption.await_args.kwargs
                self.assertEqual(
                    first["caption"],
                    f"{details}\n\n{shared_buy.PROGRESS_HEADING}{STAGE_LABELS['checking_item']}\n\n{recap}",
                )
                self.assertTrue(first["show_caption_above_media"])
                self.assertEqual(first["message_id"], 101)
                stop = first["reply_markup"].inline_keyboard[-1][0]
                self.assertTrue(stop.callback_data.startswith("shared:stop:"))
                status_update, status_query = self.tap(123, "shared:status")
                await shared_buy.callback(status_update, self.context)
                self.assertEqual(status_query.answer.await_args.args[0], STAGE_LABELS["checking_item"])
                submit.set()
                await asyncio.wait_for(paying.wait(), 4)
                payment = self.bot.edit_message_caption.await_args.kwargs
                self.assertNotIn(STAGE_LABELS["checking_item"], payment["caption"])
                self.assertEqual(len(payment["reply_markup"].inline_keyboard), 2)
            finally:
                submit.set()
                finish.set()
                await task
            buy.assert_called_once()
        final = self.bot.edit_message_caption.await_args.kwargs
        self.assertEqual(final["caption"], caption)
        self.assertIn("Bought", final["reply_markup"].inline_keyboard[-1][0].text)
        self.assertEqual(final["message_id"], 101)
        self.assertTrue(final["show_caption_above_media"])
        self.bot.send_message.assert_not_awaited()

    async def test_linked_partner_can_answer_on_each_uncertain_alert_without_rebuying(self):
        group = await self.activate()

        def purchase(row, *, progress):
            self.assertTrue(vinted_buying.claim(row))
            vinted_buying.record(
                row["item_id"], "unknown", "Waiting for confirmation",
                checkout_id="offline", buyer_id="456", transaction_id="12",
                watch=("Test listing", 2000),
            )
            return vinted_buying.result(row["item_id"])

        for item, choice_index, expected in (
            (800, 0, "paid"),
            (801, 1, "payment_failed"),
        ):
            with self.subTest(item=item, state=expected):
                self.accepted(item)
                message_id = item + 100
                self.bot.send_photo.return_value = SimpleNamespace(message_id=message_id)
                await shared.send(self.bot, group, shared.claim(group, time.time()))
                caption = "<b>Test listing</b>\n\nSeller's description"
                update, query = self.tap(456, message_id=message_id)
                query.message.photo = [SimpleNamespace(file_id="offline-file-id")]
                query.message.caption_html = caption
                with patch.object(vinted_buying, "buy", side_effect=purchase) as buy:
                    await shared_buy.callback(update, self.context)
                    alert = self.bot.edit_message_caption.await_args.kwargs
                    self.assertEqual(alert["caption"], caption + "\n\n" + shared_buy.CONFIRM_PROMPT)
                    choices = alert["reply_markup"].inline_keyboard[-1]
                    self.assertEqual([button.text for button in choices], ["Yes, I got it", "No, I didn't get it"])
                    data = choices[choice_index].callback_data
                    stranger, _ = self.tap(789, data, message_id=message_id)
                    await shared_buy.callback(stranger, self.context)
                    self.assertEqual(vinted_buying.result(str(item))["state"], "unknown")

                    decision, decision_query = self.tap(456, data, message_id=message_id)
                    decision.message = None
                    decision.effective_chat = self.chat
                    decision.effective_user = decision_query.from_user
                    self.assertTrue(shared.allowed_update(decision, "123"))
                    decision_query.message.photo = query.message.photo
                    decision_query.message.caption_html = alert["caption"]
                    await shared_buy.callback(decision, self.context)
                    self.assertEqual(decision_query.answer.await_args.args[0], "Purchase confirmation saved.")
                    saved = vinted_buying.result(str(item))
                    self.assertEqual(saved["state"], expected)
                    self.assertEqual(self.bot.edit_message_caption.await_args.kwargs["caption"], caption)
                    stale, _ = self.tap(123, choices[1 - choice_index].callback_data, message_id=message_id)
                    await shared_buy.callback(stale, self.context)
                    self.assertEqual(vinted_buying.result(str(item)), saved)
                    buy.assert_called_once()
                with closing(connection()) as conn:
                    self.assertIsNone(conn.execute(
                        "SELECT 1 FROM vinted_payment_watch WHERE item_id=?", (str(item),),
                    ).fetchone())

    async def test_unlink_checks_permission_again_at_payment_gate(self):
        group = await self.activate()
        control = PurchaseControl("800", authorise=lambda: shared.current() == group)
        shared.unlink(group.version)
        with bind_control(control), self.assertRaises(vinted_buyer.BuyerError):
            with payment_gate():
                self.fail("Payment gate accepted a revoked group")
