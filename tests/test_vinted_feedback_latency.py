"""Final alert feedback commits safely without blocking the Telegram loop."""

import asyncio
import json
import threading
import unittest
from contextlib import closing
from unittest.mock import patch

import test_vinted_telegram_progress as fixture
import photo_cards
import search_settings
import vinted_buying as buying
from vinted_telegram_progress import show_stop_feedback


class FeedbackLatencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixture.TelegramProgressTests(
            "test_queued_stages_coalesce_under_photo_lock_and_keep_fresh_card"
        )
        self.fixture.setUp()
        self.fixture.preparing()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def tearDown(self):
        self.fixture.tearDown()

    def raw_feedback(self):
        with closing(search_settings.connection()) as conn:
            row = conn.execute(
                "SELECT details FROM telegram_photo_cards WHERE message_id=42"
            ).fetchone()
        return json.loads(row[0]).get("buy_feedback")

    async def paid(self):
        buying.record("110", "paid", "Payment confirmed.")
        await buying.show_purchase_feedback(
            self.fixture.bot, self.fixture.message, buying.result("110")
        )

    async def test_unchanged_view_does_not_open_another_writer_transaction(self):
        with patch.object(photo_cards, "after_edit", wraps=photo_cards.after_edit) as edit:
            await self.paid()
        edit.assert_not_called()
        self.assertEqual(self.raw_feedback()["state"], "paid")
        self.fixture.bot.edit_message_text.assert_awaited_once()

    async def test_invalidated_notes_view_is_still_repaired_and_persisted(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE telegram_photo_cards SET view='notes:99'")
        with patch.object(photo_cards, "after_edit", wraps=photo_cards.after_edit) as edit:
            await self.paid()
        edit.assert_called_once_with("vinted", 42, view="listing")
        self.assertEqual(photo_cards.load("vinted", 42)[2]["view"], "listing")
        self.assertEqual(self.raw_feedback()["state"], "paid")

    async def test_slow_terminal_commit_yields_loop_and_finishes_before_telegram(self):
        entered, release = threading.Event(), threading.Event()
        save = buying._save_alert_feedback

        def delayed_save(*args):
            entered.set()
            if not release.wait(2):
                raise AssertionError("terminal writer was not released")
            save(*args)

        async def telegram(*args, **kwargs):
            self.assertEqual(self.raw_feedback()["state"], "paid")

        self.fixture.bot.edit_message_text.side_effect = telegram
        with patch.object(buying, "_save_alert_feedback", side_effect=delayed_save):
            task = asyncio.create_task(self.paid())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                # This coroutine can still run while the SQLite writer waits.
                await asyncio.wait_for(asyncio.sleep(0), 0.1)
                self.assertFalse(task.done())
                self.fixture.bot.edit_message_text.assert_not_awaited()
            finally:
                release.set()
            await asyncio.wait_for(task, 1)
        self.assertEqual(self.raw_feedback()["state"], "paid")

    async def test_pending_optional_bookkeeping_keeps_same_card_serialized(self):
        entered, release = threading.Event(), threading.Event()
        bookkeeping = buying._feedback_bookkeeping

        def delayed_bookkeeping(*args):
            entered.set()
            if not release.wait(2):
                raise AssertionError("bookkeeping was not released")
            bookkeeping(*args)

        with patch.object(buying, "_feedback_bookkeeping", side_effect=delayed_bookkeeping):
            task = asyncio.create_task(self.paid())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                await asyncio.wait_for(asyncio.sleep(0), 0.1)
                self.assertTrue(photo_cards.lock("vinted", 42).locked())
                self.assertFalse(task.done())
            finally:
                release.set()
            await asyncio.wait_for(task, 1)
        self.assertFalse(photo_cards.lock("vinted", 42).locked())

    async def test_cancelled_stop_display_drains_same_write_before_releasing_card(self):
        entered, release = threading.Event(), threading.Event()
        save = buying._save_alert_feedback
        bridge = self.fixture.bridge(interval=0)
        self.assertEqual(bridge.control.stop(), "stopping")

        def delayed_save(*args):
            entered.set()
            if not release.wait(2):
                raise AssertionError("stop writer was not released")
            save(*args)

        outcome = {
            "state": "stopping", "message": "Stopping without payment.",
            "updated": bridge.control.stopped_at,
        }
        with patch.object(buying, "_save_alert_feedback", side_effect=delayed_save):
            task = asyncio.create_task(show_stop_feedback(
                self.fixture.bot, self.fixture.message, "110", bridge.control.token, outcome
            ))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    self.assertTrue(photo_cards.lock("vinted", 42).locked())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        self.assertFalse(photo_cards.lock("vinted", 42).locked())
        self.assertEqual(self.raw_feedback()["state"], "stopping")
        self.fixture.bot.edit_message_text.assert_not_awaited()
        self.assertEqual(buying.result("110")["state"], "preparing")

    async def test_progress_logs_fixed_stage_and_separate_numeric_ui_spans(self):
        bridge = self.fixture.bridge(interval=0)

        async def publish():
            bridge.publish("checking_account")

        with self.assertLogs("vinted_buying", level="INFO") as logs:
            await self.fixture.render_once(bridge, publish)
        text = " ".join(logs.output)
        self.assertIn("state=in_progress stage=checking_account", text)
        self.assertRegex(text, r"prepare_ms=\d+\.\d+ telegram_ms=\d+\.\d+ bookkeeping_ms=\d+\.\d+ elapsed_ms=\d+\.\d+")
        self.assertNotIn("operation=purchase", text)

    async def test_optional_duration_diagnostics_never_change_feedback(self):
        with patch.object(buying, "_log_feedback_timing", wraps=buying._log_feedback_timing):
            with patch.object(buying.logger, "info", side_effect=RuntimeError("offline log failure")):
                # Exercise the guarded helper directly: a logging failure is
                # optional and must never alter the saved payment result.
                buying._log_feedback_timing(0, 1, 2, transient=False, stage="result")
        self.assertEqual(buying.result("110")["state"], "preparing")


if __name__ == "__main__":
    unittest.main()
