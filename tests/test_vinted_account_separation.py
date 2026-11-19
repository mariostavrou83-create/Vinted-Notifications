"""Real encrypted SQLite storage and owner/CSRF routes; no marketplace writes."""

import hashlib
import time
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

import requests
import test_dashboard
import test_vinted_buying as buying_tests
from test_search_controls import DatabaseFixture

import search_settings
import vinted_accounts as accounts
import vinted_buyer as buyer
import vinted_offers as offers
import vinted_order_messages as messages
import vinted_seller as seller
import vinted_session_worker as worker
from web_ui_plugin.web_ui import create_app
from vinted_http import BROWSER_USER_AGENT

ACCESS = "offline-new-access-token-0123456789"
REFRESH = "offline-new-refresh-token-0123456789"


class AccountSeparationTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.original = buyer.encrypt({"cookies": {"access_token_web": "old-access", "refresh_token_web": "old-refresh"}})
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET session=?,verified_at=1,user_id='111',username='old-seller',enabled=1,max_total=2000,max_extra=350,preferred_card_last4='1234' WHERE id=1", (self.original,))
        messages.ensure_schema()
        self.candidate = Mock()
        self.candidate.identity.return_value = ("222", "new-buyer")
        self.candidate.exported.return_value = {"cookies": {"access_token_web": ACCESS, "refresh_token_web": REFRESH}}

    def connect(self, username="new-buyer"):
        with patch.object(buyer, "Client", return_value=self.candidate):
            return buyer.connect_separate_buyer(username, access_token=ACCESS, refresh_token=REFRESH)

    def row(self, table="vinted_buyer"):
        self.assertIn(table, ("vinted_buyer", "vinted_seller", "vinted_buyer_maintenance", "vinted_seller_maintenance"))
        with closing(search_settings.connection()) as conn:
            return dict(conn.execute(f"SELECT * FROM {table} WHERE id=1").fetchone())

    def test_new_buyer_keeps_original_seller_offers_messages_budgets_and_session(self):
        offers.configure("preview", "half_pound", "today")
        messages.configure("preview", messages.DEFAULTS)
        old_offers = offers.state()
        old_messages = messages.state()
        self.assertFalse(accounts.separated())
        self.connect()
        self.assertTrue(accounts.separated())
        buying, selling = self.row(), self.row("vinted_seller")
        self.assertEqual((buying["user_id"], selling["user_id"]), ("222", "111"))
        self.assertEqual(selling["session"], self.original)
        self.assertEqual(seller.settings()["username"], "old-seller")
        self.assertEqual(buyer.settings()["username"], "new-buyer")
        self.assertEqual((buying["enabled"], buying["max_total"], buying["max_extra"]), (0, 2000, 350))
        self.assertEqual(buying["preferred_card_last4"], "")
        self.assertEqual(offers.state(), old_offers)
        self.assertEqual(messages.state(), old_messages)
        offers.configure("preview", "half_pound", "today")
        messages.configure("preview", messages.DEFAULTS)
        self.assertEqual(offers.state()["account_id"], "111")
        self.assertEqual(messages.state()["account_id"], "111")
        self.candidate.homepage.assert_called_once()
        self.candidate.identity.assert_called_once()
        self.candidate.request.assert_not_called()
        self.candidate.session.close.assert_called_once()

    def test_failed_identity_keeps_both_connections_and_enabling_permission(self):
        self.candidate.identity.side_effect = buyer.BuyerError("offline failure", status=403, reason="forbidden")
        with self.assertRaises(buyer.BuyerError):
            self.connect()
        self.assertFalse(accounts.separated())
        self.assertEqual((self.row()["session"], self.row()["enabled"]), (self.original, 1))
        self.candidate.request.assert_not_called()

    def test_wrong_username_and_selling_account_cannot_replace_buyer(self):
        self.candidate.identity.return_value = ("333", "wrong-buyer")
        with self.assertRaises(buyer.BuyerError):
            self.connect()
        self.assertFalse(accounts.separated())
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET login_attempt=0 WHERE id=1")
        self.candidate.identity.return_value = ("111", "old-seller")
        with self.assertRaises(buyer.BuyerError):
            self.connect("old-seller")
        self.assertFalse(accounts.separated())
        self.assertEqual(self.row()["session"], self.original)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM vinted_seller").fetchone()[0], 0)

    def test_unconfirmed_payment_blocks_switch_before_any_connection_request(self):
        for state in ("paying", "unknown", "needs_action"):
            with self.subTest(state=state), closing(search_settings.connection()) as conn, conn:
                conn.execute("INSERT OR REPLACE INTO vinted_buy_attempts(item_id,state,message,updated) VALUES ('123',?,'offline',1)", (state,))
                conn.commit()
                with patch.object(buyer, "Client") as factory, self.assertRaisesRegex(buyer.BuyerError, "unconfirmed payment"):
                    buyer.connect_separate_buyer("new-buyer", access_token=ACCESS, refresh_token=REFRESH)
                factory.assert_not_called()
        self.assertEqual(self.row()["session"], self.original)

    def test_later_buyer_switch_never_replaces_selling_connection(self):
        self.connect()
        original_seller = self.row("vinted_seller")
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET login_attempt=0 WHERE id=1")
        self.candidate.identity.return_value = ("333", "another-buyer")
        self.connect("another-buyer")
        self.assertEqual(self.row("vinted_seller"), original_seller)
        self.assertEqual(self.row()["user_id"], "333")

    def test_returned_seller_client_persists_only_selling_rotations(self):
        self.connect()
        buyer_before = self.row()
        old_saved = buyer.decrypt(self.original)
        with accounts.account_scope("seller"), patch.object(buyer, "BrowserSession", requests.Session):
            client = buyer.Client(old_saved)
            client.bind_verified_session("111", self.original, client.exported())
        try:
            client.session.cookies.set("access_token_web", "rotated-old-seller-access", domain="www.vinted.co.uk", path="/")
            client.persist_session()
            self.assertEqual(self.row(), buyer_before)
            self.assertNotEqual(self.row("vinted_seller")["session"], self.original)
            self.assertEqual(accounts.current_role(), "buyer")
            with self.assertRaisesRegex(buyer.BuyerError, "selling connection"):
                client.request("POST", "/api/v2/purchases/123/payments", {})
        finally:
            client.session.close()

    def test_role_auth_and_maintenance_do_not_overwrite_buyer(self):
        self.connect()
        before = self.row("vinted_buyer_maintenance")
        seller.record_connection_result("buyer_session", "forbidden", "identity", 403)
        self.assertEqual(buyer.public_auth_results()["buyer_session"]["http_status"], 200)
        self.assertEqual(seller.public_auth_results()["buyer_session"]["http_status"], 403)
        fingerprint = hashlib.sha256(self.row("vinted_seller")["session"]).hexdigest()
        with accounts.account_scope("seller"):
            worker.record_renewal_result(buyer.BuyerError("offline", reason="refresh_rejected", stage="renewal", status=401), session_fingerprint=fingerprint)
        self.assertEqual(self.row("vinted_buyer_maintenance"), before)
        self.assertEqual(self.row("vinted_seller_maintenance")["blocked"], 1)
        self.assertEqual(accounts.current_role(), "buyer")

    def test_preparation_pauses_only_buying_once_and_preserves_settings(self):
        offers.configure("preview", "half_pound", "today")
        old_offers = offers.state()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DELETE FROM parameters WHERE key='vinted_separate_accounts_prepared_v1'")
            accounts.migrate(conn)
        self.assertEqual(self.row()["enabled"], 0)
        self.assertEqual(self.row()["session"], self.original)
        self.assertEqual(self.row()["max_total"], 2000)
        self.assertEqual(offers.state(), old_offers)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET enabled=1 WHERE id=1")
            accounts.migrate(conn)
        self.assertEqual(self.row()["enabled"], 1)

    def test_seller_factory_uses_seller_scope_and_restores_caller(self):
        self.connect()
        seen = []
        def fake(**kwargs):
            seen.append(accounts.current_role())
            return self.candidate
        with patch.object(buyer, "connected_client", side_effect=fake):
            self.assertIs(seller.connected_client(solve_challenges=False), self.candidate)
        self.assertEqual(seen, ["seller"])
        self.assertEqual(accounts.current_role(), "buyer")

    def records(self, prefix):
        return [
            {
                "name": name,
                "value": prefix + suffix,
                "domain": ".vinted.co.uk",
                "path": "/",
                "secure": True,
                "expires": int(time.time()) + 3600,
                "domain_specified": True,
                "domain_initial_dot": True,
                "path_specified": True,
                "discard": False,
            }
            for name, suffix in (("access_token_web", "-access-token-0123456789"), ("refresh_token_web", "-refresh-token-0123456789"))
        ]

    def native_cookie_transport(self, adapter, prepared, **kwargs):
        from urllib.parse import urlsplit

        path = urlsplit(prepared.url).path
        self.native_calls.append((prepared.method, path))
        self.assertEqual(prepared.method, "GET")
        self.assertIsNone(prepared.body)
        self.assertEqual(prepared.headers["User-Agent"], BROWSER_USER_AGENT)
        self.assertEqual(kwargs["proxies"]["https"], self.native_proxy)
        self.assertTrue(kwargs["verify"])
        response = buying_tests.NativeCookieTransportTests.transport_response
        if path == "/":
            return response(self, prepared, text='<meta name="csrf-token" content="offline-csrf-token-0123456789">')
        self.assertEqual(path, "/api/v2/users/current")
        access = prepared.headers.get("Cookie", "")
        user = {"id": 222, "login": "new-buyer"} if "new-buying-access" in access else {"id": 111, "login": "old-seller"}
        return response(self, prepared, {"user": user})

    def native_cookie_context(self):
        from contextlib import ExitStack

        self.native_proxy = "http://offline-user:offline-secret@proxy.example.test:8080"
        self.native_calls = []
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET network=? WHERE id=1", (buyer.encrypt({"proxy": self.native_proxy, "api_key": "", "enabled": False}),))
        context = ExitStack()
        def native_session():
            session = requests.Session()
            session.headers["User-Agent"] = BROWSER_USER_AGENT
            return session
        context.enter_context(patch.object(buyer, "BrowserSession", side_effect=native_session))
        context.enter_context(patch.object(requests.adapters.HTTPAdapter, "send", autospec=True, side_effect=self.native_cookie_transport))
        return context

    def test_extension_records_connect_new_buyer_with_original_scope_and_transport(self):
        records = self.records("new-buying")
        with self.native_cookie_context():
            buyer.connect_separate_buyer("new-buyer", cookie_records=records)
        stored = buyer.decrypt(self.row()["session"])
        self.assertEqual(self.row("vinted_seller")["session"], self.original)
        for old, saved in zip(records, stored["cookie_records"], strict=True):
            for key in old:
                self.assertEqual(saved[key], old[key])
        self.assertEqual(self.native_calls, [("GET", "/"), ("GET", "/api/v2/users/current")])
        self.assertEqual(self.row()["user_id"], "222")
        self.assertEqual(self.row()["enabled"], 0)

    def test_seller_extension_refresh_preserves_buyer_and_resumes_same_account_offers(self):
        offers.configure("live", "half_pound", "today", now=time.time() - 60)
        with self.native_cookie_context():
            buyer.connect_separate_buyer("new-buyer", cookie_records=self.records("new-buying"))
            before = self.row()
            offers.pause("restricted")
            with closing(search_settings.connection()) as conn, conn:
                conn.execute("UPDATE vinted_seller SET login_attempt=0 WHERE id=1")
            seller.link_browser_session_records(self.records("selling-refreshed"))
            with closing(search_settings.connection()) as conn, conn:
                offers._recover_auth(conn, time.time() + 1)
        self.assertEqual(self.row(), before)
        self.assertEqual(self.row("vinted_seller")["user_id"], "111")
        self.assertEqual(offers.state()["account_id"], "111")
        self.assertEqual(offers.state()["mode"], "live")
        self.assertTrue(all(method == "GET" for method, _ in self.native_calls))

    def test_reconnect_rejects_other_accounts_cookies_and_keeps_both_sessions(self):
        with self.native_cookie_context():
            buyer.connect_separate_buyer("new-buyer", cookie_records=self.records("new-buying"))
            before = (self.row(), self.row("vinted_seller"))
            with closing(search_settings.connection()) as conn, conn:
                conn.execute("UPDATE vinted_buyer SET login_attempt=0 WHERE id=1")
                conn.execute("UPDATE vinted_seller SET login_attempt=0 WHERE id=1")
            with self.assertRaises(buyer.BuyerError):
                buyer.link_browser_session_records(self.records("selling-refreshed"))
            with self.assertRaises(buyer.BuyerError):
                seller.link_browser_session_records(self.records("new-buying"))
        self.assertEqual(self.row()["session"], before[0]["session"])
        self.assertEqual(self.row("vinted_seller")["session"], before[1]["session"])
        self.assertTrue(all(method == "GET" for method, _ in self.native_calls))


class AccountRoutesTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()

    def test_account_actions_require_owner_and_csrf(self):
        self.client.get("/connections")
        with self.client.session_transaction() as session:
            csrf = session["csrf"]
        for action in ("buyer_switch_account", "seller_recheck", "seller_session_records"):
            with patch.object(buyer, "connect_separate_buyer") as connect:
                response = self.client.post("/connections", data={"action": action, "csrf": csrf})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, "/login")
                connect.assert_not_called()
        test_dashboard.DashboardTests.owner(self)
        for action in ("buyer_switch_account", "seller_recheck", "seller_session_records"):
            self.assertEqual(self.client.post("/connections", data={"action": action}).status_code, 400)

    def test_owner_switch_redirects_and_does_not_echo_credentials(self):
        test_dashboard.DashboardTests.owner(self)
        with patch.object(buyer, "connect_separate_buyer", return_value="New buying account verified.") as connect:
            response = self.client.post("/connections", data={
                "csrf": "offline-csrf", "action": "buyer_switch_account", "new_buyer_username": "new-buyer",
                "new_buyer_access_token": ACCESS, "new_buyer_refresh_token": REFRESH,
            })
        self.assertEqual(response.status_code, 303)
        connect.assert_called_once_with("new-buyer", cookie_records=None, access_token=ACCESS, refresh_token=REFRESH)
        page = self.client.get("/connections").data
        self.assertNotIn(ACCESS.encode(), page)
        self.assertNotIn(REFRESH.encode(), page)
        self.assertIn(b"Vinted selling account", page)
        self.assertIn(b"Vinted buying account", page)
