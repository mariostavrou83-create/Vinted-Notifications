"""Allowance response fixtures, real persistence and owner-dashboard integration."""

import unittest
from contextlib import closing
from unittest.mock import Mock, patch

import requests
import test_ebay_dashboard
from test_ebay_monitor import EbayFixture

import ebay_connections
import ebay_quota
import ebay_store
from search_settings import connection
from web_ui_plugin.web_ui import create_app


def response_payload(limit=5000, remaining=4000, extra=None):
    resources = [
        {
            "name": "buy.browse",
            "rates": [
                {
                    "limit": limit,
                    "remaining": remaining,
                    "count": limit - remaining,
                    "timeWindow": 86400,
                    "reset": "2026-10-01T07:00:00.000Z",
                }
            ],
        },
        {
            "name": "buy.browse.item.bulk",
            "rates": [{"limit": 10000000, "remaining": 10000000, "timeWindow": 86400}],
        },
    ]
    if extra:
        resources.append(extra)
    return {
        "rateLimits": [
            {
                "apiContext": "buy",
                "apiName": "Browse",
                "apiVersion": "v1",
                "resources": resources,
            }
        ]
    }


class QuotaTests(EbayFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        ebay_store.save_configuration(
            {"client_id": "test-id", "client_secret": "private-secret"}
        )
        self.config = ebay_store.configuration()
        self.client = Mock(token="private-token")
        self.client.session.get.return_value.status_code = 200

    def check(self, payload, now=1000):
        self.client.session.get.return_value.json.return_value = payload
        with patch.object(ebay_quota.time, "time", return_value=now):
            return ebay_quota.check_allowance(self.client, self.config)

    def test_search_capacity_excludes_bulk_and_uses_every_search_window(self):
        payload = response_payload(
            1920000,
            1800000,
            {
                "name": "search",
                "rates": [{"limit": 20, "remaining": 10, "timeWindow": 60}],
            },
        )
        self.check(payload)
        result = ebay_quota.allowance_summary(self.config, 100, now=1001)
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(
            result["capacity"], 1
        )  # Short limit, not generous daily/bulk limit.
        self.assertFalse(result["meets_target"])
        payload["rateLimits"][0]["resources"].pop()
        self.check(payload)
        result = ebay_quota.allowance_summary(self.config, 100, now=1001)
        self.assertEqual(result["capacity"], 100)
        self.assertTrue(result["meets_target"])
        self.assertIn("01 Oct 2026, 07:00:00 UTC", result["rows"][0]["reset_label"])

    def test_default_allowance_and_zero_remaining_are_not_extra_capacity(self):
        self.check(response_payload(5000, 0))
        result = ebay_quota.allowance_summary(
            dict(self.config, daily_budget=1920000), 100, now=1001
        )
        self.assertEqual(result["rows"][0]["remaining"], 0)
        self.assertEqual(result["capacity"], 0)
        self.assertTrue(result["exceeds_reported"])
        self.assertFalse(result["meets_target"])
        self.check(response_payload(0, 0))
        self.assertEqual(
            ebay_quota.allowance_summary(self.config, 1, now=1001)["capacity"], 0
        )

    def test_no_confirmation_for_unknown_wrong_api_version_or_missing_daily_limit(self):
        payloads = []
        unknown = response_payload(
            1920000, 1800000, {"name": "unrecognized.future.limit", "rates": []}
        )
        payloads.append(unknown)
        for field, value in [
            ("apiContext", "sell"),
            ("apiName", "inventory"),
            ("apiVersion", "v2"),
        ]:
            payload = response_payload()
            payload["rateLimits"][0][field] = value
            payloads.append(payload)
        short = response_payload(1920000, 1800000)
        short["rateLimits"][0]["resources"][0]["rates"][0]["timeWindow"] = 60
        payloads.append(short)
        bulk_only = response_payload()
        bulk_only["rateLimits"][0]["resources"].pop(0)
        payloads.append(bulk_only)
        for payload in payloads:
            with self.subTest(payload=payload):
                self.check(payload)
                result = ebay_quota.allowance_summary(self.config, 1, now=1001)
                self.assertFalse(result["complete"])
                self.assertFalse(result["meets_target"])

    def test_failure_replaces_success_and_never_persists_raw_error_or_token(self):
        self.check(response_payload())
        response = self.client.session.get.return_value
        for status in (204, 401, 403, 429, 500):
            response.status_code = status
            response.json.return_value = {"error": "private-token"}
            self.check(response_payload())
            result = ebay_quota.allowance_summary(self.config, 1, now=1001)
            self.assertFalse(result["rows"])
            self.assertFalse(result["meets_target"])
        response.status_code = 200
        invalid = response_payload()
        invalid["rateLimits"][0]["resources"][0]["rates"][0]["limit"] = True
        for payload in (
            None,
            {"rateLimits": {}},
            invalid,
            {"errors": ["private-token"]},
        ):
            self.check(payload)
            self.assertFalse(
                ebay_quota.allowance_summary(self.config, 1, now=1001)["rows"]
            )
        self.client.session.get.side_effect = requests.Timeout("private-token")
        self.check(response_payload())
        with closing(connection()) as conn:
            saved = conn.execute(
                "SELECT value FROM parameters WHERE key=?",
                (ebay_quota.OBSERVATION_KEY,),
            ).fetchone()[0]
        self.assertNotIn("private-token", saved)
        self.assertNotIn("private-secret", saved)
        self.assertNotIn("test-id", saved)

    def test_identity_freshness_and_ledger_survive_restart_without_network(self):
        ebay_store.reserve_call(self.config, 1000)
        self.check(response_payload(1920000, 1800000))
        self.client.session.get.reset_mock()
        for key in ("client_id", "client_secret"):
            self.assertFalse(
                ebay_quota.allowance_summary(
                    dict(self.config, **{key: "changed"}), 1, now=1001
                )["checked"]
            )
        self.assertTrue(
            ebay_quota.allowance_summary(self.config, 1, now=1001)["meets_target"]
        )
        stale = ebay_quota.allowance_summary(self.config, 1, now=87400)
        self.assertTrue(stale["stale"])
        self.assertFalse(stale["meets_target"])
        self.assertFalse(
            ebay_quota.allowance_summary(self.config, 0, now=1001)["meets_target"]
        )
        self.client.session.get.assert_not_called()
        self.assertEqual(ebay_store.configuration()["daily_budget"], 5000)
        with closing(connection()) as conn:
            self.assertEqual(
                conn.execute("SELECT SUM(calls) FROM ebay_call_buckets").fetchone()[0],
                1,
            )

    def test_owner_check_reuses_application_token_after_search_and_no_listing_alerts(
        self,
    ):
        self.client.session.get.return_value.json.return_value = response_payload()
        with patch.object(ebay_connections, "BrowseClient", return_value=self.client):
            message = ebay_connections.test_connection("ebay")
        self.assertIn("production search succeeded", message)
        self.assertIn("allowance saved", message)
        call = self.client.session.get.call_args
        self.assertEqual(
            call.args[0], "https://api.ebay.com/developer/analytics/v1_beta/rate_limit/"
        )
        self.assertEqual(
            call.kwargs["params"], {"api_context": "buy", "api_name": "browse"}
        )
        self.assertEqual(
            call.kwargs["headers"]["Authorization"], "Bearer private-token"
        )
        self.assertEqual(self.outbox(), [])
        self.client.session.close.assert_called_once()

    def test_access_check_gets_budgeted_diagnostic_slot_during_continuous_polling(self):
        self.client.session.get.return_value.json.return_value = response_payload()
        self.assertIsNone(ebay_store.reserve_call(self.config, 1000))
        with patch.object(
            ebay_connections.time, "time", return_value=1002
        ), patch.object(ebay_connections, "BrowseClient", return_value=self.client):
            self.assertIn(
                "production search succeeded", ebay_connections.test_connection("ebay")
            )
            with self.assertRaisesRegex(ValueError, "request slot"):
                ebay_connections.test_connection("ebay")
        self.client.search.assert_called_once()
        with closing(connection()) as conn:
            self.assertEqual(
                conn.execute("SELECT SUM(calls) FROM ebay_call_buckets").fetchone()[0],
                2,
            )
        self.assertGreater(ebay_store.reserve_call(self.config, 1003), 1003)

    def test_failed_diagnostic_closes_session_and_keeps_shared_api_cooldown(self):
        self.client.search.side_effect = ebay_connections.EbayError(
            "eBay asked for a cooldown", retry_after=120, global_cooldown=True
        )
        with patch.object(
            ebay_connections.time, "time", return_value=1000
        ), patch.object(
            ebay_connections, "BrowseClient", return_value=self.client
        ), self.assertRaisesRegex(
            ValueError, "cooldown"
        ):
            ebay_connections.test_connection("ebay")
        self.client.session.close.assert_called_once()
        self.client.session.get.assert_not_called()
        self.assertEqual(
            ebay_store.reserve_call(self.config, 1040, diagnostic=True), 1120
        )


class QuotaDashboardTests(EbayFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()
        test_ebay_dashboard.SharedDashboardTests.login(self)

    def test_dashboard_distinguishes_entered_budget_from_reported_limits(self):
        self.enable()
        ebay_store.save_configuration(
            {
                "client_id": "private-id",
                "client_secret": "private-secret",
                "daily_budget": "1920000",
            }
        )
        with patch("ebay_connections.BrowseClient") as browse:
            html = self.client.get("/connections").text
            self.assertIn("Allowance not checked", html)
            browse.assert_not_called()
        api = Mock(token="private-token")
        api.session.get.return_value.status_code = 200
        api.session.get.return_value.json.return_value = response_payload(5000, 0)
        ebay_quota.check_allowance(api, ebay_store.configuration())
        api.session.get.reset_mock()
        html = self.client.get("/connections").text
        self.assertIn("5000 search calls per 24 hours", html)
        self.assertIn("Remaining when checked: <b>0</b>", html)
        self.assertIn("configured allowance exceeds", html)
        self.assertIn("reported limits do not cover", html)
        self.assertIn("No completed live measurements in this window", html)
        for secret in ("private-id", "private-secret", "private-token"):
            self.assertNotIn(secret, html)
        api.session.get.assert_not_called()
        ebay_store.save_configuration({"client_id": "different-app"})
        self.assertIn("Allowance not checked", self.client.get("/connections").text)

    def test_zero_enabled_and_public_mode_do_not_claim_live_api_capacity(self):
        api = Mock(token="test-token")
        api.session.get.return_value.status_code = 200
        api.session.get.return_value.json.return_value = response_payload(
            1920000, 1800000
        )
        ebay_quota.check_allowance(api, ebay_store.configuration())
        html = self.client.get("/connections").text
        self.assertIn("No eBay searches are enabled yet", html)
        self.assertNotIn("reported limits cover the configured", html)
        ebay_store.save_configuration({"source": "public"})
        self.assertNotIn(
            "eBay-reported allowance", self.client.get("/connections").text
        )
