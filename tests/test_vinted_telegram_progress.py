"""Offline live-stage races, exact alert binding and final-result precedence."""

import asyncio
import json
import threading
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_search_controls import DatabaseFixture

import photo_cards
import search_settings
import vinted_alerts
import vinted_buying as buying
from vinted_progress import STAGE_LABELS
from vinted_telegram_progress import TelegramProgress, active_status


class TelegramProgressTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.batch(1, [110])
        with closing(search_settings.connection()) as conn:
            self.row = dict(
                conn.execute(
                    "SELECT * FROM alert_outbox WHERE item_id='110'"
                ).fetchone()
            )
        self.message = SimpleNamespace(
            message_id=42, chat=SimpleNamespace(id=123), photo=[]
        )
        photo_cards.record(self.row, vinted_alerts.get_details(self.row), self.message)
        self.bot = SimpleNamespace(
            edit_message_text=AsyncMock(), edit_message_caption=AsyncMock()
        )
        self.bridges = []

    async def asyncTearDown(self):
        for bridge in self.bridges:
            await bridge.finish()

    def bridge(self, item_id="110", **kwargs):
        bridge = TelegramProgress(self.bot, self.message, item_id, **kwargs)
        self.bridges.append(bridge)
        return bridge

    def preparing(self):
        self.assertTrue(buying.claim(self.row))

    async def render_once(self, bridge, operation):
        rendered = asyncio.Event()
        original = bridge._render

        async def render(event):
            try:
                return await original(event)
            finally:
                rendered.set()

        with patch.object(bridge, "_render", side_effect=render):
            await operation()
            await asyncio.wait_for(rendered.wait(), 1)

    async def test_queued_stages_coalesce_under_photo_lock_and_keep_fresh_card(self):
        self.preparing()
        bridge = self.bridge(interval=0)
        ui_lock = photo_cards.lock("vinted", 42)
        await ui_lock.acquire()

        async def operation():
            try:
                # Exercise the actual thread-to-loop API, not an async queue put.
                await asyncio.to_thread(bridge.publish, "checking_item")
                await asyncio.to_thread(bridge.publish, "opening_checkout")
                await asyncio.to_thread(bridge.publish, "loading_choices")
                saved = photo_cards.load("vinted", 42)[1]
                saved["description"] = "New gallery description."
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute(
                        "UPDATE telegram_photo_cards SET details=?",
                        (json.dumps(saved),),
                    )
            finally:
                ui_lock.release()

        await self.render_once(bridge, operation)
        self.bot.edit_message_text.assert_awaited_once()
        text = self.bot.edit_message_text.call_args.kwargs["text"]
        self.assertIn(STAGE_LABELS["loading_choices"], text)
        _, details, _ = photo_cards.load("vinted", 42)
        self.assertEqual(details["description"], "New gallery description.")
        self.assertEqual(details["buy_feedback"]["state"], "in_progress")
        buttons = [
            b
            for r in self.bot.edit_message_text.call_args.kwargs[
                "reply_markup"
            ].inline_keyboard
            for b in r
        ]
        self.assertIn("buy:status", [b.callback_data for b in buttons])
        self.assertNotIn("buy:click", [b.callback_data for b in buttons])
        self.assertEqual(buying.result("110")["state"], "preparing")

    async def test_old_queued_stage_cannot_render_during_a_new_retry_claim(self):
        with patch.object(buying.time, "time", return_value=99):
            self.preparing()
        bridge = self.bridge(interval=0)
        ui_lock = photo_cards.lock("vinted", 42)
        await ui_lock.acquire()

        async def operation():
            try:
                with patch("vinted_telegram_progress.time.time", return_value=100):
                    bridge.publish("checking_item")
                await asyncio.sleep(0)
                with patch.object(buying.time, "time", return_value=101):
                    buying.record(
                        "110", "failed_before_payment", "Old attempt stopped."
                    )
                with patch.object(buying.time, "time", return_value=102):
                    self.assertTrue(buying.claim(self.row))
            finally:
                ui_lock.release()

        await self.render_once(bridge, operation)
        self.bot.edit_message_text.assert_not_awaited()
        self.assertEqual(buying.result("110")["updated"], 102)

    async def test_all_final_states_win_over_queued_progress(self):
        self.preparing()
        for state in (
            "paid",
            "unknown",
            "needs_action",
            "payment_failed",
            "failed_before_payment",
        ):
            with self.subTest(state=state):
                buying.record("110", "preparing", "Current attempt preparing.")
                bridge = self.bridge(interval=0)
                ui_lock = photo_cards.lock("vinted", 42)
                await ui_lock.acquire()

                async def operation(bridge=bridge, state=state, ui_lock=ui_lock):
                    try:
                        bridge.publish("checking_item")
                        await asyncio.sleep(0)
                        buying.record("110", state, "Saved final result.")
                        await buying.show_alert_feedback(
                            self.bot, self.message, buying.result("110")
                        )
                        self.bot.edit_message_text.reset_mock()
                    finally:
                        ui_lock.release()

                await self.render_once(bridge, operation)
                self.bot.edit_message_text.assert_not_awaited()
                self.assertEqual(
                    photo_cards.load("vinted", 42)[1]["buy_feedback"]["state"], state
                )
                await bridge.finish()

    async def test_progress_never_uses_a_missing_or_different_saved_item(self):
        for item_id, remove in (("999", False), ("110", True)):
            with self.subTest(item_id=item_id, remove=remove):
                bridge = self.bridge(item_id, interval=0)
                if remove:
                    with closing(search_settings.connection()) as conn, conn:
                        conn.execute(
                            "DELETE FROM telegram_photo_cards WHERE message_id=42"
                        )

                async def operation(bridge=bridge):
                    bridge.publish("waiting")

                await self.render_once(bridge, operation)
                self.bot.edit_message_text.assert_not_awaited()
                await bridge.finish()

    async def test_close_drops_pending_and_late_thread_events_before_final_feedback(
        self,
    ):
        self.preparing()
        bridge = self.bridge(interval=0)
        ui_lock = photo_cards.lock("vinted", 42)
        await ui_lock.acquire()
        bridge.publish("checking_item")
        await asyncio.sleep(0)
        bridge.close()
        buying.record("110", "paid", "Paid final result.")
        ui_lock.release()
        await bridge.finish()
        await buying.show_purchase_feedback(
            self.bot, self.message, buying.result("110")
        )
        await asyncio.to_thread(bridge.publish, "opening_checkout")
        await asyncio.sleep(0)
        self.bot.edit_message_text.assert_awaited_once()
        self.assertIn(
            "Paid final result.", self.bot.edit_message_text.call_args.kwargs["text"]
        )
        self.assertIsNone(active_status(42, "110", buying.result("110")))

    async def test_active_status_is_exact_and_durable_final_states_take_precedence(
        self,
    ):
        self.preparing()
        bridge = self.bridge(interval=0)
        buying.record("110", "paying", "Waiting for a payment response.")
        bridge.publish("submitting_payment")
        self.assertEqual(
            active_status(42, "110", buying.result("110")),
            STAGE_LABELS["submitting_payment"],
        )
        self.assertIsNone(active_status(42, "999", buying.result("110")))
        self.assertIsNone(active_status(43, "110", buying.result("110")))
        buying.record("110", "unknown", "Check Vinted.")
        self.assertIsNone(active_status(42, "110", buying.result("110")))

    async def test_actual_payment_security_stage_is_informational_and_can_resume(self):
        self.preparing()
        buying.record("110", "paying", "Waiting for a payment response.")
        bridge = self.bridge(interval=0)
        for stage in ("security_check", "submitting_payment"):

            async def operation(stage=stage):
                bridge.publish(stage)

            await self.render_once(bridge, operation)
            self.assertIn(
                STAGE_LABELS[stage], self.bot.edit_message_text.call_args.kwargs["text"]
            )
            self.assertEqual(buying.result("110")["state"], "paying")
            self.assertEqual(
                photo_cards.load("vinted", 42)[1]["buy_feedback"]["state"],
                "in_progress",
            )

    async def test_replaced_bridge_cannot_render_a_queued_old_stage(self):
        self.preparing()
        old = self.bridge(interval=0)
        ui_lock = photo_cards.lock("vinted", 42)
        await ui_lock.acquire()

        async def operation():
            try:
                old.publish("checking_item")
                await asyncio.sleep(0)
                self.bridge(interval=0)
            finally:
                ui_lock.release()

        await self.render_once(old, operation)
        self.bot.edit_message_text.assert_not_awaited()
        await old.finish()

    async def test_invalid_stage_and_unknown_detail_never_reach_telegram(self):
        bridge = self.bridge(interval=0)
        for stage in ("private-token", {"stage": "waiting", "secret": "private"}, None):
            bridge.publish(stage)
        await asyncio.sleep(0)
        await bridge.finish()
        self.bot.edit_message_text.assert_not_awaited()
        self.assertIsNone(bridge.latest)

    async def test_progress_display_failure_does_not_change_attempt_state(self):
        self.preparing()
        bridge = self.bridge(interval=0)
        self.bot.edit_message_text.side_effect = ValueError("Private error details")

        async def operation():
            bridge.publish("checking_item")

        with self.assertLogs("vinted_telegram_progress", level="WARNING") as logs:
            await self.render_once(bridge, operation)
            await bridge.finish()
        self.assertEqual(buying.result("110")["state"], "preparing")
        self.assertIn("ValueError", " ".join(logs.output))
        self.assertNotIn("Private error details", " ".join(logs.output))

    async def test_failing_progress_diagnostics_cannot_break_final_feedback(self):
        self.preparing()
        bridge = self.bridge(interval=0)
        self.bot.edit_message_text.side_effect = ValueError("Private error details")

        async def operation():
            bridge.publish("checking_item")

        with patch(
            "vinted_telegram_progress.logger.warning",
            side_effect=RuntimeError("Private log failure"),
        ):
            await self.render_once(bridge, operation)
            await bridge.finish()
        self.bot.edit_message_text.side_effect = None
        buying.record("110", "paid", "Paid final result.")
        await buying.show_purchase_feedback(
            self.bot, self.message, buying.result("110")
        )
        self.assertEqual(
            photo_cards.load("vinted", 42)[1]["buy_feedback"]["state"], "paid"
        )

    async def test_unexpected_worker_failure_is_consumed_before_final_feedback(self):
        self.preparing()
        bridge = self.bridge(interval=0)
        await bridge.finish()

        async def failed_worker():
            raise RuntimeError("Private worker failure")

        bridge._task = asyncio.create_task(failed_worker())
        with patch(
            "vinted_telegram_progress.logger.warning",
            side_effect=RuntimeError("Private log failure"),
        ):
            await bridge.finish()
        buying.record("110", "unknown", "Check the existing Vinted purchase.")
        await buying.show_purchase_feedback(
            self.bot, self.message, buying.result("110")
        )
        self.assertEqual(
            photo_cards.load("vinted", 42)[1]["buy_feedback"]["state"], "unknown"
        )

    async def test_settle_defers_repeated_cancellation_without_restarting_operation(
        self,
    ):
        bridge = self.bridge()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return "same saved result"

        waiter = asyncio.create_task(bridge.settle(operation()))
        try:
            await asyncio.wait_for(started.wait(), 1)
            for _ in range(2):
                waiter.cancel()
                await asyncio.sleep(0)
                self.assertFalse(waiter.done())
            self.assertTrue(bridge.cancelled)
            self.assertTrue(bridge.closed)
        finally:
            release.set()
        self.assertEqual(await asyncio.wait_for(waiter, 1), "same saved result")
        self.assertEqual(calls, 1)

    async def test_settle_preserves_the_operation_error_after_caller_cancellation(
        self,
    ):
        bridge = self.bridge()
        started = asyncio.Event()
        release = asyncio.Event()
        failure = ValueError("Private operation failure")

        async def operation():
            started.set()
            await release.wait()
            raise failure

        waiter = asyncio.create_task(bridge.settle(operation()))
        try:
            await asyncio.wait_for(started.wait(), 1)
            waiter.cancel()
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())
        finally:
            release.set()
        with self.assertRaises(ValueError) as caught:
            await asyncio.wait_for(waiter, 1)
        self.assertIs(caught.exception, failure)
        self.assertTrue(bridge.cancelled)

    async def test_settle_does_not_swallow_cancellation_of_the_target_operation(self):
        bridge = self.bridge()
        target = asyncio.create_task(asyncio.sleep(10))
        target.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await bridge.settle(target)
        self.assertFalse(bridge.cancelled)

    async def test_cancelled_stage_edit_drain_still_renders_paid_before_propagating(
        self,
    ):
        edit_started = asyncio.Event()
        release_edit = asyncio.Event()
        drain_started = asyncio.Event()
        release_purchase = threading.Event()
        edits = []
        bridges = []
        query = SimpleNamespace(
            data="buy:click",
            answer=AsyncMock(),
            from_user=SimpleNamespace(id=123),
            message=self.message,
        )
        context = SimpleNamespace(bot=self.bot)

        async def edit(**kwargs):
            text = kwargs["text"]
            if STAGE_LABELS["checking_item"] in text:
                edit_started.set()
                await release_edit.wait()
            edits.append(text)

        def bridge_factory(*args):
            bridge = self.bridge(interval=0)
            bridges.append(bridge)
            original = bridge.finish

            async def finish():
                drain_started.set()
                await original()

            bridge.finish = finish
            return bridge

        def purchase(row, *, progress):
            self.assertTrue(buying.claim(row))
            progress("checking_item")
            if not release_purchase.wait(2):
                raise AssertionError("The authorised attempt was not released")
            buying.record("110", "paid", "Paid final result.")
            return buying.result("110")

        self.bot.edit_message_text.side_effect = edit
        with patch.object(buying, "ready"), patch.object(
            buying, "buy", side_effect=purchase
        ) as buy, patch(
            "vinted_telegram_progress.TelegramProgress", side_effect=bridge_factory
        ):
            first = asyncio.create_task(
                buying.callback(SimpleNamespace(callback_query=query), context)
            )
            second = None
            try:
                await asyncio.wait_for(edit_started.wait(), 1)
                second = asyncio.create_task(
                    buying.callback(SimpleNamespace(callback_query=query), context)
                )
                await asyncio.sleep(0)
                buy.assert_called_once()
                release_purchase.set()
                await asyncio.wait_for(drain_started.wait(), 1)
                self.assertEqual(buying.result("110")["state"], "paid")
                for _ in range(2):
                    first.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(first.done())
                    self.assertTrue(buying.purchase_lock(42).locked())
                    self.assertFalse(bridges[0]._task.cancelled())
                    buy.assert_called_once()
            finally:
                release_purchase.set()
                release_edit.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(first, 2)
                if second:
                    await asyncio.wait_for(second, 2)
            buy.assert_called_once()
            await buying.callback(SimpleNamespace(callback_query=query), context)
            buy.assert_called_once()
        self.assertIn(STAGE_LABELS["checking_item"], edits[0])
        self.assertTrue(all("Paid final result." in text for text in edits[1:]))
        self.assertEqual(
            photo_cards.load("vinted", 42)[1]["buy_feedback"]["state"], "paid"
        )
        self.assertIsNone(active_status(42, "110", buying.result("110")))
