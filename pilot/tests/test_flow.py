import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pilot import flow, flow_worker
from pilot.app import create_app
from pilot.db import get_db, settings_for


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(Path(self.tmp.name) / "test.db"),
                "PILOT_TELEGRAM_WEBHOOK_SECRET": "test-only",
            }
        )
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        self.conn = get_db()
        self.conn.execute(
            "INSERT INTO users(id,username,password_hash) VALUES (1,'one','unused'),(2,'two','unused')"
        )
        self.conn.execute(
            "INSERT INTO pilot_buyer_link(user_id,account_ref,username) VALUES (1,'ref1','first'),(2,'ref2','second')"
        )
        self.preferences = {
            "quiet_start": "",
            "quiet_end": "",
            "pickup_id": "shop-one",
            "pickup_label": "Shop one",
            "preferred_courier": "one",
            "allowed_couriers": ["two"],
            "allow_same_shop_fallback": True,
            "max_total": "20",
            "max_shipping": "4.99",
        }
        self.conn.execute("INSERT INTO settings VALUES(1,?)", (json.dumps(self.preferences),))
        self.conn.execute("INSERT INTO settings VALUES(2,?)", (json.dumps(self.preferences),))
        self.item = {
            "id": "654",
            "seller_id": "seller3",
            "title": "Vintage coat",
            "listing_url": "https://www.vinted.co.uk/items/654-coat",
            "price_pence": 500,
            "total_pence": 800,
            "currency": "GBP",
            "listed_at": time.time() - 10,
            "category": "Women",
            "size": "8",
        }
        self.quote = {
            "quote_id": "q1",
            "listing_id": "654",
            "account_ref": "ref1",
            "currency": "GBP",
            "expires_at": time.time() + 600,
            "item_pence": 500,
            "shipping_pence": 260,
            "total_pence": 860,
            "payment_label": "Saved card",
            "delivery_mode": "pickup",
            "pickup_id": "shop-one",
            "pickup_label": "Shop one",
            "courier": "two",
        }
        self.record()
        self.aid = self.conn.execute("SELECT id FROM pilot_alerts").fetchone()[0]
        self.conn.execute("INSERT INTO pilot_chats VALUES(1,101),(2,102)")
        self.conn.execute("UPDATE pilot_alerts SET sent_chat=101,message_id=333,delivery='sent'")
        self.submits = 0

    def record(self, uid=1, source="home"):
        flow.record_listing(
            self.conn, uid, f"ref{uid}", self.item, source, settings_for(self.conn, uid), time.time()
        )

    def fake(self, method, path, payload):
        if path.endswith("/quote"):
            return self.quote.copy()
        self.submits += 1
        return {
            "listing_id": "654",
            "account_ref": "ref1",
            "idempotency_key": payload["idempotency_key"],
            "status": "purchased",
        }

    def buy(self, code=None):
        with patch("pilot.flow.partner_request", side_effect=self.fake):
            return flow.request_buy(self.conn, 1, self.aid, code)

    def test_private_listing_freshness_dedupe_and_filters(self):
        self.record()
        self.record(uid=2)
        for changes in (
            {"listed_at": time.time() - 180},
            {"listed_at": None},
            {"listed_at": time.time() + 5},
            {"listing_url": "https://evil.invalid/items/655-foo"},
        ):
            item = {
                **self.item,
                "id": "655",
                "listing_url": "https://www.vinted.co.uk/items/655-coat",
                **changes,
            }
            flow.record_listing(self.conn, 1, "ref1", item, "home", self.preferences, time.time())
        item = {**self.item, "id": "655", "listing_url": "https://www.vinted.co.uk/items/655-coat"}
        flow.record_listing(
            self.conn, 1, "ref1", item, "home", {**self.preferences, "categories": ["Men"]}, time.time()
        )
        self.assertEqual(len(flow.alerts_for_user(self.conn, 1)), 1)
        self.assertEqual(len(flow.alerts_for_user(self.conn, 2)), 1)

    def test_matching_quote_submitted_once(self):
        self.assertIn("Purchased", self.buy()[0])
        self.assertIn("recorded attempt", self.buy()[0])
        self.assertEqual(self.submits, 1)

    def test_pickup_change_requires_bound_confirmation(self):
        self.quote.update(pickup_id="different", pickup_label="Another shop")
        msg, keyboard = self.buy()
        code = keyboard["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
        self.assertIn("Another shop", msg)
        self.assertEqual(self.submits, 0)
        self.assertIn("no longer valid", self.buy("bad")[0])
        self.assertIn("Purchased", self.buy(code)[0])
        self.assertEqual(self.submits, 1)

    def test_modified_quote_after_confirmation_blocks(self):
        self.quote.update(pickup_id="different", pickup_label="Another shop")
        _, keyboard = self.buy()
        code = keyboard["inline_keyboard"][0][0]["callback_data"].split(":")[-1]
        self.quote.update(total_pence=861)
        self.assertIn("changed again", self.buy(code)[0])
        self.assertEqual(self.submits, 0)

    def test_limits_wrong_account_and_unapproved_courier(self):
        row = self.conn.execute("SELECT * FROM pilot_alerts").fetchone()
        for changes in (
            {"account_ref": "ref2"},
            {"listing_id": "99"},
            {"item_pence": 501},
            {"shipping_pence": 500},
            {"total_pence": 2001},
            {"courier": "other"},
            {"currency": "EUR"},
            {"expires_at": time.time() - 1},
        ):
            decision, _ = flow.check_quote({**self.quote, **changes}, row, self.preferences, time.time())
            self.assertEqual(decision, "blocked", changes)
        self.assertIn("Connect the account", flow.request_buy(self.conn, 2, self.aid)[0])

    def test_unknown_order_cannot_retry(self):
        def timeout(method, path, payload):
            if path.endswith("quote"):
                return self.quote
            raise flow.PartnerError("No response")

        with patch("pilot.flow.partner_request", side_effect=timeout):
            self.assertIn("pending", flow.request_buy(self.conn, 1, self.aid)[0])
            self.assertIn("recorded attempt", flow.request_buy(self.conn, 1, self.aid)[0])

    def test_private_authenticated_webhook(self):
        client = self.app.test_client()
        cb = {
            "update_id": 45,
            "callback_query": {
                "id": "abc",
                "from": {"id": 101},
                "data": f"buy:{self.aid}",
                "message": {"message_id": 333, "chat": {"id": 101, "type": "private"}},
            },
        }
        url = "/telegram/webhook"
        headers = {"X-Telegram-Bot-Api-Secret-Token": "test-only"}
        self.assertEqual(client.post(url, json=cb).status_code, 403)
        self.assertEqual(client.post(url, json=cb, headers=headers).status_code, 200)
        client.post(url, json=cb, headers=headers)
        cb["update_id"] = 46
        cb["callback_query"]["from"]["id"] = 102
        cb["callback_query"]["message"]["chat"]["id"] = 102
        client.post(url, json=cb, headers=headers)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM pilot_buy_jobs").fetchone()[0], 1)

    def test_search_baseline_and_new_listing(self):
        self.conn.execute("DELETE FROM pilot_alerts")
        self.conn.execute(
            "INSERT INTO searches VALUES(5,1,'Coats','https://www.vinted.co.uk/catalog','[\"coat\"]','[\"kid\"]',1000,1)"
        )
        self.conn.execute(
            "INSERT INTO pilot_sources(user_id,source,next_at,cursor,fingerprint,lease) VALUES(1,'home',9999999999,NULL,NULL,NULL)"
        )
        self.conn.execute(
            "INSERT INTO pilot_sources(user_id,source,next_at,cursor,fingerprint,lease) VALUES(2,'home',9999999999,NULL,NULL,NULL)"
        )
        self.conn.execute(
            "INSERT INTO pilot_sources(user_id,source,next_at,cursor,fingerprint,lease) VALUES(2,'search',9999999999,NULL,NULL,NULL)"
        )
        item = {**self.item, "search_id": 5}
        result = {"source": "search", "account_ref": "ref1", "next_cursor": "next", "listings": [item]}
        with (
            patch("pilot.flow_worker.partner_configured", return_value=True),
            patch("pilot.flow_worker.partner_request", return_value=result),
        ):
            flow_worker.discover(self.conn, time.time())
            self.assertEqual(flow.alerts_for_user(self.conn, 1), [])
            flow_worker.discover(self.conn, time.time() + 4)
            self.assertEqual(flow.alerts_for_user(self.conn, 1), [])
            flow_worker.discover(self.conn, time.time() + 5.1)
            self.assertEqual(len(flow.alerts_for_user(self.conn, 1)), 1)

    def test_100_search_batches_and_personal_home_are_isolated(self):
        self.conn.execute("DELETE FROM pilot_alerts")
        data = []
        for uid in (1, 2):
            for n in range(100):
                sid = uid * 1000 + n
                data.append(
                    (
                        sid,
                        uid,
                        f"Search {n}",
                        f"https://www.vinted.co.uk/catalog?brand_id={sid}",
                        "[]",
                        "[]",
                        1100,
                        1,
                    )
                )
        self.conn.executemany("INSERT INTO searches VALUES (?,?,?,?,?,?,?,?)", data)
        t0 = int(time.time())
        observed = []

        def feed(method, path, query):
            uid = int(query["account_ref"][-1])
            source = query["source"]
            ids = [s["id"] for s in query["searches"]]
            observed.append((uid, source, query["cursor"], ids))
            itemid = uid * 100 + (1 if source == "home" else 2)
            i = {
                **self.item,
                "id": str(itemid),
                "listing_url": f"https://www.vinted.co.uk/items/{itemid}-coat",
                "listed_at": t0,
            }
            if source == "search":
                self.assertEqual(ids, list(range(uid * 1000 + 99, uid * 1000 - 1, -1)))
                i["search_id"] = uid * 1000
            else:
                self.assertEqual(ids, [])
            return {
                "source": source,
                "account_ref": query["account_ref"],
                "next_cursor": "first",
                "listings": [i],
            }

        def tick(at):
            with patch("pilot.flow_worker.time.time", return_value=at):
                while flow_worker.discover(self.conn, at):
                    pass

        with (
            patch("pilot.flow_worker.partner_configured", return_value=True),
            patch("pilot.flow_worker.partner_request", side_effect=feed),
        ):
            tick(t0)
            self.assertEqual(len(observed), 4)
            self.assertTrue(all(r[2] is None for r in observed))
            self.assertEqual([r["listing_id"] for r in flow.alerts_for_user(self.conn, 1)], ["101"])
            tick(t0 + 0.9)
            self.assertEqual(len(observed), 4)
            tick(t0 + 1)
            self.assertEqual([r[1] for r in observed[4:]], ["home", "home"])
            tick(t0 + 5)
            for uid in (1, 2):
                rows = flow.alerts_for_user(self.conn, uid)
                self.assertEqual(
                    {r["listing_id"] for r in rows}, {str(uid * 100 + 1), str(uid * 100 + 2)}
                )
                self.assertEqual(len([r for r in observed if r[0] == uid and r[1] == "search"]), 2)

    def test_interrupted_pre_submit_job_recovers_once(self):
        prior = int(time.time()) - 110
        self.conn.execute(
            "INSERT INTO pilot_buy_jobs(update_id,user_id,chat_id,alert_id,started) VALUES(1,1,101,?,?)",
            (self.aid, prior),
        )
        self.conn.execute(
            "UPDATE pilot_alerts SET order_status='checking',check_token='stale',updated=? WHERE id=?",
            (prior, self.aid),
        )
        with (
            patch("pilot.flow.partner_request", side_effect=self.fake),
            patch("pilot.flow_worker.telegram"),
        ):
            self.assertTrue(flow_worker.buy_job(self.conn, time.time()))
            self.assertFalse(flow_worker.buy_job(self.conn, time.time()))
        self.assertEqual(self.submits, 1)
        self.assertEqual(
            self.conn.execute("SELECT order_status FROM pilot_alerts").fetchone()[0], "purchased"
        )

    def test_reconciliation_only_reads_and_checks_binding(self):
        self.conn.execute("UPDATE pilot_alerts SET order_status='submitting',updated=0")
        row = self.conn.execute("SELECT * FROM pilot_alerts").fetchone()
        reply = {
            "status": "purchased",
            "account_ref": "ref1",
            "listing_id": "654",
            "idempotency_key": row["attempt_key"],
        }
        with (
            patch("pilot.flow_worker.partner_request", return_value=reply) as probe,
            patch("pilot.flow_worker.telegram") as notify,
        ):
            self.assertTrue(flow_worker.reconcile(self.conn, time.time()))
            self.assertEqual(probe.call_args.args[1], "/v1/checkout/status")
            self.assertEqual(notify.call_args.args[1]["chat_id"], 101)
        self.assertEqual(
            self.conn.execute("SELECT order_status FROM pilot_alerts").fetchone()[0], "purchased"
        )

    def _public_record(self):
        item = {
            **self.item,
            "id": "765",
            "listing_url": "https://www.vinted.co.uk/items/765-coat",
            "subtotal_pence": 625,
        }
        return flow.record_public_listing(self.conn, 1, item, self.preferences, time.time())

    def test_public_alert_without_buyer_connection_delivers_and_prices_privately(self):
        self.conn.execute("DELETE FROM pilot_buyer_link")
        self.conn.execute("DELETE FROM pilot_alerts")
        aid = self._public_record()
        self.assertIsInstance(aid, int)
        with (
            patch("pilot.flow.tg_config", return_value="123:test"),
            patch("pilot.native_order.capability", return_value=None),
            patch("pilot.flow_worker.telegram", return_value={"message_id": 415}) as tg,
        ):
            self.assertTrue(flow_worker.deliver(self.conn, time.time()))
            msg = tg.call_args.args[1]
            self.assertEqual(msg["chat_id"], 101)
            self.assertEqual(
                msg["reply_markup"]["inline_keyboard"][0][0],
                {"text": "Buy on Vinted", "url": "https://www.vinted.co.uk/items/765-coat"},
            )
            self.assertIn("shipping extra", msg["text"])
            with (
                patch("pilot.market.comparisons_for", return_value=[]),
                patch(
                    "pilot.pricing.estimate_resale",
                    return_value={"status": "insufficient", "resale_pence": None},
                ) as estimate,
            ):
                self.assertTrue(flow_worker.review(self.conn, time.time()))
                self.assertIsNone(estimate.call_args.args[0]["all_in_pence"])
                self.assertEqual(tg.call_args.args[0], "editMessageText")
        own = flow.alerts_for_user(self.conn, 1)
        self.assertEqual((own[0]["checkout_mode"], own[0]["total_pence"]), ("vinted", None))
        self.assertEqual(flow.alerts_for_user(self.conn, 2), [])
        with patch("pilot.flow.partner_request") as partner:
            self.assertIn("Vinted", flow.request_buy(self.conn, 1, aid)[0])
            partner.assert_not_called()

    def test_native_button_never_inherits_a_newly_paired_identity(self):
        self.conn.execute("DELETE FROM pilot_alerts")
        aid = self._public_record()
        with (
            patch("pilot.flow.tg_config", return_value="123:test"),
            patch("pilot.native_order.capability", return_value="paired-original"),
            patch("pilot.flow_worker.telegram", return_value={"message_id": 415}) as tg,
        ):
            self.assertTrue(flow_worker.deliver(self.conn, time.time()))
            self.assertEqual(
                tg.call_args.args[1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                f"nbuy:{aid}",
            )
        data = {
            "update_id": 44,
            "callback_query": {
                "id": "cb",
                "from": {"id": 101},
                "data": f"nbuy:{aid}",
                "message": {"message_id": 415, "chat": {"id": 101, "type": "private"}},
            },
        }
        headers = {"X-Telegram-Bot-Api-Secret-Token": "test-only"}
        client = self.app.test_client()
        with patch("pilot.native_order.capability", return_value="replacement"):
            client.post("/telegram/webhook", json=data, headers=headers)
            self.assertEqual(self.conn.execute("SELECT count(*) FROM pilot_buy_jobs").fetchone()[0], 0)
        with patch("pilot.native_order.capability", return_value="paired-original"):
            client.post("/telegram/webhook", json=data, headers=headers)
            self.assertEqual(self.conn.execute("SELECT native FROM pilot_buy_jobs").fetchone()[0], 1)
        with (
            patch("pilot.native_order.capability", return_value="replacement"),
            patch("pilot.flow_worker.telegram") as tg,
            patch("pilot.native_order.request_native") as submit,
        ):
            self.assertTrue(flow_worker.buy_job(self.conn, time.time()))
            self.assertIn("browser changed", tg.call_args.args[1]["text"])
            submit.assert_not_called()

    def test_manual_home_stays_dashboard_only_and_never_enters_checkout(self):
        self.conn.execute("DELETE FROM pilot_alerts")
        aid = flow.record_manual_home_listing(self.conn, 1, self.item, self.preferences, time.time())
        self.assertIsInstance(aid, int)
        self.assertEqual(
            aid, flow.record_manual_home_listing(self.conn, 1, self.item, self.preferences, time.time())
        )
        self.assertIsNone(
            flow.record_manual_home_listing(
                self.conn,
                1,
                {**self.item, "listed_at": time.time() - 181},
                self.preferences,
                time.time(),
            )
        )
        expected = flow.alerts_for_user(self.conn, 1)[0]
        self.assertEqual(
            (expected["origin"], expected["checkout_mode"], expected["total_pence"]),
            ("browser", "vinted", None),
        )
        with (
            patch("pilot.flow.tg_config", return_value="123:test"),
            patch("pilot.flow_worker.telegram") as tg,
        ):
            self.assertFalse(flow_worker.deliver(self.conn, time.time()))
            tg.assert_not_called()
        self.assertIn("Vinted", flow.request_buy(self.conn, 1, aid)[0])

    def test_no_delivery_after_home_ages_out(self):
        self.conn.execute("UPDATE pilot_alerts SET delivery='new'")
        with (
            patch("pilot.flow.tg_config", return_value="123:test"),
            patch("pilot.flow_worker.telegram") as send,
        ):
            flow_worker.deliver(self.conn, time.time() + 180)
            send.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT delivery FROM pilot_alerts").fetchone()[0], "expired")


if __name__ == "__main__":
    unittest.main()
