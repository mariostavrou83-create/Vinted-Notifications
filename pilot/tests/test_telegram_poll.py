import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pilot import flow, telegram_poll
from pilot.app import create_app
from pilot.db import get_db, settings_for


class TelegramPollingTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(Path(tmp.name) / "pilot.sqlite"),
                "PILOT_TELEGRAM_BOT_TOKEN": "355:fixture",
                "PILOT_TELEGRAM_BOT_USERNAME": "Finder_test_bot",
                "PILOT_TELEGRAM_MODE": "polling",
            }
        )
        ctx = self.app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        self.conn = get_db()
        self.conn.execute("INSERT INTO users VALUES(1,'owner','hash')")
        self.now = time.time()
        self.outbound = []
        self.batch = []
        self.url = ""
        self.username = "Finder_test_bot"

    def api(self, method, payload):
        self.outbound.append((method, payload.copy()))
        if method == "getMe":
            return {"id": 355, "is_bot": True, "username": self.username}
        if method == "getWebhookInfo":
            return {"url": self.url}
        if method == "getUpdates":
            return self.batch
        return True

    def poll(self, after=0):
        with patch("pilot.flow_worker.telegram", side_effect=self.api):
            return telegram_poll.receive_updates(self.conn, self.now + after)

    def test_local_account_link_and_offset_no_secret_or_public_origin(self):
        reply = flow.new_link(self.conn, 1).get_json()
        code = reply["url"].split("start=")[1]
        self.batch = [
            {
                "update_id": 71,
                "message": {
                    "text": "/start " + code,
                    "from": {"id": 52},
                    "chat": {"id": 52, "type": "private"},
                },
            }
        ]
        self.assertTrue(self.poll())
        self.assertEqual(
            self.conn.execute("SELECT chat_id FROM pilot_chats WHERE user_id=1").fetchone()[0], 52
        )
        self.assertTrue(any("connected" in data.get("text", "") for _, data in self.outbound))
        self.outbound.clear()
        self.assertTrue(self.poll(2))
        self.assertEqual(
            self.outbound,
            [
                (
                    "getUpdates",
                    {
                        "offset": 72,
                        "timeout": 18,
                        "limit": 10,
                        "allowed_updates": ["message", "callback_query"],
                    },
                )
            ],
        )

    def test_callback_replay_never_submits_or_queues_twice(self):
        self.conn.execute("INSERT INTO pilot_chats VALUES(1,52)")
        self.conn.execute(
            "INSERT INTO pilot_buyer_link(user_id,account_ref,username) VALUES(1,'ref','one')"
        )
        item = {
            "id": "890",
            "price_pence": 100,
            "total_pence": 210,
            "title": "Item",
            "currency": "GBP",
            "listing_url": "https://www.vinted.co.uk/items/890",
        }
        prefs = settings_for(self.conn, 1)
        prefs.update(quiet_start="", quiet_end="")
        flow.record_listing(self.conn, 1, "ref", item, "search", prefs, self.now)
        self.conn.execute("UPDATE pilot_alerts SET sent_chat=52,message_id=8,delivery='sent'")
        self.batch = [
            {
                "update_id": 9,
                "callback_query": {
                    "id": "cb",
                    "data": "buy:1",
                    "from": {"id": 52},
                    "message": {"message_id": 8, "chat": {"id": 52, "type": "private"}},
                },
            }
        ]
        self.assertTrue(self.poll())
        self.assertTrue(self.poll(2))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM pilot_buy_jobs").fetchone()[0], 1)
        self.assertEqual([m for m, _ in self.outbound].count("answerCallbackQuery"), 1)

    def test_existing_webhook_untouched_and_reported(self):
        self.url = "https://example.invalid/other"
        self.assertFalse(self.poll())
        self.assertEqual([m for m, _ in self.outbound], ["getMe", "getWebhookInfo"])
        self.assertIn("already has a webhook", telegram_poll.polling_status(self.conn))
        self.assertFalse(self.poll(2))
        self.assertEqual(flow.new_link(self.conn, 1)[1], 503)
        self.assertFalse(self.app.test_cli_runner().invoke(args=["telegram-webhook"]).exit_code == 0)

    def test_wrong_username_or_unscoped_credentials_never_polled(self):
        self.username = "different"
        self.assertFalse(self.poll())
        self.assertEqual([m for m, _ in self.outbound], ["getMe"])
        self.app.config.pop("PILOT_TELEGRAM_BOT_TOKEN")
        with patch.dict(
            "os.environ", {"TELEGRAM_BOT_TOKEN": "999:unrelated", "FINDER_PILOT_TELEGRAM_BOT_TOKEN": ""}
        ):
            self.assertFalse(self.poll(301))
        self.assertEqual(len(self.outbound), 1)

    def test_single_listener_and_disabled_webhook(self):
        self.conn.execute(
            "INSERT INTO pilot_polling(bot_id,next_check,lease) VALUES('355',?,'busy')", (self.now + 90,)
        )
        self.assertFalse(self.poll())
        self.assertFalse(self.outbound)
        result = self.app.test_client().post("/telegram/webhook", json={"update_id": 9})
        self.assertEqual(result.status_code, 403)
