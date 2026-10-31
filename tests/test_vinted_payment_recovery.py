"""Offline bounded payment recovery against real attempt/receipt transactions."""

import asyncio
import copy
import sqlite3
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import NetworkError
from test_search_controls import DatabaseFixture

import search_settings
import vinted_buyer as buyer
import vinted_buying as buying
import vinted_payment_recovery as recovery
from vinted_buyer_priority import purchase_waiter


class PaymentRecoveryTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.now = 1000.0
        clock = patch.object(recovery.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?,user_id='99',verified_at=900,enabled=0",
                (buyer.encrypt({"cookies": {"access_token_web": "fictional-session-token"}}),),
            )
        self.client = Mock()
        self.client.request.side_effect = self.request
        connected = patch.object(buyer, "connected_client", return_value=self.client)
        self.connected = connected.start()
        self.addCleanup(connected.stop)
        self.transaction = {"id": 456, "buyer_id": 99, "item_id": 123,
                            "purchase_id": "checkout-123"}
        self.payment = {"payment": {"status": "unrecognised"}}
        self.bot = SimpleNamespace(send_message=AsyncMock(
            return_value=SimpleNamespace(message_id=777),
        ))
        self.notifier = recovery.PaymentRecoveryNotifier(self.bot, 123)

    def request(self, method, path, body=None):
        self.assertEqual(method, "GET", "Recovery must never create or pay an order")
        if path == "/api/v2/transactions/456":
            return {"transaction": copy.deepcopy(self.transaction)}
        if path == "/api/v2/purchases/checkout-123/checkout/payment":
            if isinstance(self.payment, Exception):
                raise self.payment
            return copy.deepcopy(self.payment)
        raise AssertionError((method, path, body))

    def pending(self, *, state="unknown", **values):
        self.assertTrue(buying.claim({"item_id": "123"}))
        buying.record("123", state, "Existing uncertain payment", checkout_id="checkout-123",
                      buyer_id="99", transaction_id="456", total=1884)
        if values:
            with closing(search_settings.connection()) as conn, conn:
                conn.execute("UPDATE vinted_buy_attempts SET " + ",".join(
                    key + "=?" for key in values
                ) + " WHERE item_id='123'", tuple(values.values()))

    def rows(self, table="vinted_payment_recovery"):
        with closing(search_settings.connection()) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM " + table)]

    def receipts(self):
        return self.rows("vinted_payment_recovery_notice")

    def test_idle_and_initial_grace_make_no_marketplace_request(self):
        self.assertEqual(recovery.tick(), "quiet")
        self.pending()
        self.assertEqual(recovery.tick(), "quiet")
        self.assertEqual(self.rows()[0]["checks"], 0)
        self.connected.assert_not_called()
        self.client.request.assert_not_called()

    async def test_unknown_payment_recovers_and_notifies_without_replaying_payment(self):
        self.pending()
        before = buyer.settings()
        self.payment = {"payment": {"status": "success"}}
        self.now += 5
        self.assertEqual(recovery.tick(), "checked")
        self.assertEqual(buying.result("123")["state"], "paid")
        self.assertEqual(buyer.settings(), before)
        self.connected.assert_called_once_with(solve_challenges=False)
        self.assertEqual([c.args[0] for c in self.client.request.call_args_list], ["GET", "GET"])
        buying.guard_new_payment("999")
        self.assertEqual(await self.notifier.check(), "sent")
        self.assertEqual(await self.notifier.check(), "quiet")
        self.bot.send_message.assert_awaited_once()
        text = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("confirmed", text)
        self.assertIn("£18.84", text)
        self.assertIn("did not send another payment", text)
        self.assertEqual(self.rows()[0]["checks"], 1)

    def test_three_read_cap_survives_mutable_updates_and_restart(self):
        self.pending()
        for index, now in enumerate((1005, 1020, 1060), 1):
            self.now = now
            self.assertEqual(recovery.tick(), "checked")
            self.assertEqual(self.rows()[0]["checks"], index)
            self.assertEqual(buying.result("123")["state"], "unknown")
            # A manual refresh must not supply another three-check budget.
            buying.record("123", "unknown", "Still uncertain")
        self.now = 1500
        self.assertEqual(recovery.tick(), "quiet")
        self.assertEqual(recovery.tick(), "quiet")
        self.assertEqual(self.connected.call_count, 3)
        self.assertEqual(len(self.client.request.call_args_list), 6)
        self.assertEqual(self.receipts(), [])
        with self.assertRaises(buyer.BuyerError) as error:
            buying.guard_new_payment("999")
        self.assertEqual(error.exception.reason, "earlier_payment_uncertain")

    def test_crash_after_reservation_consumes_budget_and_obeys_lease(self):
        self.pending()
        self.now = 1005
        abandoned = recovery.claim()
        self.assertEqual(abandoned["checks"], 1)
        self.now = 1020
        self.assertEqual(recovery.tick(), "quiet")
        self.now = 1126
        self.assertEqual(recovery.tick(), "checked")
        self.assertEqual(self.rows()[0]["checks"], 2)
        self.assertEqual(self.connected.call_count, 1)
        # A late completion cannot clear the replacement worker's reservation.
        job = self.rows()[0]
        recovery.finish(abandoned)
        self.assertEqual(self.rows()[0], job)

    def test_manual_timestamp_updates_cannot_extend_original_recovery_window(self):
        self.pending()
        self.now = 1005
        recovery.tick()
        self.now = 2801
        buying.record("123", "unknown", "An owner status refresh did not confirm payment")
        self.assertEqual(recovery.tick(), "quiet")
        self.assertEqual(self.rows()[0]["checks"], 1)
        self.connected.assert_called_once_with(solve_challenges=False)

    def test_live_paying_old_future_missing_binding_and_other_account_are_skipped(self):
        cases = (
            {"state": "paying"}, {"updated": -1000}, {"updated": 2000},
            {"buyer_id": "88"}, {"transaction_id": "unverified"},
            {"checkout_id": None},
        )
        for values in cases:
            with self.subTest(values=values):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("DELETE FROM vinted_buy_attempts")
                self.pending(**values)
                self.now = 1005
                self.assertEqual(recovery.tick(), "quiet")
                self.assertEqual(self.rows(), [])
        self.connected.assert_not_called()

    def test_owner_priority_defers_before_spending_read_budget(self):
        self.pending()
        self.now = 1005
        with purchase_waiter():
            self.assertEqual(recovery.tick(), "busy")
        self.assertEqual(self.rows(), [])
        self.connected.assert_not_called()
        self.assertEqual(recovery.tick(), "checked")
        self.assertEqual(self.rows()[0]["checks"], 1)

    def test_replaced_checkout_between_claim_and_lock_is_not_read(self):
        self.pending()
        self.now = 1005
        original_claim = recovery.claim

        def claim_then_replace():
            job = original_claim()
            with closing(search_settings.connection()) as conn, conn:
                conn.execute(
                    "UPDATE vinted_buy_attempts SET checkout_id='replacement-checkout',transaction_id='789' "
                    "WHERE item_id='123'",
                )
            return job

        with patch.object(recovery, "claim", side_effect=claim_then_replace):
            self.assertEqual(recovery.tick(), "checked")
        self.connected.assert_not_called()
        self.client.request.assert_not_called()
        self.assertEqual(buying.result("123")["checkout_id"], "replacement-checkout")
        self.assertEqual(buying.result("123")["state"], "unknown")
        self.assertEqual(self.receipts(), [])

    def test_network_or_security_refusal_does_not_release_unknown_payment(self):
        self.pending()
        self.payment = buyer.BuyerError("NEVER-PUBLISH provider body", 403,
                                        reason="security_challenge")
        self.now = 1005
        before = buying.result("123")
        self.assertEqual(recovery.tick(), "unverified")
        self.assertEqual(buying.result("123"), before)
        self.assertEqual(self.receipts(), [])
        self.connected.assert_called_once_with(solve_challenges=False)
        self.assertEqual(self.rows()[0]["checks"], 1)

    async def test_notice_delivery_retries_durably_without_more_vinted_requests(self):
        self.pending()
        self.transaction["is_paid"] = True
        self.now = 1005
        recovery.tick()
        self.bot.send_message.side_effect = NetworkError("offline")
        self.assertEqual(await self.notifier.check(), "unavailable")
        self.assertEqual(self.receipts()[0]["sent"], 0)
        self.bot.send_message.side_effect = None
        self.now += 301
        self.assertEqual(recovery.tick(), "quiet")
        self.assertEqual(await recovery.PaymentRecoveryNotifier(self.bot, 123).check(), "sent")
        self.assertEqual(self.receipts()[0]["sent"], 1)
        self.connected.assert_called_once_with(solve_challenges=False)

    async def test_changed_recipient_or_buyer_suppresses_private_result(self):
        self.pending()
        self.transaction["is_paid"] = True
        self.now = 1005
        recovery.tick()
        self.assertEqual(await recovery.PaymentRecoveryNotifier(self.bot, 999).check(), "quiet")
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET user_id='88'")
        self.assertEqual(await self.notifier.check(), "quiet")
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET user_id='99'")
            conn.execute("UPDATE parameters SET value='-123' WHERE key='telegram_chat_id'")
        self.assertEqual(await recovery.PaymentRecoveryNotifier(self.bot, -123).check(), "quiet")
        self.bot.send_message.assert_not_awaited()

    async def test_receipt_survives_crash_before_worker_finish(self):
        self.pending()
        self.transaction["is_paid"] = True
        self.now = 1005
        recovery.claim()  # Simulate worker ending after status write but before finish.
        outcome = buying.check_payment("123", background=True)
        self.assertEqual(outcome["state"], "paid")
        self.assertEqual(self.receipts()[0]["state"], "paid")
        self.assertEqual(await self.notifier.check(), "sent")

    def test_receipt_storage_failure_rolls_back_verified_status(self):
        self.pending()
        self.transaction["is_paid"] = True
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON vinted_payment_recovery_notice "
                         "BEGIN SELECT RAISE(FAIL,'offline receipt failure'); END")
        self.now = 1005
        self.assertEqual(recovery.tick(), "unavailable")
        self.assertEqual(buying.result("123")["state"], "unknown")
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.rows()[0]["checks"], 1)

    def test_owner_resolution_never_claims_vinted_verified_receipt(self):
        self.pending()
        self.now = 1005
        recovery.claim()
        current = buying.result("123")
        buying.resolve_owned("123", str(float(current["updated"])), "got")
        self.assertEqual(buying.result("123")["state"], "paid")
        self.assertEqual(self.receipts(), [])
        self.connected.assert_not_called()

    async def test_superseded_needs_action_notice_is_not_delivered(self):
        self.pending()
        self.payment = {"payment": {"status": "requires_action"}}
        self.now = 1005
        recovery.tick()
        self.assertEqual(self.receipts()[0]["state"], "needs_action")
        self.payment = {"payment": {"status": "success"}}
        self.now = 1020
        recovery.tick()
        self.assertEqual(await self.notifier.check(), "sent")
        self.assertIn("confirmed your Autobuy payment", self.bot.send_message.await_args.kwargs["text"])
        self.assertEqual(await self.notifier.check(), "quiet")

    def test_schema31_upgrade_preserves_payments_and_budgets(self):
        self.pending()
        before = buying.result("123")
        with closing(search_settings.connection()) as conn, conn:
            budgets = [tuple(row) for row in conn.execute("SELECT * FROM vinted_search_budgets")]
            conn.execute("DROP TABLE vinted_payment_recovery_notice")
            conn.execute("DROP TABLE vinted_payment_recovery")
            conn.execute("UPDATE parameters SET value='31' WHERE key='msj_search_schema'")
        backup = search_settings.ensure_schema()
        self.assertIsNotNone(backup)
        self.assertEqual(buying.result("123"), before)
        with closing(sqlite3.connect(backup)) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        with closing(search_settings.connection()) as conn:
            self.assertEqual([tuple(row) for row in conn.execute("SELECT * FROM vinted_search_budgets")], budgets)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.receipts(), [])
        self.assertIsNone(search_settings.ensure_schema())

    async def test_telegram_lifecycle_owns_and_cancels_payment_recovery(self):
        from telegram_bot_plugin.telegram_bot import LeRobot

        async def parked(*args):
            await asyncio.Event().wait()

        robot = LeRobot.__new__(LeRobot)
        robot._delivery_task = robot._delivery_worker = None
        robot._buyer_maintenance_task = robot._payment_recovery_task = None
        robot._owner_controls_task = None
        robot.set_commands = AsyncMock()
        worker = SimpleNamespace(run=parked, close=AsyncMock())
        app = SimpleNamespace(bot=self.bot)
        with (
            patch("alert_delivery.VintedDeliveryWorker", return_value=worker),
            patch("vinted_session_worker.run", side_effect=parked),
            patch("vinted_owner_controls.refresh_existing", side_effect=parked),
            patch.object(recovery, "run", side_effect=parked),
        ):
            await robot.start_delivery(app)
            await asyncio.sleep(0)
            tasks = (robot._delivery_task, robot._buyer_maintenance_task,
                     robot._payment_recovery_task, robot._owner_controls_task)
            await robot.start_delivery(app)
            self.assertIs(robot._payment_recovery_task, tasks[2])
            await robot.stop_delivery(app)
            await robot.stop_delivery(app)
        self.assertTrue(all(task.cancelled() for task in tasks))
        self.assertIsNone(robot._payment_recovery_task)
        worker.close.assert_awaited_once()
