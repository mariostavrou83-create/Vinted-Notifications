import os
import tempfile
import unittest
from unittest.mock import patch

from pilot.app import create_app
from pilot.db import get_db


class DeliveryOptionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app({"TESTING": True, "DATABASE": os.path.join(self.temp.name, "db"),
                               "FINDER_PARTNER_URL": "https://partner.example", "FINDER_PARTNER_TOKEN": "fixture"})
        self.owner, self.other = self.app.test_client(), self.app.test_client()
        with self.app.app_context():
            db = get_db()
            db.executemany("INSERT INTO users VALUES(?,?,?)", [(1, "one", "test"), (2, "two", "test")])
            db.executemany("INSERT INTO pilot_buyer_link VALUES(?,?,?,0)", [(1, "A", "one"), (2, "B", "two")])
        for i, c in enumerate((self.owner,self.other), start=1):
            with c.session_transaction() as s:
                s.update(user_id=i, csrf_token="test")
        self.headers = {"X-CSRF-Token": "test"}

    def lookup(self, client, **kwargs):
        return client.post("/api/delivery/destinations", json=kwargs, headers=self.headers)

    @patch("pilot.destinations.partner_request")
    def test_account_scoped_lookup_and_saved_choice(self, partner):
        partner.return_value = {"account_ref": "A", "destinations": [
            {"id":"shop1","label":"Main Post Office","summary":"12 High Street, W2",
             "couriers":["InPost","InPost","Evri"],"private_contact":"hidden"},
            {"id":"shop1","label":"Duplicate","couriers":[]}, {"id":"../invalid","label":"ignore"}]}
        r = self.lookup(self.owner, mode="pickup", query="Paddington")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json, {"destinations":[{"id":"shop1","label":"Main Post Office",
                                                   "summary":"12 High Street, W2","couriers":["InPost","Evri"]}]})
        partner.assert_called_once_with("POST", "/v1/delivery/destinations",
                                        {"account_ref":"A","mode":"pickup","country":"GB","query":"Paddington"})
        self.assertEqual(self.lookup(self.other,mode="pickup",query="W2",account_ref="A").status_code,400)
        self.assertEqual(self.lookup(self.other,mode="pickup",query="W2").status_code,502)
        self.assertEqual(self.other.post("/api/settings",json={"pickup_id":"shop1"},headers=self.headers).status_code,400)
        saved=self.owner.post("/api/settings",json={"pickup_id":"shop1","pickup_label":"forged"},headers=self.headers)
        self.assertEqual(saved.json["settings"]["pickup_label"],"Main Post Office")
        self.assertEqual(saved.json["settings"]["delivery_couriers"],["InPost","Evri"])
        self.assertEqual(self.owner.post("/api/settings",json={"preferred_courier":"Unknown"},
                                         headers=self.headers).status_code,400)
        with self.app.app_context():
            get_db().execute("DELETE FROM pilot_destination_choices")
        self.assertEqual(self.owner.post("/api/settings",json={"sizes":["M"]},headers=self.headers).status_code,200)
        self.assertEqual(self.owner.get("/api/state").json["settings"]["pickup_label"],"Main Post Office")

    @patch("pilot.destinations.partner_request")
    def test_home_address_is_a_provider_ref_and_no_unverified_calls(self, partner):
        partner.return_value={"account_ref":"B","destinations":[{"id":"home-4","label":"Home","postcode_area":"w2",
                              "summary":"1 Example Street","street":"private","couriers":["Evri"]}]}
        r=self.lookup(self.other,mode="home")
        self.assertEqual(r.json,{"destinations":[{"id":"home-4","label":"Home","summary":"W2","couriers":["Evri"]}]})
        self.assertNotIn("query",partner.call_args.args[2])
        self.assertEqual(self.lookup(self.other,mode="home",query="1 Example Street").status_code,400)
        self.assertEqual(self.other.post("/api/settings",json={"delivery_mode":"home","home_address_ref":"home-4"},
                                         headers=self.headers).json["settings"]["home_address_label"],"Home")
        with self.app.app_context():
            get_db().execute("UPDATE pilot_buyer_link SET revoke_pending=1 WHERE user_id=1")
        self.assertEqual(self.lookup(self.owner,mode="pickup",query="W2").status_code,409)
        with patch("pilot.destinations.partner_configured",return_value=False):
            self.assertEqual(self.lookup(self.other,mode="home").status_code,503)
        self.assertEqual(self.lookup(self.app.test_client(),mode="home").status_code,401)
        self.assertEqual(partner.call_count,1)
