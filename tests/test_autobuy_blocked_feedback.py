"""Offline blocker visibility without clearing or retrying uncertain payments."""

import json
import os
import sqlite3
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_search_controls import DatabaseFixture

import photo_cards
import search_settings
import vinted_alerts
import vinted_buy_reply as reply
import vinted_buyer as buyer
import vinted_buying as buying


class BlockedFeedbackTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"MSJ_TELEGRAM_REVIEW_ONLY": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.batch(1, [123])
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("INSERT INTO vinted_search_budgets VALUES (1,2000,220)")
            conn.execute(
                "UPDATE vinted_buyer SET session=?,verified_at=1,user_id='99',"
                "enabled=1 WHERE id=1",
                (buyer.encrypt({"cookies": {"access_token_web": "offline"}}),),
            )
            self.row = dict(
                conn.execute(
                    "SELECT * FROM alert_outbox WHERE item_id='123'"
                ).fetchone()
            )
        self.bot = SimpleNamespace(
            id=456,
            edit_message_caption=AsyncMock(),
            edit_message_text=AsyncMock(),
            send_message=AsyncMock(),
        )
        self.context = SimpleNamespace(bot=self.bot)
        self.source = SimpleNamespace(
            message_id=42,
            chat=SimpleNamespace(id=123, type="private"),
            from_user=SimpleNamespace(id=456, is_bot=True),
            forward_origin=None,
            photo=[SimpleNamespace(file_id="offline-photo")],
        )
        photo_cards.record(self.row, vinted_alerts.get_details(self.row), self.source)
        self.query = SimpleNamespace(
            data="buy:click",
            answer=AsyncMock(),
            from_user=SimpleNamespace(id=123),
            message=self.source,
        )
        self.message = SimpleNamespace(
            text="BUY",
            chat=self.source.chat,
            from_user=SimpleNamespace(id=123, is_bot=False),
            reply_to_message=self.source,
            reply_text=AsyncMock(),
        )

    def earlier(self, state="unknown", item_id="10320418087"):
        self.assertTrue(buying.claim({"item_id": item_id}))
        buying.record(
            item_id,
            state,
            "Keep the existing payment result",
            checkout_id="existing-checkout",
            total=1900,
            buyer_id="99",
            transaction_id="789",
        )
        return buying.result(item_id)

    def saved_feedback(self):
        return photo_cards.load("vinted", 42)[1]["buy_feedback"]

    def test_each_uncertain_state_blocks_before_claim_or_network(self):
        for state in ("paying", "unknown", "needs_action"):
            with self.subTest(state=state):
                previous = self.earlier(state)
                with patch.object(buying, "claim") as claim, patch.object(
                    buyer, "connected_client"
                ) as network, self.assertRaises(buyer.BuyerError) as blocked:
                    buying.buy(self.row)
                self.assertEqual(blocked.exception.reason, "earlier_payment_uncertain")
                self.assertIn("10320418087", str(blocked.exception))
                self.assertIn("Vinted Purchases", str(blocked.exception))
                self.assertIn("No new payment was sent", str(blocked.exception))
                claim.assert_not_called()
                network.assert_not_called()
                self.assertIsNone(buying.result("123"))
                self.assertEqual(buying.result("10320418087"), previous)
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("DELETE FROM vinted_buy_attempts")

    def test_non_numeric_blocking_id_is_not_exposed_and_guard_is_retained(self):
        previous = self.earlier(item_id="private-unexpected-value")
        with self.assertRaises(buyer.BuyerError) as blocked:
            buying.guard_new_payment("123")
        self.assertNotIn("private-unexpected-value", str(blocked.exception))
        self.assertIn("a saved Vinted item", str(blocked.exception))
        self.assertEqual(buying.result("private-unexpected-value"), previous)

    def test_terminal_states_and_same_item_keep_existing_guard_behavior(self):
        previous = self.earlier()
        buying.guard_new_payment("10320418087")
        self.assertEqual(buying.result("10320418087"), previous)
        for state in ("paid", "payment_failed", "failed_before_payment"):
            buying.record("10320418087", state, "Terminal result")
            previous = buying.result("10320418087")
            buying.guard_new_payment("123")
            self.assertEqual(buying.result("10320418087"), previous)

    def test_blocked_buttons_offer_details_without_retry(self):
        for state in ("setup_required", "failed_before_payment"):
            with self.subTest(state=state):
                buttons = [
                    button
                    for row in buying.feedback_buttons(
                        self.row,
                        {"state": state, "reason": "earlier_payment_uncertain"},
                    )
                    for button in row
                ]
                self.assertEqual(len(buttons), 1)
                self.assertEqual(
                    buttons[0].text, "Earlier payment unconfirmed · details"
                )
                self.assertEqual(buttons[0].callback_data, "buy:status")

    async def test_callback_reports_exact_blocker_and_preserves_uncertain_payment(self):
        previous = self.earlier()
        with patch.object(buying, "claim") as claim, patch.object(
            buyer, "connected_client"
        ) as network, self.assertLogs("vinted_buying", level="INFO") as logs:
            await buying.callback(
                SimpleNamespace(callback_query=self.query), self.context
            )
        feedback = self.saved_feedback()
        self.assertEqual(feedback["reason"], "earlier_payment_uncertain")
        self.assertIn("10320418087", feedback["message"])
        self.assertEqual(buying.result("10320418087"), previous)
        self.assertIsNone(buying.result("123"))
        claim.assert_not_called()
        network.assert_not_called()
        self.assertTrue(
            any(
                "source=callback item=123 reason=earlier_payment_uncertain http=None"
                in line
                for line in logs.output
            )
        )
        markup = self.bot.edit_message_caption.call_args.kwargs["reply_markup"]
        self.assertNotIn(
            "buy:click",
            [b.callback_data for row in markup.inline_keyboard for b in row],
        )
        self.query.data = "buy:status"
        with patch.object(buying, "buy") as buy, patch.object(
            buying, "check_payment"
        ) as check:
            await buying.callback(
                SimpleNamespace(callback_query=self.query), self.context
            )
        buy.assert_not_called()
        check.assert_not_called()
        self.assertIn("10320418087", self.query.answer.call_args.args[0])

    async def test_buy_reply_reports_exact_blocker_without_new_purchase(self):
        previous = self.earlier()
        with patch.object(buying, "claim") as claim, patch.object(
            buyer, "connected_client"
        ) as network, self.assertLogs("vinted_buying", level="INFO") as logs:
            await reply.reply_buy(SimpleNamespace(message=self.message), self.context)
        self.assertEqual(self.saved_feedback()["reason"], "earlier_payment_uncertain")
        self.assertIn("10320418087", self.message.reply_text.call_args.args[0])
        self.assertEqual(buying.result("10320418087"), previous)
        self.assertIsNone(buying.result("123"))
        claim.assert_not_called()
        network.assert_not_called()
        self.assertTrue(
            any(
                "source=reply item=123 reason=earlier_payment_uncertain http=None"
                in line
                for line in logs.output
            )
        )

    async def test_callback_local_setup_failure_is_logged_before_buy(self):
        error = buyer.BuyerError("Autobuy is off", reason="disabled")
        with patch.object(buying, "ready", side_effect=error), patch.object(
            buying, "buy"
        ) as buy, self.assertLogs("vinted_buying", level="INFO") as logs:
            await buying.callback(
                SimpleNamespace(callback_query=self.query), self.context
            )
        buy.assert_not_called()
        self.assertTrue(
            any("source=callback item=123 reason=disabled" in x for x in logs.output)
        )

    def test_logging_redacts_exception_text_and_non_fixed_values(self):
        error = buyer.BuyerError(
            "secret message", "secret status", reason="secret reason"
        )
        with self.assertLogs("vinted_buying", level="INFO") as logs:
            buying.log_setup_block(
                {"item_id": "secret item"}, error, source="secret source"
            )
        self.assertEqual(len(logs.output), 1)
        self.assertIn(
            "source=unknown item=invalid reason=not_confirmed http=None",
            logs.output[0],
        )
        self.assertNotIn("secret", json.dumps(logs.output))

    def test_logging_keeps_numeric_http_status_only(self):
        for value, expected in ((400, "400"), (True, "None"), (999, "None")):
            with self.subTest(status=value), self.assertLogs(
                "vinted_buying", level="INFO"
            ) as logs:
                buying.log_setup_block(
                    self.row,
                    buyer.BuyerError("Ignored", value, reason="busy"),
                    source="reply",
                )
            self.assertIn(f"reason=busy http={expected}", logs.output[0])


class OwnerFailedResolutionTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.item_id = "10320418087"
        self.assertTrue(buying.claim({"item_id": self.item_id}))
        buying.record(
            self.item_id,
            "unknown",
            "The prior payment was unconfirmed",
            checkout_id="original-checkout",
            total=1900,
            buyer_id="99",
            transaction_id="789",
            action_url="https://bank.example.test/private-action",
        )
        self.saved = buying.result(self.item_id)
        self.version = str(float(self.saved["updated"]))

    def audit_rows(self):
        with closing(search_settings.connection()) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='vinted_payment_resolutions'"
            ).fetchone()
            return (
                list(conn.execute("SELECT * FROM vinted_payment_resolutions"))
                if exists
                else []
            )

    def assert_untouched(self, previous=None):
        self.assertEqual(buying.result(self.item_id), previous or self.saved)
        self.assertEqual(self.audit_rows(), [])

    def test_owner_confirmation_preserves_bindings_and_audits_original_attempt(self):
        with patch.object(buyer, "connected_client") as network, patch.object(
            buying, "claim"
        ) as claim, patch.object(buying, "check_payment") as check:
            outcome = buying.resolve_failed(self.item_id, self.version)
        self.assertEqual(outcome["state"], "payment_failed")
        self.assertEqual(
            outcome["message"],
            "Owner confirmed this attempt failed; no payment resubmitted.",
        )
        for key in ("checkout_id", "transaction_id", "buyer_id", "total", "action_url"):
            self.assertEqual(outcome[key], self.saved[key])
        network.assert_not_called()
        claim.assert_not_called()
        check.assert_not_called()
        audit = self.audit_rows()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["item_id"], self.item_id)
        self.assertEqual(audit[0]["operation"], "owner_confirmed_failed")
        self.assertEqual(audit[0]["original_state"], "unknown")
        self.assertEqual(audit[0]["original_updated"], self.saved["updated"])
        original = json.loads(audit[0]["original_attempt"])
        self.assertEqual(
            original,
            {key: value for key, value in self.saved.items() if key != "action_url"},
        )
        self.assertNotIn("private-action", audit[0]["original_attempt"])
        buying.guard_new_payment("123")

    def test_same_confirmed_decision_is_idempotent(self):
        outcome = buying.resolve_failed(self.item_id, self.version)
        first_audit = [dict(row) for row in self.audit_rows()]
        repeated = buying.resolve_failed(self.item_id, self.version)
        self.assertEqual(repeated, outcome)
        self.assertEqual([dict(row) for row in self.audit_rows()], first_audit)
        with self.assertRaises(buyer.BuyerError):
            buying.resolve_failed(self.item_id, str(outcome["updated"]))
        self.assertEqual(buying.result(self.item_id), outcome)
        self.assertEqual([dict(row) for row in self.audit_rows()], first_audit)

    def test_needs_action_requires_explicit_versioned_confirmation(self):
        buying.record(self.item_id, "needs_action", "Awaiting confirmation")
        saved = buying.result(self.item_id)
        outcome = buying.resolve_failed(self.item_id, str(float(saved["updated"])))
        self.assertEqual(outcome["state"], "payment_failed")
        self.assertEqual(self.audit_rows()[0]["original_state"], "needs_action")

    def test_other_states_are_never_changed_by_owner_failure_resolution(self):
        for state in (
            "paying",
            "preparing",
            "paid",
            "payment_failed",
            "failed_before_payment",
        ):
            with self.subTest(state=state):
                buying.record(self.item_id, state, "Keep this state")
                saved = buying.result(self.item_id)
                with self.assertRaises(buyer.BuyerError) as error:
                    buying.resolve_failed(self.item_id, str(float(saved["updated"])))
                self.assertEqual(error.exception.reason, "resolution_not_allowed")
                self.assert_untouched(saved)

    def test_changed_version_is_rejected_even_if_attempt_is_still_unknown(self):
        buying.record(self.item_id, "unknown", "A newer saved result")
        saved = buying.result(self.item_id)
        with self.assertRaises(buyer.BuyerError) as error:
            buying.resolve_failed(self.item_id, self.version)
        self.assertEqual(error.exception.reason, "resolution_stale")
        self.assert_untouched(saved)

    def test_invalid_or_noncanonical_versions_never_change_attempt(self):
        for version in (
            None,
            self.saved["updated"],
            "",
            "nan",
            "inf",
            "-1.0",
            "1",
            self.version + " ",
        ):
            with self.subTest(version=version), self.assertRaises(buyer.BuyerError):
                buying.resolve_failed(self.item_id, version)
            self.assert_untouched()

    def test_invalid_or_missing_item_never_changes_existing_attempt(self):
        for item_id in (None, 123, "not-an-item", "1" * 25, "987654"):
            with self.subTest(item_id=item_id), self.assertRaises(buyer.BuyerError):
                buying.resolve_failed(item_id, self.version)
            self.assert_untouched()

    def test_busy_buyer_lock_never_changes_attempt(self):
        with patch.object(
            buyer, "exclusive", side_effect=buyer.BuyerError("Busy", reason="busy")
        ), self.assertRaises(buyer.BuyerError) as error:
            buying.resolve_failed(self.item_id, self.version)
        self.assertEqual(error.exception.reason, "busy")
        self.assert_untouched()

    def test_failed_audit_insert_rolls_back_resolution(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("""CREATE TABLE vinted_payment_resolutions (
                    id INTEGER PRIMARY KEY,item_id TEXT NOT NULL,operation TEXT NOT NULL,
                    original_state TEXT NOT NULL,original_updated REAL NOT NULL,
                    original_attempt TEXT NOT NULL,resolved_at REAL NOT NULL)""")
            conn.execute(
                "CREATE TRIGGER reject_resolution BEFORE INSERT ON vinted_payment_resolutions "
                "BEGIN SELECT RAISE(ABORT,'offline audit failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            buying.resolve_failed(self.item_id, self.version)
        self.assert_untouched()

    def test_other_uncertain_items_continue_to_block_new_purchases(self):
        self.assertTrue(buying.claim({"item_id": "456"}))
        buying.record("456", "unknown", "Keep the other uncertain payment")
        other = buying.result("456")
        buying.resolve_failed(self.item_id, self.version)
        with self.assertRaises(buyer.BuyerError) as error:
            buying.guard_new_payment("123")
        self.assertEqual(error.exception.reason, "earlier_payment_uncertain")
        self.assertIn("456", str(error.exception))
        self.assertEqual(buying.result("456"), other)
        self.assertIsNone(buying.result("123"))

    def test_failed_state_update_rolls_back_audit_and_original_attempt(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "CREATE TRIGGER reject_failed_state BEFORE UPDATE ON vinted_buy_attempts "
                "WHEN NEW.state='payment_failed' "
                "BEGIN SELECT RAISE(ABORT,'offline update failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            buying.resolve_failed(self.item_id, self.version)
        self.assert_untouched()


if __name__ == "__main__":
    unittest.main()
