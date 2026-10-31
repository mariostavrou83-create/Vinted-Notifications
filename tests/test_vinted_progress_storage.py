"""Live progress survives view changes without competing for SQLite writes."""

import asyncio
import json
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_vinted_telegram_progress as fixture
import photo_cards
import search_settings
import vinted_buying as buying
from vinted_progress import STAGE_LABELS
from vinted_telegram_progress import live_feedback, stop_purchase


class ProgressStorageTests(unittest.IsolatedAsyncioTestCase):
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

    def stored_details(self):
        with closing(search_settings.connection()) as conn:
            return json.loads(conn.execute(
                "SELECT details FROM telegram_photo_cards WHERE message_id=42"
            ).fetchone()[0])

    async def stage(self, stage="checking_item"):
        bridge = self.fixture.bridge(interval=0)

        async def publish():
            bridge.publish(stage)

        await self.fixture.render_once(bridge, publish)
        return bridge

    def buttons(self, call):
        return [
            button.callback_data
            for row in call.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]

    def query(self, data):
        return SimpleNamespace(
            data=data, answer=AsyncMock(), from_user=SimpleNamespace(id=123),
            message=self.fixture.message,
        )

    async def test_actual_stage_render_issues_no_sqlite_mutations(self):
        statements = []
        connect = search_settings.connection

        def traced():
            conn = connect()
            conn.set_trace_callback(statements.append)
            return conn

        with (
            patch.object(buying, "connection", side_effect=traced),
            patch.object(photo_cards, "connection", side_effect=traced),
        ):
            bridge = await self.stage()
        self.assertTrue(statements)
        self.assertFalse(any(sql.lstrip().upper().startswith(
            ("UPDATE", "INSERT", "DELETE", "BEGIN", "REPLACE")
        ) for sql in statements), statements)
        self.assertNotIn("buy_feedback", self.stored_details())
        details = photo_cards.load("vinted", 42)[1]
        self.assertEqual(details["buy_feedback"]["state"], "in_progress")
        self.assertEqual(details["buy_feedback"]["stop_token"], bridge.control.token)

    async def test_notes_callback_reloads_current_stage_and_exact_stop_token(self):
        details = self.stored_details()
        details["description"] = "Detailed listing notes. " * 400
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE telegram_photo_cards SET details=?", (json.dumps(details),))
        bridge = await self.stage("loading_choices")
        self.fixture.bot.edit_message_text.reset_mock()
        await photo_cards.handle_callback(
            self.fixture.bot, self.query("card:notes:0"), "vinted", "123"
        )
        call = self.fixture.bot.edit_message_text.call_args
        self.assertIn(STAGE_LABELS["loading_choices"], call.kwargs["text"])
        self.assertIn("buy:stop:" + bridge.control.token, self.buttons(call))
        self.assertNotIn("buy:click", self.buttons(call))
        self.assertEqual(photo_cards.load("vinted", 42)[2]["view"], "notes:0")
        self.assertNotIn("buy_feedback", self.stored_details())

    async def test_photo_callback_reloads_live_stop_and_stop_remains_effective(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE telegram_photo_cards SET listing_file_id='offline-photo',view='notes:0'"
            )
        self.fixture.bot.edit_message_media = AsyncMock(return_value=SimpleNamespace(
            photo=[SimpleNamespace(file_id="offline-photo")]
        ))
        bridge = await self.stage("opening_checkout")
        await photo_cards.handle_callback(
            self.fixture.bot, self.query("card:listing"), "vinted", "123"
        )
        call = self.fixture.bot.edit_message_media.call_args
        self.assertIn(STAGE_LABELS["opening_checkout"], call.kwargs["media"].caption)
        self.assertIn("buy:stop:" + bridge.control.token, self.buttons(call))
        self.assertEqual(
            stop_purchase(42, "110", bridge.control.token)["state"], "stopping"
        )
        details = photo_cards.load("vinted", 42)[1]
        self.assertEqual(details["buy_feedback"]["state"], "stopping")
        self.assertFalse(any(button.callback_data and button.callback_data.startswith("buy:stop:")
            for row in photo_cards.markup(self.fixture.row, details).inline_keyboard
            for button in row))

    async def test_durable_payment_state_always_removes_live_stop(self):
        bridge = await self.stage()
        for state in ("paying", "unknown", "needs_action", "paid", "payment_failed", "closed"):
            with self.subTest(state=state):
                buying.record("110", state, "Authoritative saved result.")
                self.assertIsNone(live_feedback(42, "110", buying.result("110")))
                await buying.show_purchase_feedback(
                    self.fixture.bot, self.fixture.message, buying.result("110")
                )
                details = photo_cards.load("vinted", 42)[1]
                self.assertEqual(details["buy_feedback"]["state"], state)
                self.assertEqual(self.stored_details()["buy_feedback"]["state"], state)
                self.assertNotIn("buy:stop:" + bridge.control.token, self.buttons(
                    self.fixture.bot.edit_message_text.call_args
                ))

    async def test_exact_card_item_and_event_loop_binding_only(self):
        await self.stage()
        durable = buying.result("110")
        self.assertIsNotNone(live_feedback(42, "110", durable))
        self.assertIsNone(live_feedback(43, "110", durable))
        self.assertIsNone(live_feedback(42, "999", durable))
        self.assertIsNone(await asyncio.to_thread(live_feedback, 42, "110", durable))

        async def another_loop():
            return live_feedback(42, "110", durable)

        self.assertIsNone(await asyncio.to_thread(lambda: asyncio.run(another_loop())))

    async def test_new_attempt_timestamp_and_finished_bridge_do_not_restore_old_stage(self):
        bridge = await self.stage()
        saved = buying.result("110")
        with patch.object(buying.time, "time", return_value=saved["updated"] + 5):
            buying.record("110", "preparing", "Newer attempt.")
        self.assertIsNone(live_feedback(42, "110", buying.result("110")))
        await bridge.finish()
        self.assertNotIn("buy_feedback", photo_cards.load("vinted", 42)[1])

    async def test_stage_saved_by_other_metadata_edit_is_replaced_after_completion(self):
        bridge = await self.stage()
        details = photo_cards.load("vinted", 42)[1]
        # Another actual card mutation may persist its reloaded metadata.
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE telegram_photo_cards SET details=?", (json.dumps(details),))
        buying.record("110", "unknown", "Check the submitted payment.")
        await bridge.finish()
        loaded = photo_cards.load("vinted", 42)[1]
        self.assertEqual(loaded["buy_feedback"]["state"], "unknown")
        self.assertIsNone(loaded["buy_feedback"]["stop_token"])

    async def test_legacy_persisted_progress_cannot_hide_a_terminal_payment_state(self):
        details = self.stored_details()
        details["buy_feedback"] = {
            "state": "in_progress", "message": "Earlier stage.", "stop_token": "a" * 16,
        }
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE telegram_photo_cards SET details=?", (json.dumps(details),))
        buying.record("110", "unknown", "Payment needs confirmation.")
        loaded = photo_cards.load("vinted", 42)[1]
        self.assertEqual(loaded["buy_feedback"]["state"], "unknown")
        self.assertIsNone(loaded["buy_feedback"]["stop_token"])

    async def test_optional_stage_telegram_failure_does_not_write_control_health(self):
        from telegram.error import TimedOut

        self.fixture.bot.edit_message_text.side_effect = TimedOut()
        with patch.object(photo_cards, "control_health") as health:
            await self.stage()
        health.assert_not_called()
        self.assertNotIn("buy_feedback", self.stored_details())
        self.assertEqual(buying.result("110")["state"], "preparing")


if __name__ == "__main__":
    unittest.main()
