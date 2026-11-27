"""Offline database, Flask and Telegram transport checks for account-scoped Home routing."""
import json
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import RetryAfter, TimedOut
from werkzeug.datastructures import MultiDict

import alert_controls
import db
import home_routing_delivery as delivery
import home_routing_store as store
import search_settings
import telegram_shared_alerts as shared
from test_search_controls import DatabaseFixture
from web_ui_plugin.web_ui import create_app


class HomeRouting(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def state(self):
        with closing(search_settings.connection()) as conn:
            return store.snapshot(conn)

    def pair(self, label, account):
        code = store.configure("pair", {"label": label})["code"]
        return store.pair_browser(code, account)

    def select(self, source, dests):
        store.configure("routes", MultiDict([("source",source), ("revision",self.state()["revision"]),
                                            *(("destination",dest) for dest in dests)]))

    def item(self, ident="200", **changes):
        return dict({"id": ident, "title": "Wool coat & belt", "url": f"https://www.vinted.co.uk/items/{ident}-coat",
                     "listed_at": time.time()-10, "price_gbp":"12.00"}, **changes)

    def bot(self):
        return SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=81, photo=[])),
                               send_photo=AsyncMock(), edit_message_text=AsyncMock(return_value=SimpleNamespace(message_id=81, photo=[])),
                               edit_message_caption=AsyncMock(), get_chat=AsyncMock(return_value=SimpleNamespace(type="group",username=None)),
                               get_chat_member_count=AsyncMock(return_value=3),
                               get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")))

    def test_existing_database_additive_install_does_not_create_another_backup(self):
        existing = list(Path(self.temp.name).joinpath("backups").iterdir())
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DROP TABLE home_route_sources")
            conn.execute("DELETE FROM parameters WHERE key='home_route_schema'")
        self.assertIsNone(search_settings.ensure_schema())
        self.assertIsNone(search_settings.ensure_schema())
        self.assertEqual(existing,list(Path(self.temp.name).joinpath("backups").iterdir()))
        self.assertEqual(self.state()["sources"][0]["destinations"], ["owner"])

    def test_pairing_scope_expiration_duplicates_and_stale_config_revision(self):
        code = store.configure("pair", {"label":"Home A"})["code"]
        pair = store.pair_browser(code, "500")
        with self.assertRaises(PermissionError):
            store.pair_browser(code,"501")
        self.assertEqual(self.state()["sources"][1]["destinations"], [])
        with self.assertRaises(PermissionError):
            store.ingest(pair["token"],"501",[])
        start = self.state()["revision"]
        self.select(pair["source_id"],["owner"])
        with self.assertRaises(store.Conflict):
            store.configure("routes",MultiDict([("source",pair["source_id"]),("revision",start)]))
        with self.assertRaises(ValueError):
            self.select(pair["source_id"],["-321"])
        code = store.configure("pair", {"label":"Old"})["code"]
        with patch("home_routing_store.time.time",return_value=time.time()+601), self.assertRaises(PermissionError):
            store.pair_browser(code,"503")
        with closing(search_settings.connection()) as conn, conn:
            token_hash = conn.execute("SELECT token FROM home_route_sources WHERE id=?", (pair["source_id"],)).fetchone()[0]
            self.assertNotEqual(token_hash,pair["token"])
            conn.execute("UPDATE parameters SET value='999' WHERE key='telegram_chat_id'")
        self.assertEqual(len(self.state()["sources"]),1)
        with self.assertRaises(PermissionError):
            store.ingest(pair["token"],"500",[])

    async def test_independent_homes_many_to_many_retry_and_exactly_one_known_receipt_per_chat(self):
        first, second = self.pair("A","10"), self.pair("B","20")
        code = store.configure("pair_destination", {"label":"Melissa"})["code"]
        store.pair_chat(code,456)
        dm = next(x["id"] for x in self.state()["destinations"] if x["label"] == "Melissa")
        self.select(first["source_id"], ["owner", dm]); self.select(second["source_id"], [dm])
        self.assertEqual(store.ingest(first["token"],"10",[self.item(),self.item("201",listed_at=time.time()-181)]),1)
        self.assertEqual(store.ingest(second["token"],"20",[self.item()]),1)
        self.assertEqual(store.ingest(first["token"],"10",[self.item()]),0)
        self.assertEqual(store.ingest(second["token"],"20",[self.item("202")]),1)
        bot = self.bot()
        for _ in range(3):
            await delivery.deliver(bot, delivery.claim())
        self.assertIsNone(delivery.claim())
        self.assertCountEqual([call.kwargs["chat_id"] for call in bot.send_message.await_args_list],[123,456,456])
        for call in bot.send_message.await_args_list:
            button=call.kwargs["reply_markup"].inline_keyboard[0][0]
            self.assertIsNone(button.callback_data)
            self.assertIn("vinted.co.uk/items/",button.url)
        self.assertIn("Wool coat &amp; belt",bot.send_message.await_args.kwargs["text"])
        self.assertEqual(store.ingest(second["token"],"20",[self.item("203")]),1)
        retry = delivery.claim()
        bot.send_message.side_effect = RetryAfter(2)
        await delivery.deliver(bot,retry)
        self.assertIsNone(delivery.claim())
        bot.send_message.side_effect = TimedOut()
        await delivery.deliver(bot,delivery.claim(time.time()+5))
        self.assertIsNone(delivery.claim(time.time()+100))
        with closing(search_settings.connection()) as conn:
            self.assertEqual(conn.execute("SELECT state FROM home_route_jobs WHERE item='203'").fetchone()[0], "unknown")
            self.assertFalse(conn.execute("SELECT 1 FROM alert_outbox WHERE item_id IN ('200','202','203')").fetchone())

    async def test_deselected_source_stops_queued_alert_but_other_selected_source_keeps_it(self):
        first,second=self.pair("A","10"),self.pair("B","20")
        for account,pair in (("10",first),("20",second)):
            self.select(pair["source_id"],["owner"])
            store.ingest(pair["token"],account,[self.item()])
        job=delivery.claim()
        store.configure("remove", {"source":first["source_id"],"revision":self.state()["revision"]})
        with self.assertRaises(PermissionError):
            store.ingest(first["token"],"10",[])
        bot = self.bot()
        await delivery.deliver(bot,job)
        bot.send_message.assert_awaited_once()
        store.ingest(second["token"],"20",[self.item("201")])
        job=delivery.claim()
        self.select(second["source_id"],[])
        await delivery.deliver(bot,job)
        self.assertEqual(bot.send_message.await_count,1)

    async def test_group_sender_membership_and_receipt_bound_to_existing_authorized_group(self):
        group = shared.save_group(-321, 123, 456)
        pair = self.pair("A","20"); self.select(pair["source_id"],["shared-"+group.version])
        store.ingest(pair["token"],"20",[self.item()])
        bot=self.bot(); bot.get_chat_member.return_value=SimpleNamespace(status="left")
        await delivery.deliver(bot,delivery.claim())
        bot.send_message.assert_not_awaited()

    async def test_known_provisional_owner_message_is_updated_by_normal_renderer(self):
        pair=self.pair("A","20");self.select(pair["source_id"],["owner"])
        store.ingest(pair["token"],"20",[self.item()])
        bot=self.bot()
        await delivery.deliver(bot,delivery.claim())
        transport=await delivery.native_transport(bot,123,"200")
        response=await transport.send_message(chat_id=123,text="Original verified card",reply_markup=None)
        self.assertEqual(response.message_id,81)
        bot.edit_message_text.assert_awaited_once_with(message_id=81,chat_id=123,text="Original verified card",reply_markup=None)
        self.assertEqual(bot.send_message.await_count,1)
        self.assertIs(await delivery.native_transport(bot,456,"200"),bot)

    async def test_native_saved_search_claim_cannot_race_provisional_send(self):
        pair=self.pair("A","20");self.select(pair["source_id"],["owner"])
        store.ingest(pair["token"],"20",[self.item()])
        job=delivery.claim()
        bot=self.bot()
        self.assertIs(await delivery.native_transport(bot,123,"200"),bot)
        await delivery.deliver(bot,job)
        bot.send_message.assert_not_awaited()
        self.assertIsNone(delivery.claim())
        # A canonical failure before sending releases the independently authorized
        # Home observation after the hold, while the actual listing is still fresh.
        self.assertEqual(delivery.claim(time.time()+31)["item"],"200")

    def test_default_native_owner_group_and_saved_search_not_coupled_to_new_destinations(self):
        group=shared.Group(-321,123,456,"ABC",time.time())
        with closing(search_settings.connection()) as conn,conn:
            conn.execute("INSERT OR REPLACE INTO parameters VALUES(?,?)",(shared.KEY,json.dumps(group._asdict())))
        pair=self.pair("C","90")
        with closing(search_settings.connection()) as conn:
            self.assertTrue(store.native_row_allowed({"item_id":"200","content":"🏠 Home","platform":"vinted"},"shared-ABC",conn))
        self.select("original",[])
        with closing(search_settings.connection()) as conn:
            self.assertFalse(store.native_row_allowed({"item_id":"200","content":"🏠 Home","platform":"vinted"},"owner",conn))
            self.assertTrue(store.native_row_allowed({"item_id":"200","content":"Saved search","platform":"vinted"},"owner",conn))
        self.assertEqual(self.state()["sources"][1]["id"],pair["source_id"])

    async def test_original_event_enters_native_outbox_and_independent_route_once(self):
        self.pair("Chrome", "22")
        code = store.configure("pair_destination", {"label":"Extra"})["code"]
        store.pair_chat(code, 456)
        extra = next(d["id"] for d in self.state()["destinations"] if d["label"] == "Extra")
        self.select("original", ["owner", extra])
        alert_controls.set_paused("home", False)
        now = time.time()
        evidence = {"version":1, "source_role":"seller", "womens_clothing":True, "reason":"recommendation",
                    "max_age_seconds":20, "observed_at":now, "expires_at":now+160}
        def record(ident):
            return db.add_item_to_db(ident, "Wool coat", None, 12, now, None, "GBP",
                    {"vinted_details":{"home_discovery":evidence}, "content":"🏠 Wool coat",
                     "url":f"https://www.vinted.co.uk/items/{ident}","search_name":"MSJ Home"},
                    update_search_timestamp=False)
        self.assertTrue(record("901"))
        self.assertFalse(record("901"))
        bot = self.bot()
        await delivery.deliver(bot, delivery.claim())
        self.assertEqual(bot.send_message.await_args.kwargs["chat_id"],456)
        self.assertIsNone(delivery.claim())
        with closing(search_settings.connection()) as conn:
            self.assertEqual(conn.execute("SELECT status FROM alert_outbox WHERE item_id='901'").fetchone()[0],"pending")
        self.select("original",[extra])
        self.assertTrue(record("902"))
        await delivery.deliver(bot, delivery.claim())
        self.assertEqual(bot.send_message.await_args.kwargs["chat_id"],456)
        with closing(search_settings.connection()) as conn:
            self.assertIsNone(conn.execute("SELECT status FROM alert_outbox WHERE item_id='902'").fetchone())

    async def test_only_verified_enrollment_creates_a_destination(self):
        group = shared.save_group(-321,123,456)
        code = store.configure("pair_destination", {"label":"Sourcing"})["code"]
        bot = self.bot()
        message = SimpleNamespace(text="/home_alerts "+code, sender_chat=None, reply_text=AsyncMock())
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=-654,type="group",title="Verified team"),
                                 effective_message=message, effective_user=SimpleNamespace(id=999,is_bot=False))
        await delivery.pair_command(update, SimpleNamespace(bot=bot))
        self.assertFalse(any(d["removable"] for d in self.state()["destinations"]))
        update.effective_user.id=123
        await delivery.pair_command(update, SimpleNamespace(bot=bot))
        self.assertTrue(any(d["label"]=="Sourcing · Verified team" for d in self.state()["destinations"]))
        self.assertEqual(bot.get_chat_member.await_count,2)
        message.reply_text.assert_awaited_once()
        self.assertEqual(shared.current(),group)

    def test_scope_checked_at_http_boundary_and_packaging_is_private(self):
        client=create_app({"TESTING":True,"SESSION_COOKIE_SECURE":False}).test_client()
        self.assertEqual(client.get("/connections/home-companion.zip").status_code,302)
        self.assertEqual(client.get("/api/home/telegram-routing").status_code,302)
        self.assertEqual(client.post("/api/home/telegram-routing", data={"action":"pair","label":"X"}).status_code,400)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE dashboard_auth SET password_hash='offline-configured'")
        with client.session_transaction() as session:
            session["owner"]=True;session["csrf"]="test-csrf"
        self.assertEqual(client.get("/connections/home-companion.zip").status_code,200)
        code=client.post("/api/home/telegram-routing",data={"action":"pair","label":"Home C","csrf":"test-csrf"},
                         headers={"Accept":"application/json"}).get_json()["home_pairing"]["code"]
        # The separately installed browser does not have a dashboard login cookie.
        remote=create_app({"TESTING":True}).test_client()
        pair=remote.post("/api/home/companion/pair",json={"code":code,"account_id":"10"})
        self.assertEqual(pair.status_code,200)
        headers={"Authorization":"Bearer "+pair.get_json()["token"]}
        self.assertEqual(remote.post("/api/home/companion/events",json={"account_id":"10","items":[self.item()]},headers=headers).get_json(),{"accepted":1})
        self.assertEqual(remote.post("/api/home/companion/events",json={"account_id":"20","items":[]},headers=headers).status_code,401)
        self.assertEqual(remote.get("/api/home/telegram-routing",headers=headers).status_code,302)
        invalid=[self.item(url="https://example.test/items/200"),self.item(price_gbp="NaN"),self.item(listed_at=None)]
        for item in invalid:
            self.assertEqual(remote.post("/api/home/companion/events",json={"account_id":"10","items":[item]},headers=headers).status_code,422)
