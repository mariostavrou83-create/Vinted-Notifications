"""Offline regression for real durable events and Telegram delivery semantics."""

import time
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

from telegram.error import NetworkError
from test_vinted_connection_notices import NoticeFixture

import search_settings
import vinted_buyer as buyer
import vinted_buyer_events as events


class BuyerEventTests(NoticeFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.client = SimpleNamespace(_verified_session=("99", self.sealed, {}))
        self.events = events.BuyerEventNotifier(self.bot, 123)

    def failure(self, *, payment_started=False):
        return events.record_checkout_failure(
            self.client,
            self.error("forbidden", "pickup_points", 403),
            "checking the final total and delivery choice",
            payment_started=payment_started,
        )

    def rows(self):
        with closing(search_settings.connection()) as conn:
            return [
                dict(row) for row in conn.execute("SELECT * FROM vinted_buyer_event")
            ]

    async def test_checkout_403_warns_without_claiming_logout_or_sending_payment(self):
        self.assertTrue(self.failure())
        self.assertEqual(await self.events.check(), "sent")
        text = self.bot.send_message.await_args.kwargs["text"]
        for expected in (
            "HTTP 403",
            "No payment was sent",
            "does not mean your login disconnected",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("NEVER-PUBLISH", text)
        self.assertIsNone(self.notice_row()["checked"])

    async def test_identity_success_and_cookie_rotation_do_not_hide_checkout_failure(
        self,
    ):
        self.failure()
        self.update_buyer(
            session=buyer.encrypt({"rotated": True}), verified_at=time.time() + 1
        )
        self.assertEqual(await self.events.check(), "sent")
        self.assertEqual(buyer.settings()["checkout_health"]["http_status"], 403)

    async def test_repeated_failed_attempts_and_worker_restart_send_one_warning(self):
        self.failure()
        self.assertEqual(await self.events.check(), "sent")
        self.failure()
        restarted = events.BuyerEventNotifier(self.bot, 123)
        self.assertEqual(await restarted.check(), "quiet")
        self.bot.send_message.assert_awaited_once()

    async def test_manual_reconnect_confirms_account_and_retains_checkout_warning(self):
        self.failure()
        self.assertTrue(
            events.record_connected(self.client._verified_session, manual=True)
        )
        await self.events.check()
        await self.events.check()
        text = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("buyer reconnected", text)
        self.assertIn("permission is ON", text)
        self.assertIn("does not test checkout", text)
        self.assertIn("last checkout failure", text)

    async def test_automatic_recovery_notifies_once_only_after_a_sent_warning(self):
        self.assertFalse(events.record_connected(self.client._verified_session))
        self.record(at=1010)
        with patch("vinted_connection_notices.time.time", return_value=1020):
            self.assertEqual(await self.notifier.check(), "sent")
        self.update_buyer(verified_at=1030)
        self.assertTrue(events.record_connected(self.client._verified_session))
        self.assertFalse(events.record_connected(self.client._verified_session))
        self.assertEqual(await self.events.check(), "sent")
        self.assertIn(
            "connection recovered", self.bot.send_message.await_args.kwargs["text"]
        )

    async def test_renewal_warning_escalates_when_identity_is_actually_rejected(self):
        self.record(at=1010)
        with patch("vinted_connection_notices.time.time", return_value=1020):
            self.assertEqual(await self.notifier.check(), "sent")
        error = self.error()
        error.identity_rejected = True
        self.record(error, at=1030)
        with patch("vinted_connection_notices.time.time", return_value=1040):
            self.assertEqual(await self.notifier.check(), "sent")
            self.assertEqual(await self.notifier.check(), "quiet")
        self.assertIn(
            "account check (HTTP 401)", self.bot.send_message.await_args.kwargs["text"]
        )
        self.assertEqual(self.notice_row()["sent_level"], 2)

    async def test_telegram_failure_retries_after_cooldown_and_survives_restart(self):
        self.failure()
        self.bot.send_message.side_effect = NetworkError("fictional-private-upstream")
        with patch.object(events.time, "time", return_value=2000):
            self.assertEqual(await self.events.check(), "unavailable")
            self.assertEqual(await self.events.check(), "quiet")
        self.bot.send_message.side_effect = None
        with patch.object(events.time, "time", return_value=2301):
            self.assertEqual(
                await events.BuyerEventNotifier(self.bot, 123).check(), "sent"
            )

    async def test_uncertain_payment_escalates_and_never_claims_no_payment(self):
        self.failure()
        await self.events.check()
        self.failure(payment_started=True)
        self.assertEqual(await self.events.check(), "sent")
        text = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("Payment may have been submitted", text)
        self.assertNotIn("No payment was sent", text)

    def test_stale_client_and_validation_errors_cannot_create_incidents(self):
        self.update_buyer(session=buyer.encrypt({"new": True}))
        self.assertFalse(self.failure())
        self.assertFalse(
            events.record_connected(self.client._verified_session, manual=True)
        )
        self.update_buyer(session=self.sealed)
        self.assertFalse(
            events.record_checkout_failure(
                self.client, buyer.BuyerError("budget"), "submitting payment"
            )
        )
        self.assertEqual(self.rows(), [])

    async def test_disconnect_removes_pending_events_and_recipient_change_blocks_delivery(
        self,
    ):
        self.failure()
        self.assertEqual(
            await events.BuyerEventNotifier(self.bot, 456).check(), "quiet"
        )
        row = self.events.claim()
        buyer.disconnect()
        self.assertFalse(self.events.still_current(row))
        self.assertEqual(self.rows(), [])
        self.bot.send_message.assert_not_awaited()

    async def test_confirmed_purchase_closes_checkout_but_stale_client_cannot(self):
        self.failure()
        stale = SimpleNamespace(_verified_session=("99", b"old", {}))
        events.clear_checkout_failure(stale)
        self.assertIsNotNone(buyer.settings()["checkout_health"])
        events.clear_checkout_failure(self.client)
        self.assertIsNone(buyer.settings()["checkout_health"])
        self.assertEqual(await self.events.check(), "quiet")

    def test_save_connected_queues_confirmation_only_after_same_account_verification(
        self,
    ):
        client = SimpleNamespace(
            identity=lambda: ("99", "fictional"), exported=lambda: {"cookies": {}}
        )
        buyer.save_connected(client)
        self.assertEqual(self.rows()[0]["kind"], "reconnected")
        client.identity = lambda: ("100", "other")
        before = self.rows()
        with self.assertRaises(buyer.BuyerError):
            buyer.save_connected(client)
        self.assertEqual(self.rows(), before)
