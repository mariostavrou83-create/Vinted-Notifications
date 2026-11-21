import json
import time
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import RetryAfter
from test_search_controls import DatabaseFixture

import finder_incidents as incidents
import search_settings
import vinted_home_controls


class FinderIncidentsTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.row = {"item_id": "1200", "title": "Ralph Lauren zip knit"}
        self.outcome = {
            "state": "failed_before_payment",
            "reason": "credentials",
            "message": "Vinted rejected the buying account sign-in.",
        }
        self.notice = SimpleNamespace(
            recipient_valid=lambda: True,
            chat_id="123",
            bot=SimpleNamespace(
                send_message=AsyncMock(return_value=SimpleNamespace(message_id=5))
            ),
        )

    def events(self):
        with closing(search_settings.connection()) as conn:
            return [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT value FROM parameters WHERE key GLOB ?",
                    (incidents.PREFIX + "*",),
                )
            ]

    def set_home(self, role, code, checked, page=None):
        with closing(search_settings.connection()) as conn, conn:
            incidents.write(
                conn,
                "home_monitor_status_" + role,
                {
                    "code": code,
                    "checked_at": checked,
                    "http_status": 401 if code == "credentials" else 200,
                },
            )
            if page:
                conn.execute(
                    "INSERT OR REPLACE INTO vinted_home_pages VALUES(?,?,?)",
                    (role, page, "[]"),
                )

    def add_delivery(self, item, found, status, attempts=0):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                """INSERT INTO alert_outbox(item_id,search_name,content,url,title,price,currency,found_at,status,sent_at,attempts)
                         VALUES(?, 'Search', 'text', 'link', 'title','8','GBP',?,?,?,?)""",
                (item, found, status, found if status == "sent" else None, attempts),
            )

    async def test_failed_buy_push_is_separate_durable_and_deduplicated_per_tap(self):
        await incidents.queue_buy_failure(self.row, self.outcome, "first-tap")
        await incidents.queue_buy_failure(self.row, self.outcome, "first-tap")
        await incidents.queue_buy_failure(
            self.row, dict(self.outcome, state="paid"), "second-tap"
        )
        with patch.object(incidents.asyncio, "sleep", new=AsyncMock()):
            await incidents.deliver(self.notice)
            await incidents.deliver(self.notice)
        self.notice.bot.send_message.assert_awaited_once()
        self.assertIn(
            "stopped before payment",
            self.notice.bot.send_message.call_args.kwargs["text"],
        )
        self.assertEqual(self.events()[0]["target"], "reconnect")
        await incidents.queue_buy_failure(self.row, self.outcome, "real-second-tap")
        self.assertEqual(len(self.events()), 2)

    async def test_telegram_failure_retains_notice_until_retry_and_never_retries_purchase(
        self,
    ):
        await incidents.queue_buy_failure(self.row, self.outcome, "once")
        self.notice.bot.send_message.side_effect = RetryAfter(80)
        await incidents.deliver(self.notice)
        event = self.events()[0]
        self.assertNotIn("sent", event)
        self.assertGreaterEqual(event["due"], self.now + 80)
        self.assertIsNone(incidents.claim(self.now + 40))
        reserved = incidents.claim(self.now + 85)
        self.assertIsNotNone(reserved)
        self.assertIsNone(incidents.claim(self.now + 86))

    def test_home_rejection_and_real_recovery_are_reported_once(self):
        vinted_home_controls.configure("1")
        self.set_home("buyer", "credentials", self.now, page=self.now - 20)
        incidents.check_health(self.now)
        incidents.check_health(self.now + 1)
        self.set_home("buyer", "busy", self.now + 2)
        incidents.check_health(self.now + 2)
        self.assertEqual(len(self.events()), 1)
        self.assertIn("HTTP 401", self.events()[0]["text"])
        self.set_home("buyer", "checked", self.now + 3, page=self.now + 3)
        incidents.check_health(self.now + 3)
        self.assertEqual(len(self.events()), 2)
        self.assertTrue(any("recovered" in e["text"] for e in self.events()))

    def test_quiet_period_and_failed_delivery_are_different_incidents(self):
        self.add_delivery("1", self.now - 1930, "sent")
        search_settings.record_health(1, self.now, 0.1, 1, "")
        incidents.check_health(self.now)
        self.assertTrue(any("still checking" in e["text"] for e in self.events()))
        self.assertFalse(any(e["kind"] == "Telegram delivery" for e in self.events()))
        self.add_delivery("2", self.now - 120, "pending", 1)
        incidents.check_health(self.now + 1)
        self.assertTrue(any(e["kind"] == "Telegram delivery" for e in self.events()))
        self.assertEqual(
            sum(e["kind"] == "30-minute listing update" for e in self.events()), 1
        )

    def test_search_stall_and_heartbeat_gap(self):
        incidents.check_health(self.now)
        search_settings.record_health(1, self.now + 1, 0.1, 1, "")
        incidents.check_health(self.now + 601)
        kinds = [e["kind"] for e in self.events()]
        self.assertIn("service", kinds)
        self.assertIn("Saved searches", kinds)
        search_settings.record_health(1, self.now + 602, 0.1, 1, "")
        incidents.check_health(self.now + 603)
        self.assertTrue(
            any("Saved searches · recovered" in e["text"] for e in self.events())
        )
