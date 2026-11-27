import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from pilot import checkout_profit, flow, flow_worker, native_order
from pilot.app import create_app
from pilot.db import get_db


class PairedChromeTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(Path(folder.name) / "pilot.sqlite"),
                "PILOT_TELEGRAM_WEBHOOK_SECRET": "webhook",
            }
        )
        ctx = self.app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        self.db = get_db()
        self.db.execute("INSERT INTO users VALUES(1,'pilot-one','unused'),(2,'pilot-two','unused')")
        self.db.execute("INSERT INTO pilot_chats VALUES(1,101),(2,102)")
        self.owner = self.login(1)
        self.device = self.app.test_client()
        self.preferences = {
            "delivery_mode": "pickup",
            "location_label": "The Newsagents",
            "postcode": "SW1A 1AA",
            "preferred_courier": "Evri",
            "allowed_couriers": ["InPost"],
            "allow_same_shop_fallback": True,
            "payment_label": "Visa •••• 1234",
        }
        self.headers = {"X-CSRF-Token": "test-csrf"}
        self.name = "buyer.one"
        self.token = self.pair()
        self.save_settings()
        self.quote = {
            "listing_id": "123",
            "currency": "GBP",
            "item_pence": 1500,
            "shipping_pence": 250,
            "buyer_fee_pence": 100,
            "total_pence": 1850,
            "delivery_mode": "pickup",
            "pickup_id": "gb:sw1a1aa:the newsagents",
            "pickup_label": "The Newsagents · SW1A 1AA",
            "courier": "Evri",
            "payment_label": "Visa ending in 1234",
        }
        self.aid = self.alert()

    def login(self, uid):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess.update(user_id=uid, csrf_token="test-csrf")
        return client

    def pair(self, client=None, name=None, pilot="pilot-one"):
        client = client or self.owner
        name = name or self.name
        code = client.post(
            "/api/native/pair-code", json={"vinted_username": name}, headers=self.headers
        ).json["code"]
        self.last_code = code
        result = self.device.post(
            "/native/device/pair",
            json={"code": code, "pilot_username": pilot, "vinted_username": name},
        )
        self.assertEqual(result.status_code, 200)
        return result.json["token"]

    def save_settings(self, **update):
        return self.owner.post(
            "/api/native/settings", headers=self.headers, json=self.preferences | update
        )

    def alert(self, listing_id="123", origin="public_search", subtotal=1600, search_budget=None):
        listing = {
            "id": listing_id,
            "title": "Vintage coat",
            "selling_cost_pence": 50,
            "listing_url": f"https://www.vinted.co.uk/items/{listing_id}",
            "price_pence": 1500,
            "subtotal_pence": subtotal,
            "search_budget_pence": search_budget,
        }
        return self.db.execute(
            """INSERT INTO pilot_alerts
            (user_id,account_ref,listing_id,listing,source,origin,created_at,native_binding,attempt_key,updated,message_id,sent_chat,delivery)
            VALUES(1,'public',?,?,'search',?,?,?,?,0,333,101,'sent')""",
            (
                listing_id,
                json.dumps(listing),
                origin,
                time.time(),
                native_order.capability(self.db, 1),
                listing_id,
            ),
        ).lastrowid

    def device_post(self, path, token=None, **body):
        return self.device.post(
            "/native/device/" + path,
            headers={"Authorization": "Bearer " + (token or self.token)},
            json={"vinted_username": self.name} | body,
        )

    def next_task(self):
        return self.device_post("next").json["task"]

    def stage(self, aid=None):
        return self.db.execute(
            "SELECT stage FROM native_orders WHERE alert_id=?", (aid or self.aid,)
        ).fetchone()[0]

    def tap(self, aid=None, confirmation=None, uid=1):
        return native_order.request_native(self.db, uid, aid or self.aid, confirmation)

    def read_task(self):
        self.tap()
        task = self.next_task()
        self.assertEqual(task["stage"], "quote")
        self.assertEqual(task["id"], self.aid)
        return task

    def result(self, task, kind, **values):
        return self.device_post(
            "result",
            task_token=task["task_token"],
            listing_id=task["listing_id"],
            kind=kind,
            **values,
        )

    def accept_quote(self, **changes):
        task = self.read_task()
        self.assertEqual(self.result(task, "quote", quote=self.quote | changes).status_code, 200)

    def webhook(self, action, *, chat=101, sender=101, message=333, kind="private", update=1):
        return self.device.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "webhook"},
            json={
                "update_id": update,
                "callback_query": {
                    "id": "cb",
                    "data": action,
                    "from": {"id": sender},
                    "message": {"message_id": message, "chat": {"type": kind, "id": chat}},
                },
            },
        )

    def test_paired_home_can_quote_and_stopping_watch_revokes_pending(self):
        binding = native_order.capability(self.db, 1)

        def watch(enabled):
            return self.device.post(
                "/api/native/home-watch",
                headers={"Authorization": "Bearer " + self.token},
                json={"username": self.name, "enabled": enabled},
            )

        self.aid = self.alert("563", origin="browser_home", subtotal=None)
        self.quote["listing_id"] = "563"
        self.tap()
        self.assertIsNone(self.next_task())
        self.db.execute(
            "UPDATE pilot_alerts SET account_ref=? WHERE id=?", ("home:" + binding, self.aid)
        )
        self.tap()
        self.assertIsNone(self.next_task())
        self.assertEqual(watch(True).status_code, 200)
        self.accept_quote()
        self.assertEqual(self.stage(), "confirm")
        self.assertIsNone(self.next_task())
        confirmation = self.db.execute(
            "SELECT confirmation FROM native_orders WHERE alert_id=?", (self.aid,)
        ).fetchone()[0]
        self.tap(confirmation=confirmation)
        self.assertEqual(self.stage(), "await_commit")
        self.assertEqual(watch(False).status_code, 200)
        self.assertEqual(watch(True).status_code, 200)
        self.tap()
        self.assertIsNone(self.next_task())
        self.assertEqual(self.stage(), "blocked")

    def test_saved_budget_and_wrong_home_address_block(self):
        self.aid = self.alert("666", search_budget=1750)
        self.quote["listing_id"] = "666"
        self.accept_quote()
        self.assertEqual(self.stage(), "blocked")
        self.save_settings(delivery_mode="home", location_label="Home")
        for listing_id, address, stage in [
            ("667", "gb:sw1a1aa:work", "blocked"),
            ("668", "gb:sw1a1aa:home", "await_commit"),
        ]:
            self.aid = self.alert(listing_id)
            self.quote["listing_id"] = listing_id
            self.accept_quote(delivery_mode="home", home_address_ref=address)
            self.assertEqual(self.stage(), stage)

    def test_scoped_pairing_and_disconnect(self):
        self.assertEqual(self.device.get("/api/native/status").status_code, 401)
        self.assertEqual(
            self.owner.post("/api/native/pair-code", json={"vinted_username": "x"}).status_code, 403
        )
        self.assertEqual(
            self.device.post("/native/device/preview", json={"code": self.last_code}).status_code,
            403,
        )
        self.assertEqual(self.device.post("/native/device/next", json={}).status_code, 401)
        second = self.login(2)
        code = second.post(
            "/api/native/pair-code", headers=self.headers, json={"vinted_username": self.name}
        ).json["code"]
        identity = self.device.post("/native/device/preview", json={"code": code}).json
        self.assertEqual(identity["pilot_username"], "pilot-two")
        for pilot, user, status in [
            ("pilot-one", self.name, 403),
            ("pilot-two", "other", 403),
            ("pilot-two", self.name, 409),
        ]:
            self.assertEqual(
                self.device.post(
                    "/native/device/pair",
                    json={"code": code, "pilot_username": pilot, "vinted_username": user},
                ).status_code,
                status,
            )
        self.assertNotIn(
            self.token, repr(list(self.db.execute("SELECT * FROM native_devices").fetchone()))
        )
        self.assertFalse(second.get("/api/native/status").json["paired"])
        self.assertTrue(self.owner.get("/api/native/status").json["online"])
        self.assertEqual(
            self.device_post("next", vinted_username="a-different-buyer").status_code, 403
        )
        self.assertEqual(self.device_post("next").status_code, 401)
        self.assertFalse(self.owner.get("/api/native/status").json["paired"])

    @patch("pilot.flow_worker.telegram")
    def test_private_telegram_through_receipt(self, telegram):
        tap = f"nbuy:{self.aid}"
        for kwargs in (
            {"chat": 102, "sender": 102},
            {"sender": 102},
            {"kind": "group"},
            {"message": 554},
        ):
            self.webhook(tap, **kwargs)
            self.assertFalse(flow_worker.buy_job(self.db, time.time()))
        self.webhook(tap)
        self.webhook(tap)
        self.assertTrue(flow_worker.buy_job(self.db, time.time()))
        self.assertFalse(flow_worker.buy_job(self.db, time.time()))
        read = self.next_task()
        self.assertIsNone(self.next_task())
        self.assertEqual(self.result(read, "purchased", order_id="XX").status_code, 409)
        self.assertEqual(self.result(read, "quote", quote=self.quote).status_code, 200)
        self.assertEqual(self.result(read, "quote", quote=self.quote).status_code, 409)
        commit = self.next_task()
        self.assertEqual((commit["id"], commit["stage"]), (read["id"], "commit"))
        self.assertEqual(commit["quote"]["payment_label"], "Visa ending in 1234")
        self.assertIsNone(self.next_task())
        self.result(commit, "purchased", order_id="#receipt123")
        self.assertEqual(self.stage(), "purchased")
        self.assertEqual(self.result(commit, "unknown").status_code, 409)
        self.tap()
        self.assertIsNone(self.next_task())
        self.assertTrue(native_order.notify(self.db, time.time()))
        final = telegram.call_args.args[1]
        self.assertEqual(final["chat_id"], 101)
        self.assertIn("£18.50", final["text"])
        self.assertFalse(native_order.notify(self.db, time.time()))

    def test_confirm_changed_shop_then_consume_exact_quote(self):
        self.accept_quote(
            pickup_id="gb:w21ua:oak shop", pickup_label="Oak Shop · W2 1UA", courier="InPost"
        )
        self.assertEqual(self.stage(), "confirm")
        self.assertIsNone(self.next_task())
        notice = self.db.execute("SELECT text,keyboard FROM native_notifications").fetchone()
        self.assertIn("Oak Shop", notice["text"])
        self.assertIn("£18.50", notice["text"])
        cf = json.loads(notice["keyboard"])["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
        self.tap(confirmation=cf, uid=2)
        self.tap(confirmation="bad")
        self.assertIsNone(self.next_task())
        self.tap(confirmation=cf)
        commit = self.next_task()
        self.assertTrue(commit["pickup_change_confirmed"])
        self.assertEqual(
            commit["alert_limits"],
            {"item_pence": 1500, "subtotal_pence": 1600, "total_pence": None},
        )
        self.assertEqual(commit["quote"]["pickup_id"], "gb:w21ua:oak shop")
        self.result(commit, "purchased")
        self.assertEqual(self.stage(), "unknown")
        self.tap(confirmation=cf)
        self.assertIsNone(self.next_task())

    def test_expired_issued_is_unknown_and_never_reissued(self):
        self.accept_quote()
        self.next_task()
        self.db.execute("UPDATE native_orders SET expires=0")
        self.assertIsNone(self.next_task())
        self.assertEqual(self.stage(), "unknown")
        self.tap()
        self.assertIsNone(self.next_task())

    def test_rebind_keeps_old_buttons_and_intents_inactive(self):
        self.accept_quote()
        self.token = self.pair()
        self.assertEqual(self.stage(), "blocked")
        self.tap()
        self.assertIsNone(self.next_task())
        self.aid = self.alert("234", origin="browser")
        self.tap()
        self.assertIsNone(self.next_task())

    def test_disconnect_after_dispatch_is_unknown(self):
        self.accept_quote()
        task = self.next_task()
        self.owner.post("/api/native/disconnect", headers=self.headers, json={})
        self.assertEqual(self.result(task, "purchased", order_id="X123").status_code, 401)
        self.assertEqual(self.stage(), "unknown")

    def test_quote_checks_provider_figures_and_preferences(self):
        cases = (
            {"buyer_fee_pence": 110, "total_pence": 1860},
            {"item_pence": 1550, "total_pence": 1900},
            {"shipping_pence": 660, "total_pence": 2260},
            {"total_pence": 10},
            {"buyer_fee_pence": True},
            {"courier": "Other new courier"},
            {"payment_label": "Mastercard •••• 1234"},
            {"listing_id": "567"},
            {"currency": "USD"},
            {"pickup_id": "gb:bad:the newsagents"},
            {"pickup_label": "Other shop"},
            {"payment_label": "4111111111111111"},
        )
        for i, case in enumerate(cases):
            with self.subTest(case=case):
                listing_id = str(i + 1000)
                self.aid = self.alert(listing_id)
                self.quote["listing_id"] = listing_id
                self.accept_quote(**case)
                self.assertEqual(self.stage(), "blocked")
                self.assertIsNone(self.next_task())

    def test_updated_settings_and_stale_quote(self):
        self.accept_quote()
        self.save_settings(location_label="Different shop")
        self.assertEqual(self.device_post("next").status_code, 409)
        self.assertEqual(self.stage(), "blocked")
        self.assertEqual(self.save_settings(payment_label="4111111111111111").status_code, 400)
        self.aid = self.alert("888")
        self.quote["listing_id"] = "888"
        self.accept_quote()
        self.db.execute("UPDATE native_orders SET expires=0 WHERE alert_id=?", (self.aid,))
        self.assertIsNone(self.next_task())
        self.assertEqual(self.stage(), "expired")

    def test_existing_valuation_uses_exact_quote_then_receipted_total(self):
        valuation = {
            "status": "estimated",
            "resale_pence": 3400,
            "profit_pence": 9876,
            "meets_target": True,
            "evidence": [{"ask_pence": 3499}],
        }
        self.db.execute(
            "UPDATE pilot_alerts SET valuation=? WHERE id=?", (json.dumps(valuation), self.aid)
        )
        self.assertIsNone(flow.alerts_for_user(self.db, 1)[0]["valuation"]["profit_pence"])
        self.accept_quote()
        card = flow.alerts_for_user(self.db, 1)[0]
        self.assertEqual(card["checkout_cost"]["basis"], "quote")
        self.assertEqual(card["checkout_cost"]["total_pence"], 1850)
        self.assertIn("valid until", card["checkout_cost"]["note"])
        self.assertTrue(card["valuation"]["meets_target"])
        self.assertEqual(card["valuation"]["profit_pence"], 1500)
        self.assertEqual(card["valuation"]["evidence"], valuation["evidence"])
        expired, cost = checkout_profit.for_alert(self.db, 1, self.aid, time.time() + 3600)
        self.assertIsNone(expired["profit_pence"])
        self.assertEqual(cost["basis"], "expired")
        self.assertIsNone(cost["total_pence"])
        self.assertEqual(checkout_profit.for_alert(self.db, 2, self.aid, time.time()), (None, None))
        self.result(self.next_task(), "purchased", order_id="receipt-123")
        with patch("pilot.flow_worker.telegram") as outbound:
            self.assertTrue(native_order.notify(self.db, time.time()))
            self.assertIn("Est. profit £15.00 · £15 target met", outbound.call_args.args[1]["text"])
            self.assertIn("Purchase confirmed on", outbound.call_args.args[1]["text"])
        self.owner.post("/api/native/disconnect", headers=self.headers, json={})
        later, cost = checkout_profit.for_alert(self.db, 1, self.aid, time.time() + 3600)
        self.assertTrue(later["meets_target"])
        self.assertEqual(cost["basis"], "purchase")

    def test_quote_before_valuation_updates_private_alert_without_another_lookup(self):
        self.accept_quote()
        self.assertIsNone(checkout_profit.for_alert(self.db, 1, self.aid, time.time())[0])
        valuation = {
            "status": "estimated",
            "resale_pence": 3399,
            "profit_pence": None,
            "evidence": [],
        }
        with (
            patch("pilot.market.comparisons_for", return_value=[]) as comparisons,
            patch("pilot.pricing.estimate_resale", return_value=valuation) as estimate,
            patch("pilot.flow_worker.telegram") as tg,
        ):
            self.assertTrue(flow_worker.review(self.db, time.time()))
            self.assertFalse(flow_worker.review(self.db, time.time()))
            comparisons.assert_called_once()
            estimate.assert_called_once()
            message = tg.call_args.args[1]
            self.assertEqual(message["chat_id"], 101)
            self.assertIn("Est. profit £14.99 · £15 target missed", message["text"])
            self.assertIn("Checkout quote valid until", message["text"])
            page = self.owner.get("/alerts/fragment")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Quoted checkout", page.text)
            self.assertIn("£14.99", page.text)
        self.db.execute("UPDATE pilot_alerts SET native_binding=NULL WHERE id=?", (self.aid,))
        projected, cost = checkout_profit.for_alert(self.db, 1, self.aid, time.time())
        self.assertIsNone(cost)
        self.assertIsNone(projected["profit_pence"])

    def test_receipted_home_estimate_survives_unpairing_without_exposing_it_to_another_account(
        self,
    ):
        binding = native_order.capability(self.db, 1)
        self.device.post(
            "/api/native/home-watch",
            headers={"Authorization": "Bearer " + self.token},
            json={"username": self.name, "enabled": True},
        )
        self.db.execute(
            "UPDATE pilot_alerts SET origin='browser_home',account_ref=? WHERE id=?",
            ("home:" + binding, self.aid),
        )
        self.accept_quote()
        self.result(self.next_task(), "purchased", order_id="receipt-home")
        self.owner.post("/api/native/disconnect", headers=self.headers, json={})
        self.db.execute(
            "UPDATE pilot_alerts SET valuation=? WHERE id=?",
            (json.dumps({"status": "estimated", "resale_pence": 3400}), self.aid),
        )
        valuation, checkout = checkout_profit.for_alert(self.db, 1, self.aid, time.time())
        self.assertTrue(valuation["meets_target"])
        self.assertEqual(checkout["basis"], "purchase")
        self.assertFalse(flow.alerts_for_user(self.db, 2))
        # A listing with another Home pairing may not reuse the previous receipt.
        self.db.execute(
            "UPDATE pilot_alerts SET account_ref='home:unrelated' WHERE id=?", (self.aid,)
        )
        self.assertIsNone(checkout_profit.for_alert(self.db, 1, self.aid, time.time())[1])

    def test_unconfirmed_result_and_manual_subtotal_cannot_supply_profit(self):
        self.accept_quote()
        self.result(self.next_task(), "unknown")
        valuation = {
            "status": "estimated",
            "resale_pence": 4000,
            "profit_pence": 2000,
            "meets_target": True,
        }
        self.db.execute("UPDATE pilot_alerts SET valuation=?", (json.dumps(valuation),))
        val, cost = checkout_profit.for_alert(self.db, 1, self.aid, time.time())
        self.assertIsNone(cost)
        self.assertIsNone(val["profit_pence"])
        self.db.execute(
            "UPDATE pilot_alerts SET origin='browser',order_status=NULL WHERE id=?", (self.aid,)
        )
        val, cost = checkout_profit.for_alert(self.db, 1, self.aid, time.time())
        self.assertIsNone(cost)
        self.assertIsNone(val["meets_target"])

    def test_unbounded_unknown_fees_require_buyer_confirmation(self):
        self.aid = self.alert("2222", subtotal=None)
        self.quote["listing_id"] = "2222"
        self.accept_quote()
        self.assertEqual(self.stage(), "confirm")
        self.assertIsNone(self.next_task())

    def test_immediate_transaction_grants_commit_once_concurrently(self):
        self.accept_quote()

        def claim(_):
            client = self.app.test_client()
            return client.post(
                "/native/device/next",
                headers={"Authorization": "Bearer " + self.token},
                json={"vinted_username": self.name},
            ).json["task"]

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(claim, range(3)))
        self.assertEqual([v["stage"] for v in results if v], ["commit"])


if __name__ == "__main__":
    unittest.main()
