"""Offline checkout review, explicit permission and real Telegram purchase gates."""

import asyncio
import copy
import json
import os
import time
import unittest
from contextlib import ExitStack, closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_dashboard
from test_search_controls import DatabaseFixture
from test_vinted_buying import DEVICE, web_checkout

import search_settings
import vinted_buyer as buyer
import vinted_buying as buying
import vinted_telegram_review as review
from web_ui_plugin.web_ui import create_app

PROXY = "http://offline-owner:offline-password@proxy.example.test:12323"
KEY = "CAP-offline-private-key"


class ReviewTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "MSJ_TELEGRAM_REVIEW_ONLY": "1",
                    "MSJ_TELEGRAM_REVIEW_ON_START": "",
                    "MSJ_TELEGRAM_ITEM_APPROVAL_ON_START": "",
                },
            )
        )
        self.network = self.stack.enter_context(
            patch.object(
                review.vinted_network_check,
                "check_connection",
                return_value={"outcome": "verified"},
            )
        )
        self.client = Mock()
        self.connected = self.stack.enter_context(
            patch.object(buyer, "connected_client", return_value=self.client)
        )
        self.final = web_checkout()
        self.final["components"]["shipping_pickup_details"]["pickup_details"][
            "shipping_point"
        ]["name"] = "Fictional pickup point"
        self.payment = {"payment": {"status": "success"}}

        def response(method, path, body=None):
            if path == "/api/v2/items/123":
                return {
                    "item": {
                        "id": 123,
                        "user_id": 100,
                        "can_buy": True,
                        "price": {"amount": "15.00", "currency_code": "GBP"},
                    }
                }
            if path == "/api/v2/conversations":
                return {"conversation": {"transaction": {"id": 456}}}
            if path == "/api/v2/purchases/checkout/build":
                return {"checkout": {"id": "checkout-123"}}
            if path == "/api/v2/purchases/checkout-123/checkout":
                return {"checkout": copy.deepcopy(self.final)}
            if path == "/api/v2/purchases/checkout-123/checkout/payment":
                if isinstance(self.payment, Exception):
                    raise self.payment
                return self.payment
            raise AssertionError((method, path))

        self.client.request.side_effect = response
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("INSERT OR IGNORE INTO search_dashboard(query_id) VALUES (1)")
            conn.execute("INSERT INTO vinted_search_budgets VALUES (1,2000,350)")
            conn.execute(
                "UPDATE vinted_buyer SET session=?,verified_at=1,user_id='99',username='owner',enabled=0,browser_info=?,network=? WHERE id=1",
                (
                    buyer.encrypt(
                        {"cookies": {"access_token_web": "private-session-token"}}
                    ),
                    json.dumps(DEVICE),
                    buyer.encrypt({"proxy": PROXY, "api_key": KEY, "enabled": True}),
                ),
            )
            conn.execute(
                "INSERT INTO alert_outbox(item_id,query_id,search_name,content,url,title,price,currency,found_at,status,telegram_message_id,sent_at,platform) VALUES ('123',1,'Search 1','saved alert','https://www.vinted.co.uk/items/123','Fictional test item','15.00','GBP',?,'sent',777,?,'vinted')",
                (time.time(), time.time()),
            )
            self.row = dict(
                conn.execute(
                    "SELECT * FROM alert_outbox WHERE item_id='123'"
                ).fetchone()
            )

    def posts(self, suffix):
        return [
            c
            for c in self.client.request.call_args_list
            if c.args[0] == "POST" and c.args[1].endswith(suffix)
        ]

    def quoted(self):
        result = review.review_latest()
        self.assertEqual(result["outcome"], "quoted", result)
        return result

    def approved(self):
        self.quoted()
        return review.approve("123", 1884)

    def test_review_quotes_actual_choices_without_payment_and_preserves_searches(self):
        result = self.quoted()
        self.assertEqual(result["total"], 1884)
        self.assertEqual(result["search_maximum_total"], 2000)
        self.assertEqual(result["delivery_choice"], "pickup")
        self.assertEqual(result["payment_method"], "Saved card ending 1234")
        self.assertEqual(result["pickup_name"], "Fictional pickup point")
        self.assertTrue(result["checkout_created"])
        self.assertEqual(result["reservation"], "not_confirmed")
        self.assertFalse(result["payment_submitted"])
        self.assertFalse(buyer.settings()["enabled"])
        self.assertEqual(len(self.posts("/conversations")), 1)
        self.assertEqual(len(self.posts("/checkout/build")), 1)
        self.assertEqual(self.posts("/payment"), [])
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0], 44
            )
            self.assertEqual(
                conn.execute(
                    "SELECT max_total FROM vinted_search_budgets WHERE query_id=1"
                ).fetchone()[0],
                2000,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM vinted_buy_attempts").fetchone()[0],
                0,
            )
        text = json.dumps(result) + json.dumps(review.public_review())
        for secret in (
            PROXY,
            KEY,
            "private-session-token",
            "verified-checksum",
            "address_id",
            "buyer_id",
        ):
            self.assertNotIn(secret, text)

    def test_repeated_review_reuses_checkout_and_revokes_previous_permission(self):
        self.approved()
        # A real connection check always turns buying off before reviewing.
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET enabled=0")
        result = self.quoted()
        self.assertTrue(result["reused_checkout"])
        self.assertFalse(review.public_review()["approved"])
        self.assertEqual(len(self.posts("/conversations")), 1)
        self.assertEqual(len(self.posts("/checkout/build")), 1)
        self.assertEqual(self.posts("/payment"), [])

    def test_connection_refusal_creates_no_checkout_or_login(self):
        self.network.return_value = {"outcome": "unverified", "stage": "buyer_account"}
        result = review.review_latest()
        self.assertEqual(result["stage"], "connection_buyer_account")
        self.client.request.assert_not_called()
        self.assertFalse(result["checkout_created"])

    def test_actual_over_budget_total_is_reported_and_cannot_be_approved(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_search_budgets SET max_total=1800 WHERE query_id=1"
            )
        result = review.review_latest()
        self.assertEqual(result["stage"], "search_limit")
        self.assertEqual(result["total"], 1884)
        self.assertEqual(result["search_maximum_total"], 1800)
        self.assertTrue(result["checkout_created"])
        with self.assertRaises(buyer.BuyerError):
            review.approve("123", 1884)
        self.assertFalse(buyer.settings()["enabled"])
        self.assertEqual(self.posts("/payment"), [])

    def test_wrong_checkout_item_is_rejected(self):
        self.final["components"]["order_summary_v2"]["order_items"][0]["id"] = 999
        result = review.review_latest()
        self.assertEqual(result["outcome"], "unverified")
        self.assertEqual(self.posts("/payment"), [])

    def test_exact_item_and_total_are_required_before_enabling(self):
        self.quoted()
        for item, maximum in (("999", 1884), ("123", 1900), ("123", True)):
            with self.subTest(item=item, maximum=maximum), self.assertRaises(
                buyer.BuyerError
            ):
                review.approve(item, maximum)
        self.assertEqual(self.network.call_count, 1)
        self.assertFalse(buyer.settings()["enabled"])

    def test_approval_never_sends_payment_and_other_alerts_are_blocked(self):
        result = self.approved()
        self.assertTrue(result["only_reviewed_item_enabled"])
        self.assertFalse(result["payment_submitted"])
        self.assertTrue(buyer.settings()["enabled"])
        self.assertEqual(self.posts("/payment"), [])
        with self.assertRaises(buyer.BuyerError) as error:
            buying.buy(
                dict(self.row, item_id="999", url="https://www.vinted.co.uk/items/999")
            )
        self.assertEqual(error.exception.reason, "item_approval_required")
        self.assertEqual(self.posts("/payment"), [])

    def test_approved_telegram_path_reuses_checkout_and_duplicate_taps_pay_once(self):
        self.approved()
        self.assertEqual(buying.buy(self.row)["state"], "paid")
        self.assertEqual(buying.buy(self.row)["state"], "paid")
        self.assertEqual(len(self.posts("/payment")), 1)
        self.assertEqual(len(self.posts("/conversations")), 1)
        self.assertEqual(len(self.posts("/checkout/build")), 1)
        self.assertEqual(buying.result("123")["total"], 1884)

    def test_owner_bound_telegram_callback_uses_approved_quote_and_pays_once(self):
        import photo_cards

        self.approved()
        query = SimpleNamespace(
            data="buy:click",
            from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(chat=SimpleNamespace(id=123), message_id=777),
        )
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace(bot=Mock())
        with patch.object(
            photo_cards, "recover", return_value=(self.row, {}, {})
        ), patch.object(photo_cards, "answer", new_callable=AsyncMock), patch.object(
            buying, "show_feedback", new_callable=AsyncMock
        ) as feedback:
            asyncio.run(buying.callback(update, context))
            asyncio.run(buying.callback(update, context))
        self.assertEqual(len(self.posts("/payment")), 1)
        self.assertEqual(feedback.call_args.args[-1]["state"], "paid")

    def test_uncertain_payment_is_reconciled_without_another_post(self):
        self.approved()
        self.payment = buyer.BuyerError("Timeout", reason="network")
        self.assertEqual(buying.buy(self.row)["state"], "unknown")
        self.assertEqual(buying.buy(self.row)["state"], "unknown")
        self.payment = {"payment": {"status": "success"}}
        self.assertEqual(buying.check_payment("123")["state"], "paid")
        self.assertEqual(len(self.posts("/payment")), 1)

    def test_price_or_delivery_change_cannot_charge(self):
        self.approved()
        self.final["components"]["pay_button_v2"]["total"]["price"]["amount"] = "19.00"
        self.assertEqual(buying.buy(self.row)["state"], "failed_before_payment")
        self.assertEqual(self.posts("/payment"), [])
        self.final = web_checkout()
        self.final["components"]["shipping_pickup_details"]["pickup_details"][
            "shipping_point"
        ]["uuid"] = "different-point"
        self.assertEqual(buying.buy(self.row)["state"], "failed_before_payment")
        self.assertEqual(self.posts("/payment"), [])

    def test_current_search_limit_is_rechecked_after_delivery_selection(self):
        self.approved()
        original = buying.configure_checkout_choices

        def changed(*args):
            checked = original(*args)
            with closing(search_settings.connection()) as conn, conn:
                conn.execute(
                    "UPDATE vinted_search_budgets SET max_total=1800 WHERE query_id=1"
                )
            return checked

        with patch.object(buying, "configure_checkout_choices", side_effect=changed):
            self.assertEqual(buying.buy(self.row)["state"], "failed_before_payment")
        self.assertEqual(self.posts("/payment"), [])

    def test_quote_expiring_during_checkout_stops_before_payment(self):
        self.approved()
        original = buying.configure_checkout_choices
        clock = [time.time()]

        def expires(*args):
            checked = original(*args)
            clock[0] += 1801
            return checked

        with patch.object(
            buying.time, "time", side_effect=lambda: clock[0]
        ), patch.object(buying, "configure_checkout_choices", side_effect=expires):
            outcome = buying.buy(self.row)
        self.assertEqual(outcome["state"], "failed_before_payment")
        self.assertIn("expired", outcome["message"])
        self.assertEqual(self.posts("/payment"), [])

    def test_expired_quote_changed_network_and_inactive_search_fail_closed(self):
        self.quoted()
        saved = review.load_review()
        saved["created"] -= 1801
        review.save_review(saved)
        with self.assertRaises(buyer.BuyerError):
            review.approve("123", 1884)
        self.quoted()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE search_dashboard SET paused=1 WHERE query_id=1")
        with self.assertRaises(buyer.BuyerError):
            review.approve("123", 1884)
        self.assertFalse(buyer.settings()["enabled"])

    def test_dashboard_payment_path_cannot_bypass_telegram_item_guard(self):
        self.approved()
        with self.assertRaises(buyer.BuyerError):
            buying.buy_checkout_quote(review.load_review()["token"])
        self.assertEqual(self.posts("/payment"), [])

    def test_existing_uncertain_payment_cannot_be_requoted(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT INTO vinted_buy_attempts(item_id,state,message,updated) VALUES ('123','unknown','Check Vinted',?)",
                (time.time(),),
            )
        result = review.review_latest()
        self.assertEqual(result["stage"], "existing_payment")
        self.client.request.assert_not_called()

    def test_startup_reserves_once_and_approval_is_bound_to_item_and_total(self):
        with patch.dict(
            os.environ, {"MSJ_TELEGRAM_REVIEW_ON_START": "offline-selected-alert-1"}
        ):
            self.assertEqual(review.run_once()["outcome"], "quoted")
            self.assertIsNone(review.run_once())
        with patch.dict(
            os.environ,
            {
                "MSJ_TELEGRAM_ITEM_APPROVAL_ON_START": "123:1884:offline-owner-approval-1"
            },
        ):
            self.assertEqual(review.run_once()["outcome"], "approved")
            self.assertIsNone(review.run_once())
        self.assertEqual(self.posts("/payment"), [])


class PrivateRoutesTests(DatabaseFixture, unittest.TestCase):
    owner = test_dashboard.DashboardTests.owner

    def setUp(self):
        super().setUp()
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()

    def test_review_and_approval_require_owner_and_csrf(self):
        for action, method in (
            ("buyer_telegram_review_latest", "review_latest"),
            ("buyer_telegram_review_approve", "approve"),
        ):
            data = {
                "action": action,
                "csrf": "offline-csrf",
                "buyer_review_item": "123",
                "buyer_review_maximum": "1884",
            }
            with patch.object(review, method) as operation:
                self.assertEqual(
                    self.client.post("/connections", data=data).status_code, 400
                )
                operation.assert_not_called()
        self.owner()
        for action, method in (
            ("buyer_telegram_review_latest", "review_latest"),
            ("buyer_telegram_review_approve", "approve"),
        ):
            data = {
                "action": action,
                "csrf": "invalid",
                "buyer_review_item": "123",
                "buyer_review_maximum": "1884",
            }
            with patch.object(review, method) as operation:
                self.assertEqual(
                    self.client.post("/connections", data=data).status_code, 400
                )
                operation.assert_not_called()

    def test_review_post_redirects_and_refresh_never_recreates_checkout(self):
        self.owner()
        with patch.object(
            review,
            "review_latest",
            return_value={"outcome": "quoted", "stage": "awaiting_item_approval"},
        ) as operation:
            response = self.client.post(
                "/connections",
                data={"action": "buyer_telegram_review_latest", "csrf": "offline-csrf"},
            )
            self.assertEqual(response.status_code, 303)
            self.client.get(response.headers["Location"])
            self.client.get("/connections")
        operation.assert_called_once()

    def test_exact_approval_post_is_private_and_does_not_submit_payment(self):
        self.owner()
        with patch.object(review, "approve") as operation:
            response = self.client.post(
                "/connections",
                data={
                    "action": "buyer_telegram_review_approve",
                    "csrf": "offline-csrf",
                    "buyer_review_item": "123",
                    "buyer_review_maximum": "1884",
                },
            )
        self.assertEqual(response.status_code, 303)
        operation.assert_called_once_with("123", 1884)
        page = self.client.get(response.headers["Location"])
        self.assertIn(b"Tap Autobuy", page.data)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
