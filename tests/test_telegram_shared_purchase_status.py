"""A shared alert can open a review without sharing private payment controls."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import telegram_shared_alerts as shared
import telegram_shared_buy as shared_buy


class SharedPurchaseStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.group = shared.Group(-100, 123, 456, "offline", 0)
        self.chat = SimpleNamespace(type="group", id=self.group.chat)
        self.bot = SimpleNamespace(username="TestFinderBot", send_message=AsyncMock(),
                                   edit_message_reply_markup=AsyncMock())
        self.ctx = SimpleNamespace(bot=self.bot)
        self.row = {"item_id": "987", "url": "https://example.com/item/987"}
        self.url = "https://t.me/TestFinderBot?start=purchase_status"

    def tap(self, sender, action):
        return SimpleNamespace(callback_query=SimpleNamespace(
            id="offline", data=action, from_user=SimpleNamespace(id=sender, is_bot=False),
            message=SimpleNamespace(chat=self.chat, message_id=20), answer=AsyncMock(),
        ))

    async def test_earlier_block_sends_owner_navigation_to_the_group(self):
        outcome = {"state": "setup_required", "reason": "earlier_payment_uncertain"}
        with (patch.object(shared, "current", return_value=self.group),
              patch.object(shared, "lookup", return_value=self.row),
              patch.object(shared_buy.buying, "result", return_value=None),
              patch.object(shared_buy.buying, "buy", return_value=outcome) as buy,
              patch.object(shared_buy, "queue_buy_failure", new_callable=AsyncMock)):
            await shared_buy.callback(self.tap(self.group.partner, "shared:buy"), self.ctx)
        buy.assert_called_once()
        note = self.bot.send_message.await_args.kwargs
        self.assertEqual(note["chat_id"], self.group.chat)
        self.assertIn("/purchase_status", note["text"])
        ((button,),) = note["reply_markup"].inline_keyboard
        self.assertEqual(button.url, self.url)
        self.assertIsNone(button.callback_data)

    async def test_status_of_uncertain_item_shows_private_navigation_without_another_buy(self):
        with (patch.object(shared, "current", return_value=self.group),
              patch.object(shared, "lookup", return_value=self.row),
              patch.object(shared_buy.buying, "result", return_value={"state": "unknown"}),
              patch.object(shared_buy.buying, "buy", side_effect=AssertionError("Unexpected purchase"))):
            await shared_buy.callback(self.tap(self.group.owner, "shared:status"), self.ctx)
        buttons = [b for row in self.bot.edit_message_reply_markup.await_args.kwargs["reply_markup"].inline_keyboard
                   for b in row]
        self.assertIn(self.url, [b.url for b in buttons])
        self.assertEqual([b.callback_data for b in buttons if b.callback_data], ["shared:status"])
