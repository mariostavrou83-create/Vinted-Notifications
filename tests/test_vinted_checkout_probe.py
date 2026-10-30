"""Full checkout diagnostics preserve ordinary Autobuy and never submit payment."""

import copy
import json
import os
import time
import unittest
from contextlib import ExitStack, closing
from unittest.mock import Mock, patch

from test_search_controls import DatabaseFixture
from test_vinted_buying import DEVICE, web_checkout
from werkzeug.security import generate_password_hash

import search_settings
import vinted_buyer as buyer
import vinted_buying as buying
import vinted_telegram_review as review
from web_ui_plugin.web_ui import create_app


class CheckoutProbeTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"MSJ_TELEGRAM_REVIEW_ONLY": "0"}))
        self.network = self.stack.enter_context(patch.object(
            review.vinted_network_check, "check_connection", return_value={"outcome": "verified"},
        ))
        self.client = Mock()
        self.client.listing_page.return_value = {"item": {
            "id": "123", "user_id": "100", "price": {"amount": "15.00", "currency_code": "GBP"},
            "can_buy": True, "is_reserved": False, "is_hidden": False,
        }}
        self.connected = self.stack.enter_context(patch.object(buyer, "connected_client", return_value=self.client))
        self.final = web_checkout()
        self.client.request.side_effect = self.response
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("INSERT OR IGNORE INTO search_dashboard(query_id) VALUES (1)")
            conn.execute("INSERT INTO vinted_search_budgets VALUES (1,2000,350)")
            conn.execute(
                "UPDATE vinted_buyer SET session=?,verified_at=1,user_id='99',username='owner',enabled=1,browser_info=? WHERE id=1",
                (buyer.encrypt({"cookies": {"access_token_web": "private-session-token"}}), json.dumps(DEVICE)),
            )
            conn.execute(
                "INSERT INTO alert_outbox(item_id,query_id,search_name,content,url,title,price,currency,found_at,status,telegram_message_id,sent_at,platform) VALUES ('123',1,'Search 1','saved alert','https://www.vinted.co.uk/items/123','Fictional probe item','15.00','GBP',?,'sent',777,?,'vinted')",
                (time.time(), time.time()),
            )

    def response(self, method, path, body=None):
        if path == "/api/v2/conversations":
            return {"conversation": {"transaction": {"id": 456}}}
        if path == "/api/v2/purchases/checkout/build":
            return {"checkout": {"id": "checkout-123"}}
        if path == "/api/v2/purchases/checkout-123/checkout":
            return {"checkout": copy.deepcopy(self.final)}
        raise AssertionError((method, path))  # Includes every attempted payment.

    def posts(self, suffix):
        return [call for call in self.client.request.call_args_list
                if call.args[0] == "POST" and call.args[1].endswith(suffix)]

    def snapshot(self):
        with closing(search_settings.connection()) as conn:
            return {name: [tuple(row) for row in conn.execute(f"SELECT * FROM {name}")]
                    for name in ("vinted_buyer", "vinted_search_budgets", "vinted_buy_attempts", "queries", "alert_outbox")}

    def test_full_probe_quotes_choices_and_preserves_enabled_and_disabled_modes(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("UPDATE vinted_buyer SET enabled=?", (int(enabled),))
                before = self.snapshot()
                result = review.probe_latest(item_id="123")
                self.assertEqual(result["outcome"], "quoted", result)
                self.assertEqual(result["stage"], "checkout_verified")
                self.assertEqual(result["total"], 1884)
                self.assertTrue(result["connection_verified"])
                self.assertTrue(result["diagnostic_only"])
                self.assertFalse(result["payment_submitted"])
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(review.public_probe_result(), result)
                self.assertFalse(review.public_review()["approved"])
        self.network.assert_not_called()
        self.assertEqual(len(self.posts("/conversations")), 1)
        self.assertEqual(len(self.posts("/checkout/build")), 1)
        self.assertEqual(self.posts("/payment"), [])
        self.assertTrue(all(call.kwargs == {"solve_challenges": False} for call in self.connected.call_args_list))

    def test_probe_returns_private_canonical_checkout_link_without_logging_it(self):
        with self.assertLogs(review.logger, level="INFO") as logs:
            result = review.probe_latest()
        self.assertEqual(result["checkout_url"], "https://www.vinted.co.uk/checkout?purchase_id=checkout-123&order_id=456&order_type=transaction")
        self.assertNotIn("checkout-123", " ".join(logs.output))
        with closing(search_settings.connection()) as conn:
            encrypted = conn.execute("SELECT value FROM parameters WHERE key=?", (review.PROBE_KEY,)).fetchone()[0]
        self.assertNotIn("checkout-123", encrypted)

    def test_probe_keeps_existing_unknown_payment_and_global_guard(self):
        self.assertTrue(buying.claim(review.latest_row()))
        buying.record("123", "unknown", "Earlier payment unconfirmed", checkout_id="old-checkout", buyer_id="99", transaction_id="456")
        before = self.snapshot()
        result = review.probe_latest()
        self.assertEqual(result["stage"], "existing_payment")
        self.assertEqual(result["payment_state"], "unknown")
        self.assertEqual(self.snapshot(), before)
        self.client.request.assert_not_called()
        self.connected.assert_not_called()
        with self.assertRaises(buyer.BuyerError) as error:
            buying.guard_new_payment("999")
        self.assertEqual(error.exception.reason, "earlier_payment_uncertain")

    def test_other_uncertain_payment_blocks_probe_before_any_checkout_request(self):
        for state in ("paying", "unknown", "needs_action"):
            with self.subTest(state=state):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("DELETE FROM vinted_buy_attempts")
                    conn.execute(
                        "INSERT INTO vinted_buy_attempts(item_id,state,message,updated) VALUES ('987',?,'Existing uncertain payment',1)",
                        (state,),
                    )
                before = self.snapshot()
                result = review.probe_latest()
                self.assertEqual(result["stage"], "payment_guard")
                self.assertEqual(result["reason"], "earlier_payment_uncertain")
                self.assertEqual(self.snapshot(), before)
        self.connected.assert_not_called()
        self.client.request.assert_not_called()

    def test_payment_recorded_during_probe_cannot_be_reported_as_a_fresh_quote(self):
        def owner_payment(method, path, body=None):
            if path == "/api/v2/purchases/checkout-123/checkout":
                self.assertTrue(buying.claim(review.latest_row()))
                buying.record("123", "unknown", "Owner payment unconfirmed", checkout_id="checkout-123")
            return self.response(method, path, body)
        self.client.request.side_effect = owner_payment
        result = review.probe_latest()
        self.assertEqual(result["outcome"], "unverified")
        self.assertEqual(result["stage"], "existing_payment")
        self.assertEqual(result["payment_state"], "unknown")
        self.assertEqual(buying.result("123")["state"], "unknown")
        self.assertNotIn("token", review.load_review())
        self.assertEqual(self.posts("/payment"), [])

    def test_closed_alert_is_not_reopened_or_quoted(self):
        self.assertTrue(buying.claim(review.latest_row()))
        buying.record("123", "closed", "Owner closed this alert")
        before = self.snapshot()
        result = review.probe_latest()
        self.assertEqual(result["outcome"], "unverified")
        self.assertEqual(result["stage"], "closed_alert")
        self.assertEqual(self.snapshot(), before)
        self.client.request.assert_not_called()
        self.connected.assert_not_called()

    def test_alert_closed_during_probe_cannot_be_reported_as_a_fresh_quote(self):
        def owner_closes(method, path, body=None):
            if path == "/api/v2/purchases/checkout-123/checkout":
                self.assertTrue(buying.claim(review.latest_row()))
                buying.record("123", "closed", "Owner closed this alert")
            return self.response(method, path, body)
        self.client.request.side_effect = owner_closes
        result = review.probe_latest()
        self.assertEqual(result["outcome"], "unverified")
        self.assertEqual(result["stage"], "closed_alert")
        self.assertEqual(buying.result("123")["state"], "closed")
        self.assertNotIn("token", review.load_review())
        self.assertEqual(self.posts("/payment"), [])

    def test_build_403_preserves_mode_and_is_not_replayed_or_solved(self):
        def refused(method, path, body=None):
            if path == "/api/v2/purchases/checkout/build":
                raise buyer.BuyerError("private-upstream-detail", 403, reason="forbidden", api_code=106)
            return self.response(method, path, body)
        self.client.request.side_effect = refused
        before = self.snapshot()
        result = review.probe_latest()
        self.assertEqual(result["stage"], "checkout_build")
        self.assertEqual(result["reason"], "forbidden")
        self.assertEqual(result["api_code"], 106)
        self.assertEqual(result["http_status"], 403)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.posts("/checkout/build")), 1)
        self.assertEqual(self.posts("/payment"), [])
        self.connected.assert_called_once_with(solve_challenges=False)
        self.assertNotIn("private-upstream-detail", json.dumps(result))

    def test_checkout_choice_403_preserves_mode_and_error_evidence(self):
        def refused(method, path, body=None):
            if path == "/api/v2/purchases/checkout-123/checkout":
                raise buyer.BuyerError("private-detail", 403, reason="forbidden", api_code=106)
            return self.response(method, path, body)
        self.client.request.side_effect = refused
        before = self.snapshot()
        result = review.probe_latest()
        self.assertEqual(result["stage"], "checkout_choices")
        self.assertEqual(result["api_code"], 106)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.posts("/payment"), [])

    def test_probe_enforces_existing_search_budget_without_changing_it(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_search_budgets SET max_total=1800 WHERE query_id=1")
        before = self.snapshot()
        result = review.probe_latest()
        self.assertEqual(result["stage"], "search_limit")
        self.assertEqual(result["outcome"], "unverified")
        self.assertEqual(result["total"], 1884)
        self.assertEqual(result["search_maximum_total"], 1800)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.posts("/payment"), [])

    def test_probe_does_not_overwrite_restricted_review_approval(self):
        original = {"item_id": "123", "created": time.time(), "approved": True, "result": {"outcome": "quoted"}}
        review.save_review(original)
        before = self.snapshot()
        with patch.dict(os.environ, {"MSJ_TELEGRAM_REVIEW_ONLY": "1"}):
            result = review.probe_latest()
        self.assertEqual(result["reason"], "review_mode_enabled")
        self.assertEqual(review.load_review(), original)
        self.assertEqual(self.snapshot(), before)
        self.network.assert_not_called()
        self.connected.assert_not_called()

    def test_existing_setup_review_default_still_turns_autobuy_off(self):
        result = review.review_latest()
        self.assertEqual(result["outcome"], "quoted", result)
        self.network.assert_called_once_with()
        self.assertFalse(buyer.settings()["enabled"])
        self.assertFalse(result["payment_submitted"])
        self.assertEqual(self.posts("/payment"), [])

    def test_network_uncertainty_does_not_create_another_checkout_on_next_probe(self):
        def failed(method, path, body=None):
            if path == "/api/v2/purchases/checkout/build":
                raise buyer.BuyerError("network failed", reason="network")
            return self.response(method, path, body)
        self.client.request.side_effect = failed
        review.probe_latest()
        result = review.probe_latest()
        self.assertEqual(result["stage"], "checkout_reconciliation")
        self.assertEqual(len(self.posts("/checkout/build")), 1)
        self.assertTrue(buyer.settings()["enabled"])


class CheckoutProbeRouteTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE dashboard_auth SET password_hash=?", (generate_password_hash("offline probe owner"),))

    def owner(self):
        with self.client.session_transaction() as session:
            session["owner"] = True
            session["csrf"] = "offline-probe-csrf"

    def test_probe_route_is_owner_and_csrf_protected(self):
        self.owner()
        with self.client.session_transaction() as session:
            session.pop("owner")
        with patch.object(review, "probe_latest") as probe:
            response = self.client.post("/connections", data={"action": "buyer_checkout_probe", "csrf": "offline-probe-csrf"})
            self.assertEqual(response.location, "/login")
            self.owner()
            self.assertEqual(self.client.post("/connections", data={"action": "buyer_checkout_probe"}).status_code, 400)
        probe.assert_not_called()

    def test_probe_post_redirects_and_refresh_does_not_make_another_checkout(self):
        self.owner()
        result = {"outcome": "quoted", "stage": "checkout_verified", "title": "Offline item", "total": 1884}
        with patch.object(review, "probe_latest", return_value=result) as probe:
            response = self.client.post("/connections", data={"action": "buyer_checkout_probe", "csrf": "offline-probe-csrf", "buyer_item_id": "123"})
            self.assertEqual(response.status_code, 303)
            self.client.get(response.location)
            self.client.get("/connections")
        probe.assert_called_once_with(item_id="123")
