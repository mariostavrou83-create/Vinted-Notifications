"""Native pickup/card selection and explicitly quoted one-off payment tests."""

import asyncio
import copy
import json
import time
import unittest
import requests
from contextlib import closing
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, Mock, patch

import test_dashboard
from test_search_controls import DatabaseFixture
from test_vinted_buying import DEVICE, web_checkout

import photo_cards
import search_settings
import vinted_alerts
import vinted_buyer as buyer
import vinted_buying as buying


def pickup_data():
    return {
        "shipping_rates": [
            {
                "rate_uuid": "near-rate",
                "price": {"amount": "3.00", "currency_code": "GBP"},
            },
            {
                "rate_uuid": "far-rate",
                "price": {"amount": "1.00", "currency_code": "GBP"},
            },
            {
                "rate_uuid": "blocked-rate",
                "price": {"amount": "1.00", "currency_code": "GBP"},
                "restriction": "unavailable",
            },
        ],
        "shipping_points": [
            {
                "preferred": True,
                "point": {
                    "uuid": "far-point",
                    "code": "FAR",
                    "rate_uuid": "far-rate",
                    "latitude": 51.01,
                    "longitude": -0.1,
                    "name": "Fictional far shop",
                },
            },
            {
                "preferred": False,
                "point": {
                    "uuid": "near-point",
                    "code": "NEAR",
                    "rate_uuid": "near-rate",
                    "latitude": 51.001,
                    "longitude": -0.1,
                    "name": "Fictional nearest shop",
                },
            },
            {
                "point": {
                    "uuid": "blocked-point",
                    "code": "BLOCKED",
                    "rate_uuid": "blocked-rate",
                    "latitude": 51.0001,
                    "longitude": -0.1,
                }
            },
        ],
    }


class CheckoutPreferencesTests(DatabaseFixture, unittest.TestCase):
    url = "https://www.vinted.co.uk/checkout?purchase_id=checkout-123&order_id=456&order_type=transaction"

    def setUp(self):
        super().setUp()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?,verified_at=1,user_id='99',username='owner',enabled=1,browser_info=?,pickup_mode='nearest',preferred_card_last4='1234' WHERE id=1",
                (
                    buyer.encrypt({"cookies": {"access_token_web": "fictional-token"}}),
                    json.dumps(DEVICE),
                ),
            )
        self.checkout = web_checkout()
        c = self.checkout["components"]
        c["shipping_address"]["address"].update(
            country_code="GB", coordinates={"latitude": 51.0, "longitude": -0.1}
        )
        c["shipping_pickup_details"]["shipping_order_id"] = 300
        c["shipping_pickup_details"]["pickup_details"]["shipping_point"][
            "code"
        ] = "SAVED"
        self.points = pickup_data()
        self.payment = {"payment": {"status": "success"}}
        self.corrupt = None
        self.client = Mock()
        self.client.request.side_effect = self.request
        self.row = {
            "item_id": "123",
            "query_id": 1,
            "url": "https://www.vinted.co.uk/items/123",
            "price": "15.00",
            "currency": "GBP",
        }

    def request(self, method, path, body=None, **kwargs):
        if path.endswith("/nearby_pickup_points"):
            return copy.deepcopy(self.points)
        if path == "/api/v2/purchases/checkout-123/checkout":
            if body and body.get("components"):
                changes = body["components"]
                c = self.checkout["components"]
                if (
                    changes.get("shipping_pickup_options", {}).get("pickup_type")
                    is not None
                ):
                    c["shipping_pickup_options"]["selected_pickup_option"] = changes[
                        "shipping_pickup_options"
                    ]["pickup_type"]
                    detail = changes["shipping_pickup_details"]
                    point = next(
                        entry["point"]
                        for entry in self.points["shipping_points"]
                        if entry["point"]["uuid"] == detail["point_uuid"]
                    )
                    c["shipping_pickup_details"]["pickup_details"].update(
                        selected_rate_uuid=detail["rate_uuid"],
                        shipping_point=copy.deepcopy(point),
                    )
                if changes.get("payment_method", {}).get("payment_method"):
                    choice = changes["payment_method"]
                    selected = {
                        "pay_in_method": {"payment_method": choice["payment_method"]}
                    }
                    if "card_id" in choice:
                        selected["credit_card"] = next(
                            card
                            for card in c["payment_method"]["cards"]
                            if str(card["id"]) == choice["card_id"]
                        )
                    c["payment_method"]["selected_payment_method"] = copy.deepcopy(
                        selected
                    )
                if self.corrupt:
                    self.corrupt(self.checkout)
            return {"checkout": copy.deepcopy(self.checkout)}
        if path.endswith("/checkout/payment") and method == "POST":
            if isinstance(self.payment, Exception):
                raise self.payment
            return self.payment
        if path == "/api/v2/items/123":
            return {
                "item": {
                    "id": 123,
                    "price": {"amount": "15.00", "currency_code": "GBP"},
                    "user": {"id": 100},
                }
            }
        if path == "/api/v2/conversations":
            return {"conversation": {"transaction": {"id": 456}}}
        if path == "/api/v2/purchases/checkout/build":
            return {"checkout": copy.deepcopy(self.checkout)}
        raise AssertionError(path)

    def configure(self):
        return buying.configure_checkout_choices(
            self.client, copy.deepcopy(self.checkout), buyer.settings()
        )

    def prepare(self):
        with patch.object(buyer, "connected_client", return_value=self.client):
            return buying.check_checkout(self.url, prepare_test=True)

    def pay(self, token):
        with patch.object(buyer, "connected_client", return_value=self.client):
            return buying.buy_checkout_quote(token)

    def payments(self):
        return [
            call
            for call in self.client.request.call_args_list
            if call.args[0] == "POST" and call.args[1].endswith("/payment")
        ]

    def wallet_checkout(self, *, credit="-14.00", payable="4.84"):
        components = self.checkout["components"]
        summary = components["order_summary_v2"]
        summary["subtotal"] = {"price": {"amount": "18.84", "currency_code": "GBP"}}
        summary["deductions"] = [
            {
                "type": "order-summary-wallet-deduction",
                "price": {"amount": credit, "currency_code": "GBP"},
            }
        ]
        components["pay_button_v2"]["total"]["price"]["amount"] = payable
        return self.checkout

    def test_balance_and_card_are_quoted_separately_and_full_cost_is_recorded(self):
        self.wallet_checkout()
        prepared = self.prepare()
        self.assertEqual(
            (prepared["total"], prepared["payment_due"], prepared["wallet_credit"]),
            (1884, 484, 1400),
        )
        self.assertIn("Vinted balance contributes £14.00", prepared["message"])
        self.assertEqual(self.payments(), [])
        quote = buyer.decrypt(prepared["token"].encode("ascii"))
        self.assertEqual(quote["payment_due"] + quote["wallet_credit"], quote["total"])
        outcome = self.pay(prepared["token"])
        self.assertEqual((outcome["state"], outcome["total"]), ("paid", 1884))
        self.assertEqual(len(self.payments()), 1)

    def test_balance_cannot_make_an_over_budget_purchase_look_affordable(self):
        self.wallet_checkout()
        with self.assertRaises(buyer.BuyerError) as error:
            buying.checkout_prices(self.checkout, 1500, 500, item_id="123")
        self.assertEqual(error.exception.reason, "total_over_budget")
        self.assertEqual(self.payments(), [])

    def test_full_balance_funding_can_have_zero_remaining_payment(self):
        self.wallet_checkout(credit="-18.84", payable="0.00")
        prepared = self.prepare()
        self.assertEqual(
            (prepared["total"], prepared["payment_due"], prepared["wallet_credit"]),
            (1884, 0, 1884),
        )
        self.assertEqual(self.payments(), [])

    def test_changed_balance_cannot_increase_the_quoted_card_charge(self):
        self.wallet_checkout()
        prepared = self.prepare()
        self.wallet_checkout(credit="-13.00", payable="5.84")
        outcome = self.pay(prepared["token"])
        self.assertEqual(outcome["state"], "failed_before_payment")
        self.assertEqual(self.payments(), [])

    def test_unknown_or_inconsistent_funding_fails_before_payment(self):
        valid = copy.deepcopy(self.wallet_checkout())
        cases = []
        for value in ("14.00", "NaN", "-14.001", "-1000001.00"):
            bad = copy.deepcopy(valid)
            bad["components"]["order_summary_v2"]["deductions"][0]["price"][
                "amount"
            ] = value
            cases.append(bad)
        for value in ("EUR", None):
            bad = copy.deepcopy(valid)
            bad["components"]["order_summary_v2"]["deductions"][0]["price"][
                "currency_code"
            ] = value
            cases.append(bad)
        for deductions in (
            [],
            None,
            "unknown",
            [
                {
                    "type": "unknown",
                    "price": {"amount": "-14.00", "currency_code": "GBP"},
                }
            ],
        ):
            bad = copy.deepcopy(valid)
            bad["components"]["order_summary_v2"]["deductions"] = deductions
            cases.append(bad)
        bad = copy.deepcopy(valid)
        bad["components"]["order_summary_v2"]["deductions"] *= 2
        cases.append(bad)
        for subtotal in (None, {"price": {"amount": "4.84", "currency_code": "GBP"}}):
            bad = copy.deepcopy(valid)
            bad["components"]["order_summary_v2"]["subtotal"] = subtotal
            cases.append(bad)
        bad = copy.deepcopy(valid)
        bad["components"]["pay_button_v2"]["order_summary_v2"] = copy.deepcopy(
            bad["components"]["order_summary_v2"]
        )
        bad["components"]["pay_button_v2"]["order_summary_v2"]["deductions"] = []
        cases.append(bad)
        for index, bad in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(buyer.BuyerError):
                buying.checkout_prices(bad, 1500, 2000, item_id="123")
        self.assertEqual(self.payments(), [])

    def test_old_or_malformed_quotes_cannot_authorize_wallet_funding(self):
        self.wallet_checkout()
        prepared = self.prepare()
        original = buyer.decrypt(prepared["token"].encode("ascii"))
        malformed = dict(original, wallet_credit=1401)
        with self.assertRaises(buyer.BuyerError):
            self.pay(buyer.encrypt(malformed).decode("ascii"))
        old = dict(original)
        old.pop("wallet_credit")
        old.pop("payment_due")
        outcome = self.pay(buyer.encrypt(old).decode("ascii"))
        self.assertEqual(outcome["state"], "failed_before_payment")
        self.assertEqual(self.payments(), [])

    def test_nearest_available_point_wins_over_preferred_and_cheaper_points(self):
        selected = self.configure()
        self.assertEqual(
            selected["components"]["shipping_pickup_details"]["pickup_details"][
                "shipping_point"
            ]["code"],
            "NEAR",
        )
        calls = self.client.request.call_args_list
        self.assertEqual(
            calls[0].kwargs["params"],
            {"country_code": "GB", "latitude": 51.0, "longitude": -0.1},
        )
        self.assertEqual(
            calls[1].args,
            (
                "PUT",
                "/api/v2/purchases/checkout-123/checkout",
                {
                    "components": {
                        "shipping_pickup_options": {"pickup_type": "pickup"},
                        "shipping_pickup_details": {
                            "rate_uuid": "near-rate",
                            "point_code": "NEAR",
                            "point_uuid": "near-point",
                        },
                    }
                },
            ),
        )
        self.assertEqual(
            buying.checkout_prices(selected, 1500, 1884, item_id="123"), 1884
        )
        self.assertFalse(self.payments())

    def test_already_nearest_only_queries_availability_without_selection_put(self):
        self.configure()
        self.client.reset_mock()
        self.configure()
        self.assertEqual(self.client.request.call_count, 1)
        self.assertEqual(self.client.request.call_args.args[0], "GET")

    def test_missing_invalid_or_foreign_address_coordinates_stop_without_request(self):
        for change in (
            {"coordinates": None},
            {"coordinates": {"latitude": "nan", "longitude": 0}},
            {"country_code": "FR"},
            {"is_complete": False},
        ):
            with self.subTest(change=change):
                value = copy.deepcopy(self.checkout)
                value["components"]["shipping_address"]["address"].update(change)
                with self.assertRaises(buyer.BuyerError):
                    buying.configure_checkout_choices(
                        self.client, value, buyer.settings()
                    )
        self.client.request.assert_not_called()

    def test_unavailable_and_optional_verification_rates_are_never_selected(self):
        self.points["shipping_rates"][0][
            "verification_service_type"
        ] = "item_verification"
        chosen = self.configure()
        self.assertEqual(
            chosen["components"]["shipping_pickup_details"]["pickup_details"][
                "shipping_point"
            ]["code"],
            "FAR",
        )
        self.points["shipping_rates"][1]["restriction"] = "unavailable"
        with self.assertRaises(buyer.BuyerError):
            self.configure()

    def test_selection_must_confirm_point_rate_address_and_checkout(self):
        mutations = [
            lambda value: value["components"]["shipping_pickup_details"][
                "pickup_details"
            ]["shipping_point"].update(code="WRONG"),
            lambda value: value["components"]["shipping_pickup_details"][
                "pickup_details"
            ].update(selected_rate_uuid="wrong-rate"),
            lambda value: value["components"]["shipping_address"]["address"].update(
                id=999
            ),
            lambda value: value.update(id="wrong-checkout"),
        ]
        original = copy.deepcopy(self.checkout)
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.checkout = copy.deepcopy(original)
                self.corrupt = mutate
                with self.assertRaises(buyer.BuyerError):
                    self.configure()
        self.assertFalse(self.payments())

    def test_card_selection_uses_only_matching_valid_saved_card(self):
        payment = self.checkout["components"]["payment_method"]
        payment["selected_payment_method"]["credit_card"]["last4"] = "5678"
        chosen = self.configure()
        self.assertEqual(
            chosen["components"]["payment_method"]["selected_payment_method"][
                "credit_card"
            ]["last4"],
            "1234",
        )
        changes = self.client.request.call_args.args[2]["components"]
        self.assertEqual(
            changes["payment_method"], {"card_id": "789", "payment_method": "card"}
        )

    def test_vinted_balance_is_accepted_and_can_be_selected_as_available_fallback(self):
        payment = self.checkout["components"]["payment_method"]
        payment["selected_payment_method"]["credit_card"]["last4"] = "5678"
        payment["cards"] = []
        payment["pay_in_methods"].append({"payment_method": "balance", "enabled": True})
        chosen = self.configure()
        self.assertEqual(
            chosen["components"]["payment_method"]["selected_payment_method"][
                "pay_in_method"
            ]["payment_method"],
            "balance",
        )
        self.assertEqual(
            buying.checkout_prices(chosen, 1500, 1884, item_id="123"), 1884
        )
        self.assertEqual(
            buying.checkout_choice_details(chosen)["payment_label"], "Vinted balance"
        )
        self.client.reset_mock()
        self.configure()
        self.assertEqual(self.client.request.call_count, 1)

    def test_wrong_or_expired_card_and_disabled_balance_stop_before_payment(self):
        payment = self.checkout["components"]["payment_method"]
        payment["selected_payment_method"]["credit_card"].update(
            last4="5678", expired=True
        )
        payment["cards"][0]["expired"] = True
        payment["pay_in_methods"].append(
            {"payment_method": "balance", "enabled": False}
        )
        with self.assertRaises(buyer.BuyerError):
            self.configure()
        self.assertEqual(self.client.request.call_count, 1)
        self.assertFalse(self.payments())

    def test_current_and_legacy_card_codes_both_require_nonexpired_card(self):
        for code in ("card", "credit_card"):
            with self.subTest(code=code):
                value = web_checkout()
                selected = value["components"]["payment_method"][
                    "selected_payment_method"
                ]
                selected["pay_in_method"]["payment_method"] = code
                selected["credit_card"]["expired"] = True
                with self.assertRaises(buyer.BuyerError):
                    buying.checkout_prices(value, 1500, 1884, item_id="123")

    def test_preparation_has_encrypted_handle_without_claim_charge_or_budget_change(
        self,
    ):
        quote = self.prepare()
        self.assertEqual(quote["total"], 1884)
        self.assertEqual(quote["pickup_name"], "Fictional nearest shop")
        self.assertEqual(quote["payment_label"], "Saved card ending 1234")
        self.assertNotIn("checkout-123", json.dumps(quote))
        self.assertNotIn("coordinates", json.dumps(quote))
        self.assertEqual(
            buyer.decrypt(quote["token"].encode())["checkout_id"], "checkout-123"
        )
        self.assertIsNone(buying.result("123"))
        self.assertFalse(self.payments())
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM vinted_search_budgets").fetchone()[
                    0
                ],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM alert_outbox").fetchone()[0], 0
            )

    def test_oneoff_without_search_budget_pays_once_and_duplicate_is_not_submitted(
        self,
    ):
        quote = self.prepare()
        self.client.reset_mock()
        outcome = self.pay(quote["token"])
        self.assertEqual(outcome["state"], "paid", outcome["message"])
        self.assertEqual(outcome["total"], 1884)
        self.assertEqual(len(self.payments()), 1)
        self.assertEqual(
            self.payments()[0].args[2],
            {
                "checksum": "verified-checksum",
                "payment_options": {"browser_info": DEVICE},
            },
        )
        self.assertEqual(self.pay(quote["token"])["state"], "paid")
        self.assertEqual(len(self.payments()), 1)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM vinted_search_budgets").fetchone()[
                    0
                ],
                0,
            )

    def test_normal_autobuy_uses_same_nearest_and_card_checks(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("INSERT INTO vinted_search_budgets VALUES (1,2000,350)")
        with patch.object(buyer, "connected_client", return_value=self.client):
            outcome = buying.buy(self.row)
        self.assertEqual(outcome["state"], "paid")
        self.assertEqual(
            self.checkout["components"]["shipping_pickup_details"]["pickup_details"][
                "shipping_point"
            ]["code"],
            "NEAR",
        )
        self.assertEqual(len(self.payments()), 1)

    def telegram_alert(self):
        self.batch(1, [123])
        with closing(search_settings.connection()) as conn:
            row = dict(
                conn.execute(
                    "SELECT * FROM alert_outbox WHERE platform='vinted' AND item_id='123'"
                ).fetchone()
            )
        details = vinted_alerts.get_details(row)
        photo_cards.record(
            row,
            details,
            SimpleNamespace(
                message_id=42, photo=[SimpleNamespace(file_id="fictional-photo")]
            ),
        )
        query = SimpleNamespace(
            data="buy:click",
            message=SimpleNamespace(message_id=42, chat=SimpleNamespace(id=123)),
            from_user=SimpleNamespace(id=123),
            answer=AsyncMock(),
        )
        bot = SimpleNamespace(edit_message_caption=AsyncMock())
        return SimpleNamespace(callback_query=query), SimpleNamespace(bot=bot)

    def test_telegram_single_tap_without_budget_selects_nearest_card_and_pays_once(
        self,
    ):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE queries SET query='https://www.vinted.co.uk/catalog?price_to=15' WHERE id=1"
            )
        payment = self.checkout["components"]["payment_method"]
        payment["selected_payment_method"]["credit_card"]["last4"] = "5678"
        update, context = self.telegram_alert()
        submitted = []

        def request(method, path, body=None, **kwargs):
            if method == "POST" and path.endswith("/checkout/payment"):
                submitted.append(buying.result("123"))
            return self.request(method, path, body, **kwargs)

        self.client.request.side_effect = request
        with patch.object(buyer, "connected_client", return_value=self.client):
            asyncio.run(buying.callback(update, context))
            asyncio.run(buying.callback(update, context))
        self.assertEqual(len(self.payments()), 1)
        self.assertEqual(submitted[0]["state"], "paying")
        self.assertEqual(submitted[0]["total"], 1884)
        self.assertEqual(buying.result("123")["state"], "paid")
        choices = buying.checkout_choice_details(self.checkout)
        self.assertEqual(choices["pickup_name"], "Fictional nearest shop")
        self.assertEqual(choices["payment_label"], "Saved card ending 1234")
        self.assertEqual(
            self.payments()[0].args[2],
            {
                "checksum": self.checkout["checksum"],
                "payment_options": {"browser_info": DEVICE},
            },
        )
        feedback = photo_cards.load("vinted", 42)[1]["buy_feedback"]
        self.assertEqual(feedback["state"], "paid")
        self.assertEqual(feedback["total"], 1884)
        edit = context.bot.edit_message_caption.call_args.kwargs
        self.assertEqual(edit["message_id"], 42)
        self.assertIn("Paid", edit["caption"])
        self.assertIn("£18.84", edit["caption"])
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM vinted_search_budgets").fetchone()[
                    0
                ],
                0,
            )

    def test_telegram_unbudgeted_pending_payment_is_shown_without_resubmitting(self):
        self.payment = {"payment": {"status": "pending"}}
        update, context = self.telegram_alert()
        with patch.object(buyer, "connected_client", return_value=self.client):
            asyncio.run(buying.callback(update, context))
            asyncio.run(buying.callback(update, context))
        self.assertEqual(len(self.payments()), 1)
        self.assertEqual(buying.result("123")["state"], "needs_action")
        self.assertEqual(
            photo_cards.load("vinted", 42)[1]["buy_feedback"]["state"], "needs_action"
        )

    def test_expired_tampered_changed_account_or_changed_preferences_stop_before_network(
        self,
    ):
        quote = self.prepare()
        decoded = buyer.decrypt(quote["token"].encode())
        expired = dict(decoded, created=time.time() - 1801)
        self.client.reset_mock()
        for token in (buyer.encrypt(expired).decode(), "X" + quote["token"]):
            with self.subTest(token=token[:8]), self.assertRaises(buyer.BuyerError):
                self.pay(token)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET user_id='100'")
        with self.assertRaises(buyer.BuyerError):
            self.pay(quote["token"])
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET user_id='99',preferred_card_last4='5678'"
            )
        with self.assertRaises(buyer.BuyerError):
            self.pay(quote["token"])
        self.client.request.assert_not_called()

    def test_price_increase_item_mismatch_or_changed_choices_never_pay(self):
        quote = self.prepare()
        original = copy.deepcopy(self.checkout)
        mutations = [
            lambda value: value["components"]["pay_button_v2"]["total"]["price"].update(
                amount="18.85"
            ),
            lambda value: value["components"]["order_summary_v2"]["order_items"][0][
                "pricing"
            ]["final_price"].update(amount="15.01"),
            lambda value: value["components"]["order_summary_v2"]["order_items"][
                0
            ].update(id=999),
            lambda value: value["components"]["payment_method"][
                "selected_payment_method"
            ].update(pay_in_method={"payment_method": "balance"}, credit_card=None),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.checkout = copy.deepcopy(original)
                mutate(self.checkout)
                self.assertEqual(
                    self.pay(quote["token"])["state"], "failed_before_payment"
                )
        self.assertFalse(self.payments())

    def test_unknown_payment_is_durable_and_never_replayed(self):
        quote = self.prepare()
        self.payment = buyer.BuyerError("Connection lost", reason="network")
        self.assertEqual(self.pay(quote["token"])["state"], "unknown")
        self.payment = {"payment": {"status": "success"}}
        self.assertEqual(self.pay(quote["token"])["state"], "unknown")
        self.assertEqual(len(self.payments()), 1)

    def test_bank_confirmation_stays_pending_and_does_not_replay(self):
        quote = self.prepare()
        self.payment = {
            "payment": {"status": "requires_action"},
            "action": {"parameters": {"url": "https://www.vinted.co.uk/checkout"}},
        }
        self.assertEqual(self.pay(quote["token"])["state"], "needs_action")
        self.assertEqual(self.pay(quote["token"])["state"], "needs_action")
        self.assertEqual(len(self.payments()), 1)

    def test_durable_marker_failure_stops_before_payment(self):
        quote = self.prepare()
        original = buying.record

        def record(*args, **kwargs):
            if args[1] == "paying":
                raise OSError("fictional persistence failure")
            return original(*args, **kwargs)

        with patch.object(buying, "record", side_effect=record):
            self.assertEqual(self.pay(quote["token"])["state"], "failed_before_payment")
        self.assertFalse(self.payments())

    def test_disabled_after_native_validation_stops_before_payment(self):
        quote = self.prepare()
        original = self.client.request.side_effect

        def request(*args, **kwargs):
            data = original(*args, **kwargs)
            if args[1].endswith("/nearby_pickup_points"):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("UPDATE vinted_buyer SET enabled=0")
            return data

        self.client.request.side_effect = request
        self.assertEqual(self.pay(quote["token"])["state"], "failed_before_payment")
        self.assertFalse(self.payments())

    def test_settings_save_and_older_forms_preserve_private_preferences(self):
        form = {"buyer_browser_info": json.dumps(DEVICE), "buyer_enabled": "yes"}
        buyer.save_limits(form)
        self.assertEqual(buyer.settings()["pickup_mode"], "nearest")
        self.assertEqual(buyer.settings()["preferred_card_last4"], "1234")
        buyer.save_limits(
            dict(form, buyer_pickup_mode="saved", buyer_card_last4="5678")
        )
        self.assertEqual(buyer.settings()["pickup_mode"], "saved")
        self.assertEqual(buyer.settings()["preferred_card_last4"], "5678")
        for value in ("123", "１２３４", "12345", "1234 5678"):
            with self.subTest(value=value), self.assertRaises(buyer.BuyerError):
                buyer.save_limits(dict(form, buyer_card_last4=value))
        self.assertEqual(buyer.settings()["preferred_card_last4"], "5678")


class ExistingBuyerMigrationTests(DatabaseFixture, unittest.TestCase):
    def test_previous_production_schema_upgrades_preferences_without_losing_saved_data(
        self,
    ):
        session = buyer.encrypt({"cookies": {"access_token_web": "fictional-token"}})
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("ALTER TABLE vinted_buyer DROP COLUMN pickup_mode")
            conn.execute("ALTER TABLE vinted_buyer DROP COLUMN preferred_card_last4")
            conn.execute(
                "UPDATE parameters SET value='17' WHERE key='msj_search_schema'"
            )
            conn.execute(
                "UPDATE vinted_buyer SET session=?,verified_at=1,user_id='99',username='owner',enabled=1,browser_info=?",
                (session, json.dumps(DEVICE)),
            )
            conn.execute("INSERT INTO vinted_search_budgets VALUES (1,2000,350)")
            conn.execute(
                "INSERT INTO vinted_buy_attempts(item_id,state,checkout_id,total,message,updated) VALUES ('123','unknown','checkout-123',1884,'Preserve the uncertain payment',1)"
            )
            saved_buyer = dict(conn.execute("SELECT * FROM vinted_buyer").fetchone())
            saved_queries = [
                tuple(row) for row in conn.execute("SELECT * FROM queries ORDER BY id")
            ]
        backup = search_settings.ensure_schema()
        self.assertIsNotNone(backup)
        with closing(search_settings.connection()) as conn:
            upgraded = dict(conn.execute("SELECT * FROM vinted_buyer").fetchone())
            self.assertEqual(upgraded.pop("pickup_mode"), "saved")
            self.assertEqual(upgraded.pop("preferred_card_last4"), "")
            self.assertEqual(upgraded, saved_buyer)
            self.assertEqual(
                [
                    tuple(row)
                    for row in conn.execute("SELECT * FROM queries ORDER BY id")
                ],
                saved_queries,
            )
            self.assertEqual(
                tuple(
                    conn.execute(
                        "SELECT * FROM vinted_search_budgets WHERE query_id=1"
                    ).fetchone()
                ),
                (1, 2000, 350),
            )
        self.assertEqual(buying.result("123")["state"], "unknown")
        self.assertEqual(buying.result("123")["total"], 1884)
        self.assertIsNone(search_settings.ensure_schema())


class PickupGatewayTests(DatabaseFixture, unittest.TestCase):
    path = "/shipping-estimation/external/shipping_orders/300/nearby_pickup_points"
    params: ClassVar = {"country_code": "GB", "latitude": 51.0, "longitude": -0.1}

    def test_native_gateway_uses_owned_session_and_exact_first_party_headers(self):
        client = buyer.Client()
        response = Mock(status_code=200, text="", json=Mock(return_value=pickup_data()))
        with patch.object(client.session, "request", return_value=response) as request:
            self.assertEqual(
                client.request("GET", self.path, params=self.params), pickup_data()
            )
        request.assert_called_once_with(
            "GET",
            buyer.PICKUP_BASE + self.path,
            json=None,
            timeout=(4, 12),
            allow_redirects=False,
            params=self.params,
            headers={
                "Platform": "web",
                "X-Next-App": "marketplace-web",
                "Sec-Fetch-Site": "same-site",
            },
        )
        client.session.close()

    def test_api_host_retains_cookie_scopes_and_rejects_other_destinations(self):
        client = buyer.Client()
        client.session.cookies.set(
            "domain_test", "offline-domain-cookie", domain=".vinted.co.uk", path="/"
        )
        client.session.cookies.set(
            "website_test",
            "offline-website-cookie",
            domain="www.vinted.co.uk",
            path="/",
        )
        response = Mock(status_code=200, text="", json=Mock(return_value=pickup_data()))
        with patch.object(client.session, "request", return_value=response) as request:
            client.request("GET", self.path, params=self.params)
            target = request.call_args.args[1]
            prepared = client.session.prepare_request(requests.Request("GET", target))
            self.assertIn(
                "domain_test=offline-domain-cookie", prepared.headers["Cookie"]
            )
            self.assertNotIn("website_test", prepared.headers["Cookie"])
            self.assertEqual(
                {cookie.name: cookie.domain for cookie in client.session.cookies},
                {"domain_test": ".vinted.co.uk", "website_test": "www.vinted.co.uk"},
            )
            for path in (
                "https://api.vinted.co.uk" + self.path,
                "//api.vinted.co.uk" + self.path,
                "/shipping-estimation/external/shipping_orders/300/other",
                "/web/gateway" + self.path,
            ):
                with self.subTest(path=path), self.assertRaises(buyer.BuyerError):
                    client.request("GET", path, params=self.params)
            self.assertEqual(request.call_count, 1)
        client.session.close()


class QuotedPurchaseDashboardTests(DatabaseFixture, unittest.TestCase):
    owner = test_dashboard.DashboardTests.owner

    def setUp(self):
        super().setUp()
        from web_ui_plugin.web_ui import create_app

        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()
        self.prepared = {
            "token": "fictional-encrypted-quote",
            "title": "Fictional test item",
            "item_id": "123",
            "item_price": 1500,
            "total": 1884,
            "pickup_name": "Fictional nearest shop",
            "payment_label": "Saved card ending 1234",
            "message": "Test prepared; no payment submitted.",
        }

    def test_test_purchase_requires_owner_csrf_and_session_bound_quote(self):
        data = {
            "csrf": "offline-csrf",
            "action": "buyer_checkout_pay",
            "buyer_test_quote": self.prepared["token"],
        }
        with patch.object(buying, "buy_checkout_quote") as pay:
            self.assertEqual(
                self.client.post("/connections", data=data).status_code, 400
            )
            self.owner()
            self.assertEqual(
                self.client.post(
                    "/connections", data=dict(data, csrf="invalid")
                ).status_code,
                400,
            )
            self.assertEqual(
                self.client.post("/connections", data=data).status_code, 303
            )
            with self.client.session_transaction() as session:
                session["buyer_test_quote"] = self.prepared
            self.assertEqual(
                self.client.post(
                    "/connections", data=dict(data, buyer_test_quote="wrong-quote")
                ).status_code,
                303,
            )
            pay.assert_not_called()

    def test_preparation_displays_public_choices_without_private_checkout_handle(self):
        self.owner()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?,verified_at=1,user_id='99',username='owner',enabled=1",
                (buyer.encrypt({"cookies": {"access_token_web": "fictional-token"}}),),
            )
        data = {
            "csrf": "offline-csrf",
            "action": "buyer_checkout_prepare",
            "buyer_checkout_url": CheckoutPreferencesTests.url,
        }
        with patch.object(
            buying, "check_checkout", return_value=self.prepared
        ) as prepare, patch.object(buying, "buy_checkout_quote") as pay:
            response = self.client.post("/connections", data=data)
            self.assertEqual(response.status_code, 303)
            page = self.client.get(response.headers["Location"])
            prepare.assert_called_once_with(
                CheckoutPreferencesTests.url, prepare_test=True
            )
            pay.assert_not_called()
        self.assertIn("Maximum total £18.84", page.data.decode())
        self.assertIn(b"Fictional nearest shop", page.data)
        self.assertIn(b"Saved card ending 1234", page.data)
        self.assertNotIn(b"purchase_id=checkout-123", page.data)
        self.assertEqual(page.headers["Cache-Control"], "no-store")

    def test_payment_success_redirects_clears_quote_and_refresh_never_reposts(self):
        self.owner()
        with self.client.session_transaction() as session:
            session["buyer_test_quote"] = self.prepared
        data = {
            "csrf": "offline-csrf",
            "action": "buyer_checkout_pay",
            "buyer_test_quote": self.prepared["token"],
        }
        with patch.object(
            buying,
            "buy_checkout_quote",
            return_value={"state": "paid", "message": "Paid fictional total."},
        ) as pay:
            response = self.client.post("/connections", data=data)
            self.assertEqual(response.status_code, 303)
            self.client.get(response.headers["Location"])
            self.client.get("/connections")
            self.client.post("/connections", data=data)
            pay.assert_called_once_with(self.prepared["token"])
        with self.client.session_transaction() as session:
            self.assertNotIn("buyer_test_quote", session)


class PickupGatewayRefusalTests(DatabaseFixture, unittest.TestCase):
    path = PickupGatewayTests.path
    params: ClassVar = PickupGatewayTests.params

    def test_gateway_refusals_stop_once_and_never_probe_alternate_route(self):
        client = buyer.Client()
        for status, data, reason in (
            (
                403,
                {"url": "https://geo.captcha-delivery.com/captcha/"},
                "security_challenge",
            ),
            (429, {}, "rate_limited"),
        ):
            response = Mock(status_code=status, text="", json=Mock(return_value=data))
            with patch.object(
                client.session, "request", return_value=response
            ) as request, self.assertRaises(buyer.BuyerError) as error:
                client.request("GET", self.path, params=self.params)
            self.assertEqual(error.exception.reason, reason)
            request.assert_called_once()
        client.session.close()

    def test_unknown_paths_methods_or_unbounded_coordinates_are_refused_locally(self):
        client = buyer.Client()
        cases = [
            ("POST", self.path, self.params),
            ("GET", "/web/gateway/unknown", self.params),
            ("GET", self.path, dict(self.params, latitude=float("nan"))),
            ("GET", self.path, dict(self.params, longitude=181)),
            ("GET", self.path, dict(self.params, country_code="FR")),
        ]
        with patch.object(client.session, "request") as request:
            for method, path, params in cases:
                with self.subTest(method=method, path=path), self.assertRaises(
                    buyer.BuyerError
                ):
                    client.request(method, path, params=params)
            request.assert_not_called()
        client.session.close()
