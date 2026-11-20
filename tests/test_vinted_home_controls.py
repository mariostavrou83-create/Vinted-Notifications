import unittest
from contextlib import closing

import test_dashboard
from test_search_controls import DatabaseFixture

import db
import search_settings
from web_ui_plugin.web_ui import create_app


class HomeControlsTests(DatabaseFixture, unittest.TestCase):
    owner = test_dashboard.DashboardTests.owner

    def setUp(self):
        super().setUp()
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()

    def test_owner_and_csrf_required_and_searches_unchanged(self):
        form = {"action": "home_discovery", "home_enabled": "1", "csrf": "offline-csrf"}
        with self.client.session_transaction() as session:
            session["csrf"] = "offline-csrf"
        self.assertEqual(self.client.post("/connections", data=form).status_code, 302)
        self.assertNotEqual(db.get_parameter("home_monitor_enabled"), "1")
        self.owner()
        self.assertEqual(
            self.client.post(
                "/connections", data={**form, "csrf": "wrong"}
            ).status_code,
            400,
        )
        before = db.get_queries()
        with closing(search_settings.connection()) as conn:
            budget = list(conn.execute("SELECT * FROM vinted_search_budgets"))
        self.assertEqual(self.client.post("/connections", data=form).status_code, 302)
        self.assertEqual(db.get_parameter("home_monitor_enabled"), "1")
        self.assertEqual(db.get_queries(), before)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                list(conn.execute("SELECT * FROM vinted_search_budgets")), budget
            )
        self.assertEqual(
            self.client.post(
                "/connections", data={**form, "home_enabled": "0"}
            ).status_code,
            302,
        )
        self.assertEqual(db.get_parameter("home_monitor_enabled"), "0")

    def test_summary_no_raw_status_error_leak(self):
        import vinted_home_controls as controls

        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO parameters VALUES('home_monitor_status_seller',?)",
                (
                    '{"code":"PRIVATE_ERROR", "checked_at":"bad", "alerts":"PRIVATE_VALUE"}',
                ),
            )
        result = str(controls.summary())
        self.assertNotIn("PRIVATE", result)
