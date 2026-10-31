"""Background status reads yield to purchases and clear only their paid checkout."""

import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

import test_vinted_payment_reconciliation as fixture_module

import search_settings
import vinted_buyer as buyer
import vinted_buyer_events as events
import vinted_buying as buying


class PaymentPriorityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.PaymentReconciliationTests(
            "test_bound_payment_reconciliation_preserves_off_and_all_saved_controls"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.addCleanup(self.fixture.doCleanups)
        with closing(search_settings.connection()) as conn:
            sealed = conn.execute("SELECT session FROM vinted_buyer WHERE id=1").fetchone()[0]
        self.fixture.client._verified_session = ("99", sealed, {})

    def warning(self, checkout_id="checkout-123"):
        self.assertTrue(events.record_checkout_failure(
            self.fixture.client,
            buyer.BuyerError("Payment refused", 403, reason="forbidden"),
            "submitting payment", payment_started=True, checkout_id=checkout_id,
        ))

    def test_manual_status_check_keeps_existing_priority_behavior(self):
        self.fixture.pending()
        with patch.object(buying, "yield_to_purchase") as priority:
            self.assertEqual(buying.check_payment("123")["state"], "paid")
        priority.assert_not_called()
        self.fixture.assert_only_gets()

    def test_background_yields_before_identity_and_each_status_get(self):
        self.fixture.pending()
        with patch.object(buying, "yield_to_purchase") as priority:
            self.assertEqual(buying.check_payment("123", background=True)["state"], "paid")
        self.assertEqual(priority.call_count, 4)
        self.fixture.connect_mock.assert_called_once_with(solve_challenges=False)
        self.fixture.assert_only_gets()

    def test_waiting_purchase_preempts_every_background_boundary(self):
        self.fixture.pending()
        original = buying.result("123")
        for boundary in range(4):
            with self.subTest(boundary=boundary):
                self.fixture.client.reset_mock()
                self.fixture.connect_mock.reset_mock()
                effects = [None] * boundary + [buyer.BuyerError("Owner waiting", reason="busy")]
                with patch.object(buying, "yield_to_purchase", side_effect=effects):
                    with self.assertRaises(buyer.BuyerError) as failure:
                        buying.check_payment("123", background=True)
                self.assertEqual(failure.exception.reason, "busy")
                self.assertEqual(buying.result("123"), original)
                self.assertEqual(self.fixture.client.request.call_count, 1 if boundary == 3 else 0)
                if boundary < 2:
                    self.fixture.connect_mock.assert_not_called()
                else:
                    self.fixture.client.session.close.assert_called_once()
                self.assertTrue(all(call.args[0] == "GET" for call in self.fixture.client.request.call_args_list))

    def test_verified_paid_transaction_closes_its_pending_checkout_warning(self):
        self.fixture.pending()
        self.warning()
        self.fixture.transaction["is_paid"] = True
        outcome = buying.check_payment("123", background=True)
        self.assertEqual(outcome["state"], "paid")
        self.assertIsNone(buyer.settings()["checkout_health"])
        self.fixture.client.request.assert_called_once_with("GET", "/api/v2/transactions/456")

    def test_verified_paid_payment_response_closes_its_pending_checkout_warning(self):
        self.fixture.pending()
        self.warning()
        self.assertEqual(buying.check_payment("123")["state"], "paid")
        self.assertIsNone(buyer.settings()["checkout_health"])
        self.fixture.assert_only_gets()

    def test_paid_reconciliation_keeps_another_checkout_warning(self):
        self.fixture.pending()
        self.warning("different-checkout")
        before = buyer.settings()["checkout_health"]
        self.assertEqual(buying.check_payment("123")["state"], "paid")
        self.assertEqual(buyer.settings()["checkout_health"], before)

    def test_rechecking_an_already_paid_order_does_not_clear_a_new_warning(self):
        self.fixture.pending("paid")
        self.warning()
        before = buyer.settings()["checkout_health"]
        self.fixture.transaction["is_paid"] = True
        self.assertEqual(buying.check_payment("123", verify_paid=True)["state"], "paid")
        self.assertEqual(buyer.settings()["checkout_health"], before)

    def test_refused_status_read_preserves_unknown_payment_and_warning(self):
        self.fixture.pending()
        self.warning()
        before = buyer.settings()["checkout_health"]
        original = buying.result("123")
        self.fixture.transaction = buyer.BuyerError("Refused", 403, reason="forbidden")
        with self.assertRaises(buyer.BuyerError):
            buying.check_payment("123", background=True)
        self.assertEqual(buying.result("123"), original)
        self.assertEqual(buyer.settings()["checkout_health"], before)
        self.fixture.assert_only_gets()

    def test_recovery_receipt_failure_rolls_back_payment_state_and_keeps_warning(self):
        self.fixture.pending()
        self.warning()
        original = buying.result("123")
        warning = buyer.settings()["checkout_health"]
        with patch("vinted_payment_recovery.record_verified", side_effect=sqlite3.OperationalError("outbox unavailable")):
            with self.assertRaises(sqlite3.OperationalError):
                buying.check_payment("123", background=True)
        self.assertEqual(buying.result("123"), original)
        self.assertEqual(buyer.settings()["checkout_health"], warning)
        self.fixture.assert_only_gets()

    def test_ordinary_payment_record_does_not_create_a_recovery_receipt(self):
        self.fixture.pending()
        with patch("vinted_payment_recovery.record_verified") as receipt:
            buying.record("123", "paid", "Ordinary direct payment result")
            buying.record("missing", "paid", "No matching attempt", reconciled=True)
        receipt.assert_not_called()

    def test_background_claim_cannot_read_a_replacement_payment_under_old_budget(self):
        self.fixture.pending()
        expected = ("99", "456", "checkout-123")
        buying.record("123", "unknown", "Replacement payment", checkout_id="replacement-checkout", transaction_id="789")
        replacement = buying.result("123")
        outcome = buying.check_payment("123", background=True, expected_payment=expected)
        self.assertEqual(outcome["reconciliation_stage"], "attempt_replaced")
        self.assertEqual(outcome["reconciliation_outcome"], "unverified")
        self.assertEqual(buying.result("123"), replacement)
        self.fixture.connect_mock.assert_not_called()
        self.fixture.client.request.assert_not_called()

    def test_expected_payment_binding_is_validated_before_account_or_http_access(self):
        self.fixture.pending()
        original = buying.result("123")
        for invalid in (("99", "456"), ["99", "456", "checkout-123"],
                        (99, "456", "checkout-123"), ("99", "456", "../payment"),
                        ("99", "456", "private\ncheckout"), ("88", "456", "checkout-123")):
            with self.subTest(binding=invalid):
                outcome = buying.check_payment("123", background=True, expected_payment=invalid)
                self.assertEqual(outcome["reconciliation_stage"], "attempt_replaced")
                self.assertEqual(buying.result("123"), original)
        self.fixture.connect_mock.assert_not_called()
        self.fixture.client.request.assert_not_called()

    def test_matching_expected_payment_reads_only_its_bound_existing_order(self):
        self.fixture.pending()
        outcome = buying.check_payment("123", background=True, expected_payment=("99", "456", "checkout-123"))
        self.assertEqual(outcome["state"], "paid")
        self.fixture.assert_only_gets()
