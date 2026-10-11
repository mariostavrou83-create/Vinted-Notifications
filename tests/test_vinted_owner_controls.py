"""Offline owner decisions against the real attempt and Telegram card stores."""

import json
import re
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest
from test_search_controls import DatabaseFixture

import photo_cards
import search_settings
import vinted_alerts
import vinted_buy_reply as reply
import vinted_buyer as buyer
import vinted_buying as buying
import vinted_owner_controls as controls


class OwnerControlsTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.item_id = "10338351117"
        self.pending_id = "10338220313"
        self.batch(1, [int(self.item_id), 10338351118])
        with closing(search_settings.connection()) as conn, conn:
            self.row = dict(
                conn.execute(
                    "SELECT * FROM alert_outbox WHERE item_id=?", (self.item_id,)
                ).fetchone()
            )
        self.bot = SimpleNamespace(
            id=456,
            edit_message_caption=AsyncMock(),
            edit_message_text=AsyncMock(),
            send_message=AsyncMock(),
            send_photo=AsyncMock(),
        )
        self.context = SimpleNamespace(bot=self.bot)
        self.source = self.card(self.row, 42)
        self.query = SimpleNamespace(
            data="buy:status",
            answer=AsyncMock(),
            from_user=SimpleNamespace(id=123),
            message=self.source,
        )
        # Every exercised path must remain local. Assertions also catch calls
        # swallowed by a defensive handler rather than merely raised errors.
        self.forbidden = []
        for target, name in (
            (buyer, "connected_client"),
            (buying, "buy"),
            (buying, "check_payment"),
        ):
            guard = patch.object(
                target, name, side_effect=AssertionError("Unexpected marketplace call")
            )
            mocked = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mocked.assert_not_called)
            self.forbidden.append(mocked)
        self.addCleanup(self.bot.send_message.assert_not_awaited)
        self.addCleanup(self.bot.send_photo.assert_not_awaited)

    def card(self, row, message_id):
        source = SimpleNamespace(
            message_id=message_id,
            chat=SimpleNamespace(id=123, type="private"),
            from_user=SimpleNamespace(id=456, is_bot=True),
            forward_origin=None,
            photo=[SimpleNamespace(file_id=f"offline-photo-{message_id}")],
        )
        self.assertTrue(photo_cards.record(row, vinted_alerts.get_details(row), source))
        return source

    def pending(self, item_id=None, state="unknown", updated=100.25):
        identity = item_id or self.pending_id
        self.assertTrue(buying.claim({"item_id": identity}))
        buying.record(
            identity,
            state,
            "The existing payment is unconfirmed",
            checkout_id="original-checkout",
            total=1900,
            buyer_id="99",
            transaction_id="789",
        )
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buy_attempts SET updated=? WHERE item_id=?",
                (updated, identity),
            )
        return buying.result(identity)

    async def offer(self, outcome=None):
        await buying.show_purchase_feedback(
            self.bot,
            self.source,
            outcome
            or {
                "state": "setup_required",
                "reason": "earlier_payment_uncertain",
                "message": "An earlier purchase remains unconfirmed.",
            },
        )
        return self.feedback()

    def feedback(self, message_id=42):
        return photo_cards.load("vinted", message_id)[1]["buy_feedback"]

    def callback_data(self, decision="got"):
        data = [
            button.callback_data
            for row in controls.buttons(self.feedback())
            for button in row
        ]
        return next(value for value in data if f":{decision}:" in value)

    async def tap(self, data=None):
        if data is not None:
            self.query.data = data
        await buying.callback(SimpleNamespace(callback_query=self.query), self.context)

    def audits(self):
        with closing(search_settings.connection()) as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE name='vinted_owner_purchase_resolutions'"
            ).fetchone():
                return []
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM vinted_owner_purchase_resolutions ORDER BY id"
                )
            ]

    def markup_data(self):
        row, details, _ = photo_cards.load("vinted", 42)
        return [
            button.callback_data
            for buttons in photo_cards.markup(row, details).inline_keyboard
            for button in buttons
        ]

    def assert_source_closed(self):
        saved = buying.result(self.item_id)
        self.assertIsNotNone(saved)
        self.assertNotIn(
            saved["state"],
            ("preparing", "failed_before_payment", "paying", "unknown", "needs_action"),
        )
        self.assertFalse(buying.claim(self.row, recover_preparing=True))

    async def test_got_records_owner_attestation_for_exact_earlier_item_only(self):
        original = self.pending()
        offered = await self.offer()
        self.assertEqual(offered["owner_resolution"]["item_id"], self.pending_id)
        self.assertIn(f"Did you get item {self.pending_id}?", offered["message"])
        self.assertNotIn(f"Did you get item {self.item_id}?", offered["message"])
        await self.tap(self.callback_data("got"))
        saved = buying.result(self.pending_id)
        self.assertEqual(saved["state"], "paid")
        self.assertIn("Owner confirmed", saved["message"])
        self.assertNotIn("Vinted confirmed", saved["message"])
        self.assertEqual(saved["checkout_id"], original["checkout_id"])
        audit = self.audits()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["operation"], "got")
        self.assertEqual(audit[0]["original_state"], "unknown")
        self.assertEqual(audit[0]["original_updated"], original["updated"])
        self.assertEqual(
            json.loads(audit[0]["original_attempt"])["transaction_id"], "789"
        )
        self.assert_source_closed()
        self.assertFalse(
            buying.claim({"item_id": self.pending_id}, recover_preparing=True)
        )

    async def test_miss_releases_global_block_but_never_retries_same_item(self):
        self.pending(state="needs_action")
        await self.offer()
        await self.tap(self.callback_data("miss"))
        saved = buying.result(self.pending_id)
        self.assertEqual(saved["state"], "payment_failed")
        self.assertIn("Owner confirmed", saved["message"])
        buying.guard_new_payment("10339999999")
        self.assertFalse(
            buying.claim({"item_id": self.pending_id}, recover_preparing=True)
        )
        self.assertEqual(self.audits()[0]["operation"], "miss")
        self.assert_source_closed()

    async def test_resolving_blocked_alert_does_not_rearm_manually_bought_latest(self):
        self.pending()
        await self.offer()
        await self.tap(self.callback_data("got"))
        feedback = self.feedback()
        self.assertEqual(feedback["state"], "owner_resolution_recorded")
        self.assertNotIn("buy:click", self.markup_data())
        self.assertIn("new items", feedback["message"])
        await self.tap("buy:status")
        self.assert_source_closed()
        self.assertNotIn("buy:click", self.markup_data())
        # Buttons on an older Telegram version and a fresh BUY reply cannot
        # replay the closed source listing even after the global block clears.
        closed = buying.result(self.item_id)
        await self.tap("buy:click")
        message = SimpleNamespace(
            text="BUY",
            chat=self.source.chat,
            from_user=SimpleNamespace(id=123, is_bot=False),
            reply_to_message=self.source,
            reply_text=AsyncMock(),
        )
        await reply.reply_buy(SimpleNamespace(message=message), self.context)
        self.assertEqual(buying.result(self.item_id), closed)

    async def test_same_item_decision_keeps_terminal_feedback_and_duplicate_guard(self):
        self.pending(self.item_id, state="needs_action")
        await self.offer(buying.result(self.item_id))
        await self.tap(self.callback_data("got"))
        self.assertEqual(self.feedback()["state"], "paid")
        self.assertNotIn("buy:click", self.markup_data())
        self.assertFalse(buying.claim(self.row, recover_preparing=True))
        self.assertFalse(self.feedback().get("owner_resolution"))

    async def test_other_pending_attempt_still_blocks_and_gets_its_exact_offer(self):
        self.pending(updated=100.25)
        other = self.pending("10320418087", state="needs_action", updated=200.25)
        await self.offer()
        await self.tap(self.callback_data("miss"))
        self.assertEqual(buying.result(other["item_id"]), other)
        with self.assertRaises(buyer.BuyerError) as blocked:
            buying.guard_new_payment("10339999999")
        self.assertEqual(blocked.exception.reason, "earlier_payment_uncertain")
        self.assertEqual(
            self.feedback()["owner_resolution"]["item_id"], other["item_id"]
        )
        self.assertNotIn("buy:click", self.markup_data())
        self.assertEqual(len(self.audits()), 1)

    async def test_source_payment_started_while_owner_decision_waits_stays_visible(
        self,
    ):
        self.pending()
        await self.offer()
        data = self.callback_data("miss")
        resolve = buying.resolve_owned
        observed = {}

        def payment_started_before_resolution(*args, **kwargs):
            # Simulate another authorized callback having reached its durable
            # payment marker before this owner decision acquires the buyer lock.
            observed["source"] = self.pending(
                self.item_id, state="paying", updated=200.25
            )
            return resolve(*args, **kwargs)

        with patch.object(
            buying, "resolve_owned", side_effect=payment_started_before_resolution
        ):
            await self.tap(data)
        self.assertEqual(buying.result(self.pending_id)["state"], "payment_failed")
        self.assertEqual(buying.result(self.item_id), observed["source"])
        feedback = self.feedback()
        self.assertEqual(feedback["state"], "paying")
        self.assertIn(self.item_id, feedback["message"])
        self.assertNotIn("You can use Autobuy on new items now", feedback["message"])
        self.assertFalse(feedback.get("owner_resolution"))
        self.assertNotIn("buy:click", self.markup_data())
        with self.assertRaises(buyer.BuyerError) as blocked:
            buying.guard_new_payment("10339999999")
        self.assertEqual(blocked.exception.reason, "earlier_payment_uncertain")

    async def test_corrupt_pending_identity_is_not_exposed_after_owner_decision(self):
        self.pending()
        corrupt_id = "private-unexpected-pending-value"
        corrupt = self.pending(corrupt_id, updated=200.25)
        await self.offer()
        await self.tap(self.callback_data("got"))
        self.assertEqual(buying.result(corrupt_id), corrupt)
        feedback = self.feedback()
        self.assertIn("another saved item", feedback["message"])
        self.assertNotIn(corrupt_id, json.dumps(feedback))
        self.assertNotIn("You can use Autobuy on new items now", feedback["message"])
        self.assertFalse(feedback.get("owner_resolution"))
        self.assertNotIn("buy:click", self.markup_data())
        rendered = self.bot.edit_message_caption.call_args.kwargs["caption"]
        self.assertNotIn(corrupt_id, rendered)
        with self.assertRaises(buyer.BuyerError):
            buying.guard_new_payment("10339999999")

    async def test_live_paying_attempt_offers_no_owner_decision_and_retains_guard(self):
        original = self.pending(state="paying")
        offered = await self.offer()
        self.assertFalse(offered.get("owner_resolution"))
        self.assertEqual(controls.buttons(offered), [])
        self.assertEqual(buying.result(self.pending_id), original)
        with self.assertRaises(buyer.BuyerError):
            buying.resolve_owned(self.pending_id, str(original["updated"]), "miss")
        self.assertEqual(buying.result(self.pending_id), original)
        self.assertEqual(self.audits(), [])

    async def test_callback_cannot_switch_to_another_pending_item_on_same_card(self):
        original = self.pending()
        other = self.pending("10320418087", updated=200.25)
        await self.offer()
        offer = controls.descriptor(other)
        await self.tap(f"buy:resolve:got:{other['item_id']}:{offer['token']}")
        self.assertEqual(buying.result(self.pending_id), original)
        self.assertEqual(buying.result(other["item_id"]), other)
        self.assertEqual(self.audits(), [])
        self.assertIn("changed", self.query.answer.call_args.args[0])

    async def test_callback_cannot_be_moved_to_different_saved_alert(self):
        original = self.pending()
        await self.offer()
        data = self.callback_data("miss")
        with closing(search_settings.connection()) as conn:
            other_row = dict(
                conn.execute(
                    "SELECT * FROM alert_outbox WHERE item_id='10338351118'"
                ).fetchone()
            )
        self.query.message = self.card(other_row, 43)
        await self.tap(data)
        self.assertEqual(buying.result(self.pending_id), original)
        self.assertEqual(self.audits(), [])

    async def test_stale_offer_cannot_clear_newer_pending_state(self):
        self.pending()
        await self.offer()
        data = self.callback_data("got")
        buying.record(self.pending_id, "needs_action", "A newer payment state")
        current = buying.result(self.pending_id)
        await self.tap(data)
        self.assertEqual(buying.result(self.pending_id), current)
        self.assertEqual(self.audits(), [])
        self.assertEqual(
            self.feedback()["owner_resolution"]["updated"], str(current["updated"])
        )

    async def test_same_decision_is_idempotent_and_opposite_decision_rejected(self):
        original = self.pending()
        version = str(original["updated"])
        first = buying.resolve_owned(self.pending_id, version, "got")
        self.assertEqual(buying.resolve_owned(self.pending_id, version, "got"), first)
        with self.assertRaises(buyer.BuyerError) as rejected:
            buying.resolve_owned(self.pending_id, version, "miss")
        self.assertEqual(rejected.exception.reason, "resolution_not_allowed")
        self.assertEqual(buying.result(self.pending_id), first)
        self.assertEqual(len(self.audits()), 1)

    async def test_unauthorized_owner_or_chat_cannot_record_or_edit_resolution(self):
        original = self.pending()
        await self.offer()
        data = self.callback_data("got")
        for owner, chat, chat_type in (
            (999, 123, "private"),
            (123, 999, "private"),
            (123, 123, "group"),
        ):
            with self.subTest(owner=owner, chat=chat, chat_type=chat_type):
                self.query.from_user.id = owner
                self.source.chat.id = chat
                self.source.chat.type = chat_type
                self.bot.edit_message_caption.reset_mock()
                await self.tap(data)
                self.assertEqual(buying.result(self.pending_id), original)
                self.assertEqual(self.audits(), [])
                self.bot.edit_message_caption.assert_not_awaited()

    async def test_missing_card_callback_rejects_without_recovery_or_resolution(self):
        original = self.pending()
        await self.offer()
        data = self.callback_data("miss")
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DELETE FROM telegram_photo_cards WHERE message_id=42")
        with patch.object(photo_cards, "recover") as recover:
            await self.tap(data)
        recover.assert_not_called()
        self.assertEqual(buying.result(self.pending_id), original)
        self.assertEqual(self.audits(), [])
        self.assertIn("no longer available", self.query.answer.call_args.args[0])

    async def test_malformed_or_changed_token_cannot_record_a_decision(self):
        original = self.pending()
        await self.offer()
        data = self.callback_data("got")
        for malformed in (
            data + ":extra",
            data.replace(":got:", ":reset:"),
            data[:-1] + ("0" if data[-1] != "0" else "1"),
        ):
            with self.subTest(data=malformed):
                await self.tap(malformed)
                self.assertEqual(buying.result(self.pending_id), original)
                self.assertEqual(self.audits(), [])

    def test_longest_valid_item_callbacks_are_ascii_and_below_64_bytes(self):
        saved = {"item_id": "9" * 24, "state": "unknown", "updated": 1791742312.1234567}
        offer = controls.descriptor(saved)
        rows = controls.buttons({"owner_resolution": offer})
        self.assertEqual(len(rows[0]), 2)
        for button in rows[0]:
            self.assertLessEqual(len(button.callback_data.encode("utf-8")), 64)
            self.assertIsNotNone(
                re.fullmatch(controls.CALLBACK_PATTERN, button.callback_data)
            )

    def test_invalid_descriptors_never_create_resolution_buttons(self):
        valid = {"item_id": self.pending_id, "state": "unknown", "updated": 100.25}
        for field, value in (
            ("state", "paying"),
            ("item_id", "9" * 25),
            ("item_id", "private-value"),
            ("updated", float("nan")),
            ("updated", True),
            ("updated", -1),
        ):
            with self.subTest(field=field, value=value):
                self.assertIsNone(controls.descriptor(dict(valid, **{field: value})))
        for offer in (
            {"item_id": "9" * 25, "token": "a" * 16},
            {"item_id": self.pending_id, "token": "a" * 17},
            {"item_id": self.pending_id, "token": "not-hex"},
        ):
            self.assertEqual(controls.buttons({"owner_resolution": offer}), [])

    async def test_startup_refresh_edits_existing_blocked_card_and_sends_nothing(self):
        original = self.pending()
        with closing(search_settings.connection()) as conn, conn:
            _row, details, _ = photo_cards.load("vinted", 42)
            details["buy_feedback"] = {
                "state": "setup_required",
                "reason": "earlier_payment_uncertain",
                "message": "Previous payment is unconfirmed",
            }
            conn.execute(
                "UPDATE telegram_photo_cards SET details=? WHERE message_id=42",
                (json.dumps(details),),
            )
        self.bot.edit_message_caption.reset_mock()
        await controls.refresh_existing(self.bot, "123")
        self.bot.edit_message_caption.assert_awaited_once()
        self.assertEqual(
            self.bot.edit_message_caption.call_args.kwargs["message_id"], 42
        )
        self.assertEqual(
            self.feedback()["owner_resolution"]["item_id"], self.pending_id
        )
        self.assertEqual(buying.result(self.pending_id), original)
        self.assertIsNone(buying.result(self.item_id))

    async def test_startup_cached_earlier_block_survives_retryable_source_failure(self):
        pending = self.pending()
        source = self.pending(
            self.item_id, state="failed_before_payment", updated=200.25
        )
        with closing(search_settings.connection()) as conn, conn:
            _row, details, _ = photo_cards.load("vinted", 42)
            details["buy_feedback"] = {
                "state": "setup_required",
                "reason": "earlier_payment_uncertain",
                "message": "An earlier purchase remains unconfirmed.",
            }
            conn.execute(
                "UPDATE telegram_photo_cards SET details=? WHERE message_id=42",
                (json.dumps(details),),
            )
        await controls.refresh_existing(self.bot, "123")
        feedback = self.feedback()
        self.assertEqual(feedback["reason"], "earlier_payment_uncertain")
        self.assertEqual(feedback["owner_resolution"]["item_id"], self.pending_id)
        self.assertNotIn("buy:click", self.markup_data())
        self.assertEqual(buying.result(self.item_id), source)
        self.assertEqual(buying.result(self.pending_id), pending)
        self.bot.edit_message_caption.assert_awaited_once()

    async def test_startup_refresh_handles_vanished_card_without_recovery(self):
        self.pending()
        with patch.object(photo_cards, "load", return_value=None), patch.object(
            photo_cards, "recover"
        ) as recover:
            await controls.refresh_existing(self.bot, "123")
        recover.assert_not_called()
        self.bot.edit_message_caption.assert_not_awaited()
        self.bot.edit_message_text.assert_not_awaited()

    async def test_startup_refresh_is_bounded_to_ten_existing_messages(self):
        original = self.pending()
        identities = range(10338351200, 10338351215)
        self.batch(1, list(identities))
        messages = []
        with closing(search_settings.connection()) as conn:
            for offset, identity in enumerate(identities):
                row = dict(
                    conn.execute(
                        "SELECT * FROM alert_outbox WHERE item_id=?", (str(identity),)
                    ).fetchone()
                )
                messages.append(self.card(row, 100 + offset).message_id)
        with closing(search_settings.connection()) as conn, conn:
            for message_id in messages:
                _row, details, _ = photo_cards.load("vinted", message_id)
                details["buy_feedback"] = {
                    "state": "setup_required",
                    "reason": "earlier_payment_uncertain",
                    "message": "Previous payment is unconfirmed",
                }
                conn.execute(
                    "UPDATE telegram_photo_cards SET details=? WHERE message_id=?",
                    (json.dumps(details), message_id),
                )
            before = conn.execute(
                "SELECT COUNT(*) FROM telegram_photo_cards"
            ).fetchone()[0]
        await controls.refresh_existing(self.bot, "123")
        self.assertEqual(self.bot.edit_message_caption.await_count, 10)
        edited = {
            call.kwargs["message_id"]
            for call in self.bot.edit_message_caption.await_args_list
        }
        self.assertEqual(edited, set(messages[-10:]))
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM telegram_photo_cards").fetchone()[0],
                before,
            )
        self.assertEqual(buying.result(self.pending_id), original)

    async def test_startup_missing_telegram_message_does_not_resend_or_stop_next_edit(
        self,
    ):
        original = self.pending()
        with closing(search_settings.connection()) as conn:
            other_row = dict(
                conn.execute(
                    "SELECT * FROM alert_outbox WHERE item_id='10338351118'"
                ).fetchone()
            )
        self.card(other_row, 43)
        with closing(search_settings.connection()) as conn, conn:
            for message_id in (42, 43):
                _row, details, _ = photo_cards.load("vinted", message_id)
                details["buy_feedback"] = {
                    "state": "setup_required",
                    "reason": "earlier_payment_uncertain",
                    "message": "Previous payment is unconfirmed",
                }
                conn.execute(
                    "UPDATE telegram_photo_cards SET details=? WHERE message_id=?",
                    (json.dumps(details), message_id),
                )
        self.bot.edit_message_caption.side_effect = [
            BadRequest("Message to edit not found"),
            None,
        ]
        await controls.refresh_existing(self.bot, "123")
        self.assertEqual(self.bot.edit_message_caption.await_count, 2)
        self.assertEqual(
            [
                call.kwargs["message_id"]
                for call in self.bot.edit_message_caption.await_args_list
            ],
            [43, 42],
        )
        self.assertEqual(buying.result(self.pending_id), original)
        self.assertEqual(self.audits(), [])

    async def test_startup_refresh_does_not_touch_paid_or_live_paying_result(self):
        for state in ("paid", "paying"):
            with self.subTest(state=state):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("DELETE FROM vinted_buy_attempts")
                original = self.pending(self.item_id, state=state)
                await controls.refresh_existing(self.bot, "123")
                self.assertEqual(buying.result(self.item_id), original)
                self.bot.edit_message_caption.assert_not_awaited()

    async def test_startup_refresh_rejects_non_private_chat_identifiers(self):
        self.pending(self.item_id)
        for chat_id in (None, "", "-123", "0", "invalid"):
            with self.subTest(chat_id=chat_id):
                await controls.refresh_existing(self.bot, chat_id)
        self.bot.edit_message_caption.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
