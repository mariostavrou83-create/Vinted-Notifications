import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import photo_cards
import resale_telegram as display
import telegram_shared_alerts as shared
import vinted_alerts
from test_finds_delivery import outbox
from test_search_controls import DatabaseFixture


LINE = "Over £15 profit? Not confident (likely No) · est. profit £11.00 · est. resale £24.00"


def receipt(chat, number, markup=None):
    return SimpleNamespace(message_id=number, chat=SimpleNamespace(id=chat),
                           photo=[SimpleNamespace(file_id="test-photo")], caption_html="<b>Jacket</b>",
                           reply_markup=markup, edit_caption=AsyncMock(return_value=True))


class TelegramResaleTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        display.configure(True)
        self.group = shared.Group(-100, 123, 456, "test", 0.)
        self.addAsyncCleanup(display.close)

    async def drain(self):
        if display._tasks:
            await asyncio.gather(*list(display._tasks))

    async def test_one_result_keeps_owner_card_and_both_group_purchase_choices(self):
        self.batch(1, [110])
        row = outbox(110)
        details = vinted_alerts.get_details(row)
        owner = receipt(123, 40)
        photo_cards.record(row, details, owner)
        original_controls = photo_cards.markup(row, details)
        group_message = receipt(self.group.chat, 22, ["Autobuy"])
        updated = receipt(self.group.chat, 22, ["Yes, I got it", "No, I didn't get it"])
        updated.caption_html = "<b>Jacket</b>\n\nDid you get it?"
        display.remember("110", owner)
        display.remember("110", group_message, group=self.group)
        display.group_revision("110", updated)
        display.group_revision("110", receipt(-999, 22))
        with patch.object(shared, "current", return_value=self.group):
            display.publish("110", LINE)
            await self.drain()
        private = owner.edit_caption.await_args.kwargs
        self.assertIn(LINE, private["caption"])
        self.assertEqual(private["reply_markup"], original_controls)
        self.assertTrue(private["show_caption_above_media"])
        saved_row, saved_details, _ = photo_cards.load("vinted", 40)
        self.assertEqual(saved_details["resale_line"], LINE)
        self.assertIn(LINE, photo_cards.captions(saved_row, saved_details)[0])
        group_message.edit_caption.assert_not_awaited()
        group = updated.edit_caption.await_args.kwargs
        self.assertTrue(group["show_caption_above_media"])
        self.assertEqual(group["reply_markup"], updated.reply_markup)
        self.assertIn("Did you get it?", group["caption"])
        self.assertEqual(display.group_body("110", group["caption"]).count(LINE), 1)

    async def test_late_group_receipt_still_gets_result_and_revoked_group_gets_no_edit(self):
        display.publish(200, LINE)
        display.finish_sample()
        late = receipt(self.group.chat, 30)
        with patch.object(shared, "current", return_value=self.group):
            display.remember(200, late, group=self.group)
            await self.drain()
        self.assertIn(LINE, late.edit_caption.await_args.kwargs["caption"])
        revoked = receipt(self.group.chat, 31)
        with patch.object(shared, "current", return_value=None):
            display.remember(200, revoked, group=self.group)
            await self.drain()
        revoked.edit_caption.assert_not_awaited()
        display.remember(201, receipt(self.group.chat, 32), group=self.group)
        self.assertNotIn(("201", "group"), display._messages)

    async def test_already_priced_group_copy_needs_no_edit_and_shadow_mode_is_silent(self):
        display.restore_lines([("303", LINE)])
        msg = receipt(self.group.chat, 40)
        msg.caption_html = display.group_body("303", msg.caption_html)
        with patch.object(shared, "current", return_value=self.group):
            display.remember("303", msg, group=self.group)
            await self.drain()
        msg.edit_caption.assert_not_awaited()
        display.configure(False)
        display.publish("404", LINE)
        self.assertEqual(display.group_body("404", "Jacket"), "Jacket")
