"""Buyer and administrator separation, Home filters and exact-location pickup."""
import json
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from pilot import flow, home_filters, pickup, team
from pilot.app import create_app, digest
from pilot.db import get_db, settings_for
from pilot.partner import PartnerError


class MemberSetupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.app = create_app({"TESTING": True, "MULTIPLE_ACCOUNTS": True,
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
