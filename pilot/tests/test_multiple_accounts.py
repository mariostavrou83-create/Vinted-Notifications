import hashlib
import hmac
import io
import json
import secrets
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from unittest.mock import patch

from pilot import connection, flow, flow_worker, hosted_feeds, partner, team, telegram_reconnect
from pilot.app import create_app, digest
from pilot.db import get_db, settings_for, transaction


def item(number, **other):
    return {"id": str(number), "title": "Red jacket", "currency": "GBP", "price_pence": 500,
            "subtotal_pence": 600, "listing_url": f"https://www.vinted.co.uk/items/{number}",
            "listed_at": time.time() - 5, **other}


class HostedAccountsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.bot = "12345:" + secrets.token_urlsafe(32)
        self.app = create_app({
            "TESTING": True, "MULTIPLE_ACCOUNTS": True, "DATABASE": str(Path(directory.name) / "finder.db"),
            "FINDER_PUBLIC_URL": "https://finder.example", "FINDER_PARTNER_URL": "https://buyer.example",
            "FINDER_PARTNER_TOKEN": "fixture", "PILOT_TELEGRAM_BOT_TOKEN": self.bot,
            "PILOT_TELEGRAM_BOT_USERNAME": "finder_fixture", "PILOT_TELEGRAM_WEBHOOK_SECRET": "fixture"})
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.db = get_db()

    def members(self, count):
        with transaction(self.db):
            for uid in range(1, count + 1):
                self.db.execute("INSERT INTO users VALUES(?,?,?)", (uid, f"user{uid}", "fixture"))
                self.db.execute("INSERT INTO pilot_buyer_link VALUES(?,?,?,0)", (uid, f"ref{uid}", f"buyer{uid}"))
                self.db.execute("INSERT INTO pilot_chats VALUES(?,?)", (uid, 1000 + uid))

    def rule(self, uid, query="jacket", include=(), exclude=(), budget=None):
        return self.db.execute("""INSERT INTO searches(user_id,name,url,include_keywords,exclude_keywords,budget_pence,enabled)
            VALUES(?,?,?,?,?,?,1)""", (uid, query, "https://www.vinted.co.uk/catalog?search_text=" + query,
                                      json.dumps(include), json.dumps(exclude), budget)).lastrowid

    def client(self, uid):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = uid
            session["csrf_token"] = "proof"
        return client

    def test_seats_persist_reserve_open_invites_and_prevent_concurrent_last_signup(self):
        self.members(9)
        self.db.execute("INSERT INTO team_owner VALUES(1,'fixture',1)")
        owner, member = self.client(1), self.client(2)
        self.assertEqual(team.capacity(self.db), {"joined": 9, "reserved": 0, "limit": 10})
        form = {"csrf_token": "proof", "label": "member"}
        self.assertEqual(owner.post("/team/invites", data=form).status_code, 200)
        self.assertEqual(owner.post("/team/invites", data=form).status_code, 409)
        self.assertEqual(member.post("/team/capacity", data={**form, "limit": "50"}).status_code, 403)
        self.assertEqual(owner.post("/team/capacity", data={**form, "limit": "51"}).status_code, 400)
        self.assertEqual(owner.post("/team/capacity", data={"limit": "50"}).status_code, 403)
        # Two still-valid invitations from a previous installation cannot occupy the final place twice.
        codes = [secrets.token_urlsafe() for _ in range(2)]
        self.db.executemany("INSERT INTO invitations VALUES(?,?,NULL)", ((digest(c), time.time()+60) for c in codes))
        barrier = threading.Barrier(2)
        def join(pair):
            idx, code = pair
            guest = self.app.test_client()
            with guest.session_transaction() as session:
                session["csrf_token"] = "signup"
            barrier.wait()
            return guest.post("/signup", data={"username": f"guest{idx}", "password": "sign-up-fixture-12",
                                              "invite": code, "csrf_token": "signup"}).status_code
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sorted(pool.map(join, enumerate(codes))), [302, 409])
        self.assertEqual(team.capacity(self.db)["joined"], 10)
        self.assertEqual(owner.post("/team/capacity", data={**form, "limit": "50"}).status_code, 303)
        self.assertEqual(team.capacity(self.db)["limit"], 50)
        self.assertEqual(owner.post("/team/capacity", data={**form, "limit": "10"}).status_code, 409)

    def test_personal_homes_check_every_member_and_quarantine_only_the_wrong_response(self):
        self.members(10)
        def feed(method, path, data):
            index = int(data["account_ref"][3:])
            self.assertEqual((method, path, data["source"]), ("POST", "/v1/feed", "home"))
            return {"account_ref": "wrong" if index == 3 else data["account_ref"], "source": "home",
                    "next_cursor": "next", "listings": [item(index), item(100+index, listed_at=time.time()-181)]}
        jobs = hosted_feeds.claim_homes(self.db, time.time(), 50)
        self.assertEqual(len(jobs), 10)
        self.assertEqual(hosted_feeds.claim_homes(self.db, time.time(), 50), [])
        with patch("pilot.hosted_feeds.partner_request", side_effect=feed):
            for job in jobs:
                hosted_feeds.check_home(self.app, job)
        got = [(r["user_id"], r["account_ref"], r["listing_id"]) for r in self.db.execute("SELECT * FROM pilot_alerts ORDER BY user_id")]
        self.assertEqual(got, [(i, f"ref{i}", str(i)) for i in range(1,11) if i != 3])
        self.assertIsNone(hosted_feeds.status(self.db, 3, time.time())["home"]["last_checked_at"])
        self.assertEqual(self.client(2).get("/api/state").json["alerts"][0]["listing_id"], "2")

    def test_shared_checks_baseline_dedupe_filters_budget_and_recheck_after_an_edit(self):
        self.members(3)
        first = self.rule(1, include=["red"], budget=1100)
        second = self.rule(2, exclude=["kids"], budget=900)
        third = self.rule(3, exclude=["kids"])
        changed = False
        calls = []
        fail = None
        tick = 0
        def feed(method, path, data):
            self.assertEqual(path, "/v1/catalogue/changes")
            self.assertNotIn("account_ref", data)
            calls.append(data)
            if changed:
                self.db.execute("UPDATE searches SET exclude_keywords=? WHERE id=?", ('["red"]', third))
            return {"market": "GB", "currency": "GBP", "scope": "all_public_listings",
                    "complete": fail != "partial", "through": time.time() - (15 if fail == "stale" else 0),
                    "from_cursor": data["cursor"], "next_cursor": f"cursor{tick}",
                    "listings": [item(800+tick, total_pence=9999)]}
        def run():
            nonlocal tick
            tick += 1
            self.db.execute("UPDATE hosted_catalogue SET next_at=0")
            return hosted_feeds.catalogue(self.db, time.time())
        with patch("pilot.hosted_feeds.partner_request", side_effect=feed):
            self.assertTrue(run())
            self.assertEqual(set(calls[0]), {"market", "currency", "cursor"})
            self.assertEqual(self.db.execute("SELECT count(*) FROM pilot_alerts").fetchone()[0], 0)
            self.assertTrue(run())
            self.assertNotIn("filters", calls[-1])
            self.assertEqual(self.db.execute("SELECT count(*) FROM pilot_alerts").fetchone()[0], 2)
            changed = True
            self.assertTrue(run())
            changed = False
            for fail in ("partial", "stale"):
                self.assertFalse(run())
                self.assertEqual(self.db.execute("SELECT cursor FROM hosted_catalogue").fetchone()[0], "cursor3")
        rows = self.db.execute("SELECT user_id,listing FROM pilot_alerts ORDER BY id").fetchall()
        self.assertEqual([r[0] for r in rows], [1,3,1])
        self.assertEqual([json.loads(r[1])["total_pence"] for r in rows], [None]*3)
        self.assertEqual(json.loads(rows[0][1])["search_id"], first)
        self.assertEqual(json.loads(rows[1][1])["search_id"], third)
        self.assertNotEqual(first, second)
        sent = []
        def telegram(method, body):
            sent.append(body)
            return {"message_id": len(sent)}
        with patch("pilot.flow_worker.telegram", side_effect=telegram):
            for _ in range(3):
                flow_worker.deliver(self.db, time.time())
        self.assertEqual({r["chat_id"] for r in sent}, {1001})
        self.assertEqual(self.db.execute("SELECT delivery FROM pilot_alerts WHERE user_id=3").fetchone()[0], "expired")
        self.assertTrue(all("shipping extra" in r["text"] for r in sent))
        self.assertTrue(all("Estimated total" not in r["text"] for r in sent))

    def test_one_account_can_expire_replace_its_grant_and_resume_only_its_own_work(self):
        self.members(2)
        def fail_account(code, obj, status):
            exc = HTTPError("https://buyer.example", status, "", {}, io.BytesIO(json.dumps(obj).encode()))
            with patch("pilot.partner.build_opener") as opener:
                opener.return_value.open.side_effect = exc
                with self.assertRaises(partner.PartnerError):
                    partner.partner_request("POST", "/v1/feed", {"account_ref": code})
        fail_account("ref1", {"code":"account_authorization_expired","account_ref":"ref2"}, 401)
        self.assertEqual(connection.linked_account(self.db, 1), "ref1")
        fail_account("ref1", {"code":"account_authorization_expired","account_ref":"ref1"}, 401)
        self.assertIsNone(connection.linked_account(self.db, 1))
        self.assertEqual(connection.linked_account(self.db, 2), "ref2")
        with patch("pilot.flow_worker.telegram", return_value={"message_id": 25}) as send:
            self.assertTrue(telegram_reconnect.notify(self.db, time.time()))
            self.assertFalse(telegram_reconnect.notify(self.db, time.time()))
            self.assertEqual(send.call_args.args[1]["chat_id"], 1001)
        def signed(uid, age=0):
            fields = {"auth_date":str(int(time.time())-age), "query_id":secrets.token_hex(6),
                      "user":json.dumps({"id":uid})}
            key = hmac.digest(b"WebAppData", self.bot.encode(), "sha256")
            payload = "\n".join(f"{k}={v}" for k,v in sorted(fields.items()))
            return urlencode(dict(fields, hash=hmac.digest(key,payload.encode(),"sha256").hex()))
        guest = self.app.test_client()
        url = "/connect/telegram/start"
        for bad in (signed(1001, 610), signed(1003), signed(1002).replace("1002", "1001")):
            self.assertEqual(guest.post(url, json={"init_data":bad}).status_code, 403)
        self.db.execute("INSERT INTO settings VALUES(1,?)",(json.dumps({"pickup_id":"own-shop","preferred_courier":"own-courier"}),))
        before = settings_for(self.db,1)
        def start():
            init = signed(1001)
            with patch("pilot.connection.partner_request",return_value={"authorization_url":"https://buyer.example/sign-in"}) as mocked:
                self.assertEqual(guest.post(url,json={"init_data":init}).status_code,200)
                self.assertEqual(guest.post(url,json={"init_data":init}).status_code,409)
            return mocked.call_args.args[2]["state"]
        def callback(state, who):
            with patch("pilot.connection.partner_request", return_value={"account_ref":f"ref{who}","username":f"buyer{who}"}):
                return guest.get("/connect/vinted/callback", query_string={"state":state,"code":"single-use"}).get_data(as_text=True)
        self.assertIn("different Vinted account",callback(start(),2))
        self.assertIsNone(connection.linked_account(self.db,1))
        state = start()
        self.assertIn("Vinted reconnected",callback(state,1))
        self.assertNotIn("Vinted reconnected",callback(state,1))
        self.assertEqual(connection.linked_account(self.db,1),"ref1")
        self.assertEqual(settings_for(self.db,1), before)
        self.assertEqual(len(hosted_feeds.claim_homes(self.db,time.time(),50)),2)
        self.assertEqual(guest.get("/api/state").status_code,401)

    def test_reconnect_command_and_buttons_are_private_and_checkout_uses_own_courier_once(self):
        self.members(2)
        def message(chat, sender, kind="private"):
            return {"message": {"chat":{"id":chat,"type":kind},"from":{"id":sender},"text":"/reconnect"}}
        for update in [message(-30,1001,"group"),message(1001,1002)]:
            self.assertEqual(flow.telegram_update(self.db,update),{"ok":True})
        self.assertIn("web_app",flow.telegram_update(self.db,message(1001,1001))["reply_markup"]["inline_keyboard"][0][0])
        for uid in (1,2):
            self.db.execute("INSERT INTO settings VALUES(?,?)",(uid,json.dumps({"preferred_courier":f"courier{uid}","pickup_id":f"shop{uid}","max_total":"20"})))
        aid = flow.record_catalogue_listing(self.db,1,"ref1",item(75),settings_for(self.db,1),time.time())
        self.db.execute("UPDATE pilot_alerts SET sent_chat=1001,message_id=23,delivery='sent' WHERE id=?",(aid,))
        def tap(chat, update_id):
            return flow.telegram_update(self.db,{"update_id":update_id,"callback_query":{"id":"cb","data":f"buy:{aid}","from":{"id":chat},
                  "message":{"chat":{"type":"private","id":chat},"message_id":23}}})
        self.assertIn("unavailable",tap(1002,7)["text"])
        tap(1001,8)
        tap(1001,9)
        submissions = []
        def checkout(method,path,data):
            self.assertEqual(data["account_ref"],"ref1")
            if path.endswith("quote"):
                self.assertEqual(data["preferences"]["pickup_id"],"shop1")
                return {"account_ref":"ref1","listing_id":"75","quote_id":"q","currency":"GBP","item_pence":500,
                        "shipping_pence":250,"total_pence":900,"delivery_mode":"pickup","pickup_id":"shop1",
                        "courier":"courier1","pickup_label":"Private shop","payment_label":"Saved card","expires_at":time.time()+60}
            self.assertEqual(path,"/v1/checkout/submit")
            submissions.append(data["idempotency_key"])
            return {**data,"status":"purchased"}
        with patch("pilot.flow.partner_request",side_effect=checkout), patch("pilot.flow_worker.telegram",return_value={"message_id":24}):
            self.assertTrue(flow_worker.buy_job(self.db,time.time()))
            self.assertTrue(flow_worker.buy_job(self.db,time.time()))
        self.assertEqual(len(submissions),1)
        self.assertEqual(self.db.execute("SELECT order_status FROM pilot_alerts WHERE id=?",(aid,)).fetchone()[0],"purchased")

    def test_fifty_people_and_five_thousand_distinct_searches_share_one_check(self):
        self.members(50)
        with transaction(self.db):
            for uid in range(1, 51):
                for n in range(100):
                    self.rule(uid, f"garment{uid}style{n}")
        now = time.time()
        def changes(method, path, payload):
            self.assertEqual(set(payload), {"market", "currency", "cursor"})
            return {"market":"GB", "currency":"GBP", "scope":"all_public_listings", "complete":True,
                    "from_cursor":payload["cursor"], "next_cursor":str(time.time()), "through":time.time(),
                    "listings":[item(uid, title=f"Garment{uid}style99") for uid in range(1,51)]}
        with patch("pilot.hosted_feeds.partner_request", side_effect=changes) as source:
            self.assertTrue(hosted_feeds.catalogue(self.db, now))
            self.assertEqual(self.db.execute("SELECT count(*) FROM hosted_search_baseline").fetchone()[0], 5000)
            self.assertFalse(hosted_feeds.catalogue(self.db, now+1))
            self.db.execute("UPDATE hosted_catalogue SET next_at=0")
            self.assertTrue(hosted_feeds.catalogue(self.db, time.time()))
            self.assertEqual(source.call_count, 2)
        got = self.db.execute("SELECT user_id,listing_id FROM pilot_alerts ORDER BY user_id")
        self.assertEqual([tuple(x) for x in got], [(uid, str(uid)) for uid in range(1,51)])
        response = self.client(50).post("/api/searches", headers={"X-CSRF-Token":"proof"},
                                         json={"name":"too many","url":"https://www.vinted.co.uk/catalog"})
        self.assertEqual(response.status_code, 409)

    def test_marketplace_filters_use_listing_attributes_and_whole_words_locally(self):
        from pilot.catalogue_rules import CatalogueMatcher, catalogue_filter
        url = ("https://www.vinted.co.uk/catalog/3-women?size_ids[]=5&size_ids[]=7&brand_ids[]=12"
               "&status_ids[]=2&color_ids[]=4&material_ids[]=8&price_to=7&search_text=red+jacket")
        key, spec = catalogue_filter(url)
        same, _ = catalogue_filter(url + "&page=2&time=1234&utm_source=ad")
        self.assertEqual(key, same)
        matcher = CatalogueMatcher({key: spec})
        attrs = dict(category_ids=[3,25], size_ids=[7], brand_ids=[12], status_ids=[2], color_ids=[4], material_ids=[8])
        target = item(9, attributes=attrs, brand="Example")
        self.assertEqual(matcher.matched(target), [key])
        for miss in (dict(price_pence=701), dict(title="Redwood jacket"), dict(attributes={**attrs, "size_ids": []}),
                     dict(attributes={**attrs,"brand_ids":[13]})):
            self.assertEqual(matcher.matched({**target, **miss, "filter_keys":[key]}), [])
        with self.assertRaises(ValueError):
            matcher.matched(item(9))
        with self.assertRaises(ValueError):
            catalogue_filter("https://www.vinted.co.uk/catalog?unexpected_key=123")

    def test_telegram_rate_limit_requeues_only_the_affected_person(self):
        self.members(2)
        for uid in (1, 2):
            flow.record_listing(self.db, uid, f"ref{uid}", item(uid), "home", {}, time.time())
        now = time.time()
        with patch("pilot.flow_worker.telegram", side_effect=[flow_worker.TelegramRetry(5), {"message_id":7}]) as send:
            self.assertTrue(flow_worker.deliver(self.db, now))
            self.assertTrue(flow_worker.deliver(self.db, now))
            self.assertFalse(flow_worker.deliver(self.db, now + 1))
            self.assertEqual([c.args[1]["chat_id"] for c in send.call_args_list], [1001,1002])
        rows = self.db.execute("SELECT delivery,message_id FROM pilot_alerts ORDER BY user_id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("new", None), ("sent", 7)])

    def test_unconfigured_service_and_upstream_retry_never_claim_a_success(self):
        self.members(1)
        self.rule(1)
        with patch.dict(self.app.config, FINDER_PARTNER_TOKEN=""):
            client = self.client(1)
            self.assertIn("awaiting configuration", client.get("/api/monitoring").json["home"]["message"])
            self.assertFalse(hosted_feeds.catalogue(self.db,time.time()))
            self.assertIn("Multiple accounts method",client.get("/").get_data(as_text=True))
            self.assertEqual(client.get("/extension/install").status_code,404)
            self.assertEqual(client.get("/team/market").status_code,404)
        exc = HTTPError("https://buyer.example",429,"",{"Retry-After":"7"},io.BytesIO(b"{}"))
        with patch("pilot.partner.build_opener") as opener:
            opener.return_value.open.side_effect = exc
            with self.assertRaises(partner.PartnerError):
                partner.partner_request("POST","/v1/catalogue/changes",{})
        self.assertFalse(hosted_feeds.catalogue(self.db,time.time()))
        self.assertEqual(hosted_feeds.claim_homes(self.db,time.time(),50),[])
        check=hosted_feeds.status(self.db,1,time.time())["home"]
        self.assertIn("pause",check["message"])
        self.assertIsNone(check["last_checked_at"])
