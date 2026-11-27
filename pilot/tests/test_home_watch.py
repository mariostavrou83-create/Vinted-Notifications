import hashlib
import json
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pilot import flow, flow_worker
from pilot.app import create_app
from pilot.db import get_db, settings_for


class HomeWatchTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(Path(tmp.name) / "home.db"),
                "PILOT_TELEGRAM_BOT_TOKEN": "test",
            }
        )
        ctx = self.app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        self.conn = get_db()
        self.client = self.app.test_client()
        self.now = int(time.time())
        for uid in [1, 2]:
            self.conn.execute("INSERT INTO users VALUES(?,?,?)", (uid, f"user{uid}", "unused"))
            self.conn.execute(
                "INSERT INTO settings VALUES(?,?)", (uid, '{"quiet_start":"","quiet_end":""}')
            )
            token = self.token(uid)
            self.conn.execute(
                "INSERT INTO native_devices VALUES(?,?,?,?,?,?)",
                (
                    uid,
                    f"pair{uid}",
                    hashlib.sha256(token.encode()).hexdigest(),
                    f"buyer{uid}",
                    self.now + 800,
                    self.now,
                ),
            )
            self.conn.execute("INSERT INTO pilot_chats VALUES(?,?)", (uid, 100 + uid))
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1

    @staticmethod
    def token(uid):
        return str(uid) * 40

    def post(self, path, uid=1, **data):
        return self.client.post(
            "/api/native/" + path,
            headers={"Authorization": "Bearer " + self.token(uid)},
            json={"username": f"buyer{uid}", **data},
        )

    def watch(self, uid=1, enabled=True):
        self.assertEqual(self.post("home-watch", uid, enabled=enabled).status_code, 200)

    def item(self, id="350", age=10, **changes):
        stamp = self.now - age
        return {
            "id": id,
            "title": "Blue jeans",
            "listing_url": "https://www.vinted.co.uk/items/" + id,
            "currency": "GBP",
            "price_pence": 1200,
            "listed_at": stamp,
            "age_text": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
            **changes,
        }

    def test_authentication_explicit_activation_and_owner_isolation(self):
        self.assertEqual(
            self.client.post("/api/native/home-watch", json={"enabled": True}).status_code, 401
        )
        self.assertEqual(self.post("home", items=[self.item()]).status_code, 403)
        self.watch(2)
        self.assertFalse(self.client.get("/api/native/home-status").json["enabled"])
        self.watch()
        self.assertEqual(
            self.post("home", items=[self.item(), self.item()]).json, {"accepted": 1, "skipped": 1}
        )
        self.assertEqual(self.post("home", items=[self.item()]).status_code, 429)
        self.assertEqual(self.post("home", 2, items=[self.item()]).json["accepted"], 1)
        self.assertTrue(self.client.get("/api/native/home-status").json["last_observed_at"])
        self.assertEqual(len(flow.alerts_for_user(self.conn, 1)), 1)
        self.assertEqual(self.post("home", username="other", items=[self.item()]).status_code, 401)
        self.assertFalse(self.client.get("/api/native/home-status").json["paired"])
        self.assertEqual(self.post("home-watch", enabled=True).status_code, 401)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM native_devices WHERE user_id=2").fetchone()[0], 1
        )

    def test_age_and_user_filters_with_unknown_fields(self):
        self.watch()
        self.conn.execute(
            "UPDATE settings SET data=? WHERE user_id=1",
            (json.dumps({"quiet_start": "", "quiet_end": "", "categories": ["Women"], "sizes": ["M"]}),),
        )
        valid = self.item(category="Women", size="M", total_pence=100)
        items = [
            valid,
            self.item("1", age=180),
            self.item("2", age=-1),
            self.item("3", age_text="featured"),
            self.item("4", age_text="just now"),
            self.item("5", age_text=self.item(age=25)["age_text"]),
            self.item("6", size="M"),
            self.item("7", category="Women", size="S"),
            self.item("8", age=130, age_text="one minute ago", category="women", size="m"),
        ]
        self.assertEqual(self.post("home", items=items).json, {"accepted": 2, "skipped": 7})
        for row in self.conn.execute("SELECT listing FROM pilot_alerts"):
            self.assertIsNone(json.loads(row[0])["total_pence"])
        self.conn.execute(
            "UPDATE settings SET data=? WHERE user_id=1",
            (json.dumps({"quiet_start": "", "quiet_end": "", "categories": ["Men"]}),),
        )
        with patch.object(flow_worker, "telegram") as tg:
            self.assertTrue(flow_worker.deliver(self.conn, self.now + 20))
            tg.assert_not_called()

    def test_home_overlaps_search_once_and_keeps_manual_separate(self):
        self.watch()
        prefs = settings_for(self.conn, 1)
        item = self.item()
        self.assertTrue(flow.record_public_listing(self.conn, 1, item, prefs, self.now))
        self.assertTrue(flow.record_manual_home_listing(self.conn, 1, item, prefs, self.now))
        self.assertEqual(
            self.post("home", items=[item, self.item("99")]).json, {"accepted": 1, "skipped": 1}
        )
        self.assertIsNone(flow.record_public_listing(self.conn, 1, self.item("99"), prefs, self.now))
        self.assertIsNone(
            flow.record_listing(
                self.conn,
                1,
                "partner",
                {**self.item("99"), "total_pence": 1600},
                "home",
                prefs,
                self.now,
            )
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM pilot_alerts WHERE delivery='new'").fetchone()[0], 2
        )

    def test_link_only_and_bound_button_after_delivery(self):
        self.watch()
        self.assertEqual(self.post("home", items=[self.item(), self.item("333")]).json["accepted"], 2)
        with patch.object(flow_worker, "telegram", return_value={"message_id": 777}) as telegram:
            self.assertTrue(flow_worker.deliver(self.conn, self.now + 5))
            params = telegram.call_args.args[1]
            self.assertEqual(params["chat_id"], 101)
            self.assertIn("url", params["reply_markup"]["inline_keyboard"][0][0])
            self.assertNotIn("Estimated total", params["text"])
            self.conn.execute(
                "INSERT INTO native_settings VALUES(1,?)",
                (json.dumps({"payment_label": "Vinted balance"}),),
            )
            self.assertTrue(flow_worker.deliver(self.conn, self.now + 6))
            button = telegram.call_args.args[1]["reply_markup"]["inline_keyboard"][0][0]
            aid = self.conn.execute("SELECT id FROM pilot_alerts WHERE listing_id='333'").fetchone()[0]
            self.assertEqual(button["callback_data"], "nbuy:" + str(aid))

            def tap(userid, chat=101):
                return flow.telegram_update(
                    self.conn,
                    {
                        "update_id": userid,
                        "callback_query": {
                            "from": {"id": chat},
                            "id": "callback",
                            "data": button["callback_data"],
                            "message": {"chat": {"id": chat, "type": "private"}, "message_id": 777},
                        },
                    },
                )

            self.assertEqual(tap(11, 102)["text"], "Alert unavailable.")
            self.assertEqual(tap(12)["text"], "Checking your order.")
            self.watch(enabled=False)
            self.watch()
            self.assertEqual(tap(13)["text"], "Alert unavailable.")
            with patch("pilot.native_order.request_native") as submit:
                self.assertTrue(flow_worker.buy_job(self.conn, self.now + 10))
                submit.assert_not_called()

    def test_explicit_stop_withdraws_queued_delivery(self):
        self.watch()
        self.post("home", items=[self.item()])
        self.watch(enabled=False)
        self.watch()
        with patch.object(flow_worker, "telegram") as tg:
            self.assertFalse(flow_worker.deliver(self.conn, self.now + 1))
            tg.assert_not_called()
