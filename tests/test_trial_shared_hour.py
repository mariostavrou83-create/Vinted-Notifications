import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import telegram_shared_alerts as shared
import trial_shared_hour as trial


class HourTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "trial.db"
        self.group = shared.Group(-10, 2, 3, "one", 1)
        self.t = trial.EARLIEST + 200
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=55)))
        self.quote = {
            "status": "estimated",
            "resale_pence": 450,
            "baseline_pence": 450,
            "evidence": [
                {"url": f"https://www.vinted.co.uk/items/{n}", "ask_pence": 500 + n} for n in (1, 2)
            ],
        }
        for field, value in (
            ("_runner", None),
            ("_pending", None),
            ("_publish_lock", None),
            ("_publishers", set()),
            ("_pool_end", 0),
            ("_window_end", 0),
            ("_notice_done", False),
            ("_notice_pending", False),
            ("_accepted_at", 0),
        ):
            self.enterContext(patch.object(trial, field, value))
        self.enterContext(patch.object(trial, "audit_path", return_value=self.path))
        self.enterContext(patch.object(trial, "eligible_service", return_value=True))
        self.enterContext(patch.object(trial.time, "time", return_value=self.t))
        self.enterContext(patch("trial_public_pool.activate"))
        self.enterContext(patch.object(shared, "current", return_value=self.group))
        self.evaluate = self.enterContext(patch.object(trial, "_evaluate_one", return_value=self.quote))

    def test_window_bounded_persistent_and_group_scoped(self):
        first = trial._start(self.path, self.t, self.group.version)
        self.assertEqual(first, (self.t, self.t + 3600, "one"))
        self.assertEqual(trial._start(self.path, self.t + 2500, "other"), first)
        self.assertIsNone(trial._start(self.path.with_name("late"), trial.LAST_START + 1, "one"))
        counters = trial.Counters(self.path)
        self.assertEqual([counters.reserve("market", 99) for _ in range(3)], [True, True, False])
        with patch.object(trial.time, "time", return_value=first[1]):
            self.assertFalse(counters.reserve("model", 3))

    def test_quote_requires_two_distinct_verified_urls(self):
        self.assertIn("£4.50", trial._quote(self.quote))
        one = self.quote["evidence"][0]
        self.assertIsNone(trial._quote(dict(self.quote, evidence=[one, one])))
        self.assertIsNone(
            trial._quote(
                dict(self.quote, evidence=[one, {"url": "https://other.org/1", "ask_pence": 100}])
            )
        )

    async def run_items(self, *items):
        queue = asyncio.Queue()
        for identity in items:
            queue.put_nowait((self.bot, self.group, identity, "home", 123, self.t - 4))
        await trial._run(queue)
        if trial._publishers:
            await asyncio.gather(*trial._publishers)

    async def test_replies_exactly_once_to_shared_parent(self):
        await self.run_items("11", "11")
        self.evaluate.assert_called_once()
        self.bot.send_message.assert_awaited_once()
        call = self.bot.send_message.call_args.kwargs
        self.assertEqual(call["reply_to_message_id"], 123)
        self.assertEqual(call["chat_id"], -10)
        self.assertIs(call["allow_sending_without_reply"], False)
        with closing(sqlite3.connect(self.path)) as conn:
            row = conn.execute(
                "SELECT status,ready,reply_message,sent,publish_state FROM hour_alerts"
            ).fetchone()
        self.assertEqual(row, ("estimated", self.t, 55, self.t, "accepted"))
        self.assertGreater(trial._hold(self.path), self.t)

    async def test_only_one_abstention_reply_and_no_quote(self):
        self.evaluate.return_value = {"status": "insufficient", "evidence": []}
        await self.run_items("2", "3", "4")
        self.bot.send_message.assert_awaited_once()
        self.assertIn("could not verify a price", self.bot.send_message.call_args.kwargs["text"])
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM hour_alerts").fetchone()[0], 3)

    async def test_never_sends_if_group_changed_or_window_ended(self):
        with patch.object(shared, "current", return_value=None):
            await self.run_items("6")
        self.bot.send_message.assert_not_awaited()
        trial._start(self.path, self.t - 4, self.group.version)
        with patch.object(trial.time, "time", return_value=self.t + 3600):
            await self.run_items("7")
        self.bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
