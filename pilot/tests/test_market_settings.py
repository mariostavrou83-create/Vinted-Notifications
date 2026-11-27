import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from pilot import market, market_runtime, public_feed
from pilot import market_settings as controls
from pilot.app import create_app
from pilot.db import get_db


class OwnerMarketTest(unittest.TestCase):
    origin = "https://pilot.test"
    scavio = "private-scavio-test-value"
    mistral = "private-mistral-test-value"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cfg = {
            "DATABASE": str(Path(directory.name) / "pilot.sqlite"),
            "TESTING": True,
            "FINDER_PUBLIC_URL": self.origin,
            "FINDER_PILOT_CONFIG_KEY": Fernet.generate_key().decode(),
        }
        self.app = create_app(self.cfg)
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO users VALUES(1,'boss','unused'),(2,'tester','unused')")
            db.execute("INSERT INTO team_owner VALUES(1,'fixture',1)")
        self.owner = self.client(1)
        self.tester = self.client(2)
        self.discovery = {
            "provider": "scavio",
            controls.LIFETIME: "10",
            controls.DAILY: "5",
            controls.RATE: "1",
            controls.CONCURRENT: "1",
        }
    def client(self, uid):
        client = self.app.test_client()
        with client.session_transaction(base_url=self.origin) as session:
            session.update(user_id=uid, csrf_token="test-only")
        return client

    def post(self, form, *, provider_key="", version=0, client=None, headers=None):
        return (client or self.owner).post(
            "/team/market",
            base_url=self.origin,
            data={"csrf_token": "test-only", "api_key": provider_key, "generation": str(version)}
            | form,
            headers={"Origin": self.origin} if headers is None else headers,
        )

    def test_only_owner_over_https_and_with_csrf_can_edit(self):
        self.assertEqual(self.tester.get("/team/market", base_url=self.origin).status_code, 403)
        self.assertEqual(self.post(self.discovery, client=self.tester).status_code, 403)
        self.assertEqual(self.post(self.discovery | {"csrf_token": "wrong"}).status_code, 403)
        self.assertEqual(
            self.post(self.discovery, headers={"Origin": "https://elsewhere.test"}).status_code, 403
        )
        self.assertEqual(self.post(self.discovery | {controls.DISCOVERY: "1"}).status_code, 400)
        self.assertEqual(
            self.post(
                self.discovery | {controls.DISCOVERY: "1", controls.DAILY: "0"},
                provider_key=self.scavio,
            ).status_code,
            400,
        )
        with self.app.app_context():
            self.assertEqual(
                get_db().execute("SELECT count(*) FROM team_market_settings").fetchone()[0], 0
            )

    def test_saving_key_has_no_network_or_enabled_spend_and_survives_restart(self):
        with patch("requests.post") as http:
            self.assertEqual(self.post(self.discovery, provider_key=self.scavio).status_code, 303)
            html = self.owner.get("/team/market", base_url=self.origin).text
            self.assertIn("Public saved searches · Off", html)
            self.assertNotIn(self.scavio, html)
            for client in (self.tester, self.owner):
                self.assertNotIn(self.scavio, client.get("/api/state", base_url=self.origin).text)
            http.assert_not_called()
        self.assertEqual(
            self.post(
                self.discovery | {controls.DISCOVERY: "1"}, version=1
            ).status_code,
            303,
        )
        self.assertEqual(self.post(self.discovery, version=1).status_code, 400)
        with create_app(self.cfg).app_context():
            db = get_db()
            row = db.execute(
                "SELECT * FROM team_market_settings WHERE provider='scavio'"
            ).fetchone()
            self.assertNotIn(self.scavio, repr(tuple(row)))
            config = market_runtime.config_for(db, self.app.config)
            self.assertEqual(config["PILOT_SCAVIO_API_KEY"], self.scavio)
            self.assertEqual(public_feed.limits(config), (True, 10, 5))
            item = {
                "title": "Vintage wool coat",
                "id": "123",
                "seller_id": "seller",
                "condition": "very good",
                "currency": "GBP",
            }
            with patch("pilot.market._search", return_value=[]) as fetch:
                market.comparisons_for(db, item)
                fetch.assert_not_called()
            self.assertEqual(db.execute("SELECT used FROM market_budget").fetchone()[0], 0)
        self.assertEqual(self.post(self.discovery, version=2).status_code, 303)
        with self.app.app_context(), patch("pilot.market._search") as fetch:
            market.comparisons_for(
                get_db(),
                {
                    "title": "Second wool coat",
                    "id": "124",
                    "seller_id": "seller",
                    "condition": "good",
                },
            )
            fetch.assert_not_called()

    def test_legacy_ai_settings_cannot_activate_a_provider(self):
        self.assertEqual(self.post({"provider":"mistral"}, provider_key=self.mistral).status_code, 400)
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO team_market_settings VALUES('mistral',?,NULL,1)",
                       (json.dumps({"PILOT_MISTRAL_ENABLED":"1"}),))
            cfg = market_runtime.config_for(db, {"PILOT_MISTRAL_API_KEY": self.mistral,
                                                  "PILOT_SCAVIO_ENABLED":"1"})
            self.assertEqual(cfg["PILOT_MISTRAL_API_KEY"], "")
            self.assertEqual(cfg["PILOT_MISTRAL_ENABLED"], "0")
            self.assertEqual(cfg["PILOT_SCAVIO_ENABLED"], "0")
        self.assertNotIn("Mistral", self.owner.get("/team/market", base_url=self.origin).text)

    def test_changed_encryption_key_disables_encrypted_credentials(self):
        self.assertEqual(
            self.post(
                self.discovery | {controls.DISCOVERY: "1"}, provider_key=self.scavio
            ).status_code,
            303,
        )
        other = create_app(self.cfg | {"FINDER_PILOT_CONFIG_KEY": Fernet.generate_key().decode()})
        with other.app_context():
            cfg = market_runtime.config_for(get_db(), other.config)
            self.assertFalse(cfg["PILOT_SCAVIO_API_KEY"])
            self.assertFalse(public_feed.limits(cfg)[0])


if __name__ == "__main__":
    unittest.main()
