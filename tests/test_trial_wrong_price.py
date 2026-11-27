import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest, NetworkError

import telegram_shared_alerts as shared
import trial_shared_hour as hour
import trial_wrong_price as correction


class WrongPriceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "trial.db"
        self.group = shared.Group(-55, 12, 13, "version", 1)
        self.now = hour.EARLIEST + 60
        self.enterContext(patch.object(hour, "eligible_service", return_value=True))
        self.enterContext(patch.object(correction.time, "time", return_value=self.now))
        self.enterContext(patch.object(shared, "current", return_value=self.group))
        self.lookup = self.enterContext(
            patch.object(shared, "lookup", return_value={"item_id": correction.ITEM})
        )
        hour._start(self.path, self.now - 5, self.group.version)
        self.result = {
            "status": "estimated",
            "resale_pence": 950,
            "evidence": [
                {"id": item, "url": "https://www.vinted.co.uk/items/" + item}
                for item in correction.COMPS
            ],
        }
        self.bot = SimpleNamespace(
            edit_message_text=AsyncMock(return_value=SimpleNamespace(message_id=233)),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=244)),
        )
        self.write(self.result)

    def write(self, result):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO hour_alerts(item_id,parent_message,reply_message,source,status,accepted,ready,sent,result,publish_state) "
                "VALUES(?,211,233,'home','estimated',?,?,?,?, 'accepted')",
                (correction.ITEM, self.now - 3, self.now - 2, self.now - 1, json.dumps(result)),
            )

    async def repeat(self):
        await correction.correct(self.bot, self.group, self.path)
        await correction.correct(self.bot, self.group, self.path)

    async def test_edits_only_original_quote_once_and_archives_original_result(self):
        await self.repeat()
        self.bot.edit_message_text.assert_awaited_once()
        args = self.bot.edit_message_text.await_args.kwargs
        self.assertEqual(
            (args["message_id"], args["chat_id"], args["text"]), (233, -55, correction.TEXT)
        )
        self.assertNotIn("reply_markup", args)
        self.bot.send_message.assert_not_awaited()
        with closing(sqlite3.connect(self.path)) as conn:
            status, result, state = conn.execute(
                "SELECT status,result,publish_state FROM hour_alerts"
            ).fetchone()
            self.assertEqual((status, state), ("withdrawn", "withdrawn_edit_confirmed"))
            self.assertIsNone(json.loads(result)["resale_pence"])
            self.assertEqual(
                json.loads(
                    conn.execute("SELECT original_json FROM hour_wrong_price_original").fetchone()[0]
                ),
                self.result,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE hour_wrong_price_original SET old_status='other'")

    async def test_mismatched_candidates_or_group_do_not_withdraw(self):
        with patch.object(shared, "current", return_value=None):
            await correction.correct(self.bot, self.group, self.path)
        self.result["evidence"].pop()
        self.write(self.result)
        await self.repeat()
        self.bot.edit_message_text.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM hour_alerts").fetchone()[0], "estimated")

    async def test_definitive_rejection_falls_back_to_verified_parent_once(self):
        self.bot.edit_message_text.side_effect = BadRequest("Message can't be edited")
        await self.repeat()
        self.bot.edit_message_text.assert_awaited_once()
        self.bot.send_message.assert_awaited_once()
        args = self.bot.send_message.await_args.kwargs
        self.assertEqual(args["reply_to_message_id"], 211)
        self.assertIs(args["allow_sending_without_reply"], False)
        self.assertEqual(args["text"], correction.TEXT)

    async def test_uncertain_fallback_never_duplicates(self):
        self.bot.edit_message_text.side_effect = BadRequest("Message to edit not found")
        self.bot.send_message.side_effect = NetworkError("simulated")
        await self.repeat()
        self.bot.send_message.assert_awaited_once()
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT state FROM hour_wrong_price_delivery").fetchone()[0],
                "fallback_unconfirmed",
            )

    async def test_idempotently_confirmed_edit_after_uncertain_delivery(self):
        self.bot.edit_message_text.side_effect = BadRequest("Message is not modified")
        await self.repeat()
        self.bot.edit_message_text.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT state FROM hour_wrong_price_delivery").fetchone()[0],
                "edit_confirmed",
            )
