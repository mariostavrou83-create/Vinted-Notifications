"""No live requests: manual build diagnostics cannot reach payment."""

import time
import unittest
from contextlib import closing
from unittest.mock import patch

import test_vinted_buying as fixtures
import search_settings
import vinted_buyer as buyer
import vinted_buying as buying
import vinted_failed_checkout_check as diagnostic


class FailedCheckoutCheckTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BuyingTests("test_valid_checkout_pays_once_and_duplicate_tap_is_idempotent")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.client = self.fixture.client
        buying.claim(self.fixture.row)
        buying.record("123", "failed_before_payment", "Stopped before payment")
        self.attempt = buying.result("123")
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT INTO alert_outbox(item_id,query_id,search_name,content,url,title,price,currency,found_at,status,sent_at,platform) "
                "VALUES ('123',1,'Test','Test','https://www.vinted.co.uk/items/123','Test','15.00','GBP',?,'sent',?,'vinted')",
                (time.time(), time.time()),
            )
        self.connected = patch.object(buyer, "connected_client", return_value=self.client)
        self.connection = self.connected.start()
        self.addCleanup(self.connected.stop)

    def run_check(self):
        return diagnostic.check("123", str(self.attempt["updated"]))

    def saved(self):
        with closing(search_settings.connection()) as conn:
            value = conn.execute("SELECT value FROM parameters WHERE key LIKE 'buyer_failed_checkout_check:%'").fetchone()
        return buyer.decrypt(value[0].encode()) if value else None

    def test_stops_after_build_retains_references_and_preserves_purchase_settings(self):
        before = buyer.settings()
        message = self.run_check()
        self.assertIn("stopped before delivery choices or payment", message)
        self.assertEqual(buying.result("123"), self.attempt)
        self.assertEqual(buyer.settings(), before)
        self.assertEqual([c.args[:2] for c in self.client.request.call_args_list], [
            ("POST", "/api/v2/conversations"), ("POST", "/api/v2/purchases/checkout/build"),
        ])
        self.assertEqual(self.saved()["checkout_id"], "checkout-123")
        self.assertEqual(self.saved()["transaction_id"], "456")
        self.assertFalse(self.saved()["payment_submitted"])
        self.client.session.close.assert_called_once()

    def test_second_click_has_no_network_or_checkout_side_effect(self):
        self.run_check()
        with self.assertRaisesRegex(buyer.BuyerError, "already been checked"):
            self.run_check()
        self.connection.assert_called_once()
        self.assertEqual(self.client.request.call_count, 2)

    def test_build_challenge_retains_transaction_and_never_retries_check(self):
        self.client.request.side_effect = [
            {"conversation": {"transaction": {"id": 456}}},
            buyer.BuyerError("private challenge URL", status=403, reason="security_challenge"),
        ]
        with self.assertRaises(buyer.BuyerError) as error:
            self.run_check()
        self.assertIn("checkout build (HTTP 403)", str(error.exception))
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(self.saved()["transaction_id"], "456")
        self.assertEqual(self.saved()["stage"], "checkout_build")
        self.assertEqual(buying.result("123"), self.attempt)
        with self.assertRaisesRegex(buyer.BuyerError, "already been checked"):
            self.run_check()
        self.connection.assert_called_once()

    def test_paid_pending_and_existing_checkout_are_rejected_before_network(self):
        for state in ("paid", "unknown", "paying", "needs_action", "payment_failed", "preparing"):
            buying.record("123", state, "Test")
            with self.assertRaises(buyer.BuyerError):
                diagnostic.check("123", str(buying.result("123")["updated"]))
        buying.record("123", "failed_before_payment", "Test", checkout_id="checkout-private")
        with self.assertRaises(buyer.BuyerError):
            diagnostic.check("123", str(buying.result("123")["updated"]))
        self.connection.assert_not_called()

    def test_changed_or_old_attempt_is_rejected(self):
        with self.assertRaises(buyer.BuyerError):
            diagnostic.check("123", "stale")
        with patch.object(diagnostic.time, "time", return_value=self.attempt["updated"] + 86401):
            with self.assertRaises(buyer.BuyerError):
                self.run_check()
        self.connection.assert_not_called()

    def test_other_uncertain_purchase_is_not_ignored(self):
        buying.claim(dict(self.fixture.row, item_id="999"))
        buying.record("999", "unknown", "Awaiting confirmation")
        with self.assertRaises(buyer.BuyerError) as error:
            self.run_check()
        self.assertEqual(error.exception.reason, "earlier_payment_uncertain")
        self.connection.assert_not_called()

    def test_sold_listing_stops_before_any_checkout_request(self):
        self.fixture.item["item"]["is_sold"] = True
        with self.assertRaises(buyer.BuyerError):
            self.run_check()
        self.client.request.assert_not_called()

    def test_malformed_conversation_and_unknown_error_do_not_expose_secrets(self):
        self.client.request.side_effect = RuntimeError("private proxy password")
        with self.assertRaises(buyer.BuyerError) as error:
            self.run_check()
        self.assertNotIn("private", str(error.exception))
        self.client.request.assert_called_once()
        self.assertEqual(buying.result("123"), self.attempt)

    def test_failed_durable_marker_prevents_all_network(self):
        with patch.object(buyer, "encrypt", side_effect=RuntimeError("disk failure")):
            with self.assertRaises(RuntimeError):
                self.run_check()
        self.connection.assert_not_called()

    def test_pre_fix_explicit_refusal_can_be_checked_once_reusing_transaction(self):
        self.client.request.side_effect = [
            {"conversation": {"transaction": {"id": 456}}},
            buyer.BuyerError("Blocked", status=403, reason="security_challenge"),
        ]
        with self.assertRaises(buyer.BuyerError):
            self.run_check()
        with closing(search_settings.connection()) as conn, conn:
            key, value = conn.execute("SELECT key,value FROM parameters WHERE key LIKE 'buyer_failed_checkout_check:%'").fetchone()
            prior = buyer.decrypt(value.encode())
            prior.pop("version")
            conn.execute("UPDATE parameters SET value=? WHERE key=?", (buyer.encrypt(prior).decode(), key))
        self.client.request.reset_mock()
        self.client.request.side_effect = None
        self.client.request.return_value = {"checkout": {"id": "test"}}
        self.run_check()
        self.client.request.assert_called_once_with("POST", "/api/v2/purchases/checkout/build", {
            "purchase_items": [{"id": "456", "type": "transaction"}],
        })
        with self.assertRaisesRegex(buyer.BuyerError, "already been checked"):
            self.run_check()

    def test_pre_fix_accepted_checkout_or_uncertain_request_cannot_be_rechecked(self):
        self.run_check()
        with closing(search_settings.connection()) as conn, conn:
            key, value = conn.execute("SELECT key,value FROM parameters WHERE key LIKE 'buyer_failed_checkout_check:%'").fetchone()
            prior = buyer.decrypt(value.encode())
            prior.pop("version")
            for updates in ({"http_status": 403}, {"stage": "checkout_build", "checkout_id": None, "http_status": None}):
                prior.update(updates)
                conn.execute("UPDATE parameters SET value=? WHERE key=?", (buyer.encrypt(prior).decode(), key))
                conn.commit()
                with self.assertRaisesRegex(buyer.BuyerError, "already been checked"):
                    self.run_check()
        self.assertEqual(self.client.request.call_count, 2)


class FailedCheckoutOwnerTests(fixtures.DatabaseFixture, unittest.TestCase):
    owner = fixtures.BuyerDashboardTests.owner

    def setUp(self):
        super().setUp()
        from web_ui_plugin.web_ui import create_app
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()

    def test_owner_csrf_and_redirect_protect_manual_check(self):
        self.owner()
        form = {"csrf": "offline-csrf", "action": "buyer_failed_checkout_check",
                "buyer_item_id": "123", "buyer_attempt_updated": "1234.5"}
        with patch.object(diagnostic, "check", return_value="No payment was sent.") as check:
            self.assertEqual(self.client.post("/connections", data=dict(form, csrf="invalid")).status_code, 400)
            check.assert_not_called()
            response = self.client.post("/connections", data=form)
            self.assertEqual(response.status_code, 303)
            check.assert_called_once_with("123", "1234.5")
        with self.client.session_transaction() as session:
            session.pop("owner", None)
        with patch.object(diagnostic, "check") as check:
            self.client.post("/connections", data=form)
            check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
