import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from flask import Flask, g, request

from pilot import connection
from pilot.db import SCHEMA, close_db, get_db
from pilot.partner import PartnerError


class BuyerLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, DATABASE=os.path.join(self.tmp.name, "db"),
                               FINDER_PARTNER_URL="https://partner.example",
                               FINDER_PARTNER_TOKEN="fixture",
                               FINDER_PUBLIC_URL="https://finder.example")
        self.app.teardown_appcontext(close_db)
        with self.app.app_context():
            db = get_db()
            db.executescript(SCHEMA)
            connection.init_connection(db)
            db.executemany("INSERT INTO users VALUES(?,?,?)", [(1,"one",""), (2,"two","")])
        @self.app.before_request
        def identity():
            g.user = {"id": int(request.headers.get("X-Test-User", "1"))}
        connection.register_connection(self.app)
        self.client = self.app.test_client()
        self.calls = []
        self.name = "buyer.name"
        self.patcher = patch.object(connection, "partner_request", side_effect=self.partner)
        self.mock = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def partner(self, method, path, body):
        self.calls.append((path, body))
        if path.endswith("/start"):
            return {"authorization_url": "https://partner.example/authorize"}
        if path.endswith("/exchange"):
            challenge = base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"].encode()).digest()).rstrip(b"=").decode()
            self.assertEqual(self.calls[-2][1]["code_challenge"],challenge)
            return {"username": self.name, "account_ref": "fixture-link-1"}
        return {}

    def start(self, name=""):
        r=self.client.post("/api/connection/start",json={"account_hint":name})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json["authorization_url"], "https://partner.example/authorize")
        self.assertFalse(self.client.get("/api/connection").json["linked"])
        return self.calls[-1][1]["state"]

    def finish(self, state, user=1):
        return self.client.get("/connect/vinted/callback", query_string={"state":state,"code":"fixture-code"},
                               headers={"X-Test-User":str(user)})

    def test_extension_download_includes_loadable_manifest(self):
        result = self.client.get("/extension/download")
        self.assertEqual(result.status_code, 200)
        self.assertIn("finder-account-selector.zip", result.headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertIn(manifest["action"]["default_popup"], archive.namelist())
            self.assertIn("profile.js", archive.namelist())
            self.assertEqual(json.loads(archive.read("finder-site.json")), {"origin":"https://finder.example"})
        result.close()

    def test_preset_ignores_request_host(self):
        self.app.config["FINDER_PUBLIC_URL"] = ""
        result = self.client.get("/extension/download", base_url="https://untrusted.example")
        with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
            self.assertEqual(json.loads(archive.read("finder-site.json")), {"origin":""})
        result.close()

    def test_local_selection_then_verified_link_with_single_user_one_time_state(self):
        state=self.start("Buyer.name")
        self.assertIn("expired", self.finish(state,2).location)
        self.assertIn("linked", self.finish(state).location)
        self.assertIn("expired", self.finish(state).location)
        self.assertEqual(self.client.get("/api/connection").json["username"],"buyer.name")
        self.assertFalse(self.client.get("/api/connection",headers={"X-Test-User":"2"}).json["linked"])
        self.assertEqual(len(self.calls),2)
        with self.app.app_context():
            self.assertEqual(connection.linked_account(get_db(),1),"fixture-link-1")
            self.assertIsNone(connection.linked_account(get_db(),2))

    def test_wrong_username_and_denied_code_never_link(self):
        state=self.start("other.person")
        self.assertIn("mismatch",self.finish(state).location)
        self.assertFalse(self.client.get("/api/connection").json["linked"])
        state=self.start()
        r=self.client.get("/connect/vinted/callback",query_string={"state":state,"error":"access_denied"})
        self.assertIn("cancelled",r.location)
        self.assertIn("expired",self.finish(state).location)

    def test_no_adapter_no_authorization_request(self):
        self.app.config["FINDER_PUBLIC_URL"]=""
        with patch.dict(os.environ, {"FINDER_PUBLIC_URL":""}):
            self.assertFalse(self.client.get("/api/connection").json["configured"])
            self.assertEqual(self.client.post("/api/connection/start",json={}).status_code,503)
        self.mock.assert_not_called()

    def test_rejects_untrusted_browser_destination_and_clears_attempt(self):
        self.mock.side_effect=None
        self.mock.return_value={"authorization_url":"https://partner.example.attacker.invalid/login"}
        self.assertEqual(self.client.post("/api/connection/start",json={}).status_code,502)
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM pilot_link_attempt").fetchone()[0],0)

    def test_disconnect_immediately_disables_usage_even_if_provider_is_offline(self):
        state=self.start()
        self.finish(state)
        self.mock.side_effect=PartnerError("offline")
        response=self.client.post("/api/connection/disconnect")
        self.assertEqual(response.status_code,502)
        self.assertTrue(self.client.get("/api/connection").json["revoke_pending"])
        with self.app.app_context():
            self.assertIsNone(connection.linked_account(get_db(),1))
        self.mock.side_effect=self.partner
        self.assertEqual(self.client.post("/api/connection/disconnect").status_code,200)
        self.assertFalse(self.client.get("/api/connection").json["revoke_pending"])
