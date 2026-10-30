"""Owner imports retain browser cookie scope; every HTTP request is offline."""

import json
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import requests
import test_vinted_buying as buying_tests
from test_search_controls import DatabaseFixture
from werkzeug.security import generate_password_hash

import db
import search_settings
import vinted_buyer as buyer
import vinted_offers as offers
from web_ui_plugin.web_ui import create_app

NOW = 1793386800
SECRET = "private-original-cookie-0123456789"


def records(domain=".vinted.co.uk"):
    return [
        {
            "name": name, "value": SECRET + name,
            "domain": domain, "path": "/", "secure": True,
            "expires": NOW + 3600, "domain_specified": domain.startswith("."),
            "domain_initial_dot": domain.startswith("."),
            "path_specified": True, "discard": False,
        }
        for name in ("access_token_web", "refresh_token_web")
    ]


class ManualCookieImportTests(buying_tests.SessionRotationFixture, unittest.TestCase):
    transport_response = buying_tests.NativeCookieTransportTests.transport_response

    def setUp(self):
        super().setUp()
        for mocked in (
            patch.object(buyer, "BrowserSession", requests.Session),
            patch.object(buyer.time, "time", return_value=NOW),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET network=?,verified_at=?,pickup_mode='nearest',"
                "preferred_card_last4='1234',max_total=2000,max_extra=350 WHERE id=1",
                (buyer.encrypt({"proxy": "", "api_key": "", "enabled": False}), NOW - 100),
            )
            conn.execute(
                "INSERT INTO vinted_buy_attempts(item_id,state,checkout_id,message,updated) "
                "VALUES ('123','unknown','existing-checkout','Check Vinted',?)",
                (NOW - 10,),
            )
        self.calls = []
        self.identity_user = 99
        self.before_identity = None

    def native(self, adapter, prepared, **kwargs):
        self.calls.append((prepared.method, prepared.url))
        self.assertEqual(prepared.method, "GET")
        self.assertIsNone(prepared.body)
        if prepared.url == buyer.BASE + "/":
            return self.transport_response(
                prepared, text='<meta name="csrf-token" content="offline-csrf-token-0123456789">'
            )
        self.assertEqual(prepared.url, buyer.BASE + "/api/v2/users/current")
        if self.before_identity:
            self.before_identity()
        return self.transport_response(prepared, {"user": {"id": self.identity_user, "login": "owner"}})

    def link(self, value=None):
        with patch.object(requests.adapters.HTTPAdapter, "send", autospec=True, side_effect=self.native):
            return buyer.link_browser_session_records(records() if value is None else value)

    def controls(self):
        with closing(search_settings.connection()) as conn:
            return (
                tuple(conn.execute(
                    "SELECT user_id,enabled,max_total,max_extra,pickup_mode,preferred_card_last4,network "
                    "FROM vinted_buyer WHERE id=1"
                ).fetchone()),
                [tuple(row) for row in conn.execute("SELECT * FROM vinted_buy_attempts")],
                [tuple(row) for row in conn.execute("SELECT * FROM vinted_search_budgets")],
            )

    def test_original_domain_path_flags_and_permission_are_preserved(self):
        for enabled in (0, 1):
            with self.subTest(enabled=enabled):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("UPDATE vinted_buyer SET enabled=?,login_attempt=0", (enabled,))
                before = self.controls()
                text = self.link()
                self.assertEqual(self.saved()[1]["cookie_records"], records())
                self.assertEqual(self.controls(), before)
                self.assertIn("applies to Vinted's API host", text)
                self.assertIn("does not verify checkout or payment", text)
                self.assertNotIn(SECRET, text)
                with closing(search_settings.connection()) as conn:
                    event = conn.execute("SELECT kind,buyer_id FROM vinted_buyer_event WHERE channel='connection'").fetchone()
                self.assertEqual(tuple(event), ("reconnected", "99"))
        self.assertEqual(self.calls, [("GET", buyer.BASE + "/"), ("GET", buyer.BASE + "/api/v2/users/current")] * 2)
        self.assertNotIn(SECRET.encode(), Path(db.DB_PATH).read_bytes())

    def test_www_only_original_scope_is_retained_and_limitation_reported(self):
        original = records("www.vinted.co.uk")
        text = self.link(original)
        self.assertEqual(self.saved()[1]["cookie_records"], original)
        self.assertIn("does not apply to Vinted's API host", text)
        self.assertIn("original scope was preserved", text)

    def test_scoped_import_recovers_only_safe_offers_and_never_sends_a_marketplace_write(self):
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("UPDATE vinted_buyer SET login_attempt=0")
                    conn.execute("DELETE FROM vinted_offer_jobs")
                    conn.execute("DELETE FROM vinted_offer_events")
                offers.configure("live", "half_pound", "three_months", now=NOW - 100)
                with patch.object(offers.time, "time", return_value=NOW - 50):
                    if uncertain:
                        with closing(search_settings.connection()) as conn, conn:
                            job_id = conn.execute(
                                "INSERT INTO vinted_offer_jobs "
                                "(account_id,item_id,recipient_id,notification_id,liked_at,created,status,reason) "
                                "VALUES ('99','321','456','offline',?,?,'uncertain','uncertain')",
                                (NOW - 200, NOW - 100),
                            ).lastrowid
                            offers.event(conn, "uncertain", "uncertain", job_id)
                    offers.pause("restricted")
                start = len(self.calls)
                self.link()
                offers._recover(NOW + 1)
                self.assertEqual(offers.state()["mode"], "paused" if uncertain else "live")
                self.assertTrue(buyer.settings()["enabled"])
                self.assertEqual(self.calls[start:], [
                    ("GET", buyer.BASE + "/"),
                    ("GET", buyer.BASE + "/api/v2/users/current"),
                ])
                if uncertain:
                    with patch.object(buyer, "connected_client") as factory:
                        offers.OfferWorker(clock=lambda: NOW + 1).tick()
                    factory.assert_not_called()

    def test_wrong_buyer_cannot_replace_saved_session_or_permission(self):
        before = self.saved()[0]["session"], self.controls()
        self.identity_user = 101
        with self.assertRaises(buyer.BuyerError):
            self.link()
        self.assertEqual((self.saved()[0]["session"], self.controls()), before)

    def test_changed_session_during_verification_cannot_be_overwritten(self):
        replacement = buyer.encrypt({"cookies": {"access_token_web": "separately-reconnected-0123456789"}})

        def replace():
            with closing(search_settings.connection()) as conn, conn:
                conn.execute("UPDATE vinted_buyer SET session=?", (replacement,))

        self.before_identity = replace
        with self.assertRaises(buyer.BuyerError):
            self.link()
        self.assertEqual(self.saved()[0]["session"], replacement)

    def test_missing_existing_buyer_does_not_create_a_connection(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET session=NULL,verified_at=NULL")
        with self.assertRaises(buyer.BuyerError):
            self.link()
        self.assertFalse(self.calls)

    def test_parser_rejects_ambiguity_oversize_and_non_auth_cookies_privately(self):
        invalid = [
            "", SECRET, "[" * 10000, "x" * 32769,
            '[{"name":"access_token_web","name":"datadome"}]',
            json.dumps(records() + [{"name": "datadome", "value": SECRET}]),
            json.dumps({"cookies": records()}),
            json.dumps([{**row, "domain": "api.vinted.co.uk"} for row in records()]),
            json.dumps([{**row, "password": SECRET} for row in records()]),
        ]
        for raw in invalid:
            with self.subTest(size=len(raw)):
                with self.assertRaises(buyer.BuyerError) as error:
                    buyer.parse_browser_session_records(raw)
                self.assertNotIn(SECRET, str(error.exception))
        self.assertFalse(self.calls)

    def test_parser_preserves_exact_original_records(self):
        original = records()
        self.assertEqual(buyer.parse_browser_session_records(json.dumps(original)), original)


class ManualCookieImportRouteTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE dashboard_auth SET password_hash=?", (generate_password_hash("offline owner password"),))

    def owner(self):
        with self.client.session_transaction() as session:
            session["owner"] = True
            session["csrf"] = "offline-cookie-csrf"

    def form(self, **extra):
        return {"action": "buyer_session_records", "csrf": "offline-cookie-csrf",
                "buyer_session_records": json.dumps(records()), **extra}

    def test_owner_and_csrf_required_before_import(self):
        with patch.object(buyer, "link_browser_session_records") as link:
            self.client.post("/connections", data=self.form())
            self.owner()
            self.assertEqual(self.client.post("/connections", data=self.form(csrf="wrong")).status_code, 400)
        link.assert_not_called()

    def test_original_records_reach_verifier_and_redirect_drops_private_values(self):
        self.owner()
        with patch.object(buyer, "link_browser_session_records", return_value="Same buyer verified") as link:
            response = self.client.post("/connections", data=self.form())
        self.assertEqual(response.status_code, 303)
        link.assert_called_once_with(records())
        rendered = self.client.get(response.location).get_data(as_text=True)
        self.assertNotIn(SECRET, rendered)
        with self.client.session_transaction() as session:
            self.assertNotIn(SECRET, str(dict(session)))

    def test_invalid_records_redirect_without_network_or_private_echo(self):
        self.owner()
        for raw in (SECRET, "x" * 32769, json.dumps(records() + [{"name": "datadome", "value": SECRET}])):
            with patch.object(buyer, "link_browser_session_records") as link:
                response = self.client.post("/connections", data=self.form(buyer_session_records=raw))
            self.assertEqual(response.status_code, 303)
            link.assert_not_called()
            self.assertNotIn(SECRET, self.client.get(response.location).get_data(as_text=True))
