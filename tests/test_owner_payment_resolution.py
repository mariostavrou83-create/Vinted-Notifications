"""Owner-attested local resolution never contacts Vinted or retries payment."""

import os
import unittest
from contextlib import closing
from unittest.mock import patch

from test_search_controls import DatabaseFixture
from werkzeug.security import generate_password_hash

import search_settings
import vinted_buying as buying
import vinted_owner_payment_resolution as resolution
from web_ui_plugin.web_ui import create_app


class OwnerResolution(DatabaseFixture, unittest.TestCase):
    def seed(self, identity="10320418087", state="unknown"):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT INTO vinted_buy_attempts(item_id,state,message,updated) VALUES (?,?,?,?)",
                (identity, state, "Original uncertain payment", 123.25),
            )
        return buying.result(identity)

    def test_explicit_once_only_marker_resolves_exact_item_without_network(self):
        self.seed()
        with patch.dict(
            os.environ,
            {"MSJ_OWNER_FAILED_PAYMENT_ON_START": "10320418087:owner_failed_20261010"},
        ), patch.object(
            buying.buyer, "connected_client", side_effect=AssertionError("No network")
        ):
            result = resolution.run_once()
            self.assertEqual(result["outcome"], "owner_attested")
            self.assertEqual(result["state"], "payment_failed")
            self.assertFalse(result["payment_submitted"])
            self.assertFalse(result["other_payments_unconfirmed"])
            before = buying.result("10320418087")
            self.assertIsNone(resolution.run_once())
            self.assertEqual(before, buying.result("10320418087"))
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM vinted_payment_resolutions"
                ).fetchone()[0],
                1,
            )

    def test_paid_refuses_and_another_uncertain_attempt_is_preserved(self):
        self.seed(state="paid")
        before = buying.result("10320418087")
        with patch.dict(
            os.environ,
            {"MSJ_OWNER_FAILED_PAYMENT_ON_START": "10320418087:paid_refused"},
        ):
            self.assertEqual(resolution.run_once()["outcome"], "not_resolved")
        self.assertEqual(before, buying.result("10320418087"))
        self.seed("123", "unknown")
        self.seed("456", "unknown")
        with patch.dict(
            os.environ, {"MSJ_OWNER_FAILED_PAYMENT_ON_START": "123:owner_failed"}
        ):
            self.assertTrue(resolution.run_once()["other_payments_unconfirmed"])
        self.assertEqual(buying.result("456")["state"], "unknown")

    def test_missing_or_invalid_marker_does_nothing(self):
        for value in ("", "invalid", "10320418087", "10320418087:bad/marker"):
            with patch.dict(os.environ, {"MSJ_OWNER_FAILED_PAYMENT_ON_START": value}):
                self.assertIsNone(resolution.run_once())

    def owner_client(self):
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE dashboard_auth SET password_hash=?",
                (generate_password_hash("offline password"),),
            )
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["owner"] = True
            session["csrf"] = "offline-csrf"
        return client

    def test_owner_route_requires_confirmation_csrf_and_current_version(self):
        self.seed()
        client = self.owner_client()
        form = {
            "csrf": "offline-csrf",
            "action": "buyer_payment_failed",
            "buyer_item_id": "10320418087",
            "buyer_attempt_updated": "123.25",
        }
        self.assertEqual(
            client.post("/connections", data=dict(form, csrf="wrong")).status_code, 400
        )
        client.post("/connections", data=form)
        self.assertEqual(buying.result("10320418087")["state"], "unknown")
        client.post(
            "/connections",
            data=dict(form, buyer_failed_confirm="yes", buyer_attempt_updated="123"),
        )
        self.assertEqual(buying.result("10320418087")["state"], "unknown")
        self.assertEqual(
            client.post(
                "/connections", data=dict(form, buyer_failed_confirm="yes")
            ).status_code,
            303,
        )
        self.assertEqual(buying.result("10320418087")["state"], "payment_failed")

    def test_new_form_failure_retains_shared_inputs(self):
        client = self.owner_client()
        result = client.post(
            "/search/new",
            data={
                "csrf": "offline-csrf",
                "shared_alert_version": "1",
                "query_name": "New jacket",
                "platform_mode": "both",
                "query": "https://www.vinted.co.uk/catalog?brand_ids[]=111",
                "ebay_search_url": "https://www.ebay.co.uk/sch/i.html?_sacat=57988",
                "shared_keywords": "fur, sherpa",
                "reminder": "Resale <£60>",
                "exclusions": "teddy",
                "vinted_max_total": "",
            },
        )
        self.assertEqual(result.status_code, 200)
        content = result.get_data(as_text=True)
        self.assertIn("fur, sherpa", content)
        self.assertIn("Resale &lt;£60&gt;", content)
        self.assertIn("_sacat=57988", content)
        self.assertIn("teddy", content)
        self.assertNotIn('id="vinted_postage_estimate"', content)
