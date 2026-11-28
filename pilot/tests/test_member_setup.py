"""Buyer and administrator separation, Home filters and exact-location pickup."""
import json
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from pilot import buyer_access, flow, home_filters, hosted_feeds, pickup, team
from pilot.app import create_app, digest
from pilot.db import get_db, settings_for
from pilot.partner import PartnerError


class MemberSetupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.app = create_app({"TESTING": True, "MULTIPLE_ACCOUNTS": True, "FINDER_SUPPORT_EMAIL":"support@example.test",
                               "DATABASE": str(Path(folder.name) / "finder.sqlite"),
                               "FINDER_PUBLIC_URL": "https://finder.example",
                               "FINDER_PARTNER_URL": "https://source.example", "FINDER_PARTNER_TOKEN": "fixture"})
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.db = get_db()
        self.owner_password = secrets.token_urlsafe(20)
        self.db.execute("INSERT INTO users VALUES(1,'owner',?)", (generate_password_hash(self.owner_password),))
        self.db.execute("INSERT INTO users VALUES(2,'second-buyer','unused')")
        self.db.execute("INSERT INTO team_owner VALUES(1,'test',1)")
        self.member = self.app.test_client()
        with self.member.session_transaction() as session:
            session.update(user_id=1, csrf_token="csrf")
        self.headers = {"X-CSRF-Token": "csrf"}

    def link(self):
        self.db.execute("INSERT INTO pilot_buyer_link VALUES(1,'buyerref','vinted-buyer',0)")

    def save(self, changes):
        return self.member.post("/api/settings", headers=self.headers, json=changes)

    @staticmethod
    def directory_rows():
        names = {"Royal Mail":"Royal Mail Tracked 48", "Evri":"Evri ParcelShop", "Relay":"Relay", "InPost":"InPost UK Locker"}
        return [{"id":f"route-{i}", "physical_point_id":"corner", "label":"Corner shop", "summary":"Test Road",
                 "couriers":[names[network]]} for i,network in enumerate(pickup.CARRIERS)]

    def lookup(self, rows, mode="pickup"):
        body = {"mode":mode}
        if mode == "pickup":
            body["query"] = "W2"
        with patch("pilot.destinations.partner_request", return_value={"account_ref":"buyerref", "destinations":rows}):
            return self.member.post("/api/delivery/destinations", headers=self.headers, json=body)

    def select_point(self):
        self.assertEqual(len(self.lookup(self.directory_rows()).json["destinations"]), 1)
        self.assertEqual(self.save({"pickup_id":"corner", "preferred_courier":"Evri"}).status_code, 200)

    def admin_client(self):
        self.db.execute("INSERT INTO finder_admin VALUES(1,'admin','unused','admin-key')")
        client = self.app.test_client()
        with client.session_transaction() as session:
            session.update(admin_session="admin-key", csrf_token="admin-csrf")
        return client

    def test_pause_blocks_sessions_telegram_and_both_feeds_then_resumes_only_that_buyer(self):
        administrator = self.admin_client()
        form = {"csrf_token": "admin-csrf"}
        self.link()
        self.db.execute("INSERT INTO pilot_buyer_link VALUES(2,'buyer2','vinted-2',0)")
        for uid in (1, 2):
            self.db.execute("INSERT INTO pilot_chats VALUES(?,?)", (uid,100+uid))
            self.db.execute("""INSERT INTO searches(user_id,name,url,include_keywords,exclude_keywords,enabled)
                               VALUES(?,'Vintage','https://www.vinted.co.uk/catalog?search_text=vintage','[]','[]',1)""", (uid,))
        self.db.execute("INSERT INTO pilot_link_codes VALUES('unused',1,?)", (time.time()+30,))
        self.db.execute("INSERT INTO settings VALUES(1,?)", (json.dumps({"sizes":["S","M"]}),))
        item = {"id":"250","listed_at":time.time(),"currency":"GBP","price_pence":500,"total_pence":875,
                "title":"Vintage jacket","listing_url":"https://www.vinted.co.uk/items/250"}
        aid = flow.record_listing(self.db,1,"buyerref",item,"home",{},time.time())
        self.db.execute("INSERT INTO pilot_buy_jobs(update_id,user_id,chat_id,alert_id) VALUES(71,1,101,?)", (aid,))
        old_home = next(j for j in hosted_feeds.claim_homes(self.db,time.time(),2) if j["user_id"]==1)
        self.assertEqual(self.member.post("/admin/buyers/1/pause", headers=self.headers).status_code,403)
        self.assertEqual(administrator.post("/admin/buyers/1/pause").status_code,403)
        self.assertEqual(administrator.post("/admin/buyers/1/pause",data=form).location,"/admin")
        self.assertEqual(administrator.post("/admin/buyers/1/pause",data=form).location,"/admin")
        self.assertIn("Resume access",administrator.get("/admin").text)
        self.assertEqual(team.capacity(self.db)["joined"],2)
        for path in ("/","/setup","/alerts/fragment","/account/paused"):
            resp=self.member.get(path)
            self.assertEqual(resp.status_code,403)
            self.assertIn("mailto:"+buyer_access.support_email(),resp.text)
            self.assertNotIn("Vintage jacket",resp.text)
        self.assertEqual(self.member.get("/api/state").json["error"],buyer_access.paused_message())
        self.assertEqual(self.save({"sizes":["XL"]}).status_code,403)
        self.assertEqual(self.db.execute("SELECT count(*) FROM pilot_link_codes WHERE user_id=1").fetchone()[0],0)
        self.assertEqual(self.db.execute("SELECT delivery FROM pilot_alerts WHERE id=?",(aid,)).fetchone()[0],"expired")
        self.assertEqual(self.db.execute("SELECT finished FROM pilot_buy_jobs WHERE update_id=71").fetchone()[0],1)
        self.assertEqual({r["user_id"] for r in hosted_feeds.rules(self.db,time.time())[0].values()},{2})
        with patch("pilot.hosted_feeds.partner_request") as source, patch("pilot.flow.partner_request") as checkout:
            hosted_feeds.check_home(self.app,old_home)
            self.assertEqual(flow.request_buy(self.db,1,aid)[0],buyer_access.paused_message())
            source.assert_not_called()
            checkout.assert_not_called()
        incoming = {"chat":{"type":"private","id":101},"from":{"id":101},"text":"/reconnect"}
        self.assertEqual(flow.telegram_update(self.db,{"message":incoming})["text"],buyer_access.paused_message())
        tap = {"id":"tap","message":incoming,"from":{"id":101},"data":f"buy:{aid}"}
        self.assertEqual(flow.telegram_update(self.db,{"callback_query":tap})["text"],buyer_access.paused_message())
        other = self.app.test_client()
        with other.session_transaction() as s:
            s["user_id"] = 2
        self.assertEqual(other.get("/api/state").status_code,200)
        self.member.get("/login")
        self.assertEqual(self.member.post("/login",data={"username":"owner","password":self.owner_password,
                                                       "csrf_token":"csrf"}).location,"/account/paused")
        self.assertEqual(administrator.post("/admin/buyers/1/resume",data=form).location,"/admin")
        self.assertEqual(self.member.get("/api/state").status_code,200)
        self.assertEqual(settings_for(self.db,1)["sizes"],["S","M"])
        self.assertEqual({r["user_id"] for r in hosted_feeds.rules(self.db,time.time())[0].values()},{1,2})
        self.assertEqual(self.member.get("/account/paused").location,"/")
        with patch("pilot.flow.partner_request") as checkout:
            self.assertIn("ended",flow.request_buy(self.db,1,aid,job_id=71)[0])
            checkout.assert_not_called()

    def test_delete_purges_only_this_buyer_and_their_sessions_cannot_be_reused(self):
        administrator = self.admin_client()
        client = self.app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=2, csrf_token="old")
        self.db.execute("INSERT INTO pilot_buyer_link VALUES(2,'removed-ref','removed-vinted',0)")
        self.db.execute("INSERT INTO pilot_chats VALUES(2,102)")
        self.db.execute("INSERT INTO hosted_deliveries VALUES(102,?)",(time.time(),))
        self.db.execute("INSERT INTO pilot_link_codes VALUES('removecode',2,?)",(time.time()+30,))
        self.db.execute("INSERT INTO invitations VALUES('spent',?,?)",(time.time()+600,time.time()))
        self.db.execute("""INSERT INTO team_invites(digest,label,created_at,accepted_by) VALUES('spent','personal label',0,2)""")
        sid = self.db.execute("""INSERT INTO searches(user_id,name,url,include_keywords,exclude_keywords,enabled)
            VALUES(2,'saved','https://www.vinted.co.uk/catalog','[]','[]',1)""").lastrowid
        self.db.execute("INSERT INTO search_photos(search_id,image) VALUES(?,?)",(sid,b'fixture'))
        item = {"id":"260","listed_at":time.time(),"currency":"GBP","price_pence":500,"total_pence":875,
                "title":"Vintage jacket","listing_url":"https://www.vinted.co.uk/items/260"}
        aid = flow.record_listing(self.db,2,"removed-ref",item,"home",{},time.time())
        self.db.execute("INSERT INTO pilot_buy_jobs(update_id,user_id,chat_id,alert_id) VALUES(72,2,102,?)",(aid,))
        self.db.execute("""INSERT INTO native_orders(alert_id,user_id,chat_id,username,binding,listing_id,stage,expires,prefs)
                          VALUES(?,2,102,'removed-vinted','bind','260','issued',0,'{}')""",(aid,))
        self.db.execute("INSERT INTO native_notifications(alert_id,text) VALUES(?,'fixture')",(aid,))
        path="/admin/buyers/2/delete"
        form={"csrf_token":"admin-csrf","confirm_name":"second-buyer"}
        self.assertEqual(self.member.post(path,data=form).status_code,403)
        self.assertEqual(administrator.get(path).status_code,200)
        self.assertEqual(administrator.post(path,data={**form,"confirm_name":"owner"}).status_code,400)
        self.assertEqual(administrator.post(path,data={"confirm_name":"second-buyer"}).status_code,403)
        self.assertEqual(administrator.post(path,data=form).location,"/admin")
        self.assertEqual(administrator.post(path,data=form).status_code,404)
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(),[])
        self.assertEqual(team.capacity(self.db)["joined"],1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM search_photos WHERE search_id=?",(sid,)).fetchone()[0],0)
        self.assertFalse(self.db.execute("SELECT 1 FROM team_invites WHERE label='personal label'").fetchone())
        self.assertIsNotNone(self.db.execute("SELECT used_at FROM invitations WHERE digest='spent'").fetchone()[0])
        for table in ("pilot_chats","pilot_sources","pilot_buy_jobs","pilot_alerts","native_orders","pilot_link_codes","pilot_buyer_link"):
            self.assertFalse(self.db.execute(f"SELECT 1 FROM {table} WHERE user_id=2").fetchone(), table)
        self.assertFalse(self.db.execute("SELECT 1 FROM native_notifications WHERE alert_id=?", (aid,)).fetchone())
        now=time.time()
        with patch("pilot.buyer_access.partner_request",side_effect=PartnerError("offline")) as revoke:
            self.assertFalse(buyer_access.revoke_next(self.db,now))
            revoke.assert_called_once_with("POST","/v1/connections/revoke",{"account_ref":"removed-ref"})
        with patch("pilot.buyer_access.partner_request",return_value={}) as revoke:
            self.assertFalse(buyer_access.revoke_next(self.db,now+5))
            self.assertTrue(buyer_access.revoke_next(self.db,now+61))
            revoke.assert_called_once()
        invite=secrets.token_urlsafe()
        self.db.execute("INSERT INTO invitations VALUES(?,?,NULL)", (digest(invite),now+600))
        newcomer=self.app.test_client()
        newcomer.get("/signup")
        with newcomer.session_transaction() as s:
            csrf=s["csrf_token"]
        joined=newcomer.post("/signup",data={"csrf_token":csrf,"invite":invite,"username":"second-buyer","password":"new-identity-fixture"})
        self.assertEqual(joined.location,"/setup")
        self.assertGreater(self.db.execute("SELECT id FROM users WHERE username='second-buyer'").fetchone()[0],2)
        self.assertEqual(client.get("/api/state").status_code,401)
        self.assertEqual(newcomer.get("/api/state").status_code,200)

    def test_in_flight_home_and_checkout_are_invalidated_when_paused(self):
        administrator=self.admin_client()
        self.link()
        job=hosted_feeds.claim_homes(self.db,time.time(),1)[0]
        item={"id":"288","title":"Home jacket","currency":"GBP","price_pence":500,"total_pence":875,
              "listed_at":time.time(),"listing_url":"https://www.vinted.co.uk/items/288"}
        def pause_and_resume():
            for action in ("pause","resume"):
                self.assertEqual(administrator.post(f"/admin/buyers/1/{action}",data={"csrf_token":"admin-csrf"}).status_code,303)
        def feed(*args):
            pause_and_resume()
            return {"account_ref":"buyerref","source":"home","next_cursor":"old","listings":[item]}
        with patch("pilot.hosted_feeds.partner_request",side_effect=feed):
            hosted_feeds.check_home(self.app,job)
        self.assertFalse(self.db.execute("SELECT 1 FROM pilot_alerts WHERE listing_id='288'").fetchone())
        self.select_point()
        aid=flow.record_listing(self.db,1,"buyerref",item,"home",settings_for(self.db,1),time.time())
        def quote(method,path,body):
            self.assertEqual(path,"/v1/checkout/quote")
            pause_and_resume()
            return {"account_ref":"buyerref","listing_id":"288","currency":"GBP","quote_id":"q","expires_at":time.time()+60,
                    "item_pence":500,"total_pence":875,"shipping_pence":200,"payment_label":"Saved card","delivery_mode":"pickup",
                    "pickup_id":"route-1","physical_point_id":"corner","courier":"Evri","pickup_label":"Corner shop"}
        with patch("pilot.flow.partner_request",side_effect=quote) as provider:
            flow.request_buy(self.db,1,aid)
            self.assertEqual(provider.call_count,1)
        self.assertEqual(self.db.execute("SELECT order_status FROM pilot_alerts WHERE id=?",(aid,)).fetchone()[0],"blocked")

    def test_new_buyer_and_separate_admin_signin(self):
        code = secrets.token_urlsafe()
        self.db.execute("INSERT INTO invitations VALUES(?,?,NULL)", (digest(code), time.time()+60))
        client = self.app.test_client()
        def csrf():
            with client.session_transaction() as s:
                return s["csrf_token"]
        client.get("/signup")
        joined = client.post("/signup", data={"invite":code, "username":"new-buyer", "password":"new-buyer-fixture",
                                              "csrf_token":csrf()})
        self.assertEqual(joined.location, "/setup")
        steps = client.get("/setup/steps").text
        self.assertIn("0 of 4 steps complete", steps)
        self.assertIn("Awaiting Telegram", steps)
        self.assertNotIn("data-setup-telegram", steps)
        self.assertNotIn("second-buyer", client.get("/setup").text)
        self.assertEqual(client.get("/admin").location, "/admin/login")
        for endpoint in ("/team/capacity", "/team/invites", "/team/telegram", "/team/invites/1/revoke"):
            self.assertEqual(client.post(endpoint, data={"csrf_token":csrf()}).status_code, 403)
        enrolled = {"username":"finder-admin", "password":"test-admin-secret", "owner":"owner",
                    "owner_password":"wrong", "csrf_token":csrf()}
        self.assertEqual(client.post("/admin/activate", data=enrolled).status_code, 403)
        enrolled["owner_password"] = self.owner_password
        self.assertEqual(client.post("/admin/activate", data={**enrolled, "username":"NEW-BUYER"}).status_code, 409)
        self.assertEqual(client.post("/admin/activate", data=enrolled).location, "/admin")
        admin_page = client.get("/admin").text
        self.assertIn("3 of 10 buyer places used", admin_page)
        self.assertIn("second-buyer", admin_page)
        self.assertIn("0 of 4 complete", admin_page)
        self.assertIn("None recorded", admin_page)
        self.assertNotIn("buyerref", admin_page)
        self.assertEqual(client.get("/api/state").status_code, 401)
        self.assertEqual(client.post("/admin/logout", data={"csrf_token":csrf()}).location,"/admin/login")
        client.get("/admin/login")
        credentials = {"csrf_token":csrf(), "username":"finder-admin", "password":"test-admin-secret"}
        self.assertEqual(client.post("/login",data=credentials).status_code, 401)
        self.assertEqual(client.post("/admin/login",data=credentials).location, "/admin")
        self.assertEqual(client.get("/admin/activate").location,"/admin/login")
        self.assertEqual(team.capacity(self.db)["joined"], 3)

    def test_fixed_choices_and_home_matching(self):
        old = {"categories":["Archive selection"], "sizes":["XS"], "quiet_start":"21:07", "quiet_end":"05:00"}
        self.db.execute("INSERT INTO settings VALUES(1,?)", (json.dumps(old),))
        state = self.member.get("/api/state").json
        self.assertEqual(state["filter_options"]["categories"]["groups"]["Previously saved"], ["Archive selection"])
        self.assertIn("21:07", state["filter_options"]["times"])
        self.assertEqual(self.save({"categories":["Mispeled"]}).status_code, 400)
        self.assertEqual(self.save({"quiet_start":"21:06"}).status_code, 400)
        self.assertEqual(self.save({"sizes":["uk 10","made-up"]}).status_code, 400)
        data = self.save({"categories":["Women / Coats & jackets","Home"], "sizes":["UK 10","M"],
                          "quiet_start":"23:00","quiet_end":"07:00"})
        self.assertEqual(data.status_code,200)
        prefs = settings_for(self.db, 1)
        self.assertTrue(prefs["filters_reviewed"])
        cases = [
            ({"category_path":["Women","Clothing","Jackets"],"size":"S / UK 8–10"}, True),
            ({"category_path":["Women","Clothing","Jackets"],"size":"UK 12 / M"}, True),
            ({"category_path":["Women","Clothing","Jackets"],"size":"XL"}, False),
            ({"category_path":["Men","Clothing","Jackets"],"size":"M"}, False),
            ({"category_path":["Women","Clothing","Skirts"],"size":"M"}, False),
            ({"category":"Home"}, True),
            ({"category":"Women","title":"Jacket UK 10"}, False),
        ]
        for item, expected in cases:
            with self.subTest(item=item):
                self.assertEqual(flow.home_matches(prefs,item), expected)
        self.assertTrue(home_filters.matches({"categories":["Women / Shoes"], "sizes":["UK shoe 6.5"]},
                                            {"category_path":"Women > Shoes", "size":"UK 6.5"}))

    def test_lookup_requires_same_point_and_all_four_then_revalidates(self):
        self.link()
        rows = self.directory_rows()
        wrong = [dict(row, physical_point_id=f"different-{i}", label="Same shop",summary="Same postcode")
                 for i,row in enumerate(rows)]
        foreign = [dict(row, id=f"foreign-{i}", physical_point_id="other", couriers=["Mondial Relay"] if i==2 else row["couriers"])
                   for i,row in enumerate(rows)]
        ambiguous = [dict(row, id=f"amb-{i}", physical_point_id="ambiguous") for i,row in enumerate(rows)]
        ambiguous.append(dict(ambiguous[0],id="another-royal-mail-counter"))
        unscoped = [{"id":"same-route-number","label":"Different shop","couriers":[network]} for network in pickup.CARRIERS]
        found = self.lookup(rows + [dict(r,id="wrong-"+r["id"]) for r in wrong] + foreign + ambiguous + unscoped).json["destinations"]
        self.assertEqual([r["id"] for r in found], ["corner"])
        self.assertEqual(found[0]["couriers"], list(pickup.CARRIERS))
        self.assertNotIn("routes", found[0])
        self.assertEqual(self.save({"pickup_id":"different-0","preferred_courier":"Evri"}).status_code,400)
        self.assertEqual(self.save({"pickup_id":"corner","preferred_courier":"MadeUp"}).status_code,400)
        self.assertEqual(self.save({"pickup_id":"corner","preferred_courier":"Evri","pickup_routes":{}}).status_code,400)
        self.select_point()
        state = self.member.get("/api/state").json
        self.assertTrue(state["settings"]["pickup_verified"])
        self.assertTrue(state["setup"]["steps"]["delivery"])
        self.assertNotIn("pickup_routes",state["settings"])
        self.assertNotIn("delivery_account_ref",state["settings"])
        self.assertEqual(set(state["settings"]["allowed_couriers"]),set(pickup.CARRIERS)-{"Evri"})
        self.assertEqual(self.lookup(rows[:-1]).json["destinations"],[])
        self.assertFalse(self.member.get("/api/state").json["setup"]["steps"]["delivery"])

    def test_home_delivery_and_disconnect_preserve_filters(self):
        self.link()
        self.select_point()
        found = self.lookup([{"id":"address-a","label":"My home","couriers":["DPD"],"postcode_area":"W2"}],mode="home")
        self.assertEqual(found.json["destinations"][0]["couriers"],["DPD"])
        self.assertEqual(self.save({"delivery_mode":"home","home_address_ref":"address-a",
                                    "preferred_courier":"DPD","categories":["Women"]}).status_code,200)
        self.assertTrue(self.member.get("/api/state").json["setup"]["steps"]["delivery"])
        with patch("pilot.connection.partner_request",return_value={}):
            self.assertEqual(self.member.post("/api/connection/disconnect",headers=self.headers).status_code, 200)
        prefs = settings_for(self.db,1)
        self.assertEqual((prefs["categories"],prefs["pickup_id"],prefs["home_address_ref"]),(["Women"],"",""))
        self.assertFalse(self.member.get("/api/state").json["setup"]["steps"]["delivery"])

    def test_courier_fallback_quotes_before_exactly_one_submission(self):
        self.link()
        self.select_point()
        def run(number, fault):
            item = {"id":str(number),"listed_at":time.time()-1,"currency":"GBP","price_pence":500,"total_pence":850,
                    "title":"Vintage jacket","listing_url":f"https://www.vinted.co.uk/items/{number}"}
            aid = flow.record_listing(self.db,1,"buyerref",item,"home",settings_for(self.db,1),time.time())
            quotes, submissions = [], []
            def buy(method,path,body):
                if path.endswith("/submit"):
                    submissions.append(body["idempotency_key"])
                    return {**body, "status":"purchased"}
                self.assertEqual(path,"/v1/checkout/quote")
                self.assertEqual(body["physical_point_id"],"corner")
                pref = body["preferences"]
                quotes.append((pref["preferred_courier"],pref["pickup_id"]))
                echo = {"listing_id":str(number),"account_ref":"buyerref","pickup_id":pref["pickup_id"],
                        "physical_point_id":"corner","courier":pref["preferred_courier"]}
                if fault == "timeout":
                    raise PartnerError("unavailable")
                if fault == "wrong_point":
                    return {**echo,"code":"courier_unavailable","physical_point_id":"next-door"}
                if fault == "refused_quote":
                    return {**echo, "code":"payment_declined", "delivery_mode":"pickup","pickup_label":"Corner shop",
                            "quote_id":"invalid","payment_label":"Saved card","expires_at":time.time()+30,
                            "currency":"GBP","item_pence":500,"shipping_pence":200,"total_pence":750}
                if fault == "wrong_buyer":
                    return {**echo,"account_ref":"ref2","code":"courier_unavailable"}
                if fault == "missing_point":
                    echo.pop("physical_point_id")
                    return {**echo,"code":"courier_unavailable"}
                if (len(quotes) == 1 and fault != "over_limit") or fault == "all_unavailable":
                    return {**echo,"code":"courier_unavailable"}
                return {**echo,"delivery_mode":"pickup","pickup_label":"Corner shop","quote_id":"bound",
                        "payment_label":"Saved card","expires_at":time.time()+30,"currency":"GBP",
                        "item_pence":500,"shipping_pence":650 if fault=="over_limit" else 200,"total_pence":1250}
            with patch("pilot.flow.partner_request",side_effect=buy):
                flow.request_buy(self.db,1,aid)
                flow.request_buy(self.db,1,aid)
            return quotes,submissions
        quotes, orders = run(711,"preferred_unavailable")
        self.assertEqual(quotes,[("Evri","route-1"),("Royal Mail","route-0")])
        self.assertEqual(len(orders),1)
        for i,fault in enumerate(("wrong_point","wrong_buyer","refused_quote","timeout","missing_point","over_limit","all_unavailable")):
            with self.subTest(fault=fault):
                quotes,orders = run(720+i,fault)
                self.assertEqual(len(quotes),4 if fault=="all_unavailable" else 1)
                self.assertEqual(orders,[])
