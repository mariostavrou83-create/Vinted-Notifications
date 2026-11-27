import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from pilot import flow, home_groups
from pilot.app import create_app
from pilot.db import get_db


class GroupDelivery(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(Path(temp.name) / "group.db"),
                "PILOT_TELEGRAM_BOT_TOKEN": "9876543:example",
                "PILOT_TELEGRAM_BOT_USERNAME": "PilotUnitBot",
                "PILOT_TELEGRAM_MODE": "polling",
            }
        )
        ctx = self.app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        self.db = get_db()
        self.now = time.time()
        self.client = self.app.test_client()
        for uid in (1, 2):
            self.db.execute(
                "INSERT INTO users VALUES(?,?,?)", (uid, f"user{uid}", "stub")
            )
            self.db.execute(
                "INSERT INTO native_devices VALUES(?,?,?,?,?,?)",
                (
                    uid,
                    f"pair{uid}",
                    f"digest{uid}",
                    f"buyer{uid}",
                    self.now + 900,
                    self.now,
                ),
            )
            self.db.execute(
                "INSERT INTO pilot_home_watch VALUES(?,?,?,1,?)",
                (uid, f"pair{uid}", f"buyer{uid}", self.now),
            )
            self.db.execute("INSERT INTO pilot_chats VALUES(?,?)", (uid, 100 + uid))
            self.db.execute(
                "INSERT INTO settings VALUES(?,?)",
                (uid, '{"quiet_start":"","quiet_end":""}'),
            )
        self.login(1)

    def login(self, uid):
        with self.client.session_transaction() as session:
            session["user_id"] = uid
            session["csrf_token"] = "bound"

    def issue(self):
        r = self.client.post("/api/home-groups/link", headers={"X-CSRF-Token": "bound"})
        self.assertEqual(r.status_code, 200)
        return parse_qs(urlsplit(r.json["url"]).query)["startgroup"][0]

    def submit(self, code, sender=101, group=-10055, **kwargs):
        msg = {
            "message_id": 55,
            "chat": {"id": group, "type": "supergroup", "title": "Our finds"},
            "from": {"id": sender, "is_bot": False},
            "text": f"/start@PilotUnitBot {code}",
            **kwargs,
        }
        return flow.telegram_update(self.db, {"update_id": 1, "message": msg})

    def approve(self, code, sender=101, group=-10055):
        self.submit(code, sender, group)
        with patch(
            "pilot.flow_worker.telegram",
            return_value=[{"status": "creator", "user": {"id": sender}}],
        ) as user:
            self.assertTrue(home_groups.verify(self.db, self.now))
            user.assert_called_once_with(
                "getChatAdministrators", {"chat_id": group}, token="9876543:example"
            )

    def item(self, id="837", **fields):
        return {
            "id": id,
            "title": "Jumpsuit",
            "currency": "GBP",
            "price_pence": 1200,
            "listed_at": self.now - 1,
            "listing_url": f"https://www.vinted.co.uk/items/{id}?private_ref=never",
            "private": "delivery address",
            "description": "whose Home?",
            **fields,
        }

    def test_dashboard_auth_and_telegram_admin_challenge(self):
        self.assertEqual(self.client.post("/api/home-groups/link").status_code, 403)
        code = self.issue()
        self.submit(code, sender=102)
        self.submit(code, sender_chat={"id": -10})
        self.submit(code, forward_origin={"type": "user"})
        self.assertFalse(home_groups.verify(self.db, self.now))
        self.submit(code)
        self.assertEqual(self.client.get("/api/home-groups").json["groups"], [])
        with patch(
            "pilot.flow_worker.telegram",
            return_value=[{"status": "administrator", "user": {"id": 102}}],
        ):
            self.assertTrue(home_groups.verify(self.db, self.now))
        self.assertEqual(self.client.get("/api/home-groups").json["groups"], [])
        self.approve(self.issue())
        self.assertEqual(
            self.client.get("/api/home-groups").json["groups"],
            [{"id": 1, "title": "Our finds"}],
        )
        self.login(2)
        self.assertEqual(self.client.get("/api/home-groups").json["groups"], [])
        self.assertEqual(
            self.client.delete(
                "/api/home-groups/1", headers={"X-CSRF-Token": "bound"}
            ).status_code,
            404,
        )

    def test_fresh_public_card_only_and_group_callback_never_buys(self):
        self.approve(self.issue())
        self.assertFalse(
            home_groups.observe(self.db, 2, "pair2", self.item(), self.now)
        )
        self.assertFalse(
            home_groups.observe(
                self.db, 1, "pair1", self.item(listed_at=self.now - 180), self.now
            )
        )
        self.assertTrue(home_groups.observe(self.db, 1, "pair1", self.item(), self.now))
        self.assertFalse(
            home_groups.observe(self.db, 1, "pair1", self.item(), self.now)
        )
        with patch(
            "pilot.flow_worker.telegram",
            side_effect=[
                [{"status": "administrator", "user": {"id": 101}}],
                {"message_id": 7},
            ],
        ) as tg:
            self.assertTrue(home_groups.deliver(self.db, self.now))
            method, payload = tg.call_args.args
            self.assertEqual(method, "sendMessage")
            self.assertEqual(payload["chat_id"], -10055)
            self.assertEqual(
                payload["text"],
                "Jumpsuit\n£12.00 asking price\nhttps://www.vinted.co.uk/items/837",
            )
            buttons = payload["reply_markup"]["inline_keyboard"][0]
            self.assertEqual(
                [b["url"] for b in buttons],
                ["https://www.vinted.co.uk/items/837", "https://t.me/PilotUnitBot"],
            )
            self.assertFalse(any("callback_data" in b for b in buttons))
        cb = {
            "id": "2",
            "from": {"id": 101},
            "message": {"message_id": 7, "chat": {"id": -10055, "type": "group"}},
            "data": "nbuy:24",
        }
        flow.telegram_update(self.db, {"callback_query": cb, "update_id": 7})
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM pilot_buy_jobs").fetchone()[0], 0
        )
        self.assertFalse(
            home_groups.observe(self.db, 1, "pair1", self.item(), self.now)
        )

    def test_relink_pause_unlink_and_admin_demotion(self):
        self.approve(self.issue())
        home_groups.observe(self.db, 1, "pair1", self.item(), self.now)
        self.db.execute("UPDATE pilot_home_watch SET enabled=0 WHERE user_id=1")
        self.assertFalse(home_groups.deliver(self.db, self.now))
        self.db.execute("UPDATE pilot_home_watch SET enabled=1 WHERE user_id=1")
        home_groups.observe(self.db, 1, "pair1", self.item(), self.now)
        with patch("pilot.flow_worker.telegram", return_value=[]) as tg:
            self.assertTrue(home_groups.deliver(self.db, self.now))
            self.assertEqual(tg.call_count, 1)
        self.assertEqual(self.client.get("/api/home-groups").json["groups"], [])
        self.approve(self.issue())
        home_groups.observe(self.db, 1, "pair1", self.item(), self.now)
        self.db.execute("UPDATE pilot_chats SET chat_id=103 WHERE user_id=1")
        self.assertEqual(self.client.get("/api/home-groups").json["groups"], [])
        with patch("pilot.flow_worker.telegram") as tg:
            self.assertFalse(home_groups.deliver(self.db, self.now + 4))
            tg.assert_not_called()

    def test_dedupe_across_homes_and_ambiguous_ack(self):
        self.approve(self.issue())
        self.login(2)
        self.approve(self.issue(), sender=102)
        for uid in (1, 2):
            home_groups.observe(self.db, uid, f"pair{uid}", self.item(), self.now)
        admins = [{"status": "administrator", "user": {"id": u}} for u in (101, 102)]
        with patch(
            "pilot.flow_worker.telegram",
            side_effect=[admins, ValueError("unknown"), admins],
        ) as tg:
            self.assertTrue(home_groups.deliver(self.db, self.now))
            self.assertTrue(home_groups.deliver(self.db, self.now + 4))
            self.assertEqual(
                sum(c.args[0] == "sendMessage" for c in tg.call_args_list), 1
            )

    def test_group_uses_selected_companion_only_and_revocation_removes_pending(self):
        headers = {"X-CSRF-Token": "bound"}
        source = self.client.post(
            "/api/home-routes/sources", headers=headers, json={"label": "Extra Home"}
        )
        self.assertEqual(source.status_code, 200)
        source_id, pair_code = source.json["id"], source.json["code"]
        linked = self.client.post(
            "/api/home/companion/pair", json={"code": pair_code, "account_id": "3388"}
        )
        self.assertEqual(linked.status_code, 200, linked.get_json())
        secret = linked.json["token"]
        self.login(2)
        self.assertEqual(
            self.client.post(
                "/api/home-groups/link", headers=headers, json={"source_id": source_id}
            ).status_code,
            409,
        )
        self.login(1)
        target = self.client.post(
            "/api/home-groups/link", headers=headers, json={"source_id": source_id}
        )
        self.assertEqual(target.status_code, 200)
        self.approve(parse_qs(urlsplit(target.json["url"]).query)["startgroup"][0])
        self.assertEqual(self.client.get("/api/home-groups").json["groups"], [])
        groups = self.client.get(f"/api/home-groups?source_id={source_id}").json[
            "groups"
        ]
        self.assertEqual([g["title"] for g in groups], ["Our finds"])
        self.assertFalse(
            home_groups.observe(self.db, 1, "pair1", self.item(), self.now)
        )
        body = {
            "account_id": "3388",
            "items": [
                {
                    "id": "874",
                    "url": "https://www.vinted.co.uk/items/874",
                    "title": "Wool jumper",
                    "price_gbp": 9,
                    "listed_at": int(self.now) - 8,
                }
            ],
        }
        observed = self.client.post(
            "/api/home/companion/events",
            json=body,
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(observed.status_code, 200)
        self.assertEqual(observed.json["accepted"], 1)
        self.assertEqual(
            self.client.post(
                f"/api/home-routes/sources/{source_id}/remove", headers=headers
            ).status_code,
            200,
        )
        self.assertFalse(home_groups.deliver(self.db, self.now))
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM home_group_links").fetchone()[0], 0
        )

    def test_fair_group_schedule_and_retry_on_transient_admin_read(self):
        self.approve(self.issue(), group=-111)
        self.approve(self.issue(), group=-222)
        for item_id in ("5", "6"):
            home_groups.observe(self.db, 1, "pair1", self.item(item_id), self.now)
        admins = [{"status": "creator", "user": {"id": 101}}]
        with patch(
            "pilot.flow_worker.telegram",
            side_effect=[
                ValueError("transient"),
                admins,
                {"message_id": 1},
                admins,
                {"message_id": 2},
            ],
        ) as tg:
            self.assertTrue(home_groups.deliver(self.db, self.now))
            self.assertTrue(home_groups.deliver(self.db, self.now))
            self.assertFalse(home_groups.deliver(self.db, self.now))
            self.assertTrue(home_groups.deliver(self.db, self.now + 4))
            sends = [
                c.args[1]["chat_id"]
                for c in tg.call_args_list
                if c.args[0] == "sendMessage"
            ]
            self.assertEqual(sends, [-222, -222])
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM home_group_items").fetchone()[0], 2
        )
