"""Autobuy taps report blockers and outcomes on the existing Telegram alert."""

import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest, NetworkError
from test_search_controls import DatabaseFixture

import photo_cards
import search_settings
import vinted_alerts
import vinted_buyer as buyer
import vinted_buying as buying


class FeedbackTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.batch(1, [110])
        with closing(search_settings.connection()) as conn, conn:
            self.row = dict(
                conn.execute(
                    "SELECT * FROM alert_outbox WHERE item_id='110'"
                ).fetchone()
            )
            conn.execute(
                "UPDATE vinted_buyer SET session=?,verified_at=1,user_id='99',enabled=0",
                (
                    buyer.encrypt(
                        {"cookies": {"access_token_web": "offline-test-token"}}
                    ),
                ),
            )
        self.details = vinted_alerts.get_details(self.row)
        self.query = SimpleNamespace(
            data="buy:click",
            answer=AsyncMock(),
            from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(
                message_id=42,
                chat=SimpleNamespace(id=123),
                photo=[SimpleNamespace(file_id="listing-photo")],
            ),
        )
        photo_cards.record(self.row, self.details, self.query.message)
        self.bot = SimpleNamespace(
            edit_message_reply_markup=AsyncMock(), send_message=AsyncMock()
        )
        self.context = SimpleNamespace(bot=self.bot)
        env = patch.dict(
            "os.environ", {"DASHBOARD_URL": "https://dashboard.example.test"}
        )
        env.start()
        self.addCleanup(env.stop)

    async def tap(self):
        await buying.callback(SimpleNamespace(callback_query=self.query), self.context)

    def buttons(self):
        return [
            b
            for row in self.bot.edit_message_reply_markup.call_args.kwargs[
                "reply_markup"
            ].inline_keyboard
            for b in row
        ]

    def enable(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET enabled=1")
            conn.execute("INSERT INTO vinted_search_budgets VALUES (1,2000,350)")

    async def test_off_answers_once_with_reason_and_persists_setup_links(self):
        with patch.object(buying, "buy") as buy:
            await self.tap()
        buy.assert_not_called()
        self.query.answer.assert_awaited_once()
        self.assertIn("Autobuy is off", self.query.answer.call_args.args[0])
        self.assertTrue(self.query.answer.call_args.kwargs["show_alert"])
        buttons = self.buttons()
        self.assertIn(
            "https://dashboard.example.test/search/1", [b.url for b in buttons]
        )
        self.assertIn(
            "https://dashboard.example.test/connections#vinted-buying",
            [b.url for b in buttons],
        )
        self.assertIn("buy:status", [b.callback_data for b in buttons])
        self.assertIn("buy:click", [b.callback_data for b in buttons])
        self.bot.send_message.assert_not_awaited()
        self.assertIsNone(buying.result("110"))
        row, details, _ = photo_cards.load("vinted", 42)
        self.assertEqual(details["buy_feedback"]["state"], "setup_required")
        self.assertIn(
            "buy:status",
            [
                b.callback_data
                for r in photo_cards.markup(row, details).inline_keyboard
                for b in r
            ],
        )

    async def test_expired_popup_does_not_hide_the_persistent_reason(self):
        self.query.answer.side_effect = BadRequest("Query is too old")
        await self.tap()
        self.bot.edit_message_reply_markup.assert_awaited_once()
        self.assertIn(
            "Autobuy is off",
            photo_cards.load("vinted", 42)[1]["buy_feedback"]["message"],
        )

    async def test_missing_budget_stops_before_any_purchase(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET enabled=1")
        with patch.object(buying, "buy") as buy:
            await self.tap()
        buy.assert_not_called()
        self.assertIn("search #1", self.query.answer.call_args.args[0])

    async def test_checkout_failure_uses_persistent_status_not_a_second_answer(self):
        self.enable()
        outcome = {
            "state": "failed_before_payment",
            "message": "Vinted needs delivery details.",
        }
        with patch.object(buying, "buy", return_value=outcome) as buy:
            await self.tap()
        buy.assert_called_once()
        self.query.answer.assert_awaited_once()
        self.assertEqual(
            photo_cards.load("vinted", 42)[1]["buy_feedback"]["message"],
            outcome["message"],
        )
        self.query.data = "buy:status"
        self.query.answer.reset_mock()
        with patch.object(buying, "buy") as buy:
            await self.tap()
        buy.assert_not_called()
        self.query.answer.assert_awaited_once()
        self.assertEqual(self.query.answer.call_args.args[0], outcome["message"])

    async def test_paid_or_uncertain_purchase_never_gets_a_retry_payment_button(self):
        self.enable()
        buying.claim(self.row)
        for state in ("paid", "unknown", "needs_action", "paying"):
            with self.subTest(state=state):
                buying.record(
                    "110",
                    state,
                    "Check the result in Vinted.",
                    checkout_id="checkout-123",
                )
                with patch.object(buying, "buy") as buy:
                    await self.tap()
                buy.assert_not_called()
                self.assertNotIn("buy:click", [b.callback_data for b in self.buttons()])
                self.assertIn("buy:status", [b.callback_data for b in self.buttons()])

    async def test_telegram_edit_failure_does_not_repeat_or_lose_purchase_result(self):
        self.enable()
        self.bot.edit_message_reply_markup.side_effect = NetworkError("offline")
        outcome = {"state": "unknown", "message": "Check Vinted before retrying."}
        with patch.object(buying, "buy", return_value=outcome) as buy:
            await self.tap()
        buy.assert_called_once()
        self.assertEqual(
            photo_cards.load("vinted", 42)[1]["buy_feedback"]["state"], "unknown"
        )
        self.bot.send_message.assert_not_awaited()

    async def test_another_user_cannot_view_purchase_status(self):
        self.query.data = "buy:status"
        self.query.from_user.id = 999
        await self.tap()
        self.assertIn("Only your private", self.query.answer.call_args.args[0])
        self.bot.edit_message_reply_markup.assert_not_awaited()
