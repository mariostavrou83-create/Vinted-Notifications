import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import telegram_shared_alerts as shared
import trial_resale_button as button
import trial_shared_hour as hour
import trial_wrong_price as correction


class TargetedWithdrawalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "audit.db"
        self.group = shared.Group(-52, 2, 4, "group-version", 0)
        self.now = hour.EARLIEST + 120
        self.enterContext(patch.object(correction.time, "time", return_value=self.now))
        self.enterContext(patch.object(hour, "eligible_service", return_value=True))
        self.enterContext(patch.object(shared, "current", return_value=self.group))
        self.enterContext(patch.dict(button._labels, clear=True))
        self.enterContext(patch.object(button, "_withdrawn", set()))
        self.purchase = self.enterContext(patch("vinted_buying.result", return_value=None))
        self.rows = {
            10: {
                "item_id": correction.MANGO,
                "title": "Mango chocolate brown leather jacket",
                "url": "https://www.vinted.co.uk/items/" + correction.MANGO,
            },
            20: {
                "item_id": correction.JEANS,
                "title": "Jeans (don’t buy listing)",
                "url": "https://www.vinted.co.uk/items/" + correction.JEANS,
            },
        }
        self.enterContext(
            patch.object(shared, "lookup", side_effect=lambda group, parent: self.rows.get(parent))
        )
        hour._start(self.path, self.now - 10, self.group.version)
        self.bot = SimpleNamespace(
            edit_message_text=AsyncMock(return_value=SimpleNamespace(message_id=11)),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=21)),
            edit_message_reply_markup=AsyncMock(return_value=True),
        )
        self.mango = {
            "resale_pence": 900,
            "status": "estimated",
            "evidence": [{"id": "10924241338", "url": "https://www.vinted.co.uk/items/10924241338"}],
        }
        self.jeans = {
            "resale_pence": None,
            "status": "insufficient",
            "evidence": [],
            "rough": {
                "basis": "photo_brand",
                "status": "estimated",
                "low_pence": 1500,
                "quick_pence": 2200,
                "high_pence": 3000,
            },
        }
        self.write(self.mango, self.jeans)

    def write(self, mango, jeans):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            for key, parent, reply, result, publication, label in (
                (correction.MANGO, 10, 11, mango, "accepted", "Market-checked resale £9.00"),
                (
                    correction.JEANS,
                    20,
                    None,
                    jeans,
                    "numeric_card_accepted",
                    "Photo estimate £15.00–£30.00 · 0 verified comparables",
                ),
            ):
                conn.execute(
                    "INSERT OR REPLACE INTO hour_alerts (item_id,source,parent_message,reply_message,accepted,ready,sent,status,result,publish_state) VALUES(?,'home',?,?,?,?,?,?,?,?)",
                    (
                        key,
                        parent,
                        reply,
                        self.now - 2,
                        self.now - 1,
                        self.now if reply else None,
                        result["status"],
                        json.dumps(result),
                        publication,
                    ),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO hour_numeric_cards VALUES(?,?,?)", (key, self.now, label)
                )

    async def test_mango_withdraws_initial_keyboard_and_historical_price_reply(self):
        await correction.correct(self.bot, self.group, self.path, correction.MANGO)
        await correction.correct(self.bot, self.group, self.path, correction.MANGO)
        self.bot.edit_message_text.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()
        self.bot.edit_message_reply_markup.assert_awaited_once()
        kwargs = self.bot.edit_message_reply_markup.await_args.kwargs
        self.assertEqual(kwargs["message_id"], 10)
        self.assertEqual(
            kwargs["reply_markup"].inline_keyboard[:-1], shared.markup(self.rows[10]).inline_keyboard
        )
        self.assertEqual(kwargs["reply_markup"].inline_keyboard[-1][0].text, "Resale · withdrawn")
        self.assertEqual(self.bot.edit_message_text.await_args.kwargs["message_id"], 11)
        button.remember(correction.MANGO, self.mango)
        self.assertEqual(button.button(correction.MANGO).text, "Resale · withdrawn")

    async def test_placeholder_card_only_gets_one_correction_and_archived_original(self):
        await correction.correct(self.bot, self.group, self.path, correction.JEANS)
        await correction.correct(self.bot, self.group, self.path, correction.JEANS)
        self.bot.edit_message_text.assert_not_awaited()
        self.bot.send_message.assert_awaited_once()
        self.bot.edit_message_reply_markup.assert_awaited_once()
        self.assertEqual(self.bot.send_message.await_args.kwargs["reply_to_message_id"], 20)
        with closing(sqlite3.connect(self.path)) as conn:
            stored = conn.execute(
                "SELECT status,result FROM hour_alerts WHERE item_id=?", (correction.JEANS,)
            ).fetchone()
            self.assertEqual(stored[0], "withdrawn")
            self.assertNotIn("rough", json.loads(stored[1]))
            row = conn.execute(
                "SELECT original_json FROM hour_wrong_price_original WHERE item_id=?",
                (correction.JEANS,),
            ).fetchone()
            self.assertEqual(json.loads(row[0])["rough"]["quick_pence"], 2200)
            self.assertIn(
                "£15.00",
                conn.execute(
                    "SELECT old_label FROM hour_wrong_price_card WHERE item_id=?", (correction.JEANS,)
                ).fetchone()[0],
            )

    async def test_mismatched_item_fields_do_not_publish(self):
        self.rows[20]["title"] = "Jeans available"
        self.mango["evidence"] = []
        self.write(self.mango, self.jeans)
        await correction.correct(self.bot, self.group, self.path, correction.JEANS)
        await correction.correct(self.bot, self.group, self.path, correction.MANGO)
        self.bot.edit_message_text.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()
        self.bot.edit_message_reply_markup.assert_not_awaited()

    async def test_in_progress_purchase_never_overwrites_purchase_actions(self):
        self.purchase.return_value = {"state": "paying"}
        await correction.correct(self.bot, self.group, self.path, correction.MANGO)
        self.bot.edit_message_text.assert_awaited_once()
        self.bot.edit_message_reply_markup.assert_not_awaited()
        self.assertEqual(button.button(correction.MANGO).text, "Resale · withdrawn")

    async def test_terminal_trade_keeps_exact_existing_controls_when_editing_label(self):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        existing = shared.markup(self.rows[10], state="paid", show_resale=True)
        controls = InlineKeyboardMarkup(
            [
                *existing.inline_keyboard,
                (InlineKeyboardButton("Original status detail", callback_data="shared:status"),),
            ]
        )
        self.bot.edit_message_text.return_value = SimpleNamespace(
            message_id=11,
            reply_to_message=SimpleNamespace(
                message_id=10, chat=SimpleNamespace(id=self.group.chat), reply_markup=controls
            ),
        )
        self.purchase.return_value = {"state": "paid"}
        await correction.correct(self.bot, self.group, self.path, correction.MANGO)
        rebuilt = self.bot.edit_message_reply_markup.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(rebuilt[:2], controls.inline_keyboard[:2])
        self.assertEqual(rebuilt[-1], controls.inline_keyboard[-1])
        self.assertEqual(rebuilt[2][0].text, "Resale · withdrawn")

    async def test_changed_amount_or_wrong_photo_basis_is_not_withdrawn(self):
        self.mango["resale_pence"] = 1050
        self.jeans["rough"]["basis"] = "title"
        self.write(self.mango, self.jeans)
        await correction.correct(self.bot, self.group, self.path, correction.MANGO)
        await correction.correct(self.bot, self.group, self.path, correction.JEANS)
        self.bot.edit_message_text.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()
        self.bot.edit_message_reply_markup.assert_not_awaited()
