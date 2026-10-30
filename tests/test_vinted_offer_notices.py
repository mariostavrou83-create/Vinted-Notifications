"""Real SQLite incidents with offline Telegram; no marketplace calls."""

import asyncio
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import NetworkError, RetryAfter
from test_search_controls import DatabaseFixture

import search_settings
import vinted_offer_notices as notices
import vinted_offers as offers
import vinted_session_worker as session_worker

NOW = 1793365200.0


class NoticeFixture(DatabaseFixture):
    def setUp(self):
        super().setUp()
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=456)))
        self.notifier = notices.OfferNotifier(self.bot, "123")
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET user_id='111',verified_at=?,session=? WHERE id=1",
                (NOW, b"private-session-must-not-appear"),
            )
        offers.configure("live", "half_pound", "three_months", now=NOW)
        self.update(heartbeat=NOW)

    def update(self, **values):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_offer_settings SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=1",
                tuple(values.values()),
            )

    def row(self):
        with closing(search_settings.connection()) as conn:
            return dict(conn.execute("SELECT * FROM vinted_offer_notice").fetchone())

    def pause(self, reason="restricted", at=NOW):
        with patch.object(offers.time, "time", return_value=at):
            offers.pause(reason)

    def active(self, at=NOW + 1, mode="live"):
        offers.configure(mode, "half_pound", "three_months", now=at)
        self.update(heartbeat=at, cooldown_until=0)

    async def check(self, at=NOW, notifier=None):
        with patch.object(notices.time, "time", return_value=at):
            return await (notifier or self.notifier).check()


class DeliveryTests(NoticeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_preexisting_pause_warns_once_and_restart_deduplicates(self):
        self.pause()
        self.assertEqual(await self.check(), "sent")
        text = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("Vinted refused an offers request", text)
        self.assertNotIn("private-session", text)
        self.assertNotIn("111", text)
        self.assertEqual(self.row()["warned"], 1)
        restarted = notices.OfferNotifier(self.bot, "123")
        self.assertEqual(await self.check(NOW + 600, restarted), "quiet")
        self.bot.send_message.assert_awaited_once()

    async def test_crash_pause_without_pause_event_is_detected(self):
        self.update(mode="paused", pause_reason="uncertain", revision=99)
        self.assertEqual(await self.check(), "sent")
        self.assertIn("uncertain offer will not be sent again", self.bot.send_message.await_args.kwargs["text"])

    async def test_delivery_failure_retries_after_deadline_across_restart(self):
        self.pause()
        self.bot.send_message.side_effect = NetworkError("private-failure-body")
        with self.assertLogs("vinted_offer_notices", level="WARNING") as logged:
            self.assertEqual(await self.check(), "unavailable")
        self.assertNotIn("private-failure-body", str(logged.output))
        self.assertEqual(self.row()["warned"], 0)
        self.assertEqual(self.row()["retry_at"], NOW + notices.DELIVERY_RETRY)
        restarted = notices.OfferNotifier(self.bot, "123")
        self.assertEqual(await self.check(NOW + notices.DELIVERY_RETRY - 1, restarted), "quiet")
        self.bot.send_message.side_effect = None
        self.assertEqual(await self.check(NOW + notices.DELIVERY_RETRY, restarted), "sent")
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_telegram_retry_after_is_respected(self):
        self.pause()
        self.bot.send_message.side_effect = RetryAfter(900)
        self.assertEqual(await self.check(), "unavailable")
        self.assertEqual(self.row()["retry_at"], NOW + 902)
        self.assertEqual(await self.check(NOW + 901), "quiet")

    async def test_recovery_notifies_once_and_next_incident_warns_again(self):
        self.pause()
        self.assertEqual(await self.check(), "sent")
        self.active()
        self.assertEqual(await self.check(NOW + 1), "sent")
        self.assertIn("Live offers are running again", self.bot.send_message.await_args.kwargs["text"])
        self.assertEqual(await self.check(NOW + 2), "quiet")
        self.pause(at=NOW + 3)
        self.assertEqual(await self.check(NOW + 3), "sent")
        self.assertEqual(self.bot.send_message.await_count, 3)

    async def test_failed_recovery_delivery_retries_durably(self):
        self.pause()
        await self.check()
        self.active()
        self.bot.send_message.side_effect = NetworkError("offline")
        self.assertEqual(await self.check(NOW + 1), "unavailable")
        self.assertEqual(self.row()["recovery_pending"], 1)
        self.bot.send_message.side_effect = None
        self.update(heartbeat=NOW + 301)
        self.assertEqual(await self.check(NOW + 301, notices.OfferNotifier(self.bot, "123")), "sent")
        self.assertEqual(self.row()["recovery_pending"], 0)

    async def test_recovery_does_not_claim_an_offer_was_sent(self):
        self.pause()
        await self.check()
        self.active(mode="preview")
        self.assertEqual(await self.check(NOW + 1), "sent")
        self.assertIn("no offers are being sent in preview", self.bot.send_message.await_args.kwargs["text"])
        self.assertNotIn("successfully sent", self.bot.send_message.await_args.kwargs["text"])

    async def test_recovered_before_warning_delivery_cancels_stale_warning(self):
        self.pause()
        self.bot.send_message.side_effect = NetworkError("offline")
        await self.check()
        self.active()
        self.bot.send_message.side_effect = None
        self.assertEqual(await self.check(NOW + 1), "quiet")
        self.assertEqual(self.row()["active"], 0)
        self.assertEqual(self.row()["recovery_pending"], 0)
        self.bot.send_message.assert_awaited_once()

    async def test_manual_pause_never_warns_or_claims_running(self):
        self.pause()
        await self.check()
        self.pause("", at=NOW + 1)
        self.assertEqual(await self.check(NOW + 1), "quiet")
        self.assertEqual(await self.check(NOW + 600), "quiet")
        self.bot.send_message.assert_awaited_once()

    async def test_short_cooldown_is_quiet_and_extended_wait_warns_once(self):
        with closing(search_settings.connection()) as conn, conn:
            offers.event(conn, "cooldown", "read", now=NOW)
        self.update(pause_reason="cooldown", cooldown_until=NOW + 1000)
        self.assertEqual(await self.check(), "quiet")
        self.update(heartbeat=NOW + 899)
        self.assertEqual(await self.check(NOW + 899), "quiet")
        self.update(cooldown_until=NOW + 2000)
        with closing(search_settings.connection()) as conn, conn:
            offers.event(conn, "cooldown", "read", now=NOW + 899)
        self.assertEqual(await self.check(NOW + 900), "sent")
        self.assertIn("at least 15 minutes", self.bot.send_message.await_args.kwargs["text"])
        self.assertEqual(await self.check(NOW + 1000), "quiet")

    async def test_dead_worker_is_detected_during_a_long_cooldown(self):
        self.update(pause_reason="cooldown", cooldown_until=NOW + 3600)
        self.assertEqual(await self.check(), "quiet")
        self.assertEqual(await self.check(NOW + 181), "sent")
        self.assertIn("heartbeat", self.bot.send_message.await_args.kwargs["text"])

    async def test_stale_heartbeat_warns_without_pausing_or_touching_queue(self):
        self.assertEqual(await self.check(NOW + 180), "quiet")
        before = offers.state()
        self.assertEqual(await self.check(NOW + 181), "sent")
        self.assertIn("not updated its heartbeat", self.bot.send_message.await_args.kwargs["text"])
        self.assertEqual(offers.state(), before)
        self.update(heartbeat=NOW + 182)
        self.assertEqual(await self.check(NOW + 182), "sent")

    async def test_missing_startup_heartbeat_has_grace_period(self):
        self.update(heartbeat=None)
        self.assertEqual(await self.check(), "quiet")
        self.assertEqual(await self.check(NOW + 179), "quiet")
        self.assertEqual(await self.check(NOW + 180), "sent")

    async def test_current_identity_success_does_not_hide_offers_pause(self):
        self.pause()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET verified_at=?", (NOW + 10,))
        self.assertEqual(await self.check(NOW + 10), "sent")

    async def test_unknown_pause_reason_never_leaks_into_message_or_log(self):
        self.update(mode="paused", pause_reason="private upstream body", revision=99)
        with self.assertLogs("vinted_offer_notices", level="INFO") as logs:
            self.assertEqual(await self.check(), "sent")
        text = self.bot.send_message.await_args.kwargs["text"]
        self.assertNotIn("private upstream body", text + str(logs.output))
        self.assertEqual(self.row()["reason"], "shape")

    async def test_wrong_or_changed_recipient_prevents_delivery(self):
        self.pause()
        self.assertEqual(await self.check(notifier=notices.OfferNotifier(self.bot, "456")), "quiet")
        with patch.object(notices.time, "time", return_value=NOW):
            claimed = self.notifier.claim()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE parameters SET value='999' WHERE key='telegram_chat_id'")
        self.assertFalse(self.notifier.still_current(claimed))
        self.bot.send_message.assert_not_awaited()

    async def test_live_lease_blocks_other_worker_and_expired_lease_recovers(self):
        self.pause()
        with patch.object(notices.time, "time", return_value=NOW):
            claimed = self.notifier.claim()
        self.assertIsNotNone(claimed)
        self.assertEqual(await self.check(NOW + notices.DELIVERY_LEASE - 1), "quiet")
        self.assertEqual(await self.check(NOW + notices.DELIVERY_LEASE), "sent")

    async def test_state_change_invalidates_old_claim(self):
        self.pause()
        with patch.object(notices.time, "time", return_value=NOW):
            claimed = self.notifier.claim()
        self.active()
        with patch.object(notices.time, "time", return_value=NOW + 1):
            self.assertFalse(self.notifier.still_current(claimed))

    async def test_new_incident_does_not_inherit_stale_completion(self):
        self.pause()
        with patch.object(notices.time, "time", return_value=NOW):
            old = self.notifier.claim()
        self.active()
        self.pause("auth", at=NOW + 2)
        with patch.object(notices.time, "time", return_value=NOW + 2):
            new = self.notifier.claim()
            self.notifier.finish(old, 888)
        self.assertNotEqual(old["incident_id"], new["incident_id"])
        self.assertEqual(self.row()["warned"], 0)
        self.assertEqual(self.row()["lease_id"], new["lease_id"])


class MaintenanceIntegrationTests(NoticeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_offer_warning_check_runs_even_when_account_maintenance_fails(self):
        self.pause()
        notifier = SimpleNamespace(bot=self.bot, chat_id="123", check=AsyncMock())
        async def stop(_delay):
            raise asyncio.CancelledError
        with patch.object(session_worker, "maintain_connection", side_effect=RuntimeError("private")), \
                patch.object(session_worker.asyncio, "sleep", side_effect=stop):
            with self.assertRaises(asyncio.CancelledError):
                await session_worker.run(notifier)
        self.bot.send_message.assert_awaited_once()
        self.assertIn("offers need attention", self.bot.send_message.await_args.kwargs["text"])


class DeliveryTestControlTests(NoticeFixture, unittest.IsolatedAsyncioTestCase):
    def protected_rows(self):
        with closing(search_settings.connection()) as conn:
            names = [
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ) if row[0] not in ("vinted_offer_notice", "sqlite_sequence")
            ]
            return {
                name: [tuple(row) for row in conn.execute(f'SELECT * FROM "{name}"')]
                for name in names
            }

    def async_bot(self):
        bot = AsyncMock()
        bot.__aenter__.return_value = bot
        bot.send_message.return_value = SimpleNamespace(message_id=789)
        return bot

    async def test_owner_requested_test_is_labelled_and_only_changes_its_throttle(self):
        before = self.protected_rows()
        notice_before = self.row()
        bot = self.async_bot()
        with patch.object(notices, "Bot", return_value=bot), \
                patch.object(notices.time, "time", return_value=NOW):
            result = await notices.send_test()
        self.assertIn("Telegram accepted", result)
        text = bot.send_message.await_args.kwargs["text"]
        self.assertTrue(text.startswith("TEST — Vinted offer warnings"))
        self.assertIn("No Vinted request or offer was made", text)
        self.assertEqual(self.protected_rows(), before)
        after = self.row()
        self.assertEqual(after.pop("test_checked"), NOW)
        notice_before.pop("test_checked")
        self.assertEqual(after, notice_before)

    async def test_test_throttle_survives_restart_and_allows_after_five_minutes(self):
        bot = self.async_bot()
        with patch.object(notices, "Bot", return_value=bot):
            with patch.object(notices.time, "time", return_value=NOW):
                await notices.send_test()
            with patch.object(notices.time, "time", return_value=NOW + 299):
                with self.assertRaisesRegex(ValueError, "five minutes"):
                    await notices.send_test()
            with patch.object(notices.time, "time", return_value=NOW + 300):
                await notices.send_test()
        self.assertEqual(bot.send_message.await_count, 2)

    async def test_failed_test_is_private_and_keeps_throttle(self):
        bot = self.async_bot()
        bot.send_message.side_effect = NetworkError("private token and response")
        with patch.object(notices, "Bot", return_value=bot), \
                patch.object(notices.time, "time", return_value=NOW), \
                self.assertLogs("vinted_offer_notices", level="WARNING") as logged:
            with self.assertRaisesRegex(ValueError, "Telegram did not confirm") as raised:
                await notices.send_test()
        self.assertNotIn("private token", str(logged.output) + str(raised.exception))
        self.assertEqual(self.row()["test_checked"], NOW)

    async def test_invalid_recipient_blocks_test_before_telegram(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE parameters SET value='0' WHERE key='telegram_chat_id'")
        with patch.object(notices, "Bot") as bot:
            with self.assertRaisesRegex(ValueError, "Configure"):
                await notices.send_test()
        bot.assert_not_called()

    def test_notice_migration_is_idempotent_and_upgrades_the_test_throttle(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("ALTER TABLE vinted_offer_notice DROP COLUMN test_checked")
            notices.migrate(conn)
            notices.migrate(conn)
        self.assertEqual(self.row()["test_checked"], 0)


class UrlTests(unittest.TestCase):
    def test_button_targets_offers_only_on_a_valid_https_dashboard(self):
        with patch.dict(notices.os.environ, {"DASHBOARD_URL": "https://example.test", "RAILWAY_PUBLIC_DOMAIN": ""}):
            self.assertEqual(notices.dashboard_url(), "https://example.test/offers")
        for url in ("http://example.test", "https://u:p@example.test", "https://example.test/?token=private", "https://example.test/other"):
            with self.subTest(url=url), patch.dict(notices.os.environ, {"DASHBOARD_URL": url}):
                self.assertIsNone(notices.dashboard_url())
