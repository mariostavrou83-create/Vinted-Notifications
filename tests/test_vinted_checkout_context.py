"""Checkout entry points bind the same validated browser context before loading."""

import unittest
from unittest.mock import patch

import test_vinted_buying as buying_fixture
import test_vinted_checkout_preferences as quote_fixture

import vinted_buyer as buyer
import vinted_buying as buying

URL = "https://www.vinted.co.uk/checkout?purchase_id=checkout-123&order_id=456&order_type=transaction"


class CheckoutContextTests(unittest.TestCase):
    def setUp(self):
        self.fixture = buying_fixture.BuyingTests(
            "test_valid_checkout_pays_once_and_duplicate_tap_is_idempotent"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_main_binds_context_before_initial_load_without_changing_body_or_payment(self):
        client = self.fixture.client
        original = client.request.side_effect
        context_at_load = []

        def request(method, path, body=None):
            if method == "PUT" and path.endswith("/checkout"):
                client.set_checkout_context.assert_called_once_with("checkout-123", "456")
                self.assertEqual(body, {"components": {
                    "additional_service": {}, "payment_method": {}, "shipping_address": {},
                    "shipping_pickup_options": {}, "shipping_pickup_details": {},
                }})
                self.assertEqual(self.fixture.payments(), [])
                context_at_load.append(True)
            return original(method, path, body)

        client.request.side_effect = request
        self.assertEqual(self.fixture.run_buy()["state"], "paid")
        self.assertEqual(self.fixture.run_buy()["state"], "paid")
        self.assertEqual(context_at_load, [True])
        self.assertEqual(len(self.fixture.payments()), 1)
        self.assertEqual(self.fixture.payments()[0].args[2], {
            "checksum": "verified-checksum",
            "payment_options": {"browser_info": buying_fixture.DEVICE},
        })

    def test_existing_checkout_binds_full_context_before_unchanged_bodyless_load(self):
        client = self.fixture.client
        self.fixture.final = buying_fixture.web_checkout()
        original = client.request.side_effect

        def request(method, path, body=None):
            client.set_checkout_context.assert_called_once_with("checkout-123", "456")
            self.assertEqual((method, path, body), ("PUT", "/api/v2/purchases/checkout-123/checkout", None))
            return original(method, path, body)

        client.request.side_effect = request
        with patch.object(buyer, "connected_client", return_value=client):
            result = buying.check_checkout(URL)
        self.assertIn("No payment was submitted", result)
        self.assertEqual(self.fixture.payments(), [])

    def test_quoted_purchase_rebinds_context_on_its_new_client_before_load(self):
        fixture = quote_fixture.CheckoutPreferencesTests(
            "test_settings_save_and_older_forms_preserve_private_preferences"
        )
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        quote = fixture.prepare()
        fixture.client.reset_mock()
        original = fixture.client.request.side_effect
        initial_loads = []

        def request(method, path, body=None, **kwargs):
            if method == "PUT" and path.endswith("/checkout") and body is None:
                fixture.client.set_checkout_context.assert_called_once_with("checkout-123", "456")
                initial_loads.append(True)
            return original(method, path, body, **kwargs)

        fixture.client.request.side_effect = request
        self.assertEqual(fixture.pay(quote["token"])["state"], "paid")
        self.assertEqual(initial_loads, [True])
        self.assertEqual(len(fixture.payments()), 1)

    def test_invalid_context_stops_before_checkout_load_and_payment(self):
        self.fixture.client.set_checkout_context.side_effect = buyer.BuyerError(
            "Checkout context could not be verified", reason="not_confirmed",
        )
        outcome = self.fixture.run_buy()
        self.assertEqual(outcome["state"], "failed_before_payment")
        self.assertEqual(self.fixture.payments(), [])
        self.assertFalse(any(call.args[0] == "PUT" for call in self.fixture.client.request.call_args_list))
        self.fixture.client.set_checkout_context.assert_called_once_with("checkout-123", "456")

    def test_invalid_build_id_cannot_bind_a_context_or_load_checkout(self):
        original = self.fixture.client.request.side_effect

        def request(method, path, body=None):
            if path == "/api/v2/purchases/checkout/build":
                return {"checkout": {"id": "../other"}}
            return original(method, path, body)

        self.fixture.client.request.side_effect = request
        self.assertEqual(self.fixture.run_buy()["state"], "failed_before_payment")
        self.fixture.client.set_checkout_context.assert_not_called()
        self.assertEqual(self.fixture.payments(), [])
