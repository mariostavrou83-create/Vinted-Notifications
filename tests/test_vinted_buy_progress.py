"""Offline stage transitions preserve payment permissions and durable guards."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import test_vinted_buying as buying_fixture
import test_vinted_checkout_preferences as quote_fixture

import vinted_buyer as buyer
import vinted_buying as buying
import vinted_checkout_diagnostics as diagnostics
import vinted_progress as progress


class PurchaseProgressTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing fictional purchase fixture without inheriting its
        # unrelated test methods or altering the shared suite during UI edits.
        self.fixture = buying_fixture.BuyingTests(
            "test_valid_checkout_pays_once_and_duplicate_tap_is_idempotent"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def purchase(self, reporter):
        with patch.object(buyer, "connected_client", return_value=self.fixture.client):
            return buying.buy(self.fixture.row, progress=reporter)

    def test_real_transitions_and_payment_stage_follow_durable_paying_marker(self):
        events = []
        submission_facts = []

        def reporter(stage):
            events.append(stage)
            if stage == "submitting_payment":
                submission_facts.append(
                    (buying.result("123")["state"], len(self.fixture.payments()))
                )

        outcome = self.purchase(reporter)
        self.assertEqual(outcome["state"], "paid")
        self.assertEqual(
            events,
            [
                "waiting",
                "checking_account",
                "checking_item",
                "opening_checkout",
                "loading_choices",
                "selecting_delivery",
                "submitting_payment",
            ],
        )
        self.assertEqual(submission_facts, [("paying", 0)])
        self.assertEqual(len(self.fixture.payments()), 1)

    def test_reporter_and_timing_logger_failures_do_not_change_the_purchase(self):
        before = copy.deepcopy(buyer.settings())
        reporter = Mock(side_effect=RuntimeError("Telegram unavailable"))
        with patch(
            "vinted_timing.logger.info", side_effect=RuntimeError("timing logger")
        ):
            outcome = self.purchase(reporter)
        self.assertEqual(outcome["state"], "paid")
        self.assertEqual(len(self.fixture.payments()), 1)
        self.assertEqual(buyer.settings(), before)
        self.assertEqual(reporter.call_count, 7)

    def test_listing_refusal_never_announces_checkout_or_payment(self):
        self.fixture.item["item"]["is_reserved"] = True
        events = []
        outcome = self.purchase(events.append)
        self.assertEqual(outcome["state"], "failed_before_payment")
        self.assertEqual(events, ["waiting", "checking_account", "checking_item"])
        self.fixture.client.request.assert_not_called()

    def test_account_refusal_never_announces_item_check_or_payment(self):
        events = []
        with patch.object(
            buyer,
            "connected_client",
            side_effect=buyer.BuyerError(
                "Account rejected", reason="credentials", stage="identity", status=401
            ),
        ):
            outcome = buying.buy(self.fixture.row, progress=events.append)
        self.assertEqual(outcome["state"], "failed_before_payment")
        self.assertEqual(events, ["waiting", "checking_account"])
        self.fixture.client.request.assert_not_called()

    def test_failed_durable_marker_never_reports_or_submits_payment(self):
        events = []
        original = buying.record

        def record(item, state, message, **kwargs):
            if state == "paying":
                raise RuntimeError("fictional durable write failure")
            return original(item, state, message, **kwargs)

        with patch.object(buying, "record", side_effect=record):
            outcome = self.purchase(events.append)
        self.assertEqual(outcome["state"], "failed_before_payment")
        self.assertNotIn("submitting_payment", events)
        self.assertEqual(self.fixture.payments(), [])

    def test_pending_payment_and_duplicate_never_replay_a_submission(self):
        self.fixture.payment = buyer.BuyerError(
            "Payment outcome uncertain", reason="network"
        )
        first, second = [], []
        self.assertEqual(self.purchase(first.append)["state"], "unknown")
        self.assertIn("submitting_payment", first)
        self.assertEqual(self.purchase(second.append)["state"], "unknown")
        self.assertEqual(second, ["waiting"])
        self.assertEqual(len(self.fixture.payments()), 1)

    def test_restricted_quote_route_is_called_once_with_original_binding(self):
        events = []
        sentinel = {"state": "quoted-path-only"}
        with patch("vinted_telegram_review.restricted", return_value=True), patch(
            "vinted_telegram_review.approved_token", return_value="fictional-approval"
        ) as token, patch.object(
            buying, "buy_checkout_quote", return_value=sentinel
        ) as quote:
            self.assertIs(
                buying.buy(self.fixture.row, progress=events.append), sentinel
            )
        token.assert_called_once_with(self.fixture.row)
        quote.assert_called_once_with("fictional-approval", alert_row=self.fixture.row)
        self.assertEqual(events, [])
        self.fixture.client.request.assert_not_called()


class CheckoutEvidenceIntegrationTests(unittest.TestCase):
    """Optional build evidence cannot weaken or alter the real purchase gates."""

    def setUp(self):
        self.fixture = buying_fixture.BuyingTests(
            "test_valid_checkout_pays_once_and_duplicate_tap_is_idempotent"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.build = buying_fixture.checkout()
        original = self.fixture.client.request.side_effect

        def response(method, path, body=None):
            if path == "/api/v2/purchases/checkout/build":
                return {"checkout": self.build}
            return original(method, path, body)

        self.fixture.client.request.side_effect = response

    def purchase(self):
        with patch.object(buyer, "connected_client", return_value=self.fixture.client):
            return buying.buy(self.fixture.row)

    def assert_unchanged_paid_flow(self):
        self.assertEqual(
            self.fixture.client.request.call_args_list,
            [
                call(
                    "POST",
                    "/api/v2/conversations",
                    {"initiator": "buy", "item_id": "123", "opposite_user_id": "100"},
                ),
                call(
                    "POST",
                    "/api/v2/purchases/checkout/build",
                    {"purchase_items": [{"id": 456, "type": "transaction"}]},
                ),
                call(
                    "PUT",
                    "/api/v2/purchases/checkout-123/checkout",
                    {
                        "components": {
                            "additional_service": {},
                            "payment_method": {},
                            "shipping_address": {},
                            "shipping_pickup_options": {},
                            "shipping_pickup_details": {},
                        }
                    },
                ),
                call(
                    "POST",
                    "/api/v2/purchases/checkout-123/checkout/payment",
                    {
                        "checksum": "verified-checksum",
                        "payment_options": {"browser_info": buying_fixture.DEVICE},
                    },
                ),
            ],
        )
        self.assertEqual(len(self.fixture.payments()), 1)
        self.assertEqual(buying.result("123")["total"], 1900)

    def test_true_evidence_keeps_load_checksum_full_cost_and_single_payment(self):
        with patch.object(diagnostics, "observe_build", return_value=True) as observe:
            self.assertEqual(self.purchase()["state"], "paid")
            self.assertEqual(self.purchase()["state"], "paid")
        observe.assert_called_once()
        self.assert_unchanged_paid_flow()

    def test_false_evidence_keeps_identical_requests_and_payment(self):
        with patch.object(diagnostics, "observe_build", return_value=False) as observe:
            self.assertEqual(self.purchase()["state"], "paid")
        observe.assert_called_once()
        self.assert_unchanged_paid_flow()

    def test_observation_follows_load_and_precedes_payment_with_isolated_snapshots(
        self,
    ):
        observed = []

        def observe(build, loaded, **values):
            self.assertIsNot(build, self.build)
            self.assertIsNot(loaded, self.fixture.final)
            self.assertIsNot(loaded["components"], self.fixture.final["components"])
            self.assertEqual(
                self.fixture.client.request.call_args.args[:2],
                ("PUT", "/api/v2/purchases/checkout-123/checkout"),
            )
            self.assertEqual(self.fixture.payments(), [])
            self.assertEqual(buying.result("123")["state"], "preparing")
            observed.append(values)

        with patch.object(diagnostics, "observe_build", side_effect=observe):
            self.assertEqual(self.purchase()["state"], "paid")
        self.assertEqual(
            observed, [{"item_id": "123", "item_price": 1500, "maximum": 2000}]
        )
        self.assert_unchanged_paid_flow()

    def test_missing_build_components_remain_observation_only(self):
        self.build = {"id": "checkout-123"}
        with patch.object(
            diagnostics, "observe_build", wraps=diagnostics.observe_build
        ) as observe:
            self.assertEqual(self.purchase()["state"], "paid")
        observe.assert_called_once()
        self.assert_unchanged_paid_flow()

    def test_malformed_build_components_remain_observation_only(self):
        self.build = {"id": "checkout-123", "components": "unreadable-build"}
        with patch.object(
            diagnostics, "observe_build", wraps=diagnostics.observe_build
        ) as observe:
            self.assertEqual(self.purchase()["state"], "paid")
        observe.assert_called_once()
        self.assert_unchanged_paid_flow()

    def test_throwing_observer_cannot_stop_or_repeat_purchase(self):
        with patch.object(
            diagnostics, "observe_build", side_effect=RuntimeError("optional-observer")
        ):
            self.assertEqual(self.purchase()["state"], "paid")
        self.assert_unchanged_paid_flow()

    def test_mutating_throwing_observer_cannot_change_live_checksum_or_full_total(self):
        before = copy.deepcopy((self.build, self.fixture.final))

        def observe(build, loaded, **values):
            build.clear()
            loaded["id"] = "different-checkout"
            loaded["checksum"] = "unsafe-checksum"
            loaded["components"].clear()
            raise RuntimeError("optional-observer")

        with patch.object(diagnostics, "observe_build", side_effect=observe):
            self.assertEqual(self.purchase()["state"], "paid")
        self.assertEqual((self.build, self.fixture.final), before)
        self.assert_unchanged_paid_flow()

    def test_snapshot_failure_does_not_call_observer_or_change_purchase(self):
        with patch(
            "copy.deepcopy", side_effect=RuntimeError("optional-snapshot")
        ), patch.object(diagnostics, "observe_build") as observe:
            self.assertEqual(self.purchase()["state"], "paid")
        observe.assert_not_called()
        self.assert_unchanged_paid_flow()

    def test_missing_optional_module_does_not_change_purchase(self):
        import builtins

        original = builtins.__import__

        def import_module(name, *args, **kwargs):
            if name == "vinted_checkout_diagnostics":
                raise ImportError("optional-observer")
            return original(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_module), patch.object(
            diagnostics, "observe_build"
        ) as observe:
            self.assertEqual(self.purchase()["state"], "paid")
        observe.assert_not_called()
        self.assert_unchanged_paid_flow()

    def test_different_loaded_checkout_aborts_before_observer_and_payment(self):
        self.fixture.final["id"] = "different-checkout"
        with patch.object(diagnostics, "observe_build", return_value=True) as observe:
            self.assertEqual(self.purchase()["state"], "failed_before_payment")
        observe.assert_not_called()
        self.assertEqual(self.fixture.payments(), [])

    def test_true_evidence_cannot_override_full_cost_budget_with_balance_credit(self):
        self.fixture.final = buying_fixture.web_checkout("3.84")
        summary = self.fixture.final["components"]["order_summary_v2"]
        summary["subtotal"]["price"]["amount"] = "23.84"
        summary["deductions"] = [
            {
                "type": "order-summary-wallet-deduction",
                "price": {"amount": "-20.00", "currency_code": "GBP"},
            }
        ]
        with patch.object(diagnostics, "observe_build", return_value=True) as observe:
            outcome = self.purchase()
        observe.assert_called_once()
        self.assertEqual(outcome["state"], "failed_before_payment")
        self.assertEqual(outcome["reason"], "total_over_budget")
        self.assertEqual(self.fixture.payments(), [])


class QuoteProgressTests(unittest.TestCase):
    def setUp(self):
        self.fixture = quote_fixture.CheckoutPreferencesTests(
            "test_oneoff_without_search_budget_pays_once_and_duplicate_is_not_submitted"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_existing_quote_announces_only_operations_it_actually_performs(self):
        quote = self.fixture.prepare()
        self.fixture.client.reset_mock()
        events = []
        submission_facts = []

        def reporter(stage):
            events.append(stage)
            if stage == "submitting_payment":
                submission_facts.append(
                    (buying.result("123")["state"], len(self.fixture.payments()))
                )

        with progress.bind_progress(reporter):
            outcome = self.fixture.pay(quote["token"])
        self.assertEqual(outcome["state"], "paid")
        self.assertEqual(
            events,
            [
                "waiting",
                "checking_account",
                "loading_choices",
                "selecting_delivery",
                "submitting_payment",
            ],
        )
        self.assertEqual(submission_facts, [("paying", 0)])
        self.assertEqual(len(self.fixture.payments()), 1)
        self.assertNotIn("opening_checkout", events)
        self.assertNotIn("checking_item", events)

    def test_invalid_quote_does_not_announce_a_purchase_operation(self):
        events = []
        with progress.bind_progress(events.append), self.assertRaises(buyer.BuyerError):
            self.fixture.pay("invalid-fictional-quote")
        self.assertEqual(events, [])
        self.assertEqual(self.fixture.payments(), [])


class SecurityProgressTests(unittest.TestCase):
    def client(self):
        client = object.__new__(buyer.Client)
        client.solver_attempted = False
        client.network = {
            "enabled": True,
            "proxy": "fictional-proxy",
            "api_key": "fictional-key",
        }
        return client

    def test_actual_solver_work_restores_preceding_purchase_stage(self):
        client = self.client()
        events = []

        def solver(*args, **kwargs):
            self.assertEqual(progress.current_stage(), "security_check")
            return SimpleNamespace(state="unavailable")

        with progress.bind_progress(events.append), patch(
            "vinted_captcha.extract_challenge", return_value={"fictional": True}
        ), patch("vinted_captcha.solve_datadome", side_effect=solver) as solved:
            progress.report("opening_checkout")
            self.assertFalse(client.solve_challenge(Mock()))
            self.assertEqual(progress.current_stage(), "opening_checkout")
        solved.assert_called_once()
        self.assertEqual(
            events, ["opening_checkout", "security_check", "opening_checkout"]
        )

    def test_missing_challenge_never_announces_or_runs_a_solver(self):
        client = self.client()
        events = []
        with progress.bind_progress(events.append), patch(
            "vinted_captcha.extract_challenge", return_value=None
        ), patch("vinted_captcha.solve_datadome") as solved:
            progress.report("checking_item")
            self.assertFalse(client.solve_challenge(Mock()))
        solved.assert_not_called()
        self.assertEqual(events, ["checking_item"])

    def test_payment_challenge_never_announces_or_retries_security_work(self):
        client = self.client()
        events = []
        with progress.bind_progress(events.append), patch.object(
            client, "solve_challenge"
        ) as solved:
            progress.report("submitting_payment")
            self.assertFalse(
                client.retry_security_check(
                    Mock(status_code=403),
                    {},
                    buyer.BuyerError("challenge", reason="security_challenge"),
                    payment=True,
                )
            )
        solved.assert_not_called()
        self.assertEqual(events, ["submitting_payment"])
