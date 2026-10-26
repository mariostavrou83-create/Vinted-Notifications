"""Offline cancellation races, native checkout paths and foreground priority."""

import asyncio
import threading
import unittest
from contextlib import ExitStack, closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_vinted_buying as buying_fixture
import test_vinted_checkout_preferences as quote_fixture
import test_vinted_offers as offers_fixture

import photo_cards
import search_settings
import vinted_alerts
import vinted_buyer as buyer
import vinted_buying as buying
from vinted_buyer_priority import purchase_waiter, yield_to_purchase
from vinted_purchase_control import (
    PurchaseControl,
    bind_control,
    checkpoint,
    payment_gate,
)
from vinted_telegram_progress import TelegramProgress, stop_purchase


class StopTests(unittest.TestCase):
    def setUp(self):
        self.fixture = buying_fixture.BuyingTests(
            "test_valid_checkout_pays_once_and_duplicate_tap_is_idempotent"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def buy(self, control, reporter=None):
        with (
            bind_control(control),
            patch.object(buyer, "connected_client", return_value=self.fixture.client),
        ):
            return buying.buy(self.fixture.row, progress=reporter)

    def reset(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DELETE FROM vinted_buy_attempts")
        self.fixture.client.reset_mock()

    def test_stop_at_every_pre_payment_stage_never_submits(self):
        for stage in (
            "waiting",
            "checking_account",
            "checking_item",
            "opening_checkout",
            "loading_choices",
            "selecting_delivery",
        ):
            with self.subTest(stage=stage):
                self.reset()
                control = PurchaseControl("123")
                outcomes = []
                result = self.buy(
                    control,
                    lambda value, outcomes=outcomes, control=control, stage=stage: (
                        outcomes.append(control.stop()) if value == stage else None
                    ),
                )
                self.assertEqual(outcomes, ["stopping"])
                self.assertEqual(result["state"], "cancelled")
                self.assertEqual(self.fixture.payments(), [])
                if stage != "waiting":
                    self.assertEqual(buying.result("123")["state"], "cancelled")

    def test_stop_during_each_inflight_request_stops_the_next_step(self):
        original = self.fixture.client.request.side_effect
        for path in (
            "/api/v2/conversations",
            "/api/v2/purchases/checkout/build",
            "/api/v2/purchases/checkout-123/checkout",
        ):
            with self.subTest(path=path):
                self.reset()
                control = PurchaseControl("123")

                def response(method, route, body=None, *, path=path, control=control):
                    result = original(method, route, body)
                    if route == path:
                        self.assertEqual(control.stop(), "stopping")
                    return result

                self.fixture.client.request.side_effect = response
                self.assertEqual(self.buy(control)["state"], "cancelled")
                self.assertEqual(self.fixture.payments(), [])
                self.assertEqual(self.fixture.client.request.call_args.args[1], path)

    def test_stop_after_total_checks_prevents_durable_payment_marker(self):
        control = PurchaseControl("123")
        original = buying.saved_browser_info

        def info():
            control.stop()
            return original()

        with patch.object(buying, "saved_browser_info", side_effect=info):
            self.assertEqual(self.buy(control)["state"], "cancelled")
        self.assertEqual(self.fixture.payments(), [])
        self.assertFalse(control.paying)

    def test_stop_after_payment_boundary_keeps_one_paid_or_uncertain_result(self):
        for payment, expected in (
            ({"payment": {"status": "success"}}, "paid"),
            (buyer.BuyerError("Uncertain", reason="network"), "unknown"),
        ):
            with self.subTest(state=expected):
                self.reset()
                self.fixture.payment = payment
                control = PurchaseControl("123")
                stops = []
                outcome = self.buy(
                    control,
                    lambda stage, stops=stops, control=control: (
                        stops.append(control.stop())
                        if stage == "submitting_payment"
                        else None
                    ),
                )
                self.assertEqual(stops, ["too_late"])
                self.assertEqual(outcome["state"], expected)
                self.assertEqual(self.buy(PurchaseControl("123"))["state"], expected)
                self.assertEqual(len(self.fixture.payments()), 1)

    def test_stop_while_waiting_releases_promptly_without_account_or_payment(self):
        control = PurchaseControl("123")
        waiting, done = threading.Event(), threading.Event()
        outcomes = []

        def run():
            try:
                outcomes.append(
                    self.buy(
                        control,
                        lambda stage: waiting.set() if stage == "waiting" else None,
                    )
                )
            finally:
                done.set()

        with buyer.exclusive():
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(waiting.wait(1))
                self.assertEqual(control.stop(), "stopping")
                self.assertTrue(done.wait(1))
            finally:
                control.stop()
        thread.join(1)
        self.assertEqual(outcomes[0]["state"], "cancelled")
        self.fixture.client.request.assert_not_called()

    def test_queued_older_request_cannot_undo_stop_but_new_explicit_retry_can(self):
        queued = PurchaseControl("123")
        stopped = PurchaseControl("123")
        self.assertEqual(
            self.buy(
                stopped,
                lambda stage: stopped.stop() if stage == "checking_item" else None,
            )["state"],
            "cancelled",
        )
        self.assertEqual(self.buy(queued)["state"], "cancelled")
        self.assertEqual(self.fixture.payments(), [])
        self.assertEqual(self.buy(PurchaseControl("123"))["state"], "paid")
        self.assertEqual(len(self.fixture.payments()), 1)

    def test_stop_and_durable_payment_write_share_one_atomic_gate(self):
        control = PurchaseControl("123")
        attempting = threading.Event()
        outcomes = []

        def stop():
            attempting.set()
            outcomes.append(control.stop())

        with bind_control(control), payment_gate():
            thread = threading.Thread(target=stop)
            thread.start()
            self.assertTrue(attempting.wait(1))
            self.assertEqual(outcomes, [])
        thread.join(1)
        self.assertEqual(outcomes, ["too_late"])

    def test_failed_durable_write_does_not_close_stop_gate(self):
        control = PurchaseControl("123")
        with bind_control(control), self.assertRaises(RuntimeError), payment_gate():
            raise RuntimeError("fictional write failure")
        self.assertEqual(control.stop(), "stopping")
        with bind_control(control), self.assertRaises(buyer.BuyerError):
            checkpoint()

    def test_local_stop_does_not_create_a_connection_failure_notice(self):
        control = PurchaseControl("123")
        control.stop()
        with (
            bind_control(control),
            patch.object(buyer.Client, "identity", side_effect=checkpoint),
            patch.object(buyer, "record_auth") as auth,
            patch("vinted_connection_notices.record_failure") as failure,
            self.assertRaises(buyer.BuyerError) as stopped,
        ):
            buyer.connected_client()
        self.assertEqual(stopped.exception.reason, "cancelled")
        auth.assert_not_called()
        failure.assert_not_called()


class QuotedStopTests(unittest.TestCase):
    def test_approved_quote_observes_stop_before_payment(self):
        fixture = quote_fixture.CheckoutPreferencesTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        token = fixture.prepare()["token"]
        control = PurchaseControl("123")
        original = buying.saved_browser_info

        def stop():
            control.stop()
            return original()

        with (
            bind_control(control),
            patch.object(buying, "saved_browser_info", side_effect=stop),
        ):
            self.assertEqual(fixture.pay(token)["state"], "cancelled")
        self.assertEqual(fixture.payments(), [])


class StopTelegramTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = buying_fixture.BuyingTests(
            "test_valid_checkout_pays_once_and_duplicate_tap_is_idempotent"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.batch(1, [123])
        with closing(search_settings.connection()) as conn:
            self.row = dict(
                conn.execute(
                    "SELECT * FROM alert_outbox WHERE item_id='123'"
                ).fetchone()
            )
        self.message = SimpleNamespace(
            message_id=42, chat=SimpleNamespace(id=123, type="private"), photo=[]
        )
        photo_cards.record(self.row, vinted_alerts.get_details(self.row), self.message)
        self.bot = SimpleNamespace(
            edit_message_text=AsyncMock(), edit_message_caption=AsyncMock()
        )

    async def test_real_callback_stops_during_listing_despite_purchase_and_ui_locks(
        self,
    ):
        entered, release = threading.Event(), threading.Event()
        bridge = TelegramProgress(self.bot, self.message, "123", interval=0)

        def listing(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError("listing not released")
            return self.fixture.item

        self.fixture.client.listing_page.side_effect = listing
        with patch.object(buyer, "connected_client", return_value=self.fixture.client):
            task = asyncio.create_task(bridge.execute(buying.buy, self.row))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            query = SimpleNamespace(
                data="buy:stop:" + bridge.control.token,
                answer=AsyncMock(),
                from_user=SimpleNamespace(id=123),
                message=self.message,
            )
            try:
                async with buying.purchase_lock(42):
                    await buying.callback(
                        SimpleNamespace(callback_query=query),
                        SimpleNamespace(bot=self.bot),
                    )
                self.assertTrue(bridge.control.requested.is_set())
                self.assertIn("No payment will be sent", query.answer.call_args.args[0])
                self.assertEqual(
                    photo_cards.load("vinted", 42)[1]["buy_feedback"]["state"],
                    "stopping",
                )
            finally:
                release.set()
                outcome = await asyncio.wait_for(task, 2)
                await bridge.finish()
            await buying.show_purchase_feedback(self.bot, self.message, outcome)
        self.assertEqual(outcome["state"], "cancelled")
        self.assertEqual(self.fixture.payments(), [])
        self.assertEqual(
            photo_cards.load("vinted", 42)[1]["buy_feedback"]["state"], "cancelled"
        )

    async def test_stale_wrong_item_and_unauthorized_stop_cannot_cancel(self):
        bridge = TelegramProgress(self.bot, self.message, "123")
        try:
            self.assertEqual(stop_purchase(42, "123", "0" * 16)["state"], "finished")
            self.assertEqual(
                stop_purchase(42, "456", bridge.control.token)["state"], "finished"
            )
            query = SimpleNamespace(
                data="buy:stop:" + bridge.control.token,
                answer=AsyncMock(),
                from_user=SimpleNamespace(id=999),
                message=self.message,
            )
            await buying.callback(
                SimpleNamespace(callback_query=query), SimpleNamespace(bot=self.bot)
            )
            self.assertFalse(bridge.control.requested.is_set())
        finally:
            await bridge.finish()

    async def test_button_survives_card_save_and_disappears_at_payment(self):
        feedback = {
            "state": "in_progress",
            "message": "Checking",
            "stop_token": "a" * 16,
        }
        await buying.show_purchase_feedback(self.bot, self.message, feedback)
        saved = photo_cards.load("vinted", 42)
        buttons = [
            b.callback_data
            for row in photo_cards.markup(saved[0], saved[1]).inline_keyboard
            for b in row
        ]
        self.assertIn("buy:stop:" + "a" * 16, buttons)
        for state in ("paying", "paid", "unknown", "cancelled", "stopping"):
            buttons = [
                b.callback_data
                for row in buying.feedback_buttons(
                    self.row, dict(feedback, state=state)
                )
                for b in row
            ]
            self.assertFalse(
                any(value and value.startswith("buy:stop:") for value in buttons)
            )

    async def test_delayed_stop_popup_cannot_overwrite_completed_unclaimed_stop(self):
        bridge = TelegramProgress(self.bot, self.message, "123")
        query = SimpleNamespace(
            data="buy:stop:" + bridge.control.token,
            answer=AsyncMock(),
            from_user=SimpleNamespace(id=123),
            message=self.message,
        )

        async def finish_during_popup(*args, **kwargs):
            await bridge.finish()
            await buying.show_purchase_feedback(
                self.bot,
                self.message,
                {"state": "cancelled", "message": "Stopped. No payment was sent."},
            )

        query.answer.side_effect = finish_during_popup
        try:
            await buying.callback(
                SimpleNamespace(callback_query=query), SimpleNamespace(bot=self.bot)
            )
        finally:
            await bridge.finish()
        self.assertEqual(
            photo_cards.load("vinted", 42)[1]["buy_feedback"]["state"], "cancelled"
        )


class PurchasePriorityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = offers_fixture.OfferTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.enable()

    def test_background_scan_yields_without_http_or_extra_workers(self):
        with purchase_waiter():
            self.fixture.worker.tick()
        self.fixture.factory.assert_not_called()
        yield_to_purchase()  # The marker is released after the owner finishes.

    def test_offer_job_yields_after_item_read_and_remains_queued(self):
        self.fixture.add(at=self.fixture.now - 600)
        config = __import__("vinted_offers").state()
        job = self.fixture.worker.claim(config)
        self.assertIsNotNone(job)
        original = self.fixture.worker.item_reader
        with ExitStack() as stack:

            def read(client, row):
                value = original(client, row)
                stack.enter_context(purchase_waiter())
                return value

            with patch.object(self.fixture.worker, "item_reader", side_effect=read):
                self.fixture.worker.process(self.fixture.client, config, job)
        saved = self.fixture.jobs()[0]
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["reason"], "")
        self.assertEqual(len(self.fixture.client.calls), 1)
        self.assertEqual(self.fixture.client.calls[0][0], "GET")

    def test_idle_session_workers_yield_to_a_waiting_purchase(self):
        import vinted_session_worker

        with purchase_waiter(), patch.object(buyer, "connected_client") as client:
            self.assertEqual(vinted_session_worker.maintain_connection(), "busy")
            self.assertEqual(vinted_session_worker.check_account_health(), "busy")
        client.assert_not_called()
