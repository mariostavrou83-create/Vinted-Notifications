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

    @patch.dict("os.environ", {"FINDER_EBAY_ALERTS": "0"})
    def test_retired_ebay_keeps_vinted_controls_and_blocks_listing_actions(self):
        self.login()
        for path in ("/", "/connections", "/search/new", "/search/1"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertNotIn('href="#ebay-connection"', response.text)
            self.assertNotIn('id="ebay_search_url"', response.text)
            self.assertNotIn('id="tab-ebay"', response.text)
            self.assertNotIn('value="test_ebay_photos"', response.text)
            self.assertNotIn('/preview-ebay', response.text)
        self.assertIn('name="query"', self.client.get("/search/new").text)
        for path in ("/search/1/check-ebay", "/search/1/preview-ebay", "/ebay/import-search"):
            self.assertEqual(self.client.post(path, data={"csrf": "test-csrf"}).status_code, 410)
        for action in ("save", "ebay", "telegram", "live_searches", "test_ebay_photos"):
            response = self.client.post("/connections", data={"csrf": "test-csrf", "action": action})
            self.assertEqual(response.status_code, 410, action)

    @patch.dict("os.environ", {"FINDER_EBAY_ALERTS": "0"})
    def test_retirement_keeps_mixed_search_settings_and_only_activates_vinted(self):
        previous = {"vinted_enabled": 1, "ebay_enabled": 1,
                    "ebay": dict(ebay_store.DEFAULTS, search_url="https://www.ebay.co.uk/sch/example")}
        self.assertEqual(ebay_store.parse_form({"platform_mode": "both"}, previous), (1, 0, previous["ebay"]))
        vinted, ebay, config = ebay_store.parse_shared_form(
            {"platform_mode": "both", "shared_keywords": "bomber"}, previous, maximum=1500)
        self.assertEqual((vinted, ebay), (1, 0))
        self.assertEqual(config["search_url"], previous["ebay"]["search_url"])
        self.assertEqual(config["shared_keywords"], ["bomber"])

    @patch.dict("os.environ", {"FINDER_EBAY_ALERTS": "0"})
    def test_retired_worker_uses_existing_maintenance_without_polling(self):
        import ebay_monitor
        import vinted_notifications as service

        with patch.object(service.db, "get_parameter", return_value=None), \
             patch.object(service, "ebay_worker_process", None), \
             patch.object(service.multiprocessing, "Process") as create_process:
            service._monitor_processes(None, None, None)
        create_process.assert_called_once_with(target=service.ebay_history_process, name="ebay-history")
        with patch.object(ebay_monitor, "run_delivery") as maintenance, \
             patch.object(ebay_monitor, "Poller") as poller:
            service.ebay_history_process()
        maintenance.assert_awaited_once()
        poller.assert_not_called()
        self.assertEqual(ebay_store.live_ids(None), [])

    def test_retiring_a_search_suppresses_its_already_queued_listing(self):
        import asyncio
        import alert_delivery
        from test_ebay_monitor import item
        from test_photo_cards import bot

        previous = self.enable()
        self.snapshot(previous, [], 1000)
        self.snapshot(previous, [item(123)], 1020)
        self.assertEqual(self.outbox()[0]["status"], "pending")
        fake_bot = bot()
        worker = alert_delivery.EbayPhotoDeliveryWorker(fake_bot, "123", bot_id="12345")
        with patch.dict("os.environ", {"FINDER_EBAY_ALERTS": "0"}):
            self.assertTrue(asyncio.run(worker.tick(now=1030)))
        fake_bot.send_photo.assert_not_awaited()
        fake_bot.send_message.assert_not_awaited()
        self.assertEqual(self.outbox()[0]["status"], "cancelled")
        self.assertEqual(self.outbox()[0]["error"], "Search no longer active for eBay")

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
        self.assertNotIn("650000", html)
        self.assertEqual(ebay_store.configuration()["daily_budget"], 650000)
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
        self.assertEqual(ebay_store.connection_summary()["timing"]["sent"], 0)
        self.assertNotIn("eBay checking speed", html)
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
        self.assertNotIn('name="ebay_keywords"', response.text)

    def test_vinted_search_editor_and_historical_find_filters(self):
        self.login()
        search = self.enable()
        from test_ebay_monitor import item

        self.snapshot(search, [], 1000)
        self.snapshot(search, [item(123)], 1020)
        self.batch(1, [456])
        for path in ("/", "/search/1", "/search/new", "/connections", "/finds"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        html = self.client.get("/search/1").text
        self.assertNotIn('role="tablist"', html)
        self.assertNotIn("Copy keywords &amp; prices", html)
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
