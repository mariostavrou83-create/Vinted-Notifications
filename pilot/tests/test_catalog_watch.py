import json
import hashlib
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pilot import flow, flow_worker
from pilot.app import create_app
from pilot.db import get_db
from pilot.catalog_watch import key_for


class CatalogWatchTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(Path(tmp.name) / "a.db"),
                "PILOT_TELEGRAM_BOT_TOKEN": "test",
            }
        )
        ctx = self.app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        self.db = get_db()
        self.client = self.app.test_client()
        self.now = int(time.time())
        self.url = "https://www.vinted.co.uk/catalog?order=newest_first&brand_ids[]=7"
        self.token = "a" * 40
        self.db.execute("INSERT INTO users VALUES(1,'one','unused'),(2,'two','unused')")
        self.db.execute(
            "INSERT INTO settings VALUES(1,?)",
            (json.dumps({"quiet_start": "", "quiet_end": "", "max_shipping": "2.00"}),),
        )
        self.db.execute(
            "INSERT INTO native_devices VALUES(1,'paired',?,'buyer',?,?)",
            (hashlib.sha256(self.token.encode()).hexdigest(), self.now + 600, self.now),
        )
        self.db.execute("INSERT INTO pilot_chats VALUES(1,123)")
        self.db.execute(
            "INSERT INTO native_settings VALUES(1,?)",
            (
                json.dumps(
                    {
                        "payment_label": "Vinted balance",
                        "postcode": "W2 1UA",
                        "location_label": "Collection point",
                        "delivery_mode": "pickup",
                        "preferred_courier": "Evri",
                        "allowed_couriers": [],
                        "allow_same_shop_fallback": False,
                    }
                ),
            ),
        )
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1

    def search(self, num=1, uid=1, url=None, inc="red", budget=None):
        self.db.execute(
            "INSERT INTO searches VALUES(?,?,?,?,?,?,?,1)",
            (
                num,
                uid,
                "My search " + str(num),
                url or self.url,
                json.dumps([inc]),
                '["replica"]',
                budget,
            ),
        )

    def post(self, path, **body):
        return self.client.post(
            "/api/native/" + path,
            json={"username": "buyer", "url": self.url, **body},
            headers={"Authorization": "Bearer " + self.token},
        )

    def start(self):
        result = self.post("search-watch", enabled=True)
        self.assertEqual(result.status_code, 200)
        watch = result.json["watch_id"]
        self.assertEqual(
            self.post(
                "search-items", watch_id=watch, items=[], baseline=True, rendered_ids=["900"]
            ).status_code,
            200,
        )
        return watch

    def item(self, i="99", **fields):
        return {
            "id": i,
            "listing_url": "https://www.vinted.co.uk/items/" + i,
            "price_pence": 900,
            "title": "Red coat",
            "currency": "GBP",
            "listed_at": self.now - 10,
            "age_text": datetime.fromtimestamp(self.now - 10, timezone.utc).isoformat(),
            **fields,
        }

    def test_exact_configured_source_and_recipient(self):
        self.search(uid=2)
        self.assertEqual(self.post("search-watch", enabled=True).status_code, 403)
        self.search(2)
        self.assertEqual(
            self.post("search-watch", url=self.url + "&page=2", enabled=True).status_code, 403
        )
        self.assertEqual(
            key_for("https://vinted.co.uk/catalog?brand_ids%5B%5D=7&order=newest_first"),
            key_for(self.url),
        )
        w = self.start()
        self.assertEqual(
            self.post("search-items", watch_id=w, items=[self.item("900"), self.item("19")]).json[
                "accepted"
            ],
            1,
        )
        self.assertEqual(
            self.post("search-items", watch_id=w, items=[self.item("19")]).json["accepted"], 0
        )
        self.assertFalse(flow.alerts_for_user(self.db, 2))
        self.assertEqual(
            self.post("search-items", watch_id=w, username="stranger", items=[]).status_code, 401
        )
        self.assertFalse(self.client.get("/api/native/search-status").json["searches"]["2"]["enabled"])

    def test_all_rules_apply_per_sample_and_show_exact_evidence(self):
        for i in range(1, 101):
            self.search(i, inc="unused" if i < 100 else "blue", budget=1400)
        w = self.start()
        raw = [
            self.item("1", title="Blue replica"),
            self.item("2", title="Blue jacket"),
            self.item("3", title="Blue jacket", subtotal_pence=1300),
            self.item(
                "4",
                title="Blue jacket",
                subtotal_pence=999,
                brand="Levis",
                condition="Good",
                size="S",
                photo_url="https://images1.vinted.net/t/01_test/f800/image.webp",
            ),
        ]
        response = self.post(
            "search-items", watch_id=w, items=raw, last_changed=self.now, rendered_count=5
        )
        self.assertEqual(response.json, {"accepted": 1, "skipped": 3, "matched": 100})
        feed = flow.alerts_for_user(self.db, 1)
        self.assertEqual(
            (feed[0]["search_name"], feed[0]["brand"], feed[0]["listed_at"]),
            ("My search 100", "Levis", self.now - 10),
        )
        self.assertEqual(feed[0]["photo_url"], raw[-1]["photo_url"])
        self.assertIsNone(feed[0]["total_pence"])
        self.assertEqual(len(self.client.get("/api/native/search-status").json["searches"]), 100)
        self.db.execute("DELETE FROM searches WHERE id=100")
        self.assertEqual(flow.alerts_for_user(self.db, 1)[0]["search_name"], "My search 100")

    def test_restarted_consent_cannot_revive_purchase_or_accept_previous_tab(self):
        self.search()
        w = self.start()
        self.assertEqual(self.post("search-items", watch_id=w, items=[self.item()]).json["accepted"], 1)
        with patch.object(flow_worker, "telegram", return_value={"message_id": 310}) as tg:
            self.assertTrue(flow_worker.deliver(self.db, self.now + 1))
            button = tg.call_args.args[1]["reply_markup"]["inline_keyboard"][0][0]
            self.assertTrue(button["callback_data"].startswith("nbuy:"))

            def tap(n):
                return flow.telegram_update(
                    self.db,
                    {
                        "update_id": n,
                        "callback_query": {
                            "id": "tap",
                            "from": {"id": 123},
                            "message": {"message_id": 310, "chat": {"type": "private", "id": 123}},
                            "data": button["callback_data"],
                        },
                    },
                )

            self.assertEqual(tap(11)["text"], "Checking your order.")
            self.assertEqual(self.post("search-watch", enabled=False, watch_id=w).status_code, 200)
            new = self.start()
            self.assertNotEqual(new, w)
            self.assertEqual(self.post("search-watch", enabled=False, watch_id=w).status_code, 409)
            self.assertEqual(self.post("search-items", watch_id=w, items=[]).status_code, 409)
            self.assertEqual(tap(12)["text"], "Alert unavailable.")
            with patch("pilot.native_order.request_native") as submit:
                self.assertTrue(flow_worker.buy_job(self.db, self.now + 2))
                submit.assert_not_called()

    def test_edits_and_absent_evidence_withdraw_old_action(self):
        self.search()
        w = self.start()
        self.post("search-items", watch_id=w, items=[self.item()])
        row = self.db.execute("SELECT * FROM pilot_alerts").fetchone()
        self.assertEqual(flow.native_button_binding(self.db, row), "paired")
        self.db.execute("UPDATE searches SET include_keywords='[\"red updated\"]'")
        self.assertIsNone(flow.native_button_binding(self.db, row))
        with patch.object(flow_worker, "telegram") as send:
            self.assertTrue(flow_worker.deliver(self.db, self.now + 5))
            send.assert_not_called()

    def test_verified_device_can_read_quote_only_until_catalogue_watch_stops(self):
        from pilot import native_order

        self.search()
        watch = self.start()
        self.post("search-items", watch_id=watch, items=[self.item()])
        with patch.object(flow_worker, "telegram", return_value={"message_id": 995}):
            self.assertTrue(flow_worker.deliver(self.db, self.now + 1))
        aid = self.db.execute("SELECT id FROM pilot_alerts WHERE listing_id='99'").fetchone()[0]
        native_order.request_native(self.db, 1, aid)

        def next_task():
            return self.client.post(
                "/native/device/next",
                headers={"Authorization": "Bearer " + self.token},
                json={"vinted_username": "buyer"},
            ).json["task"]

        task = next_task()
        self.assertEqual(task["stage"], "quote")
        self.assertEqual(task["listing_id"], "99")
        self.assertEqual(self.post("search-watch", enabled=False, watch_id=watch).status_code, 200)
        self.assertIsNone(next_task())
        self.assertEqual(
            self.db.execute("SELECT stage FROM native_orders WHERE alert_id=?", (aid,)).fetchone()[0],
            "blocked",
        )
