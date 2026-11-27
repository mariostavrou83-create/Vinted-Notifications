import json
from unittest.mock import patch

from pilot import flow, flow_worker, native_order
from pilot.tests import test_home_watch


class ShareHomeTests(test_home_watch.HomeWatchTest):
    """All Telegram receipts and identities in this test are local fixtures."""

    def as_user(self, uid):
        with self.client.session_transaction() as sess:
            sess["user_id"] = uid
            sess["csrf_token"] = "test-csrf"

    def change(self, url, uid, **body):
        self.as_user(uid)
        return self.client.post(
            "/api/home-routes/" + url, json=body, headers={"X-CSRF-Token": "test-csrf"}
        )

    def connect(self):
        invite = self.change("invite", 2).json["code"]
        self.assertEqual(self.change("accept", 1, code=invite).status_code, 200)
        self.assertEqual(self.change("accept", 1, code=invite).status_code, 400)
        self.as_user(2)
        return self.client.get("/api/home-routes").json["incoming"][0]["id"]

    def test_many_homes_buyer_binding_and_revocation(self):
        self.watch()
        self.watch(2)
        rid = self.connect()
        self.conn.execute(
            "INSERT INTO native_settings VALUES(2,?)",
            (json.dumps({"payment_label": "Vinted balance"}),),
        )
        self.assertEqual(self.post("home", items=[self.item()]).json["accepted"], 1)
        # Same listing on the recipient's own Home must not generate a duplicate.
        self.assertEqual(self.post("home", 2, items=[self.item()]).json["accepted"], 0)
        with patch.object(
            flow_worker, "telegram", return_value={"message_id": 51}
        ) as tg:
            self.assertTrue(flow_worker.deliver(self.conn, self.now + 1))
            self.assertTrue(flow_worker.deliver(self.conn, self.now + 1))
            self.assertFalse(flow_worker.deliver(self.conn, self.now + 1))
            self.assertEqual(
                [c.args[1]["chat_id"] for c in tg.call_args_list], [101, 102]
            )
        row = self.conn.execute("SELECT * FROM pilot_alerts WHERE user_id=2").fetchone()
        self.assertEqual(row["native_binding"], "pair2")
        dev = self.conn.execute(
            "SELECT * FROM native_devices WHERE user_id=2"
        ).fetchone()
        self.assertTrue(native_order._source_current(self.conn, row, dev))

        def tap(chat):
            return flow.telegram_update(
                self.conn,
                {
                    "update_id": 8,
                    "callback_query": {
                        "from": {"id": chat},
                        "data": "nbuy:" + str(row["id"]),
                        "message": {
                            "chat": {"type": "private", "id": chat},
                            "message_id": 51,
                        },
                    },
                },
            )

        self.assertEqual(tap(101)["text"], "Alert unavailable.")
        self.assertEqual(tap(102)["text"], "Checking your order.")
        self.assertEqual(self.change(str(rid) + "/remove", 2).status_code, 200)
        self.assertEqual(tap(102)["text"], "Alert unavailable.")
        self.assertFalse(native_order._source_current(self.conn, row, dev))

    def test_destination_filters_and_chat_switch(self):
        self.watch()
        self.connect()
        self.conn.execute(
            "UPDATE settings SET data=? WHERE user_id=2",
            (json.dumps({"quiet_start": "", "quiet_end": "", "sizes": ["XXL"]}),),
        )
        self.post("home", items=[self.item(size="S"), self.item("351", size="XXL")])
        ids = [
            r[0]
            for r in self.conn.execute(
                "SELECT listing_id FROM pilot_alerts WHERE user_id=2"
            )
        ]
        self.assertEqual(ids, ["351"])
        self.conn.execute("UPDATE pilot_chats SET chat_id=555 WHERE user_id=2")
        with patch.object(
            flow_worker, "telegram", return_value={"message_id": 52}
        ) as tg:
            for _ in range(4):
                flow_worker.deliver(self.conn, self.now + 1)
            self.assertNotIn(555, [c.args[1]["chat_id"] for c in tg.call_args_list])

    def test_pause_then_restart_does_not_reactivate_old_button(self):
        self.watch()
        self.connect()
        self.post("home", items=[self.item()])
        self.watch(enabled=False)
        self.watch()
        with patch.object(
            flow_worker, "telegram", return_value={"message_id": 53}
        ) as tg:
            for _ in range(3):
                flow_worker.deliver(self.conn, self.now + 1)
            tg.assert_not_called()
