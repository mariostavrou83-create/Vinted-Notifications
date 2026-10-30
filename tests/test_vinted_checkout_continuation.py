"""Offline checks for owner continuation after refused checkout requests."""

import unittest

import vinted_buyer as buyer
import vinted_buying as buying


class CheckoutContinuationTests(unittest.TestCase):
    row = {"item_id": "123", "url": "https://untrusted.invalid/ignored"}

    def buttons(self, feedback, row=None):
        return [button for line in buying.feedback_buttons(row or self.row, feedback) for button in line]

    def test_pre_payment_refusal_opens_canonical_item_without_retrying_the_server(self):
        for reason in ("security_challenge", "forbidden", "api_cookie_scope"):
            with self.subTest(reason=reason):
                buttons = self.buttons({"state": "failed_before_payment", "reason": reason})
                self.assertEqual([b.url for b in buttons if b.url], [buyer.BASE + "/items/123"])
                self.assertIn("Continue in Vinted", buttons[-1].text)
                self.assertNotIn("buy:click", [b.callback_data for b in buttons])

    def test_existing_checkout_has_one_continuation_link(self):
        buttons = self.buttons({"state": "failed_before_payment", "reason": "security_challenge", "checkout_id": "checkout-123"})
        self.assertEqual([b.url for b in buttons if b.url], [buyer.BASE + "/checkout?purchase_id=checkout-123"])
        self.assertEqual(sum("Continue in Vinted" in b.text for b in buttons), 1)

    def test_malformed_checkout_is_not_used_as_a_url(self):
        buttons = self.buttons({"state": "failed_before_payment", "reason": "forbidden", "checkout_id": "123&redirect=https://untrusted.invalid"})
        self.assertEqual([b.url for b in buttons if b.url], [buyer.BASE + "/items/123"])

    def test_invalid_item_does_not_create_a_continuation(self):
        buttons = self.buttons({"state": "failed_before_payment", "reason": "security_challenge"}, {"item_id": "../evil", "url": self.row["url"]})
        self.assertFalse(any(b.url for b in buttons))

    def test_uncertain_payment_never_gets_a_fresh_item_purchase_link(self):
        for state in ("unknown", "paying", "needs_action", "paid"):
            with self.subTest(state=state):
                buttons = self.buttons({"state": state, "reason": "forbidden", "checkout_id": "checkout-123"})
                self.assertFalse(any(b.url == buyer.BASE + "/items/123" for b in buttons))
                self.assertFalse(any("Continue in Vinted" in b.text for b in buttons))
                self.assertNotIn("buy:click", [b.callback_data for b in buttons])
