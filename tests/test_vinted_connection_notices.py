"""Real SQLite notice episodes with fictional sessions and offline Telegram replies."""

import asyncio
import hashlib
import time
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import NetworkError, RetryAfter
from test_search_controls import DatabaseFixture
from werkzeug.security import generate_password_hash

import search_settings
import vinted_buyer as buyer
import vinted_connection_notices as notices
import vinted_session_worker as worker
from web_ui_plugin.web_ui import create_app


class NoticeFixture(DatabaseFixture):
    def setUp(self):
        super().setUp()
        self.sealed = buyer.encrypt(
            {
                "cookies": {
                    "access_token_web": "fictional-access-not-a-real-token",
                    "refresh_token_web": "fictional-refresh-not-a-real-token",
                }
            }
        )
        self.fingerprint = hashlib.sha256(self.sealed).hexdigest()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                """UPDATE vinted_buyer SET session=?,user_id='99',username='fictional',
                verified_at=1000,enabled=1,max_total=2000,max_extra=450,
                pickup_mode='saved',preferred_card_last4='1234',network=? WHERE id=1""",
                (self.sealed, buyer.encrypt({"proxy": "fictional-existing-proxy"})),
            )
            conn.execute(
                """INSERT INTO vinted_buy_attempts(item_id,state,checkout_id,total,
                message,updated,buyer_id,transaction_id)
                VALUES('fictional-previous-item','paid','fictional-checkout',1915,
                'existing payment must remain intact',999,'99','fictional-transaction')"""
            )
        self.bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=777))
        )
        self.notifier = notices.ConnectionNotifier(self.bot, 123)

    def error(self, reason="renewal_failed", stage="renewal", status=400):
        return buyer.BuyerError(
            "fictional-private-upstream-body-NEVER-PUBLISH",
            status,
            reason=reason,
            stage=stage,
        )

    def record(self, error=None, at=1010, fingerprint=None):
        with patch.object(notices.time, "time", return_value=at):
            return notices.record_failure(
                error or self.error(),
                self.fingerprint if fingerprint is None else fingerprint,
            )

    def notice_row(self):
        with closing(search_settings.connection()) as conn:
            return dict(
                conn.execute("SELECT * FROM vinted_connection_notice").fetchone()
            )

    def update_buyer(self, **values):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET "
                + ",".join(f"{key}=?" for key in values)
                + " WHERE id=1",
                tuple(values.values()),
            )

    def protected_rows(self):
        with closing(search_settings.connection()) as conn:
            names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
                if row[0] not in ("vinted_connection_notice", "sqlite_sequence")
            ]
            return {
                name: [tuple(row) for row in conn.execute(f'SELECT * FROM "{name}"')]
                for name in names
            }


class RecordingTests(NoticeFixture, unittest.TestCase):
    def test_records_fixed_metadata_for_the_exact_current_session(self):
        before = self.protected_rows()
        self.assertTrue(self.record())
        row = self.notice_row()
        self.assertEqual(row["session_fingerprint"], self.fingerprint)
        self.assertEqual(row["buyer_id"], "99")
        self.assertEqual(
            (row["reason"], row["stage"], row["http_status"], row["checked"]),
            ("renewal_failed", "renewal", 400, 1010),
        )
        self.assertNotIn("NEVER-PUBLISH", str(row))
        self.assertEqual(self.protected_rows(), before)

    def test_replaced_session_cannot_inherit_an_earlier_failure(self):
        replacement = buyer.encrypt({"cookies": {"access_token_web": "new-fictional"}})
        self.update_buyer(session=replacement, verified_at=1020)
        self.assertFalse(self.record(at=1030))
        self.assertIsNone(self.notice_row()["checked"])

    def test_unlinked_or_unverified_buyer_cannot_create_a_warning(self):
        for values in ({"session": None}, {"user_id": None}, {"verified_at": None}):
            with self.subTest(values=values):
                self.update_buyer(session=self.sealed, user_id="99", verified_at=1000)
                self.update_buyer(**values)
                self.assertFalse(self.record())
        self.assertIsNone(self.notice_row()["checked"])

    def test_password_sign_in_and_success_metadata_are_not_disconnects(self):
        errors = [
            self.error("credentials", "sign_in", 403),
            self.error("connected", "identity", 200),
            self.error("renewed", "renewal", 200),
            self.error("verification_code", "sign_in", 200),
            self.error("endpoint_reached", "sign_in", 400),
            self.error("unknown-raw-reason", "identity", 400),
            self.error("network", "unknown-raw-stage", None),
            ValueError("fictional-private-value"),
        ]
        for error in errors:
            with self.subTest(
                error=type(error).__name__, reason=getattr(error, "reason", None)
            ):
                self.assertFalse(self.record(error))
        self.assertIsNone(self.notice_row()["checked"])

    def test_invalid_fingerprint_and_status_never_become_untrusted_metadata(self):
        for fingerprint in ("x" * 64, "0" * 63, "0" * 65, "A" * 64, ""):
            with self.subTest(fingerprint=fingerprint):
                self.assertFalse(self.record(fingerprint=fingerprint))
        self.assertTrue(self.record(self.error(status=True)))
        self.assertIsNone(self.notice_row()["http_status"])

    def test_same_incident_retains_original_start_and_new_verification_resets_it(self):
        self.assertTrue(self.record(self.error("network", "identity", None)))
        self.assertTrue(self.record(self.error("network", "identity", None), at=1610))
        self.assertEqual(self.notice_row()["incident_started"], 1010)
        self.update_buyer(verified_at=1700)
        self.assertTrue(self.record(at=1800))
        self.assertEqual(self.notice_row()["incident_started"], 1800)

    def test_delayed_same_seal_failure_cannot_overwrite_later_identity_verification(
        self,
    ):
        self.update_buyer(verified_at=1020)
        with patch.object(notices.time, "time", return_value=1030):
            self.assertFalse(
                notices.record_failure(self.error(), self.fingerprint, checked_at=1010)
            )
        self.assertIsNone(self.notice_row()["checked"])

    def test_nonfinite_or_boolean_observation_times_are_rejected(self):
        for checked_at in (float("nan"), float("inf"), True, "1010"):
            with self.subTest(checked_at=checked_at):
                self.assertFalse(
                    notices.record_failure(
                        self.error(), self.fingerprint, checked_at=checked_at
                    )
                )
        self.assertIsNone(self.notice_row()["checked"])

    def test_optional_database_failure_does_not_replace_the_account_error(self):
        with patch.object(
            notices, "connection", side_effect=OSError("private-db-path")
        ), self.assertLogs("vinted_connection_notices", level="WARNING") as logged:
            self.assertFalse(self.record())
        self.assertIn("OSError", " ".join(logged.output))
        self.assertNotIn("private-db-path", " ".join(logged.output))


class DeliveryTests(NoticeFixture, unittest.IsolatedAsyncioTestCase):
    async def check(self, at=1011, notifier=None):
        with patch.object(notices.time, "time", return_value=at):
            return await (notifier or self.notifier).check()

    async def test_ambiguous_400_warns_once_and_survives_notifier_restart(self):
        self.assertTrue(self.record())
        self.assertEqual(await self.check(), "sent")
        sent = self.bot.send_message.await_args.kwargs
        self.assertEqual(sent["chat_id"], "123")
        self.assertIn("HTTP 400", sent["text"])
        self.assertIn("does not prove that your login has expired", sent["text"])
        self.assertNotIn("NEVER-PUBLISH", sent["text"])
        self.assertEqual(self.notice_row()["sent_level"], 1)
        restarted = notices.ConnectionNotifier(self.bot, "123")
        self.assertEqual(await self.check(2000, restarted), "quiet")
        self.assertTrue(self.record(at=2010))
        self.assertEqual(await self.check(2011, restarted), "quiet")
        self.bot.send_message.assert_awaited_once()

    async def test_explicit_refresh_rejection_escalates_only_once(self):
        self.record()
        self.assertEqual(await self.check(), "sent")
        self.record(self.error("refresh_rejected", "renewal", 401), at=1020)
        self.assertEqual(await self.check(1021), "sent")
        self.assertEqual(self.notice_row()["sent_level"], 2)
        self.assertIn(
            "reconnect the same buyer", self.bot.send_message.await_args.kwargs["text"]
        )
        self.record(self.error("refresh_rejected", "renewal", 401), at=1030)
        self.assertEqual(await self.check(1031), "quiet")
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_stale_warning_is_closed_after_a_new_identity_verification(self):
        self.record()
        self.update_buyer(verified_at=1020)
        self.assertEqual(await self.check(1021), "quiet")
        self.assertIsNone(self.notice_row()["checked"])
        self.bot.send_message.assert_not_awaited()

    async def test_replacement_session_suppresses_warning_even_without_new_timestamp(
        self,
    ):
        self.record()
        replacement = buyer.encrypt({"cookies": {"access_token_web": "replacement"}})
        self.update_buyer(session=replacement)
        self.assertEqual(await self.check(), "quiet")
        self.bot.send_message.assert_not_awaited()

    async def test_new_episode_can_warn_after_the_connection_has_recovered(self):
        self.record()
        self.assertEqual(await self.check(), "sent")
        self.update_buyer(verified_at=1020)
        self.assertEqual(await self.check(1021), "quiet")
        self.record(at=1030)
        self.assertEqual(await self.check(1031), "sent")
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(self.notice_row()["incident_started"], 1030)

    async def test_transient_failure_has_a_15_minute_grace_and_no_repeat_storm(self):
        self.record(self.error("network", "identity", None))
        for at in (1011, 1310, 1909):
            self.assertEqual(await self.check(at), "quiet")
        self.assertTrue(self.record(self.error("network", "identity", None), at=1909))
        self.assertEqual(await self.check(1910), "sent")
        for at in (1970, 2210, 2810):
            self.record(self.error("network", "identity", None), at=at)
            self.assertEqual(await self.check(at), "quiet")
        self.bot.send_message.assert_awaited_once()

    async def test_http_503_and_rate_limit_share_transient_grace(self):
        for reason, status in (("http_error", 503), ("rate_limited", 429)):
            with self.subTest(reason=reason):
                self.update_buyer(verified_at=1000)
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute(
                        "UPDATE vinted_connection_notice SET checked=NULL WHERE id=1"
                    )
                self.record(self.error(reason, "identity", status))
                self.assertEqual(await self.check(1909), "quiet")
                self.assertEqual(await self.check(1910), "sent")

    async def test_busy_real_buyer_lock_skips_without_blocking_or_sending(self):
        self.record()
        started = time.perf_counter()
        with buyer.exclusive():
            self.assertEqual(await self.check(), "busy")
        self.assertLess(time.perf_counter() - started, 1)
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(await self.check(), "sent")

    async def test_only_the_current_configured_telegram_recipient_is_used(self):
        self.record()
        for recipient in ("456", "0", "123?private", "", "123456789012345678901"):
            with self.subTest(recipient=recipient):
                notifier = notices.ConnectionNotifier(self.bot, recipient)
                self.assertEqual(await self.check(notifier=notifier), "quiet")
        self.bot.send_message.assert_not_awaited()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE parameters SET value='456' WHERE key='telegram_chat_id'"
            )
        self.assertEqual(await self.check(), "quiet")
        self.assertEqual(
            await self.check(notifier=notices.ConnectionNotifier(self.bot, 456)), "sent"
        )
        self.assertEqual(self.bot.send_message.await_args.kwargs["chat_id"], "456")

    async def test_recipient_change_between_claim_and_send_suppresses_delivery(self):
        self.record()
        claim = self.notifier.claim

        def claim_then_change_recipient():
            row = claim()
            with closing(search_settings.connection()) as conn, conn:
                conn.execute(
                    "UPDATE parameters SET value='456' WHERE key='telegram_chat_id'"
                )
            return row

        with patch.object(
            self.notifier, "claim", side_effect=claim_then_change_recipient
        ):
            self.assertEqual(await self.check(), "superseded")
        self.bot.send_message.assert_not_awaited()

    async def test_new_identity_between_claim_and_send_suppresses_delivery(self):
        self.record()
        claim = self.notifier.claim

        def claim_then_verify():
            row = claim()
            self.update_buyer(verified_at=1020)
            return row

        with patch.object(self.notifier, "claim", side_effect=claim_then_verify):
            self.assertEqual(await self.check(), "superseded")
        self.bot.send_message.assert_not_awaited()

    async def test_concurrent_notifiers_do_not_send_duplicate_warning(self):
        self.record()
        other = notices.ConnectionNotifier(self.bot, 123)
        with patch.object(notices.time, "time", return_value=1011):
            results = await asyncio.gather(self.notifier.check(), other.check())
        self.assertEqual(results.count("sent"), 1)
        self.assertTrue(set(results) <= {"sent", "quiet", "busy"})
        self.bot.send_message.assert_awaited_once()

    async def test_failure_capture_during_send_does_not_repeat_accepted_warning(self):
        self.record()

        async def send_while_another_failure_arrives(**kwargs):
            self.assertTrue(self.record(at=1020))
            return SimpleNamespace(message_id=777)

        self.bot.send_message.side_effect = send_while_another_failure_arrives
        self.assertEqual(await self.check(), "sent")
        self.assertEqual(await self.check(1021), "quiet")
        self.bot.send_message.assert_awaited_once()

    async def test_network_error_retries_safely_after_five_minutes(self):
        self.record()
        self.bot.send_message.side_effect = NetworkError("private-telegram-endpoint")
        with self.assertLogs("vinted_connection_notices", level="WARNING") as logged:
            self.assertEqual(await self.check(), "unavailable")
        self.assertNotIn("private-telegram-endpoint", " ".join(logged.output))
        self.assertEqual(self.notice_row()["retry_at"], 1311)
        self.assertEqual(self.notice_row()["sent_level"], 0)
        self.assertEqual(await self.check(1310), "quiet")
        self.bot.send_message.side_effect = None
        self.assertEqual(await self.check(1311), "sent")
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_telegram_retry_after_honours_the_longer_cooldown(self):
        self.record()
        self.bot.send_message.side_effect = RetryAfter(900)
        self.assertEqual(await self.check(), "unavailable")
        self.assertEqual(self.notice_row()["retry_at"], 1913)
        self.assertEqual(await self.check(1912), "quiet")
        self.bot.send_message.side_effect = None
        self.assertEqual(await self.check(1913), "sent")

    async def test_notification_never_changes_saved_auth_or_buying_preferences(self):
        self.record()
        before = self.protected_rows()
        with patch.object(buyer, "Client") as client, patch.object(
            buyer, "connected_client"
        ) as connected:
            self.assertEqual(await self.check(), "sent")
        client.assert_not_called()
        connected.assert_not_called()
        self.assertEqual(self.protected_rows(), before)
        for table in (
            "vinted_buyer",
            "vinted_buyer_maintenance",
            "vinted_buy_attempts",
            "parameters",
        ):
            self.assertEqual(self.protected_rows()[table], before[table])

    async def test_message_has_a_safe_dashboard_link_and_no_credentials(self):
        self.record(self.error("refresh_rejected", "renewal", 401))
        with patch.dict(
            notices.os.environ, {"DASHBOARD_URL": "https://fictional-msj.example"}
        ):
            self.assertEqual(await self.check(), "sent")
        kwargs = self.bot.send_message.await_args.kwargs
        button = kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(
            button.url, "https://fictional-msj.example/connections#vinted-buying"
        )
        self.assertTrue(kwargs["disable_web_page_preview"])
        for private in (
            "fictional-access",
            "fictional-refresh",
            "fictional-existing-proxy",
            "NEVER-PUBLISH",
        ):
            self.assertNotIn(private, kwargs["text"])

    async def test_unconfigured_or_unsafe_dashboard_url_omits_the_button(self):
        self.record()
        with patch.dict(
            notices.os.environ,
            {"DASHBOARD_URL": "http://fictional.example", "RAILWAY_PUBLIC_DOMAIN": ""},
        ):
            self.assertEqual(await self.check(), "sent")
        self.assertIsNone(self.bot.send_message.await_args.kwargs["reply_markup"])

    async def test_healthy_worker_tick_has_no_warning_or_auth_request(self):
        self.update_buyer(verified_at=time.time())
        before = self.protected_rows()
        with patch.object(worker, "due", return_value=False), patch.object(
            buyer, "connected_client"
        ) as account, patch.object(
            worker.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)
        ), self.assertRaises(
            asyncio.CancelledError
        ):
            await worker.run(self.notifier)
        account.assert_not_called()
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(self.protected_rows(), before)

    def bot_context(self):
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=self.bot)
        context.__aexit__ = AsyncMock(return_value=False)
        return context

    async def test_owner_delivery_test_is_labelled_and_preserves_all_saved_state(self):
        self.record()
        before = self.protected_rows()
        prior_notice = self.notice_row()
        with patch.object(
            notices, "Bot", return_value=self.bot_context()
        ), patch.object(notices.time, "time", return_value=1100), patch.object(
            buyer, "connected_client"
        ) as account:
            result = await notices.send_test()
        self.assertIn("Telegram accepted", result)
        kwargs = self.bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], "123")
        self.assertTrue(kwargs["text"].startswith("TEST"))
        self.assertIn("not an account failure", kwargs["text"])
        self.assertIn("No Vinted request, checkout or payment", kwargs["text"])
        account.assert_not_called()
        self.assertEqual(self.protected_rows(), before)
        after = self.notice_row()
        self.assertEqual(after.pop("test_checked"), 1100)
        prior_notice.pop("test_checked")
        self.assertEqual(after, prior_notice)

    async def test_owner_delivery_timeout_keeps_the_five_minute_test_throttle(self):
        before = self.protected_rows()
        self.bot.send_message.side_effect = NetworkError("private-bot-endpoint")
        with patch.object(
            notices, "Bot", return_value=self.bot_context()
        ) as constructor, patch.object(notices.time, "time", return_value=1100):
            with self.assertLogs(
                "vinted_connection_notices", level="WARNING"
            ) as logged, self.assertRaisesRegex(
                ValueError, "Telegram did not confirm"
            ) as error:
                await notices.send_test()
            self.assertNotIn("private-bot-endpoint", str(error.exception))
            self.assertNotIn("private-bot-endpoint", " ".join(logged.output))
            with self.assertRaisesRegex(ValueError, "Wait five minutes"):
                await notices.send_test()
        constructor.assert_called_once()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.notice_row()["test_checked"], 1100)
        self.assertEqual(self.protected_rows(), before)

    async def test_repeated_owner_delivery_test_is_throttled_before_bot_creation(self):
        with patch.object(
            notices, "Bot", return_value=self.bot_context()
        ) as constructor, patch.object(notices.time, "time", return_value=1100):
            await notices.send_test()
            with self.assertRaisesRegex(ValueError, "Wait five minutes"):
                await notices.send_test()
        constructor.assert_called_once()
        self.bot.send_message.assert_awaited_once()

    async def test_owner_delivery_test_rejects_invalid_recipient_without_bot_creation(
        self,
    ):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE parameters SET value='0' WHERE key='telegram_chat_id'")
        with patch.object(notices, "Bot") as constructor, self.assertRaisesRegex(
            ValueError, "Configure the Vinted Telegram chat"
        ):
            await notices.send_test()
        constructor.assert_not_called()
        self.assertEqual(self.notice_row()["test_checked"], 0)


class OwnerRouteTests(NoticeFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()

    def owner(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE dashboard_auth SET password_hash=?",
                (generate_password_hash("fictional offline password"),),
            )
        with self.client.session_transaction() as session:
            session["owner"] = True
            session["csrf"] = "offline-csrf"

    def test_authenticated_csrf_guarded_test_route_does_not_check_the_buyer(self):
        self.owner()
        before = self.protected_rows()
        with patch.object(
            notices,
            "send_test",
            new=AsyncMock(return_value="Telegram accepted fictional test"),
        ) as send, patch.object(buyer, "connected_client") as account:
            response = self.client.post(
                "/connections",
                data={"action": "buyer_connection_notice_test", "csrf": "offline-csrf"},
            )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.location, "/connections")
        send.assert_awaited_once()
        account.assert_not_called()
        self.assertEqual(self.protected_rows(), before)

    def test_missing_csrf_rejects_delivery_test_before_sending(self):
        self.owner()
        with patch.object(notices, "send_test", new=AsyncMock()) as send:
            response = self.client.post(
                "/connections", data={"action": "buyer_connection_notice_test"}
            )
        self.assertEqual(response.status_code, 400)
        send.assert_not_awaited()

    def test_signed_out_owner_cannot_trigger_delivery_test(self):
        with self.client.session_transaction() as session:
            session["csrf"] = "offline-csrf"
        with patch.object(notices, "send_test", new=AsyncMock()) as send:
            response = self.client.post(
                "/connections",
                data={"action": "buyer_connection_notice_test", "csrf": "offline-csrf"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.startswith(("/login", "/setup")))
        send.assert_not_awaited()

    def test_connections_displays_owner_connection_warning_test_control(self):
        self.owner()
        response = self.client.get("/connections")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Telegram connection warnings", response.data)
        self.assertIn(b"Send connection-warning test", response.data)


class DashboardURLTests(unittest.TestCase):
    def test_https_root_and_railway_domain_form_the_connection_link(self):
        for variables in (
            {
                "DASHBOARD_URL": "https://fictional.example/",
                "RAILWAY_PUBLIC_DOMAIN": "",
            },
            {"DASHBOARD_URL": "", "RAILWAY_PUBLIC_DOMAIN": "fictional.example"},
            {
                "DASHBOARD_URL": "https://fictional.example:443",
                "RAILWAY_PUBLIC_DOMAIN": "",
            },
        ):
            with self.subTest(variables=variables), patch.dict(
                notices.os.environ, variables
            ):
                self.assertTrue(
                    notices.dashboard_url().endswith("/connections#vinted-buying")
                )

    def test_query_userinfo_paths_and_non_https_are_rejected(self):
        invalid = (
            "http://fictional.example",
            "javascript:alert(1)",
            "https://user:secret@fictional.example",
            "https://user@fictional.example",
            "https://fictional.example?token=private",
            "https://fictional.example#private",
            "https://fictional.example/connections",
            "https://fictional.example:444",
            "https://fictional.example:bad",
            "https://fictional.example\n/",
            "https://",
        )
        for base in invalid:
            with self.subTest(base=base), patch.dict(
                notices.os.environ, {"DASHBOARD_URL": base, "RAILWAY_PUBLIC_DOMAIN": ""}
            ):
                self.assertIsNone(notices.dashboard_url())


if __name__ == "__main__":
    unittest.main()
