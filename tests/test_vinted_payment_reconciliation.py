"""Offline bound-order reconciliation and full-cost payment provenance."""

import copy
import json
import os
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

from test_search_controls import DatabaseFixture
from test_vinted_buying import DEVICE, web_checkout

import search_settings
import vinted_buyer as buyer
import vinted_buying as buying
import vinted_payment_check


class PaymentReconciliationTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.row = {
            "item_id": "123",
            "query_id": 1,
            "url": "https://www.vinted.co.uk/items/123",
            "price": "15.00",
            "currency": "GBP",
        }
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?,verified_at=1,user_id='99',username='owner',enabled=0,browser_info=?,pickup_mode='saved',preferred_card_last4='1234' WHERE id=1",
                (
                    buyer.encrypt({"cookies": {"access_token_web": "offline-token"}}),
                    json.dumps(DEVICE),
                ),
            )
            conn.execute("INSERT INTO vinted_search_budgets VALUES (1,2000,350)")
        self.transaction = {
            "id": 456,
            "buyer_id": 99,
            "item_id": 123,
            "purchase_id": "checkout-123",
        }
        self.checkout = web_checkout()
        self.payment = {"payment": {"status": "success"}}
        self.client = Mock()
        self.client.request.side_effect = self.request
        self.client.listing_page.side_effect = lambda url, item_id: self.request(
            "GET", "/api/v2/items/" + str(item_id)
        )
        self.connected = patch.object(
            buyer, "connected_client", return_value=self.client
        )
        self.connect_mock = self.connected.start()
        self.addCleanup(self.connected.stop)

    def request(self, method, path, body=None):
        if path == "/api/v2/transactions/456":
            if isinstance(self.transaction, Exception):
                raise self.transaction
            return {"transaction": copy.deepcopy(self.transaction)}
        if path == "/api/v2/items/123":
            return {
                "item": {
                    "id": 123,
                    "user_id": 100,
                    "price": {"amount": "15.00", "currency_code": "GBP"},
                }
            }
        if path == "/api/v2/conversations":
            return {"conversation": {"transaction": {"id": 456}}}
        if path in (
            "/api/v2/purchases/checkout/build",
            "/api/v2/purchases/checkout-123/checkout",
        ):
            return {"checkout": copy.deepcopy(self.checkout)}
        if path == "/api/v2/purchases/checkout-123/checkout/payment":
            if isinstance(self.payment, Exception):
                raise self.payment
            return copy.deepcopy(self.payment)
        raise AssertionError((method, path))

    def pending(self, state="unknown", *, bound=True):
        buying.claim(self.row)
        buying.record(
            "123",
            state,
            "Existing uncertain payment",
            checkout_id="checkout-123",
            total=1884,
            buyer_id="99" if bound else None,
            transaction_id="456" if bound else None,
        )

    def assert_only_gets(self):
        self.assertTrue(self.client.request.call_args_list)
        self.assertTrue(
            all(c.args[0] == "GET" for c in self.client.request.call_args_list)
        )

    def test_bound_payment_reconciliation_preserves_off_and_all_saved_controls(self):
        self.pending()
        before = buyer.settings()
        outcome = buying.check_payment("123")
        self.assertEqual(outcome["state"], "paid")
        self.assertEqual(outcome["total"], 1884)
        self.assertEqual(buyer.settings(), before)
        self.connect_mock.assert_called_once_with(solve_challenges=False)
        self.assertEqual(
            [c.args for c in self.client.request.call_args_list],
            [
                ("GET", "/api/v2/transactions/456"),
                ("GET", "/api/v2/purchases/checkout-123/checkout/payment"),
            ],
        )
        self.assert_only_gets()

    def test_wrong_item_buyer_or_transaction_never_trusts_a_payment_success(self):
        self.pending()
        original = buying.result("123")
        for field, value in (("id", 777), ("buyer_id", 88), ("item_id", 321)):
            with self.subTest(field=field):
                self.transaction = {
                    "id": 456,
                    "buyer_id": 99,
                    "item_id": 123,
                    "purchase_id": "checkout-123",
                }
                self.transaction[field] = value
                self.client.request.reset_mock()
                outcome = buying.check_payment("123")
                self.assertEqual(outcome["state"], "unknown")
                self.assertEqual(
                    outcome["reconciliation_stage"], "transaction_binding_unverified"
                )
                self.assertEqual(buying.result("123"), original)
                self.client.request.assert_called_once_with(
                    "GET", "/api/v2/transactions/456"
                )

    def test_missing_or_conflicting_checkout_reference_stays_unverified(self):
        self.pending()
        original = buying.result("123")
        for values in (
            {},
            {"purchase_id": "other"},
            {"purchase_id": "checkout-123", "checkout_id": "other"},
        ):
            with self.subTest(values=values):
                self.transaction = dict(id=456, buyer_id=99, item_id=123, **values)
                self.client.request.reset_mock()
                outcome = buying.check_payment("123")
                self.assertEqual(
                    outcome["reconciliation_stage"], "checkout_binding_unverified"
                )
                self.assertEqual(buying.result("123"), original)
                self.client.request.assert_called_once_with(
                    "GET", "/api/v2/transactions/456"
                )

    def test_changed_saved_buyer_and_legacy_missing_binding_never_guess_an_order(self):
        self.pending(bound=False)
        original = buying.result("123")
        self.assertEqual(
            buying.check_payment("123")["reconciliation_stage"],
            "attempt_binding_missing",
        )
        self.connect_mock.assert_not_called()
        self.client.request.assert_not_called()
        self.assertEqual(buying.result("123"), original)
        buying.record(
            "123",
            "unknown",
            "Existing uncertain payment",
            buyer_id="88",
            transaction_id="456",
        )
        original = buying.result("123")
        self.assertEqual(
            buying.check_payment("123")["reconciliation_stage"], "buyer_account_changed"
        )
        self.connect_mock.assert_not_called()
        self.client.request.assert_not_called()
        self.assertEqual(buying.result("123"), original)

    def test_transaction_order_and_payment_disagreement_cannot_report_paid(self):
        self.pending()
        self.transaction["is_paid"] = False
        original = buying.result("123")
        outcome = buying.check_payment("123")
        self.assertEqual(outcome["reconciliation_stage"], "payment_order_disagree")
        self.assertEqual(buying.result("123"), original)
        self.assert_only_gets()

    def test_explicit_bound_paid_order_is_confirmed_without_replaying_payment(self):
        self.pending()
        self.transaction["is_paid"] = True
        self.assertEqual(buying.check_payment("123")["state"], "paid")
        self.client.request.assert_called_once_with("GET", "/api/v2/transactions/456")
        self.client.request.reset_mock()
        self.assertEqual(buying.check_payment("123")["state"], "paid")
        self.client.request.assert_not_called()

    def test_pending_failed_unknown_and_refused_status_never_resubmit(self):
        self.pending()
        for status, expected in (
            ("pending", "needs_action"),
            ("preparing", "needs_action"),
            ("requires_action", "needs_action"),
            ("failure", "payment_failed"),
            ("unexpected", "unknown"),
        ):
            with self.subTest(status=status):
                buying.record("123", "unknown", "Uncertain result")
                self.payment = {"payment": {"status": status}}
                self.client.request.reset_mock()
                self.assertEqual(buying.check_payment("123")["state"], expected)
                self.assert_only_gets()
                self.assertFalse(buying.claim(self.row, recover_preparing=True))
        buying.record("123", "unknown", "Uncertain result")
        original = buying.result("123")
        self.transaction = buyer.BuyerError("Refused", reason="http_error", status=403)
        with self.assertRaises(buyer.BuyerError):
            buying.check_payment("123")
        self.assertEqual(buying.result("123"), original)
        self.client.session.close.assert_called()

    def enable(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET enabled=1 WHERE id=1")

    def test_new_telegram_payment_saves_buyer_transaction_and_full_cost_before_post(
        self,
    ):
        self.enable()
        summary = self.checkout["components"]["order_summary_v2"]
        summary["subtotal"] = {"price": {"amount": "18.84", "currency_code": "GBP"}}
        summary["deductions"] = [
            {
                "type": "order-summary-wallet-deduction",
                "price": {"amount": "-14.00", "currency_code": "GBP"},
            }
        ]
        self.checkout["components"]["pay_button_v2"]["total"]["price"][
            "amount"
        ] = "4.84"
        original = self.client.request.side_effect
        observed = []

        def response(method, path, body=None):
            if method == "POST" and path.endswith("/checkout/payment"):
                observed.append(buying.result("123"))
            return original(method, path, body)

        self.client.request.side_effect = response
        self.assertEqual(buying.buy(self.row)["state"], "paid")
        self.assertEqual(len(observed), 1)
        self.assertEqual(
            (
                observed[0]["state"],
                observed[0]["buyer_id"],
                observed[0]["transaction_id"],
                observed[0]["checkout_id"],
                observed[0]["total"],
            ),
            ("paying", "99", "456", "checkout-123", 1884),
        )

    def test_new_oneoff_quote_saves_validated_transaction_provenance(self):
        self.enable()
        prepared = buying.check_checkout(
            "https://www.vinted.co.uk/checkout?purchase_id=checkout-123&order_id=456&order_type=transaction",
            prepare_test=True,
        )
        quote = buyer.decrypt(prepared["token"].encode("ascii"))
        self.assertEqual(quote["transaction_id"], "456")
        self.payment = buyer.BuyerError("Timeout", reason="network")
        outcome = buying.buy_checkout_quote(prepared["token"])
        self.assertEqual(
            (outcome["state"], outcome["buyer_id"], outcome["transaction_id"]),
            ("unknown", "99", "456"),
        )
        self.assertFalse(buying.claim(self.row, recover_preparing=True))

    def test_pre_release_oneoff_quote_without_binding_cannot_start_a_new_payment(self):
        self.enable()
        prepared = buying.check_checkout(
            "https://www.vinted.co.uk/checkout?purchase_id=checkout-123&order_id=456&order_type=transaction",
            prepare_test=True,
        )
        quote = buyer.decrypt(prepared["token"].encode("ascii"))
        quote.pop("transaction_id")
        self.client.request.reset_mock()
        with self.assertRaises(buyer.BuyerError):
            buying.buy_checkout_quote(buyer.encrypt(quote).decode("ascii"))
        self.client.request.assert_not_called()
        self.assertIsNone(buying.result("123"))

    def test_another_uncertain_submission_blocks_new_item_before_claim_or_network(self):
        self.enable()
        buying.claim({"item_id": "987"})
        for state in ("paying", "unknown", "needs_action"):
            with self.subTest(state=state):
                buying.record("987", state, "Previous payment is unconfirmed")
                original = buying.result("987")
                with self.assertRaises(buyer.BuyerError) as error:
                    buying.buy(self.row)
                self.assertEqual(error.exception.reason, "earlier_payment_uncertain")
                self.client.request.assert_not_called()
                self.assertIsNone(buying.result("123"))
                self.assertEqual(buying.result("987"), original)
        buying.record("987", "paid", "Confirmed paid")
        self.assertEqual(buying.buy(self.row)["state"], "paid")

    def test_another_uncertain_submission_also_blocks_new_oneoff_payment(self):
        self.enable()
        prepared = buying.check_checkout(
            "https://www.vinted.co.uk/checkout?purchase_id=checkout-123&order_id=456&order_type=transaction",
            prepare_test=True,
        )
        buying.claim({"item_id": "987"})
        buying.record("987", "unknown", "Previous payment is unconfirmed")
        self.client.request.reset_mock()
        with self.assertRaises(buyer.BuyerError) as error:
            buying.buy_checkout_quote(prepared["token"])
        self.assertEqual(error.exception.reason, "earlier_payment_uncertain")
        self.client.request.assert_not_called()
        self.assertIsNone(buying.result("123"))

    def test_success_response_can_be_independently_verified_without_new_submission(
        self,
    ):
        self.pending(state="paid")
        self.assertEqual(buying.check_payment("123")["state"], "paid")
        self.client.request.assert_not_called()
        self.transaction["is_paid"] = True
        outcome = buying.check_payment("123", verify_paid=True)
        self.assertEqual(outcome["state"], "paid")
        self.assertEqual(outcome["reconciliation_outcome"], "verified")
        self.assertEqual(outcome["reconciliation_stage"], "bound_transaction")
        self.assert_only_gets()

    def test_exact_startup_verification_is_consumed_once_and_has_no_raw_payload(self):
        self.pending(state="paid")
        self.transaction["is_paid"] = True
        with patch.dict(
            os.environ, {"MSJ_PAYMENT_RECONCILE_ON_START": "123:offline-proof"}
        ):
            result = vinted_payment_check.run_once()
            self.assertIsNone(vinted_payment_check.run_once())
        self.assertEqual(result["outcome"], "verified")
        self.assertEqual(result["state"], "paid")
        self.assertEqual(result["total_gbp_pence"], 1884)
        self.assertFalse(result["payment_submitted_by_check"])
        self.assertFalse(result["checkout_created_by_check"])
        self.assertFalse(result["solver_attempted_by_check"])
        self.assertNotIn("transaction_id", result)
        self.assertNotIn("buyer_id", result)
        self.assert_only_gets()

    def test_exact_startup_check_remains_consumed_after_refusal_and_malformed_flags(
        self,
    ):
        self.pending()
        self.transaction = buyer.BuyerError(
            "Private provider response", status=403, reason="security_challenge"
        )
        with patch.dict(
            os.environ, {"MSJ_PAYMENT_RECONCILE_ON_START": "123:offline-refused"}
        ):
            result = vinted_payment_check.run_once()
            self.assertIsNone(vinted_payment_check.run_once())
        self.assertEqual(result["outcome"], "unverified")
        self.assertEqual(result["http_status"], 403)
        self.assertNotIn("Private provider response", json.dumps(result))
        count = self.client.request.call_count
        for value in ("", "off", "123", "123:marker:extra", "123:https://example.test"):
            with self.subTest(value=value), patch.dict(
                os.environ, {"MSJ_PAYMENT_RECONCILE_ON_START": value}
            ):
                self.assertIsNone(vinted_payment_check.run_once())
        self.assertEqual(self.client.request.call_count, count)


if __name__ == "__main__":
    unittest.main()
