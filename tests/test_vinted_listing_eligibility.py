"""Fictional rendered listings pass through the real purchase preflight only."""

import unittest
from contextlib import closing
from unittest.mock import Mock, patch

import requests
import test_vinted_buying as buying_tests
from test_vinted_page_data import flight, next_data, purchase_item

import search_settings
import vinted_budget
import vinted_buyer as buyer
import vinted_buying as buying


class RenderedListingEligibilityTests(
    buying_tests.SessionRotationFixture, unittest.TestCase
):
    transport_response = buying_tests.NativeCookieTransportTests.transport_response

    def setUp(self):
        native = patch.object(buyer, "BrowserSession", requests.Session)
        native.start()
        self.addCleanup(native.stop)
        super().setUp()
        self.row = {
            "item_id": "123",
            "query_id": 1,
            "url": buyer.BASE + "/items/123",
            "price": "15.00",
            "currency": "GBP",
        }
        self.calls = []
        self.progress = []

    def client(self):
        row, saved = self.saved()
        client = buyer.Client(saved)
        # This test begins after an already accepted same-buyer connection. It
        # exercises the real rendered page transport and purchase eligibility
        # gate without performing authentication or any purchase API request.
        client.bind_verified_session(row["user_id"], row["session"], client.exported())
        client.request = Mock(wraps=client.request)
        client.session.close = Mock(wraps=client.session.close)
        self.addCleanup(client.session.close)
        return client

    def transport(self, html):
        def send(adapter, prepared, **kwargs):
            self.calls.append((prepared.method, prepared.url))
            self.assertEqual(prepared.method, "GET")
            self.assertEqual(prepared.url, self.row["url"])
            self.assertEqual(self.calls, [("GET", self.row["url"])])
            return self.transport_response(prepared, text=html)

        return patch.object(
            requests.adapters.HTTPAdapter, "send", autospec=True, side_effect=send
        )

    def attempt(self, html):
        self.calls.clear()
        self.progress.clear()
        client = self.client()
        before = self.saved()[0]
        with (
            patch.object(buyer, "connected_client", return_value=client),
            self.transport(html),
        ):
            outcome = buying.buy(self.row, progress=self.progress.append)
        client.request.assert_not_called()
        client.session.close.assert_called_once()
        self.assertEqual(self.calls, [("GET", self.row["url"])])
        self.assertEqual(self.saved()[0], before)
        self.assertNotIn("opening_checkout", self.progress)
        self.assertNotIn("submitting_payment", self.progress)
        self.assertEqual(outcome["state"], "failed_before_payment")
        self.assertIsNone(outcome["checkout_id"])
        self.assertIsNone(outcome["transaction_id"])
        self.assertIsNone(outcome["total"])
        with closing(search_settings.connection()) as conn:
            attempts = [
                dict(row) for row in conn.execute("SELECT * FROM vinted_buy_attempts")
            ]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["state"], "failed_before_payment")
        self.assertIsNone(attempts[0]["checkout_id"])
        self.assertIsNone(attempts[0]["transaction_id"])
        return outcome

    def test_explicit_sold_or_closed_page_stops_before_conversation_checkout_payment(
        self,
    ):
        for flag, reason, phrase in (
            ("is_sold", "item_sold", "already sold"),
            ("is_closed", "item_closed", "closed or removed"),
        ):
            for can_buy in (False, True):
                with self.subTest(flag=flag, can_buy=can_buy):
                    outcome = self.attempt(
                        next_data(purchase_item(can_buy=can_buy, **{flag: True}))
                    )
                    self.assertEqual(outcome["reason"], reason)
                    self.assertIn(phrase, outcome["message"])
                    self.assertIn("No payment was sent", outcome["message"])

    def test_boolean_flight_status_reaches_the_same_closed_gate(self):
        html = flight(
            [
                ("1", purchase_item(can_buy=False, is_closed="$2")),
                ("2", True),
            ]
        )
        self.assertEqual(self.attempt(html)["reason"], "item_closed")

    def test_conflicting_status_records_stop_as_unverified_without_mixing(self):
        for flag in ("is_sold", "is_closed"):
            for first in (purchase_item(), purchase_item(**{flag: False})):
                with self.subTest(flag=flag, first=first):
                    html = next_data([first, purchase_item(**{flag: True})])
                    self.assertEqual(self.attempt(html)["reason"], "item_unavailable")

    def test_incomplete_foreign_or_malformed_records_cannot_authorize_checkout(self):
        incomplete = purchase_item(is_sold=True)
        incomplete.pop("seller_id")
        for payload in (
            incomplete,
            purchase_item(id="999", is_sold=False, is_closed=False),
            purchase_item(is_sold="false"),
            purchase_item(is_closed=0),
            {
                "target": incomplete,
                "neighbor": purchase_item(id="999", is_sold=False),
            },
        ):
            with self.subTest(payload=payload):
                self.assertEqual(
                    self.attempt(next_data(payload))["reason"], "item_unavailable"
                )

    def test_absent_target_status_cannot_be_inferred_from_neighbor_or_plugin(self):
        html = next_data(
            {
                "item": purchase_item(can_buy=False),
                "neighbor": purchase_item(id="999", is_sold=True, is_closed=True),
                "plugin": {"item_id": "123", "is_sold": True, "is_closed": True},
            }
        )
        outcome = self.attempt(html)
        self.assertEqual(outcome["reason"], "item_unavailable")
        self.assertNotIn("already sold", outcome["message"])
        self.assertNotIn("closed or removed", outcome["message"])

    def test_strict_false_optional_status_keeps_other_listing_checks(self):
        client = self.client()
        config = buyer.settings()
        limits = vinted_budget.purchase_limits(self.row)
        with self.transport(next_data(purchase_item(is_sold=False, is_closed=False))):
            self.assertEqual(
                buying.verified_listing(client, self.row, config, limits), (1500, "456")
            )
        client.request.assert_not_called()
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM vinted_buy_attempts").fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
