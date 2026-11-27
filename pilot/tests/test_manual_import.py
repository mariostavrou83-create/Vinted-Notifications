import tempfile
import time
import unittest
from unittest.mock import patch

from pilot.app import create_app
from pilot.db import get_db


class ManualHomeImportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app=create_app({"TESTING":True,"DATABASE":temp.name+"/db.sqlite"})
        self.first,self.second=self.app.test_client(),self.app.test_client()
        self.now=int(time.time())
        with self.app.app_context():
            ids=[get_db().execute("INSERT INTO users(username,password_hash) VALUES(?,?)",
                                 (username,"unused")).lastrowid for username in ("first","second")]
        for client, owner in zip((self.first,self.second),ids):
            with client.session_transaction() as s:s.update(user_id=owner,csrf_token="csrf")
        self.capture={"source":"home","observed_at":self.now,"items":[
            {"id":"7722","title":"Example jacket","listing_url":"https://www.vinted.co.uk/items/7722-jacket",
             "price_pence":850,"currency":"GBP","listed_at":self.now-44,"age_text":"44 seconds ago"}]}
        self.headers={"X-CSRF-Token":"csrf"}

    def stage(self):
        result=self.first.post("/api/imports/preview",json=self.capture,headers=self.headers)
        self.assertEqual(result.status_code,200,result.json)
        return result.json["token"]

    def confirm(self,client,token,ids=None):
        return client.post("/api/imports/confirm",json={"token":token,"selected":ids or ["7722"]},
                           headers=self.headers)

    def test_owner_confirmation_needed_and_is_single_use(self):
        token=self.stage()
        self.assertFalse(self.first.get("/api/state").json["alerts"])
        self.assertEqual(self.confirm(self.second,token).status_code,409)
        self.assertEqual(self.confirm(self.first,token,["42"]).status_code,400)
        result=self.confirm(self.first,token)
        self.assertEqual(result.json,{"available":1,"skipped":0})
        self.assertEqual(self.confirm(self.first,token).status_code,409)
        alert=self.first.get("/api/state").json["alerts"][0]
        self.assertEqual(alert["item_price_pence"],850)
        self.assertEqual(alert["origin"],"browser")
        self.assertIsNone(alert["total_pence"])
        self.assertFalse(self.second.get("/api/state").json["alerts"])
        with self.app.app_context():
            row=get_db().execute("SELECT * FROM pilot_alerts").fetchone()
            self.assertEqual(row["account_ref"],"manual")
            self.assertNotEqual(row["delivery"],"new")

    def test_scoped_listing_attributes_survive_preview_without_arbitrary_properties(self):
        self.capture["items"][0].update(seller_id="1234",brand="Studio",condition="Very good",model="0723243441",
                                       material="Wool",category="Women",size="8",unknown_field="discard")
        r=self.first.post("/api/imports/preview",json=self.capture,headers=self.headers)
        item=r.json["items"][0]
        for key in ("seller_id","condition","brand","model","material","category","size"):
            self.assertEqual(item[key], self.capture["items"][0][key])
        self.assertNotIn("unknown_field",item)

    def test_source_origin_age_fields_and_authentication(self):
        self.assertEqual(self.app.test_client().get("/import/vinted").status_code,302)
        self.assertEqual(self.app.test_client().post("/api/imports/preview",json=self.capture).status_code,401)
        self.assertEqual(self.first.post("/api/imports/preview",json=self.capture).status_code,403)
        self.capture["items"][0]["listing_url"]="https://outside.invalid/items/7722"
        self.assertEqual(self.first.post("/api/imports/preview",json=self.capture,headers=self.headers).status_code,422)
        self.capture["items"][0]["listing_url"]="https://www.vinted.co.uk/items/7722"
        self.capture["items"][0]["listed_at"]=self.now-180
        self.assertEqual(self.first.post("/api/imports/preview",json=self.capture,headers=self.headers).status_code,422)
        self.capture["items"][0]["listed_at"]=self.now+600
        self.assertEqual(self.first.post("/api/imports/preview",json=self.capture,headers=self.headers).status_code,422)

    def test_age_rechecked_at_confirmation_and_respects_dashboard_filters(self):
        self.capture["items"][0]["listed_at"]=self.now-170
        token=self.stage()
        with patch("pilot.manual_import.time.time",return_value=self.now+10):
            self.assertEqual(self.confirm(self.first,token).json,{"available":0,"skipped":1})
        self.first.post("/api/settings",json={"sizes":["M"]},headers=self.headers)
        token=self.stage()
        self.assertEqual(self.confirm(self.first,token).json,{"available":0,"skipped":1})

    def test_expired_or_replaced_preview_rejected_and_duplicate_cards_count_once(self):
        self.capture["items"].append(dict(self.capture["items"][0]))
        old=self.stage()
        new=self.stage()
        self.assertEqual(self.confirm(self.first,old).status_code,409)
        with patch("pilot.manual_import.time.time",return_value=self.now+91):
            self.assertEqual(self.confirm(self.first,new).status_code,409)
        token=self.stage()
        self.assertEqual(self.confirm(self.first,token).json["available"],1)
        token=self.stage()
        self.assertEqual(self.confirm(self.first,token).json["available"],1)
        self.assertEqual(len(self.first.get("/api/state").json["alerts"]),1)
