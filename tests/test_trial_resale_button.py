import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import InlineKeyboardMarkup

import telegram_shared_alerts as shared
import trial_resale_button as trial
import trial_shared_hour


class ResaleButtonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "trial.db"
        self.group = shared.Group(-20, 11, 12, "linked", 10)
        self.user = SimpleNamespace(id=12, is_bot=False)
        self.chat = SimpleNamespace(id=-20, type="supergroup")
        self.query = SimpleNamespace(
            message=SimpleNamespace(chat=self.chat, message_id=20),
            from_user=self.user,
            data=trial.CALLBACK,
            answer=AsyncMock(),
        )
        self.update = SimpleNamespace(
            callback_query=self.query, message=None, effective_user=self.user, effective_chat=self.chat
        )
        self.enterContext(patch.object(trial_shared_hour, "audit_path", return_value=self.path))
        self.enterContext(patch.object(shared, "current", return_value=self.group))
        self.enterContext(patch.object(shared, "lookup", return_value={"item_id": "101"}))
        self.enterContext(patch.dict(trial._labels, clear=True))
        trial_shared_hour._start(self.path, trial_shared_hour.EARLIEST + 50, "linked")
        self.addCleanup(self.temp.cleanup)

    def add(self, result):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                "INSERT INTO hour_alerts(item_id,accepted,source,parent_message,status,result) VALUES(?,?,?,?,?,?)",
                ("101", time.time(), "home", 20, result["status"], json.dumps(result)),
            )

    async def test_stored_estimate_readable_after_cutoff_and_bound_to_group(self):
        self.add(
            {
                "status": "estimated",
                "resale_pence": 1950,
                "evidence": [{"ask_pence": 2000}, {"ask_pence": 2400}],
            }
        )
        with patch.object(trial.time, "time", return_value=trial_shared_hour.LAST_START + 9000):
            await trial.receive(self.update, None)
        text = self.query.answer.await_args.args[0]
        self.assertIn("£19.50", text)
        self.assertIn("£20.00 and £24.00", text)
        self.assertTrue(self.query.answer.await_args.kwargs["show_alert"])
        self.assertEqual(self.query.answer.await_args.kwargs["cache_time"], 0)
        self.assertNotIn("£19.50", trial.lookup(self.group._replace(version="new"), 20, "101"))
        self.assertNotIn("£19.50", trial.lookup(self.group, 21, "101"))
        self.assertNotIn("£19.50", trial.lookup(self.group, 20, "102"))

    async def test_nonmember_cannot_get_estimate(self):
        self.user.id = 13
        self.assertFalse(shared.allowed_update(self.update, "11"))
        await trial.receive(self.update, None)
        self.query.answer.assert_not_called()

    async def test_single_match_and_missing_condition_reasons(self):
        self.add({"status": "insufficient", "evidence": [{"ask_pence": 2500, "title": "Wool jumper"}]})
        self.assertTrue(shared.allowed_update(self.update, "11"))
        await trial.receive(self.update, None)
        self.assertIn("£25.00", self.query.answer.await_args.args[0])
        self.assertIn("Wool jumper", self.query.answer.await_args.args[0])
        self.assertIn("condition", trial.brief("insufficient", {"had_target_condition": False}))

    def test_button_keeps_existing_purchase_keyboard_and_uses_cached_result_on_refresh(self):
        row = {"url": "https://www.vinted.co.uk/items/101", "item_id": "101"}
        original = shared.markup(row, "paid")
        initial = self.historical_keyboard(row, "paid")
        self.assertEqual(initial.inline_keyboard[:-1], original.inline_keyboard)
        self.assertTrue(trial.on_message(SimpleNamespace(reply_markup=initial)))
        self.assertFalse(trial.on_message(SimpleNamespace(reply_markup=original)))
        result = {
            "status": "estimated",
            "resale_pence": 1500,
            "evidence": [{"ask_pence": 1600}, {"ask_pence": 1650}],
        }
        trial.remember("101", result)
        after = self.historical_keyboard(row, "paid")
        self.assertEqual(after.inline_keyboard[:-1], original.inline_keyboard)
        self.assertIn("£15.00", after.inline_keyboard[-1][0].text)
        trial.remember("101", {"status": "insufficient", "evidence": [{"ask_pence": 3400}]})
        self.assertIn("1 comparable · £34.00", trial.button("101").text)
        trial.remember("101", {"status": "insufficient"})
        self.assertIn("no verified estimate", trial.button("101").text)

    @staticmethod
    def historical_keyboard(row, state=None):
        ordinary = shared.markup(row, state)
        return InlineKeyboardMarkup([*ordinary.inline_keyboard, [trial.button(row["item_id"])]])

    def photo(self):
        return {
            "basis": "photo_brand",
            "status": "estimated",
            "photo_status": "estimated",
            "low_pence": 1800,
            "quick_pence": 2200,
            "high_pence": 2600,
        }

    async def test_photo_range_persisted_first_visible_early_and_on_unpriced_final(self):
        self.add({"status": "evaluating"})
        with patch.object(trial_shared_hour.time, "time", return_value=trial_shared_hour.EARLIEST + 100):
            trial_shared_hour.Counters(self.path).rough("101", self.photo())
        view = trial.lookup(self.group, 20, "101")
        self.assertIn("£18.00–£26.00", view)
        self.assertIn("checking comparable", view)
        result = {"status": "insufficient", "rough": self.photo(), "evidence": [{"ask_pence": 3000}]}
        trial.remember("101", result)
        self.assertIn("1 verified comparable", trial.button("101").text)
        self.assertIn("£30.00", trial.brief("insufficient", result))
        self.assertNotIn(
            "Photo", trial.brief("insufficient", {"rough": dict(self.photo(), basis="title")})
        )

    async def test_original_card_edit_replaces_only_our_button_after_photo(self):
        row = {"url": "https://www.vinted.co.uk/items/101", "item_id": "101"}
        initial = self.historical_keyboard(row)
        bot = SimpleNamespace(edit_message_reply_markup=AsyncMock())
        card = (bot, self.group, 20, initial)
        self.enterContext(patch.dict(trial._cards, {"101": card}, clear=True))
        self.enterContext(patch.dict(trial._shown, clear=True))
        self.enterContext(patch.dict(trial._rendering, clear=True))
        self.enterContext(patch.object(trial_shared_hour, "capture_active", return_value=True))
        self.enterContext(patch.object(trial_shared_hour, "card_slot", new=AsyncMock(return_value=True)))
        self.enterContext(patch("vinted_buying.result", return_value=None))
        self.enterContext(patch.object(trial.asyncio, "sleep", new=AsyncMock()))
        trial.remember("101", {"status": "evaluating", "rough": self.photo()})
        self.assertTrue(await trial.visible_on_card("101"))
        last = bot.edit_message_reply_markup.await_args.kwargs
        self.assertEqual(last["reply_markup"].inline_keyboard[:-1], initial.inline_keyboard[:-1])
        self.assertIn("Photo estimate £18.00–£26.00", last["reply_markup"].inline_keyboard[-1][0].text)
        self.assertNotIn("caption", last)
        self.assertNotIn("text", last)
        bot.edit_message_reply_markup.reset_mock()
        trial.purchase_control_pressed("101")
        await trial._render("101", card)
        bot.edit_message_reply_markup.assert_not_awaited()
        with closing(sqlite3.connect(self.path)) as conn:
            saved = conn.execute(
                "SELECT label FROM hour_numeric_cards WHERE item_id=?", ("101",)
            ).fetchone()
            self.assertIn("£18.00–£26.00", saved[0])

    def test_cross_market_guides_require_genuine_external_item_and_label_sale_kind(self):
        rows = [
            {
                "source": "ebay",
                "price_kind": "sold",
                "price_pence": 3200,
                "url": "https://www.ebay.co.uk/itm/123456789",
            },
            {
                "source": "depop",
                "price_kind": "ask",
                "price_pence": 2800,
                "url": "https://www.depop.com/products/seller-wool-jumper/",
            },
            {
                "source": "ebay",
                "price_kind": "sold",
                "price_pence": 1200,
                "url": "https://www.ebay.co.uk.example.com/itm/999",
            },
        ]
        result = {
            "status": "insufficient",
            "rough": self.photo(),
            "evidence": [],
            "cross_market": {
                "status": "estimated",
                "low_pence": 2800,
                "high_pence": 3200,
                "evidence": rows,
            },
        }
        trial.remember("101", result)
        text = trial.brief("insufficient", result)
        self.assertIn("Photo estimate £18.00–£26.00", text)
        self.assertIn("Cross-market guide £28.00–£32.00", text)
        self.assertIn("0 verified", text)
        self.assertIn("Cross-market guide", trial.button("101").text)
        lines = "\n".join(trial.external_lines(result))
        self.assertIn("eBay · sold for £32.00", lines)
        self.assertIn("Depop · asking £28.00", lines)
        self.assertNotIn("example.com", lines)
        result["cross_market"] = {"status": "insufficient", "evidence": []}
        self.assertIn("Photo", trial.brief("insufficient", result))

    def test_market_exception_keeps_persisted_photo_estimate_with_explicit_label(self):
        self.add({"status": "evaluating", "rough": self.photo()})
        result = trial_shared_hour._retained_photo(self.path, "101")
        self.assertIsNone(result["resale_pence"])
        trial.remember("101", result)
        self.assertIn("Photo estimate £18.00–£26.00", trial.button("101").text)
        self.assertIn("lookup unavailable", trial.button("101").text)
        self.assertIn("market check failed", trial.brief(result["status"], result))
