"""Canonical request context uses verified IDs without live marketplace calls."""

import json
import unittest
from unittest.mock import patch

import requests

import vinted_buyer as buyer


class RequestContextTests(unittest.TestCase):
    def setUp(self):
        with patch.object(buyer, "network_configuration", return_value={"proxy": "", "api_key": "", "enabled": False}):
            self.client = buyer.Client()
        self.addCleanup(self.client.session.close)

    def response(self, *, data=None, html=None, status=200, location=None):
        response = requests.Response()
        response.status_code = status
        response.url = buyer.BASE + "/items/123"
        response._content = html.encode() if html is not None else json.dumps(data or {}).encode()
        response._content_consumed = True
        if location:
            response.headers["Location"] = location
        return response

    def test_checkout_load_and_payment_keep_full_canonical_context(self):
        expected = buyer.BASE + "/checkout?purchase_id=checkout-123&order_id=456&order_type=transaction"
        self.assertEqual(self.client.set_checkout_context("checkout-123", 456), expected)
        observed = []

        def transport(method, url, **kwargs):
            prepared = self.client.session.prepare_request(requests.Request(method, url, json=kwargs.get("json")))
            observed.append(prepared.headers["Referer"])
            return self.response(data={"checkout": {"id": "checkout-123"}})

        with patch.object(self.client.session, "request", side_effect=transport):
            self.client.request("PUT", "/api/v2/purchases/checkout-123/checkout")
            self.client.request("POST", "/api/v2/purchases/checkout-123/checkout/payment", {"checksum": "offline-checksum"})
        self.assertEqual(observed, [expected, expected])

    def test_context_cannot_be_reused_for_a_different_purchase(self):
        self.client.set_checkout_context("checkout-123", "456")
        with patch.object(self.client.session, "request") as request:
            for suffix in ("checkout", "checkout/payment"):
                with self.subTest(suffix=suffix), self.assertRaises(buyer.BuyerError) as error:
                    self.client.request("POST", "/api/v2/purchases/different/" + suffix)
                self.assertEqual(error.exception.reason, "checkout_mismatch")
        request.assert_not_called()

    def test_invalid_ids_cannot_change_headers_or_create_a_request(self):
        before = dict(self.client.session.headers)
        for purchase, transaction in (
            ("bad&redirect=elsewhere", "456"), ("", "456"), ("x" * 101, "456"),
            ("https://untrusted.invalid", "456"), (True, "456"), (123, "456"),
            ("checkout-123", True), ("checkout-123", "456&bad=1"), ("checkout-123", -1),
        ):
            with self.subTest(purchase=purchase, transaction=transaction):
                with self.assertRaises(buyer.BuyerError):
                    self.client.set_checkout_context(purchase, transaction)
                self.assertEqual(dict(self.client.session.headers), before)

    def test_no_context_retains_existing_payment_referer_fallback(self):
        observed = []

        def transport(*args, **kwargs):
            observed.append(self.client.session.headers["Referer"])
            return self.response()

        with patch.object(self.client.session, "request", side_effect=transport):
            self.client.request("POST", "/api/v2/purchases/checkout-123/checkout/payment", {})
        self.assertEqual(observed, [buyer.BASE + "/checkout?purchase_id=checkout-123"])

    def test_verified_listing_becomes_context_and_clears_old_checkout(self):
        self.client.set_checkout_context("older-checkout", "456")
        url = buyer.BASE + "/items/123-valid-listing"
        html = '<meta name="csrf-token" content="current-csrf-token-0123456789">'
        with patch.object(self.client.session, "get", return_value=self.response(html=html)), patch(
            "vinted_page_data.parse_purchase_item", return_value={"id": "123"}
        ):
            self.client.listing_page(url, "123")
        self.assertEqual(self.client.session.headers["Referer"], url)
        self.assertIsNone(self.client._checkout_context)
        self.assertEqual(self.client.session.headers["X-CSRF-Token"], "current-csrf-token-0123456789")

    def test_verified_canonical_listing_redirect_becomes_context(self):
        initial = buyer.BASE + "/items/123"
        final = buyer.BASE + "/items/123-current-slug"
        with patch.object(self.client.session, "get", side_effect=[
            self.response(status=302, location=final), self.response(html="offline listing"),
        ]) as get, patch("vinted_page_data.parse_purchase_item", return_value={"id": "123"}):
            self.client.listing_page(initial, "123")
        self.assertEqual([call.args[0] for call in get.call_args_list], [initial, final])
        self.assertEqual(self.client.session.headers["Referer"], final)

    def test_unreadable_listing_cannot_replace_page_context(self):
        before = self.client.session.headers["Referer"]
        with patch.object(self.client.session, "get", return_value=self.response(html="offline unreadable")), patch(
            "vinted_page_data.parse_purchase_item", return_value=None
        ):
            with self.assertRaises(buyer.BuyerError):
                self.client.listing_page(buyer.BASE + "/items/123", "123")
        self.assertEqual(self.client.session.headers["Referer"], before)
