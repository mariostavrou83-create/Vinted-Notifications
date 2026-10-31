"""Optional click/poll metrics cannot delay an authorized buyer callback."""

import asyncio
import sqlite3
import threading
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_vinted_purchase_stop as fixture
import db
import photo_cards
import search_settings
import vinted_buying as buying


class AdvisoryHealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixture.StopTelegramTests(
            "test_button_survives_card_save_and_disappears_at_payment"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.blocker = search_settings.connection()
        self.addCleanup(self.blocker.close)
        self.addCleanup(self.blocker.rollback)

    async def test_real_callback_starts_buyer_while_advisory_writer_is_busy(self):
        started = threading.Event()

        def offline_buy(row, **kwargs):
            started.set()
            return {"state": "cancelled", "message": "Offline purchase not submitted."}

        query = SimpleNamespace(
            data="buy:click", answer=AsyncMock(), from_user=SimpleNamespace(id=123),
            message=self.fixture.message,
        )
        self.blocker.execute("BEGIN IMMEDIATE")
        with patch.object(buying, "buy", side_effect=offline_buy) as purchase:
            task = asyncio.create_task(buying.callback(
                SimpleNamespace(callback_query=query), SimpleNamespace(bot=self.fixture.bot)
            ))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                purchase.assert_called_once()
                self.assertFalse(task.done())  # Final feedback still needs a durable write.
            finally:
                self.blocker.rollback()
            await asyncio.wait_for(task, 2)
        self.assertIsNone(buying.result("123"))

    async def test_unowned_callback_still_cannot_start_buyer_during_storage_contention(self):
        query = SimpleNamespace(
            data="buy:click", answer=AsyncMock(), from_user=SimpleNamespace(id=999),
            message=self.fixture.message,
        )
        self.blocker.execute("BEGIN IMMEDIATE")
        with patch.object(buying, "buy") as purchase:
            await buying.callback(
                SimpleNamespace(callback_query=query), SimpleNamespace(bot=self.fixture.bot)
            )
        purchase.assert_not_called()
        self.assertIn("Only your private Telegram account", query.answer.call_args.args[0])

    async def test_poll_and_click_skip_only_the_busy_advisory_transaction(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO telegram_control_health(platform,last_poll,last_click) VALUES ('vinted',10,20)"
            )
        self.blocker.execute("BEGIN IMMEDIATE")
        for event in ("poll", "click"):
            photo_cards.control_health("vinted", event)
        with closing(search_settings.connection()) as conn:
            row = conn.execute(
                "SELECT last_poll,last_click FROM telegram_control_health WHERE platform='vinted'"
            ).fetchone()
        self.assertEqual(tuple(row), (10, 20))

    async def test_success_and_error_do_not_silently_drop_a_busy_write(self):
        connect = search_settings.connection

        def no_wait():
            conn = connect()
            conn.execute("PRAGMA busy_timeout=0")
            return conn

        self.blocker.execute("BEGIN IMMEDIATE")
        with patch.object(photo_cards, "connection", side_effect=no_wait):
            for event in ("success", "error"):
                with self.subTest(event=event), self.assertRaises(sqlite3.OperationalError):
                    photo_cards.control_health("vinted", event, "Offline diagnostic")

    async def test_unrelated_advisory_database_fault_is_not_hidden(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DROP TABLE telegram_control_health")
        with self.assertRaisesRegex(sqlite3.OperationalError, "no such table"):
            photo_cards.control_health("vinted", "click")

    async def test_uncontended_advisory_metrics_still_commit(self):
        photo_cards.control_health("vinted", "poll")
        photo_cards.control_health("vinted", "click")
        with closing(search_settings.connection()) as conn:
            row = conn.execute(
                "SELECT last_poll,last_click FROM telegram_control_health WHERE platform='vinted'"
            ).fetchone()
        self.assertGreater(row[0], 0)
        self.assertGreater(row[1], 0)

    async def test_borrowed_connection_keeps_critical_timeout_after_busy_and_success(self):
        self.blocker.execute("BEGIN IMMEDIATE")
        with db.connection_scope() as conn:
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 15000)
            photo_cards.control_health("vinted", "click")
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 15000)
            self.assertFalse(conn.in_transaction)
            self.blocker.rollback()
            conn.execute("PRAGMA busy_timeout=1234")
            photo_cards.control_health("vinted", "poll")
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 1234)
            photo_cards.control_health("vinted", "success")
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 1234)
            self.assertGreater(conn.execute(
                "SELECT last_success FROM telegram_control_health WHERE platform='vinted'"
            ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
