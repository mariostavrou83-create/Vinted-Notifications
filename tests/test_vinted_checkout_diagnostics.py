"""Offline build/load observations cannot alter or authorize a purchase."""

import copy
import json
import unittest
from unittest.mock import Mock, patch

from test_vinted_buying import web_checkout

import vinted_buying as buying
import vinted_checkout_diagnostics as diagnostics


def complete_checkout():
    value = web_checkout()
    value["id"] = "private-checkout"
    value["checksum"] = "private-checksum-value"
    components = value["components"]
    components["additional_service"] = {"selected": False}
    address = components["shipping_address"]
    address["shipping_order_id"] = 300
    address["address"].update(
        country_code="GB",
        coordinates={"latitude": 51.0, "longitude": -0.1},
        line="private-address-value",
    )
    shipping = components["shipping_pickup_details"]
    shipping["shipping_order_id"] = 300
    shipping["pickup_details"]["shipping_point"].update(
        code="private-point-code", name="private-point-name"
    )
    components["payment_method"]["selected_payment_method"]["credit_card"]["id"] = 789
    return value


class CheckoutDiagnosticTests(unittest.TestCase):
    def observe(self, build, loaded, **kwargs):
        values = {"item_id": "123", "item_price": 1500, "maximum": 2000}
        values.update(kwargs)
        return diagnostics.observe_build(build, loaded, **values)

    def test_incomplete_build_and_complete_load_are_observed_without_mutation(self):
        build = {"id": "private-checkout", "checksum": "private-checksum-value"}
        loaded = complete_checkout()
        previous = copy.deepcopy((build, loaded))
        report = self.observe(build, loaded)
        self.assertFalse(report["build_complete"])
        self.assertFalse(report["build_required_components_present"])
        self.assertTrue(report["loaded_complete"])
        self.assertTrue(report["loaded_required_components_present"])
        self.assertTrue(report["loaded_item_matches"])
        self.assertTrue(report["same_checkout"])
        self.assertFalse(report["selected_required_state_equal"])
        self.assertEqual((build, loaded), previous)

    def test_complete_equal_responses_and_checksum_change_are_separate(self):
        build = complete_checkout()
        loaded = copy.deepcopy(build)
        loaded["checksum"] = "private-new-checksum"
        report = self.observe(build, loaded)
        for key in (
            "build_complete",
            "loaded_complete",
            "same_checkout",
            "same_shipping_order",
            "same_address",
            "selected_required_state_equal",
            "checksum_changed",
        ):
            self.assertTrue(report[key], key)
        loaded["checksum"] = build["checksum"]
        self.assertFalse(self.observe(build, loaded)["checksum_changed"])

    def test_changed_selected_address_shipping_and_funding_are_not_equal(self):
        build = complete_checkout()
        for change, unequal in (
            ("address", "same_address"),
            ("shipping", "same_shipping_order"),
            ("funding", None),
            ("point", None),
        ):
            with self.subTest(change=change):
                loaded = copy.deepcopy(build)
                components = loaded["components"]
                if change == "address":
                    components["shipping_address"]["address"]["id"] = 457
                elif change == "shipping":
                    components["shipping_address"]["shipping_order_id"] = 301
                    components["shipping_pickup_details"]["shipping_order_id"] = 301
                elif change == "funding":
                    components["pay_button_v2"]["total"]["price"]["amount"] = "19.00"
                else:
                    components["shipping_pickup_details"]["pickup_details"][
                        "shipping_point"
                    ]["uuid"] = "private-other-point"
                report = self.observe(build, loaded)
                self.assertTrue(report["build_complete"])
                self.assertTrue(report["loaded_complete"])
                self.assertFalse(report["selected_required_state_equal"])
                if unequal:
                    self.assertFalse(report[unequal])

    def test_current_item_full_cost_errors_and_delivery_payment_shapes_are_required(
        self,
    ):
        complete = complete_checkout()
        changes = (
            ("item", lambda c: c["order_summary_v2"]["order_items"][0].update(id=999)),
            ("error", lambda c: c["payment_method"].update(errors=["private-error"])),
            (
                "missing_order",
                lambda c: c["shipping_pickup_details"].pop("shipping_order_id"),
            ),
            (
                "foreign_address",
                lambda c: c["shipping_address"]["address"].update(country_code="IT"),
            ),
            (
                "rate",
                lambda c: c["shipping_pickup_details"]["pickup_details"][
                    "shipping_point"
                ].update(rate_uuid="wrong-rate"),
            ),
            (
                "method",
                lambda c: c["payment_method"]["pay_in_methods"][0].update(
                    enabled=False
                ),
            ),
            ("card", lambda c: c["payment_method"]["cards"][0].update(expired=True)),
            (
                "coordinates",
                lambda c: c["shipping_address"]["address"].update(coordinates=None),
            ),
        )
        for label, change in changes:
            with self.subTest(label=label):
                build = copy.deepcopy(complete)
                change(build["components"])
                report = self.observe(build, complete)
                self.assertFalse(report["build_complete"])
                self.assertTrue(report["loaded_complete"])
                self.assertFalse(report["selected_required_state_equal"])
        self.assertFalse(
            self.observe(complete, complete, maximum=1883)["build_complete"]
        )

    def test_balance_credit_does_not_hide_the_full_purchase_cost(self):
        checkout = complete_checkout()
        components = checkout["components"]
        components["pay_button_v2"]["total"]["price"]["amount"] = "3.84"
        summary = components["order_summary_v2"]
        summary["subtotal"]["price"]["amount"] = "18.84"
        summary["deductions"] = [
            {
                "type": "order-summary-wallet-deduction",
                "price": {"amount": "-15.00", "currency_code": "GBP"},
            }
        ]
        self.assertTrue(self.observe(checkout, checkout)["build_complete"])
        self.assertFalse(
            self.observe(checkout, checkout, maximum=400)["build_complete"]
        )

    def test_missing_legacy_and_malformed_data_are_conservative(self):
        for build in (None, [], "private-body", {"id": "private-checkout"}):
            with self.subTest(type=type(build).__name__):
                report = self.observe(build, complete_checkout())
                self.assertFalse(report["build_complete"])
                self.assertTrue(report["loaded_complete"])
        legacy = complete_checkout()
        legacy["components"].pop("pay_button_v2")
        self.assertFalse(self.observe(legacy, legacy)["build_complete"])
        invalid = complete_checkout()
        invalid["checksum"] = "private\nchecksum"
        self.assertFalse(self.observe(invalid, invalid)["build_checksum_present"])
        self.assertFalse(self.observe(invalid, invalid)["build_complete"])

    def test_validator_exception_or_mutation_cannot_change_original_responses(self):
        build = complete_checkout()
        loaded = copy.deepcopy(build)
        previous = copy.deepcopy((build, loaded))

        def fail(value, *args, **kwargs):
            value["checksum"] = "modified-private-value"
            raise RuntimeError("private-validation-error")

        with patch.object(buying, "checkout_prices", side_effect=fail) as validator:
            report = self.observe(build, loaded)
        self.assertEqual(validator.call_count, 2)
        self.assertFalse(report["build_complete"])
        self.assertFalse(report["loaded_complete"])
        self.assertEqual((build, loaded), previous)

    def test_logging_failure_cannot_throw_or_change_the_evidence(self):
        with patch.object(
            diagnostics.logger, "info", side_effect=RuntimeError("private-log-error")
        ):
            report = self.observe(complete_checkout(), complete_checkout())
        self.assertTrue(report["build_complete"])
        self.assertTrue(report["selected_required_state_equal"])

    def test_output_and_logs_contain_fixed_boolean_fields_only(self):
        checkout = complete_checkout()
        logger = Mock()
        with patch.object(diagnostics, "logger", logger):
            report = self.observe(checkout, checkout)
        self.assertEqual(tuple(report), diagnostics.FIELDS)
        self.assertTrue(all(type(value) is bool for value in report.values()))
        rendered = json.dumps(report) + str(logger.info.call_args)
        for secret in (
            "private-checkout",
            "private-checksum-value",
            "private-address-value",
            "private-point-code",
            "private-point-name",
            "1234",
            "18.84",
            "1500",
        ):
            self.assertNotIn(secret, rendered)
        logger.info.assert_called_once()


if __name__ == "__main__":
    unittest.main()
