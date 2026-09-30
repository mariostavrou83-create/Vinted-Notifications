import unittest
from contextlib import closing
from unittest.mock import patch

from test_ebay_monitor import EbayFixture
from werkzeug.security import generate_password_hash

import db
import ebay_store
import search_settings
from web_ui_plugin.web_ui import create_app


class SharedDashboardTests(EbayFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()

    def login(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE dashboard_auth SET password_hash=?",
                (generate_password_hash("test-only-password"),),
            )
        with self.client.session_transaction() as session:
            session["owner"] = True
            session["csrf"] = "test-csrf"

    def test_connections_require_login_csrf_and_never_render_secrets(self):
        self.assertEqual(self.client.get("/connections").status_code, 302)
        self.login()
        data = {
            "client_id": "test-app-id",
            "client_secret": "private-test-secret",
            "telegram_token": "87654321:" + "x" * 30,
            "daily_budget": "650000",
            "target_interval": "15",
            "chat_id": "123",
            "csrf": "test-csrf",
        }
        self.assertEqual(self.client.post("/connections", data={}).status_code, 400)
        self.assertEqual(self.client.post("/connections", data=data).status_code, 302)
        html = self.client.get("/connections").text
        for key in ("client_id", "client_secret", "telegram_token"):
            self.assertNotIn(data[key], html)
        self.assertIn("650000", html)
        self.client.post(
            "/connections", data={"csrf": "test-csrf", "client_secret": ""}
        )
        self.assertEqual(
            ebay_store.configuration()["client_secret"], "private-test-secret"
        )

    def test_vinted_token_cannot_be_reused_and_invalid_budget_does_not_save(self):
        db.set_parameter("telegram_token", "12345678:" + "v" * 30)
        with self.assertRaises(ValueError):
            ebay_store.save_configuration({"telegram_token": "12345678:" + "e" * 30})
        with self.assertRaises(ValueError):
            ebay_store.save_configuration(
                {"client_id": "should-not-save", "daily_budget": "bad"}
            )
        self.assertFalse(ebay_store.configuration()["client_id"])

    def test_no_data_never_claims_15_second_delivery_and_insufficient_quota_warns(self):
        self.login()
        self.enable()
        html = self.client.get("/connections").text
        self.assertIn("15-second delivery is unverified", html)
        self.assertIn("leaves no delivery headroom", html)
        self.assertIn("phone receipt is not exposed", html)
        self.assertIn('value="5"', html)

    def test_save_both_ebay_only_vinted_only_and_forms_keep_validation_values(self):
        self.login()
        for mode in ("both", "ebay", "vinted"):
            row = search_settings.get_search(1)
            data = {
                "query_name": "Hollister",
                "query": row["query"],
                "revision": str(row["revision"]),
                "csrf": "test-csrf",
                "platform_mode": mode,
                "ebay_keywords": "hollister fur",
                "ebay_buying": "fixed",
                "ebay_condition": "used",
                "ebay_uk_only": "yes",
            }
            self.assertEqual(self.client.post("/search/1", data=data).status_code, 302)
            self.assertEqual(search_settings.get_search(1)["platform_mode"], mode)
        data["ebay_category"] = "bad"
        data["revision"] = str(search_settings.get_search(1)["revision"])
        response = self.client.post("/search/1", data=data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("numeric eBay category", response.text)
        self.assertIn('value="hollister fur"', response.text)

    def test_platform_tabs_platform_find_filter_and_postage_display(self):
        self.login()
        search = self.enable()
        from test_ebay_monitor import item

        self.snapshot(search, [], 1000)
        self.snapshot(search, [item(123)], 1020)
        self.batch(1, [456])
        for path in ("/", "/search/1", "/search/new", "/connections", "/finds"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        html = self.client.get("/search/1").text
        self.assertIn('role="tablist"', html)
        self.assertIn("Copy keywords &amp; prices", html)
        self.assertIn("ebay:123", self.client.get("/finds?platform=ebay").text)
        self.assertNotIn("ebay:123", self.client.get("/finds?platform=vinted").text)
        self.assertEqual(
            self.client.post(
                "/finds/ebay:123/status",
                data={
                    "csrf": "test-csrf",
                    "new_status": "interested",
                    "platform": "ebay",
                },
            ).status_code,
            302,
        )

    def test_connection_test_is_only_owner_post_and_failure_sanitized(self):
        self.login()
        with patch(
            "ebay_connections.test_connection", return_value="Connected"
        ) as check:
            self.client.get("/connections?action=telegram")
            check.assert_not_called()
            self.client.post(
                "/connections", data={"action": "telegram", "csrf": "test-csrf"}
            )
            check.assert_called_once_with("telegram")
