"""No live transactions: buyer auth, price gates and uncertain-payment recovery."""

import copy
import json
import stat
import subprocess
import sys
import unittest
from contextlib import closing
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import requests
import test_dashboard
from test_search_controls import DatabaseFixture

import db
import search_settings
import vinted_buyer as buyer
import vinted_buying as buying

DEVICE = {
    "color_depth": 24,
    "java_enabled": False,
    "language": "en-GB",
    "screen_height": 800,
    "screen_width": 400,
    "timezone_offset": -60,
}


def checkout(total="19.00"):
    return {
        "id": "checkout-123",
        "checksum": "verified-checksum",
        "components": {
            "order_summary_v2": {
                "subtotal": {"price": {"amount": "15.00", "currency_code": "GBP"}},
                "total": {"price": {"amount": total, "currency_code": "GBP"}},
            },
            "payment_method": {"id": 123},
            "shipping_address": {"id": 456},
            "shipping_pickup_details": {"rate_uuid": "saved-rate"},
            "shipping_pickup_options": {"selected_pickup_option": 1},
        },
    }


def web_checkout(total="18.84", *, home=False):
    """Current first-party DTO shape with fictional address/card identifiers."""
    value = checkout(total)
    components = value["components"]
    components["order_summary_v2"].pop("total")
    components["order_summary_v2"]["order_items"] = [
        {
            "id": 123,
            "title": "Fictional test item",
            "price": {"amount": "15.00", "currency_code": "GBP"},
            "pricing": {
                "final_price": {"amount": "15.00", "currency_code": "GBP"},
                "original_price": None,
            },
        }
    ]
    components["pay_button_v2"] = {
        "total": {"price": {"amount": total, "currency_code": "GBP"}}
    }
    components["payment_method"] = {
        "selected_payment_method": {
            "pay_in_method": {"payment_method": "card"},
            "credit_card": {"expired": False, "last4": "1234"},
        },
        "cards": [{"id": 789, "last4": "1234", "expired": False}],
        "pay_in_methods": [{"payment_method": "card", "enabled": True}],
    }
    address = {"id": 456, "is_complete": True}
    components["shipping_address"] = {"address": address, "address_is_missing": False}
    components["shipping_pickup_options"] = {
        "selected_pickup_option": "home" if home else "pickup",
        "pickup_options": {
            "home": {"pickup_option_type": "home"},
            "pickup": {"pickup_option_type": "pickup"},
        },
    }
    components["shipping_pickup_details"] = {
        "pickup_details": {
            "selected_rate_uuid": "saved-rate",
            "shipping_point": {"uuid": "saved-point", "rate_uuid": "saved-rate"},
        },
        "receiver_address": address if home else None,
    }
    return value


class BuyingTests(DatabaseFixture, unittest.TestCase):
    def test_response_persistence_failure_after_payment_never_submits_again(self):
        original = self.client.request.side_effect
        payment_posts = 0

        def response(method, url, **kwargs):
            nonlocal payment_posts
            path = url.removeprefix(buyer.BASE)
            cookies = requests.cookies.RequestsCookieJar()
            if path == "/api/v2/users/current":
                data = {"user": {"id": 99}}
            else:
                data = original(method, path, kwargs.get("json"))
            if path.endswith("/payment") and method == "POST":
                payment_posts += 1
                cookies.set(
                    "refresh_token_web",
                    "rotated-refresh-token-0123456789",
                    domain=".www.vinted.co.uk",
                    secure=True,
                )
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute(
                        "UPDATE vinted_buyer SET session=? WHERE id=1",
                        (
                            buyer.encrypt(
                                {
                                    "cookies": {
                                        "access_token_web": "replacement-access-token-0123456789"
                                    }
                                }
                            ),
                        ),
                    )
            return Mock(
                status_code=200,
                text="",
                headers={},
                cookies=cookies,
                json=Mock(return_value=data),
            )

        with patch.object(requests.Session, "request", side_effect=response):
            self.assertEqual(buying.buy(self.row)["state"], "unknown")
            self.assertEqual(buying.buy(self.row)["state"], "unknown")
        self.assertEqual(payment_posts, 1)
        self.assertEqual(buying.result("123")["checkout_id"], "checkout-123")

    def test_processes_share_the_buyer_lock_and_release_it_on_completion(self):
        script = (
            "import sys,db,vinted_buyer as b\n"
            "db.DB_PATH=sys.argv[1]\n"
            "try:\n"
            ' with b.exclusive(): print("acquired")\n'
            "except b.BuyerError:\n"
            ' print("busy")\n'
        )

        def child():
            return subprocess.run(
                [sys.executable, "-c", script, db.DB_PATH],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout.strip()

        with buyer.exclusive():
            self.assertEqual(child(), "busy")
        self.assertEqual(child(), "acquired")

    def test_expiry_rotation_current_page_and_bank_pending_share_one_payment(self):
        from test_vinted_page_data import next_data, purchase_item

        seed = buyer.Client(
            {
                "cookies": {
                    "access_token_web": "old-access-token-0123456789",
                    "refresh_token_web": "old-refresh-token-0123456789",
                }
            }
        )
        seed.csrf = "01234567-89ab-cdef-0123-456789abcdef"
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?", (buyer.encrypt(seed.exported()),)
            )
        seed.session.close()
        self.final = web_checkout()
        identity_count = 0
        payment_posts = 0
        payment_gets = 0
        original = self.client.request.side_effect

        def response(method, url, **kwargs):
            nonlocal identity_count, payment_posts, payment_gets
            path = url.removeprefix(buyer.BASE)
            cookies = requests.cookies.RequestsCookieJar()
            text = ""
            status = 200
            if path == "/api/v2/users/current":
                identity_count += 1
                status = 401 if identity_count == 1 else 200
                data = (
                    {"error": "invalid_token"}
                    if status == 401
                    else {"user": {"id": 99, "login": "owner"}}
                )
            elif path == "/":
                data = None
                text = '<html><meta name="csrf-token" content="01234567-89ab-cdef-0123-456789abcdef"></html>'
            elif path == "/web/api/auth/refresh":
                data = {
                    "access_token": "new-access-token-0123456789",
                    "refresh_token": "new-refresh-token-0123456789",
                    "scope": "user",
                }
                for name, value in (
                    ("access_token_web", data["access_token"]),
                    ("refresh_token_web", data["refresh_token"]),
                ):
                    cookies.set(
                        name, value, domain=".www.vinted.co.uk", path="/", secure=True
                    )
            elif path == "/api/v2/items/123":
                status = 404
                data = None
                text = "<html>Not found</html>"
            elif path == "/items/123":
                data = None
                text = next_data(purchase_item(seller_id="100"))
            elif path.endswith("/payment"):
                if method == "POST":
                    payment_posts += 1
                    data = {"payment": {"status": "pending"}}
                elif method == "GET":
                    payment_gets += 1
                    data = {"payment": {"status": "success"}}
                else:
                    raise AssertionError(method)
            else:
                data = original(method, path, kwargs.get("json"))
            return Mock(
                status_code=status,
                text=text,
                headers={},
                cookies=cookies,
                json=(
                    Mock(return_value=data)
                    if data is not None
                    else Mock(side_effect=ValueError("HTML"))
                ),
            )

        with patch.object(requests.Session, "request", side_effect=response) as wire:
            self.assertEqual(buying.buy(self.row)["state"], "needs_action")
            self.assertEqual(buying.buy(self.row)["state"], "needs_action")
            self.assertEqual(buying.check_payment("123")["state"], "paid")
            self.assertEqual(buying.buy(self.row)["state"], "paid")
        self.assertEqual(payment_posts, 1)
        self.assertEqual(payment_gets, 1)
        self.assertEqual(
            sum(
                c.args[:2] == ("POST", buyer.BASE + "/web/api/auth/refresh")
                for c in wire.call_args_list
            ),
            1,
        )
        self.assertIn(
            ("GET", buyer.BASE + "/items/123"),
            [c.args[:2] for c in wire.call_args_list],
        )
        with closing(search_settings.connection()) as conn:
            row = conn.execute(
                "SELECT session,user_id,enabled FROM vinted_buyer"
            ).fetchone()
            self.assertEqual(tuple(row[1:]), ("99", 1))
            self.assertEqual(
                buyer.decrypt(row[0])["cookies"]["access_token_web"],
                "new-access-token-0123456789",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT max_total FROM vinted_search_budgets WHERE query_id=1"
                ).fetchone()[0],
                2000,
            )

    def test_response_cookie_domain_survives_saved_session_and_real_buying_client(self):
        headers = Message()
        headers.add_header(
            "Set-Cookie",
            "anon_id=fictional-anonymous-id; Domain=www.vinted.co.uk; Path=/; Secure; HttpOnly",
        )
        client = buyer.Client(
            {"cookies": {"access_token_web": "fictional-access-token-0123456789"}}
        )
        requests.cookies.extract_cookies_to_jar(
            client.session.cookies,
            requests.Request("GET", buyer.BASE + "/").prepare(),
            SimpleNamespace(_original_response=SimpleNamespace(msg=headers)),
        )
        self.assertIn(".www.vinted.co.uk", {c.domain for c in client.session.cookies})
        with patch.object(client, "identity", return_value=("99", "owner")):
            buyer.save_connected(client)
        client.session.close()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET enabled=1")

        def response(method, url, **kwargs):
            path = url.removeprefix(buyer.BASE)
            if path == "/api/v2/users/current":
                data = {"user": {"id": 99, "login": "owner"}}
            else:
                data = self.client.request(method, path, kwargs.get("json"))
            return Mock(
                status_code=200, json=Mock(return_value=data), headers={}, text=""
            )

        with patch.object(requests.Session, "request", side_effect=response) as wire:
            self.assertEqual(buying.buy(self.row)["state"], "paid")
            self.assertEqual(buying.buy(self.row)["state"], "paid")
        self.assertEqual(
            wire.call_args_list[0].args[:2],
            ("GET", buyer.BASE + "/api/v2/users/current"),
        )
        self.assertEqual(len(self.payments()), 1)
        self.assertEqual(
            buyer.settings()["access"]["message"], buyer.AUTH_REASONS["connected"]
        )

    def test_rotated_www_domain_cookies_keep_their_received_scope(self):
        client = buyer.Client(
            {"cookies": {"access_token_web": "old-access-token-0123456789"}}
        )
        returned = requests.cookies.RequestsCookieJar()
        for name in ("access_token_web", "refresh_token_web"):
            returned.set(
                name,
                "fictional-" + name + "-0123456789",
                domain=".www.vinted.co.uk",
                path="/",
                secure=True,
            )
        self.assertTrue(client.update_tokens(Mock(cookies=returned), {}))
        restored = buyer.Client(client.exported())
        self.assertEqual(
            client.exported()["cookie_records"], restored.exported()["cookie_records"]
        )
        self.assertEqual(len(restored.session.cookies), 2)
        prepared = restored.session.prepare_request(
            requests.Request("GET", buyer.BASE + "/api/v2/users/current")
        )
        self.assertIn("access_token_web=fictional-", prepared.headers["Cookie"])
        client.session.close()
        restored.session.close()

    def test_saved_session_errors_update_connection_result_without_network_or_secrets(
        self,
    ):
        for saved in (
            b"fictional-private-invalid-ciphertext",
            buyer.encrypt(
                {
                    "cookie_records": [
                        {
                            "name": "access_token_web",
                            "value": "fictional-private-token",
                            "domain": ".www.vinted.co.uk.evil.test",
                        }
                    ]
                }
            ),
        ):
            with self.subTest(saved_type=type(saved).__name__):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("UPDATE vinted_buyer SET session=?", (saved,))
                    before = tuple(
                        conn.execute(
                            "SELECT user_id,username,enabled,max_total,max_extra FROM vinted_buyer"
                        ).fetchone()
                    )
                with patch.object(
                    requests.Session, "request"
                ) as request, self.assertLogs(
                    "vinted_buyer", level="INFO"
                ) as logs, self.assertRaises(
                    buyer.BuyerError
                ) as error:
                    buyer.connected_client()
                request.assert_not_called()
                self.assertEqual(error.exception.reason, "saved_session")
                self.assertEqual(error.exception.stage, "saved_session")
                self.assertEqual(
                    buyer.settings()["access"]["message"],
                    buyer.AUTH_REASONS["saved_session"],
                )
                self.assertIsNone(buyer.settings()["access"]["http_status"])
                self.assertNotIn("fictional-private", " ".join(logs.output))
                with closing(search_settings.connection()) as conn:
                    self.assertEqual(
                        before,
                        tuple(
                            conn.execute(
                                "SELECT user_id,username,enabled,max_total,max_extra FROM vinted_buyer"
                            ).fetchone()
                        ),
                    )
                    self.assertEqual(
                        conn.execute("SELECT session FROM vinted_buyer").fetchone()[0],
                        saved,
                    )

    def test_current_summary_inside_pay_button_supplies_verified_item_price(self):
        self.final = web_checkout()
        c = self.final["components"]
        c["pay_button_v2"]["order_summary_v2"] = c.pop("order_summary_v2")
        self.assertEqual(self.run_buy()["state"], "paid")
        self.assertEqual(len(self.payments()), 1)

    def test_current_item_prices_allow_empty_or_aggregate_summary_subtotal(self):
        for subtotal in (None, {"price": {"amount": "18.84", "currency_code": "GBP"}}):
            with self.subTest(subtotal_present=subtotal is not None):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("DELETE FROM vinted_buy_attempts")
                self.client.reset_mock()
                self.final = web_checkout()
                self.final["components"]["order_summary_v2"]["subtotal"] = subtotal
                self.assertEqual(self.run_buy()["state"], "paid")
                self.assertEqual(len(self.payments()), 1)

    def test_checkout_item_must_match_the_alert_before_payment(self):
        self.final = web_checkout()
        self.final["components"]["order_summary_v2"]["order_items"][0]["id"] = 789
        self.assertEqual(self.run_buy()["state"], "failed_before_payment")
        self.assertEqual(self.payments(), [])

    def test_current_checkout_uses_the_final_item_price_and_enforces_price_increases(
        self,
    ):
        for amount, state in (("14.50", "paid"), ("16.00", "failed_before_payment")):
            with self.subTest(amount=amount):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("DELETE FROM vinted_buy_attempts")
                self.client.reset_mock()
                self.final = web_checkout()
                self.final["components"]["order_summary_v2"]["order_items"][0][
                    "pricing"
                ]["final_price"]["amount"] = amount
                self.assertEqual(self.run_buy()["state"], state)
                self.assertEqual(len(self.payments()), 1 if state == "paid" else 0)

    def test_current_checkout_requires_one_consistent_gbp_item(self):
        invalid_items = (
            None,
            [],
            [{"id": 123}],
            web_checkout()["components"]["order_summary_v2"]["order_items"] * 2,
        )
        for items in invalid_items:
            with self.subTest(items_type=type(items).__name__):
                self.final = web_checkout()
                self.final["components"]["order_summary_v2"]["order_items"] = items
                self.assertEqual(self.run_buy()["state"], "failed_before_payment")
        for changed in ("id", "price"):
            with self.subTest(changed=changed):
                self.final = web_checkout()
                item = json.loads(
                    json.dumps(
                        self.final["components"]["order_summary_v2"]["order_items"][0]
                    )
                )
                if changed == "id":
                    item["id"] = 789
                else:
                    item["pricing"]["final_price"]["amount"] = "14.00"
                self.final["components"]["item_presentation_escrow_v2"] = {
                    "order_items": [item]
                }
                self.assertEqual(self.run_buy()["state"], "failed_before_payment")
        self.final = web_checkout()
        self.final["components"]["order_summary_v2"]["order_items"][0]["pricing"][
            "final_price"
        ]["currency_code"] = "EUR"
        self.assertEqual(self.run_buy()["state"], "failed_before_payment")
        self.assertEqual(self.payments(), [])

    def test_item_presentation_can_supply_the_current_item_without_a_subtotal(self):
        self.final = web_checkout()
        summary = self.final["components"]["order_summary_v2"]
        self.final["components"]["item_presentation_escrow_v2"] = {
            "order_items": summary.pop("order_items")
        }
        summary["subtotal"] = None
        self.assertEqual(self.run_buy()["state"], "paid")
        self.assertEqual(len(self.payments()), 1)

    def test_current_checkout_cannot_use_incomplete_legacy_selection_objects(self):
        for key in ("payment_method", "shipping_address", "shipping_pickup_details"):
            self.final = web_checkout()
            self.final["components"][key] = checkout()["components"][key]
            self.assertEqual(self.run_buy()["state"], "failed_before_payment")
        self.assertEqual(self.payments(), [])

    def test_home_delivery_requires_the_selected_complete_address(self):
        for address in ({"id": 789, "is_complete": True}, {"id": 456}, {}):
            self.final = web_checkout(home=True)
            self.final["components"]["shipping_pickup_details"][
                "receiver_address"
            ] = address
            self.assertEqual(self.run_buy()["state"], "failed_before_payment")
        self.assertEqual(self.payments(), [])

    def test_required_delivery_contact_stops_before_payment_when_absent(self):
        self.final = web_checkout()
        self.final["components"]["shipping_contact"] = {
            "is_receiver_phone_number_required": True,
            "phone_number": None,
        }
        self.assertEqual(self.run_buy()["state"], "failed_before_payment")
        self.assertEqual(self.payments(), [])
        self.final["components"]["shipping_contact"]["phone_number"] = "+447700900123"
        self.assertEqual(self.run_buy()["state"], "paid")
        self.assertEqual(len(self.payments()), 1)

    def test_explicit_failure_payment_is_recorded_without_retry(self):
        self.payment = {"payment": {"status": "failure"}}
        self.assertEqual(self.run_buy()["state"], "payment_failed")
        self.assertEqual(len(self.payments()), 1)
        self.assertEqual(self.run_buy()["state"], "payment_failed")
        self.assertEqual(len(self.payments()), 1)

    def test_submitted_payment_recheck_confirms_success_without_another_payment(self):
        self.payment = buyer.BuyerError("Timeout", reason="network")
        self.assertEqual(self.run_buy()["state"], "unknown")
        self.client.request.reset_mock()
        self.payment = {"payment": {"status": "success"}}
        with patch.object(buyer, "connected_client", return_value=self.client):
            saved = buying.check_payment("123")
        self.assertEqual(saved["state"], "paid")
        self.client.request.assert_called_once_with(
            "GET", "/api/v2/purchases/checkout-123/checkout/payment"
        )
        self.client.request.reset_mock()
        self.assertEqual(self.run_buy()["state"], "paid")
        self.client.request.assert_not_called()

    def test_failed_payment_recheck_preserves_uncertainty_and_closes_client(self):
        self.payment = buyer.BuyerError("Timeout", reason="network")
        self.run_buy()
        before = buying.result("123")
        self.client.request.reset_mock()
        self.client.session.close.reset_mock()
        with patch.object(
            buyer, "connected_client", return_value=self.client
        ), self.assertRaises(buyer.BuyerError):
            buying.check_payment("123")
        self.assertEqual(buying.result("123"), before)
        self.client.request.assert_called_once_with(
            "GET", "/api/v2/purchases/checkout-123/checkout/payment"
        )
        self.client.session.close.assert_called_once()

    def test_pending_preparing_and_failed_payment_reads_never_allow_a_payment_retry(
        self,
    ):
        buying.claim(self.row)
        for status, state in (
            ("pending", "needs_action"),
            ("preparing", "needs_action"),
            ("failure", "payment_failed"),
        ):
            buying.record(
                "123", "unknown", "Check Vinted", checkout_id="checkout-123", total=1884
            )
            self.client.request.reset_mock()
            self.payment = {"payment": {"status": status}}
            with patch.object(buyer, "connected_client", return_value=self.client):
                self.assertEqual(buying.check_payment("123")["state"], state)
            self.client.request.assert_called_once_with(
                "GET", "/api/v2/purchases/checkout-123/checkout/payment"
            )
            self.client.request.reset_mock()
            self.assertEqual(self.run_buy()["state"], state)
            self.client.request.assert_not_called()

    def test_current_web_pay_button_total_and_saved_pickup_pay_once(self):
        self.final = web_checkout()
        paid = self.run_buy()
        self.assertEqual((paid["state"], paid["total"]), ("paid", 1884))
        self.assertEqual(len(self.payments()), 1)
        self.assertEqual(self.run_buy()["state"], "paid")
        self.assertEqual(len(self.payments()), 1)

    def test_current_web_saved_home_delivery_can_pay(self):
        self.final = web_checkout(home=True)
        self.assertEqual(self.run_buy()["state"], "paid")
        self.assertEqual(len(self.payments()), 1)

    def test_current_pay_total_cannot_fall_back_or_disagree_with_another_total(self):
        bad = []
        for total in ("20.01", "NaN", "-1", "1.001"):
            bad.append(web_checkout(total))
        value = web_checkout()
        value["components"]["order_summary_v2"]["total"] = {
            "price": {"amount": "19.00", "currency_code": "GBP"}
        }
        bad.append(value)
        for pay in (
            None,
            [],
            {},
            {"total": None},
            {"total": {"price": {"amount": "18.84", "currency_code": "EUR"}}},
        ):
            value = checkout()
            value["components"]["pay_button_v2"] = pay
            bad.append(value)
        for value in bad:
            self.final = value
            self.assertEqual(self.run_buy()["state"], "failed_before_payment")
        self.assertEqual(self.payments(), [])

    def test_current_checkout_requires_saved_payment_address_rate_and_point(self):
        mutations = [
            lambda c: c["payment_method"].update(selected_payment_method=None),
            lambda c: c["payment_method"]["selected_payment_method"][
                "credit_card"
            ].update(expired=True),
            lambda c: c["payment_method"]["selected_payment_method"].update(
                pay_in_method=None
            ),
            lambda c: c["shipping_address"].update(address=None),
            lambda c: c["shipping_address"]["address"].update(is_complete=False),
            lambda c: c["shipping_pickup_details"]["pickup_details"].update(
                selected_rate_uuid=None
            ),
            lambda c: c["shipping_pickup_details"]["pickup_details"].update(
                shipping_point=None
            ),
            lambda c: c["shipping_pickup_details"]["pickup_details"][
                "shipping_point"
            ].update(rate_uuid="different-rate"),
            lambda c: c["shipping_pickup_options"].update(
                selected_pickup_option="unavailable"
            ),
        ]
        for change in mutations:
            self.final = web_checkout()
            change(self.final["components"])
            self.assertEqual(self.run_buy()["state"], "failed_before_payment")
        self.final = web_checkout(home=True)
        self.final["components"]["shipping_pickup_details"]["receiver_address"] = None
        self.assertEqual(self.run_buy()["state"], "failed_before_payment")
        self.assertEqual(self.payments(), [])

    def test_current_missing_selection_key_cannot_look_like_legacy_checkout(self):
        self.final = web_checkout()
        self.final["components"]["payment_method"] = {
            "cards": [{"id": "available-card"}],
            "pay_in_methods": [],
        }
        self.assertEqual(self.run_buy()["state"], "failed_before_payment")
        self.final = web_checkout()
        self.final["components"]["shipping_address"] = {"address_is_missing": True}
        self.assertEqual(self.run_buy()["state"], "failed_before_payment")
        self.assertEqual(self.payments(), [])

    def test_numeric_web_error_code_strings_are_classified_without_values(self):
        response = Mock(status_code=400, headers={}, text="")
        with self.assertLogs("vinted_buyer", level="INFO") as logs:
            error = buyer.response_error(
                response, {"code": "100", "message": "private-message"}, "sign_in"
            )
        self.assertEqual(error.reason, "credentials")
        self.assertIn("api_code=100", " ".join(logs.output))
        self.assertNotIn("private-message", " ".join(logs.output))

    def test_cookie_expiry_evidence_is_bounded_and_never_exposes_claims_or_tokens(self):
        import base64

        def token(expiry):
            payload = (
                base64.urlsafe_b64encode(
                    json.dumps(
                        {"exp": expiry, "private": "never-log-this-claim"}
                    ).encode()
                )
                .decode()
                .rstrip("=")
            )
            return "header." + payload + ".signature"

        with patch.object(buyer.time, "time", return_value=1000):
            self.assertEqual(buyer.token_expiry_hint(token(999)), "expired")
            self.assertEqual(buyer.token_expiry_hint(token(1001)), "not_expired")
            for value in (
                None,
                "opaque-private-refresh",
                "a.bad!.c",
                token(True),
                token(float("nan")),
                "x" * 8193,
            ):
                self.assertEqual(buyer.token_expiry_hint(value), "unknown")
        client = buyer.Client()
        self.addCleanup(client.session.close)
        client.session.cookies.set(
            "access_token_web", token(1), domain="www.vinted.co.uk", secure=True
        )
        client.session.cookies.set(
            "refresh_token_web",
            "private-refresh-root-0123456789",
            domain="www.vinted.co.uk",
            path="/",
            secure=True,
        )
        client.session.cookies.set(
            "refresh_token_web",
            "private-refresh-auth-0123456789",
            domain="www.vinted.co.uk",
            path="/web/api/auth",
            secure=True,
        )
        with self.assertLogs("vinted_buyer", level="INFO") as logs:
            client.log_cookie_evidence()
        text = " ".join(logs.output)
        self.assertIn("refresh_sent=2 refresh_stored=2", text)
        self.assertIn("access_expiry=expired", text)
        for secret in (
            token(1),
            "never-log-this-claim",
            "private-refresh-root",
            "private-refresh-auth",
        ):
            self.assertNotIn(secret, text)

    def test_unknown_400_renewal_does_not_require_reconnect_or_start_checkout(self):
        saved = {
            "csrf": "private-csrf-token-0123456789",
            "cookies": {
                "access_token_web": "private-access-token-0123456789",
                "refresh_token_web": "private-refresh-token-0123456789",
            },
        }
        with closing(search_settings.connection()) as conn, conn:
            sealed = buyer.encrypt(saved)
            conn.execute("UPDATE vinted_buyer SET session=?", (sealed,))

        def response(status, data, text=""):
            return Mock(
                status_code=status,
                json=Mock(return_value=data),
                text=text,
                headers={},
                cookies=requests.cookies.RequestsCookieJar(),
            )

        replies = [
            response(401, {"code": "unauthorized", "message": "Unauthorized"}),
            response(
                200,
                {},
                '<meta name="csrf-token" content="private-csrf-token-0123456789">',
            ),
            response(400, {"code": None, "message": "Bad Request"}),
        ]
        with patch.object(
            requests.Session, "request", side_effect=replies
        ) as wire, self.assertLogs("vinted_buyer", level="INFO") as logs:
            outcome = buying.buy(self.row)
        self.assertEqual(outcome["state"], "failed_before_payment")
        self.assertEqual(outcome["reason"], "renewal_failed")
        self.assertNotIn("Reconnect", outcome["message"])
        self.assertIn("needs checking", outcome["message"])
        self.assertIsNone(wire.call_args_list[-1].kwargs["json"])
        self.assertIsNone(wire.call_args_list[-1].kwargs["headers"]["Content-Type"])
        self.assertEqual(wire.call_count, 3)
        self.assertTrue(
            all("checkout" not in call.args[1] for call in wire.call_args_list)
        )
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute("SELECT session FROM vinted_buyer").fetchone()[0], sealed
            )
            self.assertEqual(
                conn.execute(
                    "SELECT max_total FROM vinted_search_budgets WHERE query_id=1"
                ).fetchone()[0],
                2000,
            )
        self.assertTrue(buyer.settings()["enabled"])
        self.assertEqual(buyer.settings()["access"]["stage"], "Vinted session renewal")
        self.assertIn("message_category=bad_request", " ".join(logs.output))
        for secret in (*saved["cookies"].values(), saved["csrf"]):
            self.assertNotIn(secret, " ".join(logs.output))

    def test_renewal_distinguishes_refresh_rejection_csrf_and_unknown_failure(self):
        for reason, expected in (
            ("credentials", "refresh_rejected"),
            ("csrf", "csrf"),
            ("http_error", "renewal_failed"),
            ("security_challenge", "security_challenge"),
        ):
            with self.subTest(reason=reason):
                client = Mock()
                client.request.side_effect = buyer.BuyerError(
                    "Fixed test failure", 400, reason=reason, stage="renewal"
                )
                with self.assertRaises(buyer.BuyerError) as failure:
                    buyer.renew_saved_client(client)
                self.assertEqual(failure.exception.reason, expected)
                self.assertEqual(failure.exception.stage, "renewal")
                self.assertEqual(failure.exception.status, 400)
                client.request.assert_called_once_with("POST", "/web/api/auth/refresh")

    def test_oauth_renewal_uses_one_private_grant_and_verifies_same_account(self):
        saved = {
            "csrf": "private-csrf-token-0123456789",
            "cookies": {
                "access_token_web": "private-access-token-0123456789",
                "refresh_token_web": "private-refresh-token-0123456789",
            },
        }

        def response(status, data, text=""):
            return Mock(
                status_code=status,
                json=Mock(return_value=data),
                text=text,
                headers={},
                cookies=requests.cookies.RequestsCookieJar(),
            )

        for outcome in ("connected", "different_account", "refresh_rejected"):
            with self.subTest(outcome=outcome):
                with closing(search_settings.connection()) as conn, conn:
                    sealed = buyer.encrypt(saved)
                    conn.execute("UPDATE vinted_buyer SET session=?", (sealed,))
                replies = [
                    response(401, {"code": "unauthorized"}),
                    response(
                        200,
                        {},
                        '<meta name="csrf-token" content="fresh-csrf-token-0123456789">',
                    ),
                    (
                        response(400, {"error": "invalid_grant"})
                        if outcome == "refresh_rejected"
                        else response(
                            200,
                            {
                                "access_token": "rotated-access-token-0123456789",
                                "refresh_token": "rotated-refresh-token-0123456789",
                                "scope": "user",
                            },
                        )
                    ),
                ]
                if outcome != "refresh_rejected":
                    replies.append(
                        response(
                            200, {"user": {"id": 99 if outcome == "connected" else 100}}
                        )
                    )
                with patch.dict(
                    buyer.os.environ, {"VINTED_BUYER_REFRESH_METHOD": "oauth"}
                ), patch.object(
                    requests.Session, "request", side_effect=replies
                ) as wire, self.assertLogs(
                    "vinted_buyer", level="INFO"
                ) as logs:
                    if outcome == "connected":
                        client = buyer.connected_client()
                        client.session.close()
                    else:
                        with self.assertRaises(buyer.BuyerError) as failure:
                            buyer.connected_client()
                        if outcome == "refresh_rejected":
                            self.assertEqual(failure.exception.reason, outcome)
                            self.assertEqual(failure.exception.stage, "renewal")
                self.assertEqual(wire.call_count, len(replies))
                renewal = wire.call_args_list[2]
                self.assertEqual(
                    renewal.args, ("POST", buyer.BASE + "/web/api/auth/oauth")
                )
                self.assertEqual(
                    renewal.kwargs["json"],
                    {
                        "client_id": "web",
                        "grant_type": "refresh_token",
                        "refresh_token": saved["cookies"]["refresh_token_web"],
                    },
                )
                self.assertFalse(renewal.kwargs["allow_redirects"])
                self.assertTrue(
                    all("checkout" not in c.args[1] for c in wire.call_args_list)
                )
                self.assertIn("method=oauth", " ".join(logs.output))
                for secret in (
                    *saved["cookies"].values(),
                    saved["csrf"],
                    "rotated-access-token-0123456789",
                    "rotated-refresh-token-0123456789",
                ):
                    self.assertNotIn(secret, " ".join(logs.output))
                    self.assertNotIn(secret.encode(), Path(db.DB_PATH).read_bytes())
                self.assertEqual(buyer.settings()["user_id"], "99")
                if outcome == "refresh_rejected":
                    with closing(search_settings.connection()) as conn:
                        self.assertEqual(
                            conn.execute("SELECT session FROM vinted_buyer").fetchone()[
                                0
                            ],
                            sealed,
                        )

    def test_invalid_renewal_method_cannot_select_an_arbitrary_auth_route(self):
        client = Mock()
        with patch.dict(
            buyer.os.environ, {"VINTED_BUYER_REFRESH_METHOD": "invalid"}
        ), self.assertRaises(buyer.BuyerError) as failure:
            buyer.renew_saved_client(client)
        self.assertEqual(failure.exception.reason, "renewal_failed")
        client.refresh_security_token.assert_not_called()
        client.request.assert_not_called()

    def test_reconnect_cannot_replace_the_original_buyer_with_another_account(self):
        with closing(search_settings.connection()) as conn:
            original = tuple(conn.execute("SELECT * FROM vinted_buyer").fetchone())
        client = Mock()
        client.identity.return_value = ("100", "another-private-buyer")
        with self.assertRaises(buyer.BuyerError) as failure:
            buyer.save_connected(client)
        self.assertEqual(failure.exception.reason, "account_changed")
        self.assertNotIn("another-private-buyer", str(failure.exception))
        client.exported.assert_not_called()
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                tuple(conn.execute("SELECT * FROM vinted_buyer").fetchone()), original
            )

    def test_cookie_scope_and_expiry_survive_encrypted_session_restoration(self):
        client = buyer.Client()
        client.session.cookies.set(
            "access_token_web",
            "private-access-token-0123456789",
            domain=".vinted.co.uk",
            path="/",
            secure=True,
            expires=4102444800,
        )
        client.session.cookies.set(
            "refresh_token_web",
            "private-refresh-token-0123456789",
            domain="www.vinted.co.uk",
            path="/web/api/auth",
            secure=True,
        )
        client.session.cookies.set(
            "expired", "never-send", domain="www.vinted.co.uk", expires=1
        )
        saved = buyer.decrypt(buyer.encrypt(client.exported()))
        restored = buyer.Client(saved)
        self.assertEqual(saved["cookie_records"], restored.exported()["cookie_records"])
        identity = restored.session.prepare_request(
            requests.Request("GET", buyer.BASE + "/api/v2/users/current")
        )
        renewal = restored.session.prepare_request(
            requests.Request("POST", buyer.BASE + "/web/api/auth/refresh")
        )
        self.assertIn("access_token_web=", identity.headers["Cookie"])
        self.assertNotIn("refresh_token_web=", identity.headers["Cookie"])
        self.assertNotIn("never-send", identity.headers["Cookie"])
        self.assertIn("refresh_token_web=", renewal.headers["Cookie"])
        client.session.close()
        restored.session.close()

    def test_web_cookie_rotation_uses_set_cookie_for_both_tokens_and_keeps_scope(self):
        client = buyer.Client(
            {
                "cookies": {
                    "access_token_web": "old-access-token-0123456789",
                    "refresh_token_web": "old-refresh-token-0123456789",
                }
            }
        )
        returned = requests.cookies.RequestsCookieJar()
        for name in ("access_token_web", "refresh_token_web"):
            returned.set(
                name,
                "cookie-" + name + "-0123456789",
                domain=".vinted.co.uk",
                path="/",
                secure=True,
                expires=4102444800,
            )
        response = Mock(cookies=returned)
        self.assertTrue(
            client.update_tokens(
                response,
                {
                    "access_token": "body-access-token-0123456789",
                    "refresh_token": "body-refresh-token-0123456789",
                },
            )
        )
        for cookie in client.session.cookies:
            self.assertEqual(cookie.value, "cookie-" + cookie.name + "-0123456789")
            self.assertEqual(cookie.domain, ".vinted.co.uk")
            self.assertEqual(cookie.expires, 4102444800)
        self.assertEqual(len(client.session.cookies), 2)
        client.session.close()

    def test_malformed_or_foreign_saved_cookie_records_fail_without_leaking(self):
        for record in (
            {
                "name": "access_token_web",
                "value": "private-token",
                "domain": "evil.test",
            },
            {
                "name": "access_token_web",
                "value": "private\r\ntoken",
                "domain": "www.vinted.co.uk",
            },
            {
                "name": "access_token_web",
                "value": "private-token",
                "domain": ".vinted.co.uk",
                "path": "/\r\nprivate",
            },
        ):
            with patch.object(requests.Session, "close") as close, self.assertRaises(
                buyer.BuyerError
            ) as error:
                buyer.Client({"cookie_records": [record]})
            close.assert_called_once()
            self.assertNotIn("private", str(error.exception))

    def test_nested_web_error_diagnostics_identify_csrf_without_secret_values(self):
        response = Mock(status_code=400, headers={}, text="")
        data = {
            "error": {
                "errorCode": "invalid_csrf_token",
                "errorDescription": "csrf expired private-token",
            }
        }
        with self.assertLogs("vinted_buyer", level="INFO") as logs:
            error = buyer.response_error(response, data, "sign_in")
        self.assertEqual(error.reason, "csrf")
        self.assertIn("expired_hint=True", " ".join(logs.output))
        self.assertNotIn("private", " ".join(logs.output))

    def test_renewal_bootstrap_keeps_buyer_cookies_and_closes_public_client(self):
        private = buyer.Client(
            {
                "csrf": "old-private-csrf",
                "cookies": {"access_token_web": "private-access-token-0123456789"},
            }
        )
        public = Mock(csrf="current-public-csrf-token-0123456789")
        before = private.exported()["cookie_records"]
        with patch.object(buyer, "Client", return_value=public), self.assertLogs(
            "vinted_buyer", level="INFO"
        ) as logs:
            private.refresh_security_token()
        public.homepage.assert_called_once()
        public.session.close.assert_called_once()
        self.assertEqual(private.csrf, public.csrf)
        self.assertEqual(private.exported()["cookie_records"], before)
        self.assertIn("csrf_changed=True", " ".join(logs.output))
        self.assertNotIn("private", " ".join(logs.output))
        private.session.close()

    def test_refused_public_bootstrap_never_reaches_session_renewal(self):
        saved = {
            "csrf": "old-csrf",
            "cookies": {
                "access_token_web": "private-access-token-0123456789",
                "refresh_token_web": "private-refresh-token-0123456789",
            },
        }
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET session=?", (buyer.encrypt(saved),))
        private = Mock()
        private.identity.side_effect = buyer.BuyerError(
            "expired", 401, reason="credentials"
        )
        private.refresh_security_token.side_effect = buyer.BuyerError(
            "Stop", 403, reason="security_challenge", stage="homepage"
        )
        with patch.object(buyer, "Client", return_value=private), self.assertRaises(
            buyer.BuyerError
        ):
            buyer.connected_client()
        private.request.assert_not_called()
        private.session.close.assert_called_once()
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                buyer.decrypt(
                    conn.execute("SELECT session FROM vinted_buyer").fetchone()[0]
                ),
                saved,
            )

    def test_error_field_diagnostics_never_echo_values_or_unknown_fields(self):
        response = Mock(status_code=400, headers={}, text="")
        data = {
            "code": 20,
            "message": "csrf private-account-value",
            "errors": [
                {"field": "refresh_token", "value": "Required private-token-value"},
                {"field": "private-field-value", "value": "private-password-value"},
            ],
        }
        with self.assertLogs("vinted_buyer", level="INFO") as logs:
            buyer.response_error(response, data, "sign_in")
        text = " ".join(logs.output)
        self.assertIn("api_code=20 fields=refresh_token", text)
        self.assertIn("csrf_hint=True refresh_hint=True required_hint=True", text)
        self.assertNotIn("private", text)

    def test_web_transport_uses_owned_cookies_csrf_and_locale_without_bearer(self):
        csrf = "saved-csrf-token-0123456789"
        access = "saved-access-token-0123456789"
        anonymous_id = "saved-anonymous-id-0123456789"
        client = buyer.Client(
            {
                "csrf": csrf,
                "cookies": {
                    "access_token_web": access,
                    "refresh_token_web": "saved-refresh-token-0123456789",
                    "anon_id": anonymous_id,
                },
            }
        )
        client.session.headers["Authorization"] = "Bearer stale-credential"
        client.headers()
        for method, path in (
            ("GET", "/api/v2/users/current"),
            ("POST", "/web/api/auth/refresh"),
        ):
            prepared = client.session.prepare_request(
                requests.Request(method, buyer.BASE + path)
            )
            self.assertNotIn("Authorization", prepared.headers)
            self.assertIn("access_token_web=" + access, prepared.headers["Cookie"])
            self.assertEqual(prepared.headers["X-CSRF-Token"], csrf)
            self.assertEqual(prepared.headers["X-Anon-Id"], anonymous_id)
            self.assertEqual(prepared.headers["Locale"], "en-GB")
            self.assertIsNone(prepared.body)
        client.csrf = ""
        client.session.cookies.clear()
        client.headers()
        self.assertNotIn("X-CSRF-Token", client.session.headers)
        self.assertNotIn("X-Anon-Id", client.session.headers)
        client.session.close()

    def test_signin_token_parses_json_escaped_bootstrap_and_meta_attributes(self):
        token = "01234567-89ab-cdef-0123-456789abcdef"
        value = '{"CSRF_TOKEN":"' + token + '"}'
        for html in (
            value,
            json.dumps(value),
            json.dumps(json.dumps(value)),
            f'<meta name="csrf-token" content="{token}">',
            f'<meta content="{token}" name="csrf-token">',
        ):
            self.assertEqual(buyer.csrf_from_html(html), token)
        self.assertIsNone(buyer.csrf_from_html('"CSRF_TOKEN":"bad\\nheader"'))

    def setUp(self):
        super().setUp()
        self.row = {
            "item_id": "123",
            "query_id": 1,
            "url": "https://www.vinted.co.uk/items/123",
            "price": "15.00",
            "currency": "GBP",
        }
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?,verified_at=1,user_id='99',username='owner',enabled=1,max_total=2500,max_extra=500,browser_info=? WHERE id=1",
                (
                    buyer.encrypt({"cookies": {"access_token_web": "private-token"}}),
                    json.dumps(DEVICE),
                ),
            )
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("INSERT INTO vinted_search_budgets VALUES (1,2000,350)")
        self.item = {
            "item": {
                "id": 123,
                "price": {"amount": "15.00", "currency_code": "GBP"},
                "user": {"id": 100},
            }
        }
        self.client = Mock()
        self.final = checkout()
        self.payment = {"payment": {"status": "success"}}

        def response(method, path, body=None):
            if path == "/api/v2/items/123":
                return self.item
            if path == "/api/v2/conversations":
                return {"conversation": {"transaction": {"id": 456}}}
            if path == "/api/v2/purchases/checkout/build":
                return {"checkout": checkout()}
            if path == "/api/v2/purchases/checkout-123/checkout":
                return {"checkout": self.final}
            if path.endswith("/payment"):
                if isinstance(self.payment, Exception):
                    raise self.payment
                return self.payment
            raise AssertionError(path)

        self.client.request.side_effect = response

    def run_buy(self):
        with patch.object(buyer, "connected_client", return_value=self.client):
            return buying.buy(self.row)

    def payments(self):
        return [
            c
            for c in self.client.request.call_args_list
            if c.args[1].endswith("/payment")
        ]

    def test_valid_checkout_pays_once_and_duplicate_tap_is_idempotent(self):
        self.assertEqual(self.run_buy()["state"], "paid")
        self.assertEqual(self.run_buy()["state"], "paid")
        self.assertEqual(len(self.payments()), 1)
        self.assertEqual(self.payments()[0].args[2]["checksum"], "verified-checksum")
        self.assertEqual(buying.result("123")["total"], 1900)

    def test_removed_item_api_uses_current_page_and_still_pays_only_once(self):
        original = self.client.request.side_effect

        def response(method, path, body=None):
            if path == "/api/v2/items/123":
                raise buyer.BuyerError(
                    buyer.AUTH_REASONS["unreadable"], 404, reason="unreadable"
                )
            return original(method, path, body)

        self.client.request.side_effect = response
        self.client.listing_page.return_value = {
            "item": {
                "id": "123",
                "user_id": "100",
                "price": {"amount": "15.00", "currency_code": "GBP"},
                "can_buy": True,
                "is_reserved": False,
                "is_hidden": False,
            }
        }
        self.assertEqual(self.run_buy()["state"], "paid")
        self.assertEqual(self.run_buy()["state"], "paid")
        self.client.listing_page.assert_called_once_with(self.row["url"], "123")
        self.assertEqual(len(self.payments()), 1)

    def test_item_access_refusals_never_switch_to_the_listing_page(self):
        for status, reason in (
            (401, "credentials"),
            (403, "security_challenge"),
            (429, "rate_limited"),
            (307, "session_refresh"),
            (404, "security_challenge"),
        ):
            with self.subTest(status=status, reason=reason):
                self.client.reset_mock()
                self.client.request.side_effect = buyer.BuyerError(
                    "Vinted refused this request.", status, reason=reason
                )
                self.assertEqual(self.run_buy()["state"], "failed_before_payment")
                self.client.listing_page.assert_not_called()
                self.assertEqual(self.payments(), [])

    def test_current_page_disallowed_item_stops_before_conversation_or_payment(self):
        for change, reason in (
            ({"can_buy": False}, "item_unavailable"),
            ({"is_reserved": True}, "item_reserved"),
            ({"is_hidden": True}, "item_closed"),
        ):
            with self.subTest(change=change):
                self.client.reset_mock()
                self.client.request.side_effect = buyer.BuyerError(
                    "Item API no longer available.", 404, reason="http_error"
                )
                self.client.listing_page.return_value = {
                    "item": {
                        "id": "123",
                        "user_id": "100",
                        "price": {"amount": "15.00", "currency_code": "GBP"},
                        "can_buy": True,
                        "is_reserved": False,
                        "is_hidden": False,
                        **change,
                    }
                }
                outcome = self.run_buy()
                self.assertEqual(outcome["state"], "failed_before_payment")
                self.assertIn("No payment was sent", outcome["message"])
                self.client.request.assert_called_once_with("GET", "/api/v2/items/123")
                self.assertEqual(self.payments(), [])

    def test_buyer_listing_transport_uses_scoped_page_and_one_canonical_redirect(self):
        from test_vinted_page_data import next_data, purchase_item

        page = Mock(
            status_code=200,
            text=next_data(purchase_item(seller_id="100")),
            headers={},
            cookies=requests.cookies.RequestsCookieJar(),
        )
        redirect = Mock(
            status_code=301, text="", headers={"Location": "/items/123-canonical"}
        )
        client = buyer.Client(
            {"cookies": {"access_token_web": "fictional-access-token-0123456789"}}
        )
        try:
            with patch.object(
                client.session, "get", side_effect=[redirect, page]
            ) as get:
                data = client.listing_page(self.row["url"], "123")
            self.assertEqual(data["item"]["user_id"], "100")
            self.assertEqual(
                [c.args[0] for c in get.call_args_list],
                [buyer.BASE + "/items/123", buyer.BASE + "/items/123-canonical"],
            )
            self.assertTrue(
                all(c.kwargs["allow_redirects"] is False for c in get.call_args_list)
            )
            self.assertTrue(
                all(
                    c.kwargs["headers"]["Accept"].startswith("text/html")
                    and c.kwargs["headers"]["Sec-Fetch-Mode"] == "navigate"
                    for c in get.call_args_list
                )
            )
        finally:
            client.session.close()

    def test_buyer_listing_transport_refuses_foreign_redirects_challenges_and_wrong_items(
        self,
    ):
        from test_vinted_page_data import next_data, purchase_item

        client = buyer.Client()
        try:
            for status, text, headers in (
                (302, "", {"Location": "https://foreign.example/items/123"}),
                (302, "", {"Location": "/items/999-other"}),
                (302, "", {"Location": "/member/login"}),
                (403, "<html>Verify you are human</html>", {}),
                (404, "<html>Not found</html>", {}),
                (200, next_data(purchase_item(id="999")), {}),
            ):
                with self.subTest(status=status, headers=headers):
                    response = Mock(status_code=status, text=text, headers=headers)
                    with patch.object(
                        client.session, "get", return_value=response
                    ) as get, self.assertRaises(buyer.BuyerError):
                        client.listing_page(self.row["url"], "123")
                    get.assert_called_once()
            with patch.object(client.session, "get") as get, self.assertRaises(
                buyer.BuyerError
            ):
                client.listing_page("https://www.vinted.co.uk/items/999", "123")
            get.assert_not_called()
        finally:
            client.session.close()

    def test_listing_preflight_reads_saved_alert_without_changing_purchase_state(self):
        self.batch(1, [123])
        buying.claim(self.row)
        for state in ("failed_before_payment", "unknown", "paid"):
            with self.subTest(state=state):
                buying.record(
                    "123",
                    state,
                    "Keep this payment result",
                    checkout_id="existing-checkout",
                    total=1900,
                )
                before = buying.result("123")
                self.client.reset_mock()
                with patch.object(buyer, "connected_client", return_value=self.client):
                    message = buying.check_listing("123")
                self.assertIn("Listing check passed", message)
                self.assertIn("£15.00", message)
                self.assertIn("£20.00", message)
                self.assertIn("still need verification at checkout", message)
                self.assertIn("No checkout or payment was created", message)
                self.client.request.assert_called_once_with("GET", "/api/v2/items/123")
                self.client.session.close.assert_called_once()
                self.assertEqual(buying.result("123"), before)

    def test_listing_preflight_rejects_missing_alert_unsupported_platform_and_bad_ids(
        self,
    ):
        self.batch(1, [123])
        buying.claim(self.row)
        with patch.object(buyer, "connected_client") as connect:
            for value in ("", "123/../../users/current", "١٢٣", "9" * 25, "124"):
                with self.subTest(value=value), self.assertRaises(buyer.BuyerError):
                    buying.check_listing(value)
            with closing(search_settings.connection()) as conn, conn:
                conn.execute(
                    "UPDATE alert_outbox SET platform='ebay' WHERE item_id='123'"
                )
            with self.assertRaises(buyer.BuyerError):
                buying.check_listing("123")
            connect.assert_not_called()

    def test_listing_preflight_applies_live_price_availability_and_seller_checks(self):
        self.batch(1, [123])
        buying.claim(self.row)
        before = buying.result("123")
        for changes, reason in (
            ({"is_sold": True}, "item_sold"),
            ({"is_reserved": True}, "item_reserved"),
            ({"price": {"amount": "16.00", "currency_code": "GBP"}}, "price_increased"),
            ({"id": 124}, "item_unavailable"),
            ({"user": {"id": 99}}, None),
            ({"user": []}, None),
        ):
            with self.subTest(changes=changes):
                self.item = {
                    "item": {
                        "id": 123,
                        "price": {"amount": "15.00", "currency_code": "GBP"},
                        "user": {"id": 100},
                        **changes,
                    }
                }
                self.client.reset_mock()
                with patch.object(
                    buyer, "connected_client", return_value=self.client
                ), self.assertRaises(buyer.BuyerError) as error:
                    buying.check_listing("123")
                if reason:
                    self.assertEqual(error.exception.reason, reason)
                self.assertIn("checking the listing", str(error.exception))
                self.assertIn(
                    "No checkout or payment was created", str(error.exception)
                )
                self.client.request.assert_called_once_with("GET", "/api/v2/items/123")
                self.client.session.close.assert_called_once()
                self.assertEqual(buying.result("123"), before)

    def test_listing_preflight_stops_on_disabled_or_paused_search_before_network(self):
        self.batch(1, [123])
        buying.claim(self.row)
        before = buying.result("123")
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET enabled=0")
        with patch.object(buyer, "connected_client") as connect, self.assertRaises(
            buyer.BuyerError
        ):
            buying.check_listing("123")
        connect.assert_not_called()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET enabled=1")
            conn.execute("INSERT INTO search_dashboard(query_id,paused) VALUES (1,1)")
        with patch.object(buyer, "connected_client") as connect, self.assertRaises(
            buyer.BuyerError
        ):
            buying.check_listing("123")
        connect.assert_not_called()
        self.assertEqual(buying.result("123"), before)

    def test_listing_preflight_preserves_http_reason_and_never_retries_a_refusal(self):
        self.batch(1, [123])
        buying.claim(self.row)
        before = buying.result("123")
        for status, reason in (
            (404, "request"),
            (403, "security_challenge"),
            (429, "rate_limited"),
        ):
            with self.subTest(status=status):
                self.client.reset_mock()
                self.client.request.side_effect = buyer.BuyerError(
                    "Vinted did not accept this request.", status, reason=reason
                )
                with patch.object(
                    buyer, "connected_client", return_value=self.client
                ), self.assertRaises(buyer.BuyerError) as error:
                    buying.check_listing("123")
                self.assertEqual(error.exception.status, status)
                self.assertEqual(error.exception.reason, reason)
                self.assertIn(f"HTTP {status}", str(error.exception))
                self.client.request.assert_called_once_with("GET", "/api/v2/items/123")
                self.client.session.close.assert_called_once()
                self.assertEqual(buying.result("123"), before)

    def test_exact_search_budget_is_allowed_regardless_of_old_global_caps(self):
        self.final = checkout("20.00")
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET max_total=100,max_extra=0")
        self.assertEqual(self.run_buy()["state"], "paid")
        self.assertEqual(len(self.payments()), 1)

    def test_missing_budget_buys_alerted_item_plus_verified_fees_and_delivery(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DELETE FROM vinted_search_budgets")
        self.assertEqual(self.run_buy()["state"], "paid")
        self.assertEqual(buying.result("123")["total"], 1900)
        self.assertEqual(len(self.payments()), 1)

    def test_inactive_search_cannot_prepare_checkout_without_budget(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DELETE FROM vinted_search_budgets")
        self.row["query_id"] = None
        with self.assertRaisesRegex(buyer.BuyerError, "no longer active"):
            self.run_buy()
        self.client.request.assert_not_called()

    def test_url_item_limit_blocks_overpriced_listing_before_building_checkout(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DELETE FROM vinted_search_budgets")
            conn.execute(
                "UPDATE queries SET query='https://www.vinted.co.uk/catalog?price_to=14.99' WHERE id=1"
            )
        outcome = self.run_buy()
        self.assertEqual(outcome["state"], "failed_before_payment")
        self.assertEqual(outcome["reason"], "item_over_url_limit")
        self.client.request.assert_called_once_with("GET", "/api/v2/items/123")

    def test_url_limit_is_rechecked_after_delivery_selection_before_payment(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DELETE FROM vinted_search_budgets")
            conn.execute(
                "UPDATE queries SET query='https://www.vinted.co.uk/catalog?price_to=15' WHERE id=1"
            )
        response = self.client.request.side_effect

        def lower_limit(method, path, body=None):
            data = response(method, path, body)
            if method == "PUT":
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute(
                        "UPDATE queries SET query='https://www.vinted.co.uk/catalog?price_to=14' WHERE id=1"
                    )
            return data

        self.client.request.side_effect = lower_limit
        outcome = self.run_buy()
        self.assertEqual(outcome["reason"], "item_over_url_limit")
        self.assertEqual(self.payments(), [])

    def test_removing_total_budget_during_checkout_does_not_expand_this_tap(self):
        self.final = checkout("21.00")
        response = self.client.request.side_effect

        def remove_budget(method, path, body=None):
            data = response(method, path, body)
            if method == "PUT":
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("DELETE FROM vinted_search_budgets")
            return data

        self.client.request.side_effect = remove_budget
        outcome = self.run_buy()
        self.assertEqual(outcome["reason"], "total_over_budget")
        self.assertEqual(self.payments(), [])

    def test_missing_budget_keeps_listing_and_checkout_price_increase_checks(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DELETE FROM vinted_search_budgets")
        self.item["item"]["price"]["amount"] = "15.01"
        self.assertEqual(self.run_buy()["reason"], "price_increased")
        self.assertEqual(self.payments(), [])
        self.item["item"]["price"]["amount"] = "15.00"
        self.final["components"]["order_summary_v2"]["subtotal"]["price"][
            "amount"
        ] = "15.01"
        self.assertEqual(self.run_buy()["reason"], "price_increased")
        self.assertEqual(self.payments(), [])

    def test_current_budget_rechecked_after_checkout_and_before_any_payment(self):
        response = self.client.request.side_effect

        def lower_budget(method, path, body=None):
            data = response(method, path, body)
            if method == "PUT":
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute(
                        "UPDATE vinted_search_budgets SET max_total=1800 WHERE query_id=1"
                    )
            return data

        self.client.request.side_effect = lower_budget
        outcome = self.run_buy()
        self.assertEqual(outcome["state"], "failed_before_payment")
        self.assertIn("£18.00", outcome["message"])
        self.assertEqual(self.payments(), [])

    def test_deleted_or_paused_search_during_checkout_cannot_pay(self):
        response = self.client.request.side_effect
        for mutation in (
            "INSERT OR REPLACE INTO search_dashboard(query_id,paused) VALUES (1,1)",
            "DELETE FROM queries WHERE id=1",
        ):
            with self.subTest(mutation=mutation):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("DELETE FROM search_dashboard WHERE query_id=1")

                def change_search(method, path, body=None, mutation=mutation):
                    data = response(method, path, body)
                    if method == "PUT":
                        with closing(search_settings.connection()) as conn, conn:
                            conn.execute(mutation)
                    return data

                self.client.request.side_effect = change_search
                self.assertEqual(self.run_buy()["state"], "failed_before_payment")
                self.assertEqual(self.payments(), [])

    def test_new_buyer_settings_need_no_global_caps_and_dont_buy(self):
        buyer.save_limits(
            {"buyer_enabled": "yes", "buyer_browser_info": json.dumps(DEVICE)}
        )
        self.assertTrue(buyer.settings()["enabled"])
        self.client.request.assert_not_called()
        with self.assertRaises(buyer.BuyerError):
            buyer.save_limits({"buyer_enabled": "yes", "buyer_browser_info": "{}"})

    def test_payment_timeout_remains_unknown_across_repeated_taps(self):
        self.payment = buyer.BuyerError("Connection timeout")
        self.assertEqual(self.run_buy()["state"], "unknown")
        self.assertEqual(self.run_buy()["state"], "unknown")
        self.assertEqual(len(self.payments()), 1)

    def test_pending_bank_confirmation_is_never_reported_as_paid_or_repaid(self):
        self.payment = {"payment": {"status": "pending"}}
        self.assertEqual(self.run_buy()["state"], "needs_action")
        self.run_buy()
        self.assertEqual(len(self.payments()), 1)

    def test_pay_marker_survives_crash_and_blocks_another_payment(self):
        buying.claim(self.row)
        buying.record("123", "paying", "Waiting for confirmation")
        self.assertEqual(self.run_buy()["state"], "paying")
        self.client.request.assert_not_called()

    def test_abandoned_preparing_recovers_only_after_acquiring_the_buyer_lock(self):
        buying.claim(self.row)
        buying.record("123", "preparing", "Old interrupted checkout", checkout_id="old")
        self.assertFalse(buying.claim(self.row))
        with buyer.exclusive(), self.assertRaisesRegex(
            buyer.BuyerError, "already running"
        ):
            self.run_buy()
        self.assertEqual(buying.result("123")["state"], "preparing")
        self.client.request.assert_not_called()
        self.assertEqual(self.run_buy()["state"], "paid")
        self.assertEqual(buying.result("123")["checkout_id"], "checkout-123")
        self.assertEqual(len(self.payments()), 1)
        self.assertEqual(self.run_buy()["state"], "paid")
        self.assertEqual(len(self.payments()), 1)

    def test_preparing_callback_can_reach_locked_recovery(self):
        import asyncio

        import photo_cards

        buying.claim(self.row)
        buttons = [
            b
            for row in buying.feedback_buttons(self.row, buying.result("123"))
            for b in row
        ]
        self.assertIn("buy:click", [b.callback_data for b in buttons])
        query = SimpleNamespace(
            data="buy:click",
            message=SimpleNamespace(message_id=42, chat=SimpleNamespace(id=123)),
            from_user=SimpleNamespace(id=123),
        )
        outcome = {"state": "paid", "message": "Confirmed"}
        with patch.object(
            photo_cards, "recover", return_value=(self.row, {}, {})
        ), patch.object(photo_cards, "answer", new=AsyncMock()), patch.object(
            buying, "show_feedback", new=AsyncMock()
        ), patch.object(
            buying, "buy", return_value=outcome
        ) as buy:
            asyncio.run(
                buying.callback(
                    SimpleNamespace(callback_query=query), SimpleNamespace(bot=Mock())
                )
            )
        buy.assert_called_once_with(self.row)

    def test_failed_unknown_and_action_payment_states_never_recover_preparing(self):
        buying.claim(self.row)
        for state in ("paying", "unknown", "needs_action", "paid", "payment_failed"):
            with self.subTest(state=state):
                buying.record("123", state, "Check Vinted")
                self.assertFalse(buying.claim(self.row, recover_preparing=True))
                self.assertEqual(self.run_buy()["state"], state)
        self.client.request.assert_not_called()

    def test_bank_action_link_is_shown_and_never_resubmits_payment(self):
        action_url = (
            "https://bank.example.test/confirm?challenge=private-payment-reference"
        )
        self.payment = {
            "payment": {"status": "pending"},
            "action": {"parameters": {"url": action_url}},
        }
        outcome = self.run_buy()
        self.assertEqual(outcome["state"], "needs_action")
        self.assertEqual(outcome["action_url"], action_url)
        self.assertIn("confirmation button", outcome["message"])
        buttons = [b for row in buying.feedback_buttons(self.row, outcome) for b in row]
        self.assertEqual([b.url for b in buttons if b.url], [action_url])
        self.assertNotIn("buy:click", [b.callback_data for b in buttons])
        self.assertEqual(self.run_buy()["action_url"], action_url)
        self.assertEqual(len(self.payments()), 1)

    def test_unsafe_or_malformed_bank_action_urls_use_the_vinted_fallback(self):
        for value in (
            "http://bank.example.test/confirm",
            "javascript:alert(1)",
            "https://private-reference@bank.example.test/confirm",
            "https://bank.example.test:8443/confirm",
            "https://bank.example.test:invalid/confirm",
            "https://bank.example.test/\r\nprivate-reference",
            "https://bank.example.test/" + "x" * 4096,
            [],
            None,
        ):
            with self.subTest(value=value):
                feedback = {
                    "state": "needs_action",
                    "action_url": value,
                    "checkout_id": "checkout-123",
                }
                self.assertIsNone(
                    buying.payment_action_url(
                        {"action": {"parameters": {"url": value}}}
                    )
                )
                buttons = [
                    b
                    for row in buying.feedback_buttons(self.row, feedback)
                    for b in row
                ]
                self.assertEqual(
                    [b.url for b in buttons if b.url],
                    [buyer.BASE + "/checkout?purchase_id=checkout-123"],
                )

    def test_existing_attempt_schema_migrates_bank_actions_idempotently(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("ALTER TABLE vinted_buy_attempts DROP COLUMN action_url")
            conn.execute(
                "INSERT INTO vinted_buy_attempts VALUES ('old','unknown','old-checkout',1900,'Check Vinted',1)"
            )
            buyer.migrate(conn)
            buyer.migrate(conn)
            saved = dict(
                conn.execute(
                    "SELECT * FROM vinted_buy_attempts WHERE item_id='old'"
                ).fetchone()
            )
        self.assertEqual(saved["state"], "unknown")
        self.assertEqual(saved["checkout_id"], "old-checkout")
        self.assertIsNone(saved["action_url"])

    def test_final_total_fees_currency_and_missing_fields_all_fail_closed(self):
        bad = []
        for amount in ("30.00", "21.00", "NaN", "-1", "1.001"):
            bad.append(checkout(amount))
        wrong = checkout()
        wrong["components"]["order_summary_v2"]["total"]["price"][
            "currency_code"
        ] = "EUR"
        bad.append(wrong)
        missing = checkout()
        missing["components"]["order_summary_v2"].pop("total")
        bad.append(missing)
        for field in (
            "payment_method",
            "shipping_address",
            "shipping_pickup_details",
            "shipping_pickup_options",
        ):
            value = checkout()
            value["components"].pop(field)
            bad.append(value)
        value = checkout()
        value.pop("checksum")
        bad.append(value)
        for value in bad:
            with self.subTest(checkout=value):
                self.final = value
                self.assertEqual(self.run_buy()["state"], "failed_before_payment")
        self.assertEqual(self.payments(), [])

    def test_component_errors_and_invalid_checksum_cannot_submit_payment(self):
        bad = []
        for key in (
            "order_summary_v2",
            "additional_service",
            "shipping_address",
            "payment_method",
            "shipping_pickup_details",
            "shipping_pickup_options",
        ):
            value = checkout()
            value["components"].setdefault(key, {})["errors"] = ["Needs attention"]
            bad.append(value)
        for checksum in (
            True,
            ["invalid"],
            {"invalid": "checksum"},
            "bad\nchecksum",
            "x" * 8193,
        ):
            value = checkout()
            value["checksum"] = checksum
            bad.append(value)
        for key in (
            "shipping_address",
            "payment_method",
            "shipping_pickup_details",
            "shipping_pickup_options",
            "order_summary_v2",
        ):
            value = checkout()
            value["components"][key] = ["unexpected"]
            bad.append(value)
        for value in bad:
            with self.subTest(checkout=value):
                self.final = value
                self.assertEqual(self.run_buy()["state"], "failed_before_payment")
        self.assertEqual(self.payments(), [])

    def test_sold_reserved_or_higher_item_price_never_reaches_payment(self):
        original = copy.deepcopy(self.item)
        for change in (
            {"is_sold": True},
            {"is_closed": True},
            {"is_reserved": True},
            {"price": {"amount": "16", "currency_code": "GBP"}},
            {"user": {"id": 99}},
        ):
            self.item = {"item": dict(original["item"], **change)}
            self.assertEqual(self.run_buy()["state"], "failed_before_payment")
        self.assertEqual(self.payments(), [])

    def test_sold_reserved_and_closed_report_the_actual_reason(self):
        original = copy.deepcopy(self.item)
        for flag, reason, message in (
            ("is_sold", "item_sold", "already sold"),
            ("is_reserved", "item_reserved", "reserved"),
            ("is_closed", "item_closed", "closed or removed"),
        ):
            self.item = {"item": dict(original["item"], **{flag: True})}
            result = self.run_buy()
            self.assertEqual(result["reason"], reason)
            self.assertIn(message, result["message"])
        self.assertEqual(self.payments(), [])

    def test_unreadable_response_reports_phase_and_http_without_secrets(self):
        self.client.request.side_effect = buyer.BuyerError(
            buyer.AUTH_REASONS["unreadable"], 404, reason="unreadable"
        )
        self.client.listing_page.side_effect = buyer.BuyerError(
            buyer.AUTH_REASONS["unreadable"], 404, reason="unreadable"
        )
        result = self.run_buy()
        self.assertEqual(result["state"], "failed_before_payment")
        self.assertIn("checking the listing (HTTP 404)", result["message"])
        self.assertIn("No payment was sent", result["message"])
        self.assertNotIn("account connection has not been verified", result["message"])
        self.assertEqual(self.payments(), [])

    def test_over_budget_lists_total_with_fees_and_cap(self):
        self.final = checkout("22.20")
        result = self.run_buy()
        self.assertEqual(result["reason"], "total_over_budget")
        self.assertIn("£22.20 including fees and delivery", result["message"])
        self.assertIn("£20.00", result["message"])
        self.assertEqual(self.payments(), [])

    def test_autobuy_off_does_not_make_vinted_requests(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET enabled=0")
        with self.assertRaises(buyer.BuyerError):
            self.run_buy()
        self.client.request.assert_not_called()

    def test_only_private_owner_callback_can_start_purchase(self):
        query = SimpleNamespace(
            message=SimpleNamespace(chat=SimpleNamespace(id=123)),
            from_user=SimpleNamespace(id=999),
            answer=AsyncMock(),
        )
        import asyncio

        with patch.object(buying, "buy") as buy:
            asyncio.run(
                buying.callback(
                    SimpleNamespace(callback_query=query), SimpleNamespace(bot=Mock())
                )
            )
        buy.assert_not_called()

    def test_sessions_are_encrypted_and_never_returned_to_settings(self):
        with closing(search_settings.connection()) as conn:
            raw = conn.execute("SELECT session FROM vinted_buyer").fetchone()[0]
        self.assertNotIn(b"private-token", raw)
        self.assertNotIn("session", buyer.settings())
        self.assertEqual(
            buyer.decrypt(raw)["cookies"]["access_token_web"], "private-token"
        )
        key = Path(db.DB_PATH).parent / "vinted-buyer.key"
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)

    def test_verification_or_rate_limit_never_retries_or_exposes_response(self):
        client = buyer.Client()
        for status in (403, 429, 401):
            response = Mock(
                status_code=status, json=Mock(return_value={"secret": "do not display"})
            )
            with patch.object(
                client.session, "request", return_value=response
            ) as request:
                with self.assertRaises(buyer.BuyerError) as error:
                    client.request("GET", "/api/v2/users/current")
                self.assertNotIn("do not display", str(error.exception))
                request.assert_called_once()
        client.session.close()

    def test_nominal_refresh_success_requires_a_usable_access_token(self):
        client = buyer.Client(
            {"cookies": {"access_token_web": "old-access-token-0123456789"}}
        )
        for data in (
            {},
            {"access_token": "bad\r\nvalue"},
            {"error": "invalid_grant"},
            {"error": "invalid_grant", "access_token": "new-access-token-0123456789"},
        ):
            response = Mock(
                status_code=200, json=Mock(return_value=data), text="", headers={}
            )
            with patch.object(
                client.session, "request", return_value=response
            ), self.assertRaises(buyer.BuyerError):
                client.request(
                    "POST", "/web/api/auth/oauth", {"grant_type": "refresh_token"}
                )
        for cookies, data in (
            ({}, {"access_token": "new-access-token-0123456789"}),
            ({"access_token_web": "cookie-access-token-0123456789"}, {}),
        ):
            response = Mock(
                status_code=200,
                json=Mock(return_value=data),
                text="",
                headers={},
                cookies=requests.cookies.cookiejar_from_dict(cookies),
            )
            with patch.object(client.session, "request", return_value=response):
                self.assertEqual(
                    client.request(
                        "POST", "/web/api/auth/oauth", {"grant_type": "refresh_token"}
                    ),
                    data,
                )
        client.session.close()

    def test_nominal_http_success_does_not_accept_marketplace_errors_or_error_tokens(
        self,
    ):
        old = "old-access-token-0123456789"
        client = buyer.Client({"cookies": {"access_token_web": old}})
        for path, data, reason in (
            ("/api/v2/items/123", {"error": "invalid_token"}, "credentials"),
            ("/api/v2/users/current", {"code": 100}, "credentials"),
            ("/api/v2/purchases/checkout/build", {"code": 114}, "http_error"),
            (
                "/api/v2/purchases/checkout/build",
                {"error_code": "invalid_csrf_token"},
                "csrf",
            ),
            (
                "/web/api/auth/refresh",
                {
                    "error": "invalid_grant",
                    "access_token": "untrusted-new-token-0123456789",
                },
                "credentials",
            ),
        ):
            response = Mock(
                status_code=200, text="", headers={}, json=Mock(return_value=data)
            )
            with patch.object(
                client.session, "request", return_value=response
            ) as request, self.assertRaises(buyer.BuyerError) as error:
                client.request("POST", path, {})
            self.assertEqual(error.exception.reason, reason)
            self.assertEqual(client.exported()["cookies"]["access_token_web"], old)
            self.assertNotIn("Authorization", client.session.headers)
            request.assert_called_once()
        client.session.close()

    def test_direct_refresh_rejects_empty_invalid_or_error_success_without_retry(self):
        old = "old-access-token-0123456789"
        for data in (
            {},
            {"access_token": "bad\r\nvalue"},
            {"error": "invalid_grant"},
            {
                "error_code": "invalid_token",
                "access_token": "new-access-token-0123456789",
            },
        ):
            with self.subTest(data=data):
                client = buyer.Client({"cookies": {"access_token_web": old}})
                response = Mock(
                    status_code=200,
                    json=Mock(return_value=data),
                    text="",
                    headers={},
                )
                with patch.object(
                    client.session, "request", return_value=response
                ) as request, self.assertLogs(
                    "vinted_buyer", level="INFO"
                ) as logs, self.assertRaises(
                    buyer.BuyerError
                ) as error:
                    client.request(
                        "POST",
                        "/web/api/auth/refresh",
                        {"refresh_token": "private-refresh-token-0123456789"},
                    )
                request.assert_called_once()
                for value in (old, "private-refresh-token-0123456789"):
                    self.assertNotIn(value, " ".join(logs.output))
                    self.assertNotIn(value, str(error.exception))
                client.session.close()

    def test_direct_refresh_accepts_access_token_from_body_or_rotated_cookie(self):
        new = "new-access-token-0123456789"
        for cookies, data in (
            ({}, {"access_token": new}),
            ({"access_token_web": new}, {}),
        ):
            with self.subTest(cookies=cookies):
                client = buyer.Client(
                    {"cookies": {"access_token_web": "old-access-token-0123456789"}}
                )
                response = Mock(
                    status_code=200,
                    json=Mock(return_value=data),
                    text="",
                    headers={},
                    cookies=requests.cookies.cookiejar_from_dict(cookies),
                )
                with patch.object(
                    client.session, "request", return_value=response
                ) as request:
                    self.assertEqual(
                        client.request("POST", "/web/api/auth/refresh", {}), data
                    )
                request.assert_called_once()
                self.assertEqual(client.exported()["cookies"]["access_token_web"], new)
                self.assertNotIn("Authorization", client.session.headers)
                client.session.close()

    def test_renewal_diagnostics_hide_conflicting_tokens_and_scope(self):
        old = "old-private-access-token-0123456789"
        body = "body-private-access-token-0123456789"
        cookie = "cookie-private-access-token-0123456789"
        scope = "user private-scope-marker"
        client = buyer.Client({"cookies": {"access_token_web": old}})
        response = Mock(
            status_code=200,
            json=Mock(return_value={"access_token": body, "scope": scope}),
            text="",
            headers={},
            cookies=requests.cookies.cookiejar_from_dict({"access_token_web": cookie}),
        )

        def merged_response(*args, **kwargs):
            # Requests merges response cookies before the request method returns.
            client.session.cookies.set(
                "access_token_web", cookie, domain=".vinted.co.uk"
            )
            return response

        with patch.object(
            client.session, "request", side_effect=merged_response
        ), self.assertLogs("vinted_buyer", level="INFO") as logs:
            client.request("POST", "/web/api/auth/refresh", {})
        text = " ".join(logs.output)
        self.assertIn("token_changed=True", text)
        self.assertIn("sources_match=False", text)
        self.assertIn("scope_present=True scope_user=True", text)
        for secret in (old, body, cookie, scope, "private-scope-marker"):
            self.assertNotIn(secret, text)
        self.assertEqual(client.exported()["cookies"]["access_token_web"], cookie)
        client.session.close()

    def test_redirect_diagnostics_never_include_path_or_query_secrets(self):
        for location, label in (
            ("/private-token?secret=do-not-log", "same_origin_other_path"),
            ("https://vinted.co.uk/?secret=do-not-log", "uk_apex"),
            ("https://evil.test/private-token?secret=do-not-log", "other_origin"),
            ("https://private-token@www.vinted.co.uk/", "userinfo"),
        ):
            response = Mock(status_code=307, headers={"Location": location}, text="")
            with self.assertLogs("vinted_buyer", level="INFO") as logs:
                buyer.response_error(response, None, "homepage")
            self.assertIn(label, " ".join(logs.output))
            self.assertNotIn("private-token", " ".join(logs.output))
            self.assertNotIn("do-not-log", " ".join(logs.output))

    def test_homepage_block_stops_before_sending_password(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET login_attempt=0")
        with patch.object(
            buyer.Client,
            "homepage",
            side_effect=buyer.BuyerError("Verification needed"),
        ), patch.object(buyer.Client, "request") as request, self.assertRaises(
            buyer.BuyerError
        ):
            buyer.start_login("owner@example.test", "never-store-password")
        request.assert_not_called()
        self.assertNotIn(b"never-store-password", Path(db.DB_PATH).read_bytes())

    def test_login_session_is_saved_but_payment_stays_off(self):
        with patch.object(buyer, "Client") as client:
            client.return_value.request.return_value = {}
            client.return_value.identity.return_value = ("99", "owner")
            client.return_value.exported.return_value = {
                "cookies": {"access_token_web": "new-private-token"},
                "csrf": "csrf",
            }
            buyer.start_login("owner@example.test", "never-store-password")
        self.assertTrue(buyer.settings()["connected"])
        self.assertFalse(buyer.settings()["enabled"])
        self.assertNotIn(b"never-store-password", Path(db.DB_PATH).read_bytes())

    def test_username_password_login_sends_username_without_saving_password(self):
        with patch.object(buyer, "Client") as client:
            client.return_value.request.return_value = {}
            client.return_value.identity.return_value = ("99", "owner")
            client.return_value.exported.return_value = {
                "cookies": {"access_token_web": "new-private-token"},
                "csrf": "csrf",
            }
            buyer.start_login("offline_buyer", "never-store-password")
        request = client.return_value.request.call_args
        self.assertEqual(request.args[:2], ("POST", "/web/api/auth/oauth"))
        self.assertEqual(request.args[2]["username"], "offline_buyer")
        self.assertFalse(buyer.settings()["enabled"])
        self.assertNotIn(b"never-store-password", Path(db.DB_PATH).read_bytes())

    def test_network_error_never_includes_credentials(self):
        client = buyer.Client()
        with patch.object(
            client.session,
            "request",
            side_effect=requests.RequestException("password-secret"),
        ), self.assertRaises(buyer.BuyerError) as error:
            client.request("GET", "/api/v2/users/current")
        self.assertNotIn("password-secret", str(error.exception))
        client.session.close()

    def test_redirects_are_classified_without_following_or_leaking_destinations(self):
        client = buyer.Client()
        cases = [
            ("/web/api/auth/refresh?secret=private", "session_refresh"),
            ("/?secret=private", "home_redirect"),
            ("/member/login?secret=private", "signin_redirect"),
            (
                "https://geo.captcha-delivery.com/captcha?secret=private",
                "security_challenge",
            ),
            ("https://evil.test/web/api/auth/refresh", "redirect"),
            ("https://www.vinted.co.uk@evil.test/", "redirect"),
            ("https://www.vinted.co.uk:443/web/api/auth/refresh", "redirect"),
            ("", "redirect"),
            ("/\r\nInjected: private", "redirect"),
        ]
        for location, reason in cases:
            response = Mock(
                status_code=307,
                headers={"Location": location},
                text="",
                json=Mock(side_effect=ValueError),
            )
            with patch.object(
                client.session, "request", return_value=response
            ) as request, self.assertRaises(buyer.BuyerError) as error:
                client.request("GET", "/api/v2/users/current")
            self.assertEqual(error.exception.reason, reason)
            self.assertNotIn("private", str(error.exception))
            self.assertNotIn("evil", str(error.exception))
            self.assertFalse(request.call_args.kwargs["allow_redirects"])
            request.assert_called_once()
        client.session.close()

    def test_refresh_replaces_host_and_domain_tokens_without_duplicates(self):
        old = "old-test-token-0123456789"
        new_access = "new-access-token-0123456789"
        new_refresh = "new-refresh-token-0123456789"
        client = buyer.Client(
            {"cookies": {"access_token_web": old, "refresh_token_web": old}}
        )
        returned = requests.cookies.RequestsCookieJar()
        for name, value in (
            ("access_token_web", new_access),
            ("refresh_token_web", new_refresh),
        ):
            returned.set(name, value, domain=".vinted.co.uk", secure=True)
            client.session.cookies.set(name, value, domain=".vinted.co.uk", secure=True)
        response = Mock(
            status_code=200, text="", cookies=returned, json=Mock(return_value={})
        )
        with patch.object(client.session, "request", return_value=response):
            client.request("POST", "/web/api/auth/oauth", {})
        self.assertEqual(
            client.exported()["cookies"],
            {"access_token_web": new_access, "refresh_token_web": new_refresh},
        )
        self.assertEqual(len(list(client.session.cookies)), 2)
        self.assertNotIn("Authorization", client.session.headers)
        prepared = client.session.prepare_request(
            requests.Request("GET", buyer.BASE + "/api/v2/users/current")
        )
        self.assertNotIn(old, prepared.headers["Cookie"])
        self.assertEqual(prepared.headers["Cookie"].count("access_token_web="), 1)
        client.session.close()

    def test_json_refresh_tokens_are_validated_before_cookie_update(self):
        old = "old-test-token-0123456789"
        client = buyer.Client({"cookies": {"access_token_web": old}})
        for invalid in (
            "short",
            "bad\r\nheader-value",
            {"secret": "invalid"},
            "x" * 8193,
        ):
            client.update_tokens(Mock(), {"access_token": invalid})
            self.assertEqual(client.exported()["cookies"]["access_token_web"], old)
            self.assertNotIn("Authorization", client.session.headers)
        new = "new-test-token-0123456789"
        client.update_tokens(Mock(), {"access_token": new})
        self.assertEqual(client.exported()["cookies"]["access_token_web"], new)
        self.assertNotIn("Authorization", client.session.headers)
        client.session.close()

    def test_homepage_follows_one_same_origin_canonical_redirect(self):
        client = buyer.Client()
        redirect = Mock(
            status_code=307, headers={"Location": "/?locale=en-GB"}, text=""
        )
        final = Mock(status_code=200, text='{"CSRF_TOKEN":"test-csrf-0123456789"}')
        with patch.object(client.session, "get", side_effect=[redirect, final]) as get:
            client.homepage()
        self.assertEqual(client.csrf, "test-csrf-0123456789")
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.args[0], buyer.BASE + "/?locale=en-GB")
        self.assertTrue(
            all(not c.kwargs["allow_redirects"] for c in get.call_args_list)
        )
        client.session.close()

    def test_homepage_never_follows_challenges_external_hosts_or_redirect_loops(self):
        client = buyer.Client()
        for location, text, reason in (
            ("https://evil.test/", "", "redirect"),
            ("//www.vinted.co.uk@evil.test/", "", "redirect"),
            ("/web/api/auth/refresh", "", "session_refresh"),
            ("/member/login", "", "signin_redirect"),
            ("/", "<html>Verify you are human</html>", "security_challenge"),
            ("", "", "redirect"),
        ):
            response = Mock(status_code=307, headers={"Location": location}, text=text)
            with patch.object(
                client.session, "get", return_value=response
            ) as get, self.assertRaises(buyer.BuyerError) as error:
                client.homepage()
            self.assertEqual(error.exception.reason, reason)
            get.assert_called_once()
        loop = Mock(status_code=307, headers={"Location": "/"}, text="")
        with patch.object(
            client.session, "get", return_value=loop
        ) as get, self.assertRaises(buyer.BuyerError) as error:
            client.homepage()
        self.assertEqual(get.call_count, 2)
        self.assertEqual(error.exception.reason, "home_redirect")
        client.session.close()

    def test_rotated_tokens_survive_later_failure_without_changing_permissions(self):
        old_session = {
            "cookies": {
                "access_token_web": "old-access-token-0123456789",
                "refresh_token_web": "old-refresh-token-0123456789",
            }
        }
        rotated = {
            "cookies": {
                "access_token_web": "new-access-token-0123456789",
                "refresh_token_web": "new-refresh-token-0123456789",
            }
        }
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?", (buyer.encrypt(old_session),)
            )
            before = conn.execute(
                "SELECT verified_at,user_id,username,enabled,max_total,max_extra FROM vinted_buyer"
            ).fetchone()
            budgets = conn.execute("SELECT * FROM vinted_search_budgets").fetchall()
        client = Mock()
        client.csrf = ""
        client.identity.side_effect = buyer.BuyerError(
            buyer.AUTH_REASONS["session_refresh"],
            307,
            reason="session_refresh",
            stage="identity",
        )
        client.exported.return_value = rotated
        client.homepage.side_effect = buyer.BuyerError(
            buyer.AUTH_REASONS["redirect"], 307, reason="redirect", stage="homepage"
        )
        with patch.object(buyer, "Client", return_value=client), self.assertLogs(
            "vinted_buyer", level="INFO"
        ) as logs, self.assertRaises(buyer.BuyerError):
            buyer.connected_client()
        with closing(search_settings.connection()) as conn:
            saved = conn.execute("SELECT session FROM vinted_buyer").fetchone()[0]
            after = conn.execute(
                "SELECT verified_at,user_id,username,enabled,max_total,max_extra FROM vinted_buyer"
            ).fetchone()
            self.assertEqual(
                budgets, conn.execute("SELECT * FROM vinted_search_budgets").fetchall()
            )
        self.assertEqual(buyer.decrypt(saved), rotated)
        self.assertEqual(before, after)
        client.request.assert_called_once()
        self.assertEqual(client.request.call_args.args[1], "/web/api/auth/refresh")
        client.request.assert_called_once_with("POST", "/web/api/auth/refresh")
        client.identity.assert_called_once()
        client.session.close.assert_called_once()
        self.assertEqual(buyer.settings()["access"]["http_status"], 307)
        for value in rotated["cookies"].values():
            self.assertNotIn(value, " ".join(logs.output))
            self.assertNotIn(value.encode(), Path(db.DB_PATH).read_bytes())

    def test_only_explicit_session_expiry_refreshes_once_and_updates_diagnostics(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?",
                (
                    buyer.encrypt(
                        {
                            "cookies": {
                                "access_token_web": "access",
                                "refresh_token_web": "refresh",
                            }
                        }
                    ),
                ),
            )
        for reason, status, refresh_expected in [
            ("credentials", 401, True),
            ("session_refresh", 307, True),
            ("security_challenge", 401, False),
            ("security_challenge", 403, False),
            ("redirect", 307, False),
            ("forbidden", 403, False),
        ]:
            client = Mock()
            client.identity.side_effect = [
                buyer.BuyerError(
                    buyer.AUTH_REASONS[reason], status, reason=reason, stage="identity"
                ),
                ("99", "owner"),
            ]
            client.exported.return_value = {
                "cookies": {
                    "access_token_web": "access",
                    "refresh_token_web": "refresh",
                }
            }
            with patch.object(buyer, "Client", return_value=client):
                if refresh_expected:
                    self.assertIs(buyer.connected_client(), client)
                    client.request.assert_called_once()
                    client.request.assert_called_once_with(
                        "POST", "/web/api/auth/refresh"
                    )
                    self.assertEqual(buyer.settings()["access"]["http_status"], 200)
                else:
                    with self.assertRaises(buyer.BuyerError):
                        buyer.connected_client()
                    client.request.assert_not_called()
                    self.assertEqual(buyer.settings()["access"]["http_status"], status)
                    client.session.close.assert_called_once()

    def test_direct_refresh_rechecks_identity_once_and_preserves_rotated_session(self):
        old = {
            "csrf": "old-csrf-token-0123456789",
            "cookies": {
                "access_token_web": "old-access-token-0123456789",
                "refresh_token_web": "old-refresh-token-0123456789",
            },
        }
        new = {
            "access_token": "new-access-token-0123456789",
            "refresh_token": "new-refresh-token-0123456789",
        }

        def response(status, data, *, location=None, text=""):
            return Mock(
                status_code=status,
                text=text,
                json=Mock(return_value=data),
                headers={"Location": location} if location else {},
                cookies=requests.cookies.RequestsCookieJar(),
            )

        for outcome in ("connected", "second_redirect", "changed_account"):
            with self.subTest(outcome=outcome):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute(
                        "UPDATE vinted_buyer SET session=?", (buyer.encrypt(old),)
                    )
                final = (
                    response(307, None, location="/web/api/auth/refresh")
                    if outcome == "second_redirect"
                    else response(
                        200,
                        {"user": {"id": 99 if outcome == "connected" else 100}},
                    )
                )
                responses = [
                    response(
                        307,
                        None,
                        location="/web/api/auth/refresh?private=never-log-this",
                    ),
                    response(
                        200,
                        {},
                        text='<meta name="csrf-token" content="fresh-csrf-token-0123456789">',
                    ),
                    response(200, new),
                    final,
                ]
                with patch.object(
                    requests.Session, "request", side_effect=responses
                ) as request, self.assertLogs("vinted_buyer", level="INFO") as logs:
                    if outcome == "connected":
                        client = buyer.connected_client()
                        client.session.close()
                    else:
                        with self.assertRaises(buyer.BuyerError):
                            buyer.connected_client()
                self.assertEqual(
                    [(c.args[0], c.args[1]) for c in request.call_args_list],
                    [
                        ("GET", buyer.BASE + "/api/v2/users/current"),
                        ("GET", buyer.BASE + "/"),
                        ("POST", buyer.BASE + "/web/api/auth/refresh"),
                        ("GET", buyer.BASE + "/api/v2/users/current"),
                    ],
                )
                self.assertIsNone(request.call_args_list[2].kwargs["json"])
                self.assertIsNone(
                    request.call_args_list[2].kwargs["headers"]["Content-Type"]
                )
                self.assertTrue(
                    all(not c.kwargs["allow_redirects"] for c in request.call_args_list)
                )
                with closing(search_settings.connection()) as conn:
                    saved = conn.execute("SELECT session FROM vinted_buyer").fetchone()[
                        0
                    ]
                self.assertEqual(
                    buyer.decrypt(saved)["cookies"],
                    {
                        "access_token_web": new["access_token"],
                        "refresh_token_web": new["refresh_token"],
                    },
                )
                self.assertEqual(
                    buyer.decrypt(saved)["csrf"], "fresh-csrf-token-0123456789"
                )
                self.assertTrue(buyer.settings()["enabled"])
                self.assertEqual(buyer.settings()["user_id"], "99")
                for value in (
                    *old["cookies"].values(),
                    *new.values(),
                    "never-log-this",
                ):
                    self.assertNotIn(value, " ".join(logs.output))
                    self.assertNotIn(value.encode(), Path(db.DB_PATH).read_bytes())

    def test_native_web_renewal_uses_current_csrf_and_rejects_unverified_identity(self):
        old = {
            "csrf": "saved-csrf-token-0123456789",
            "cookies": {
                "access_token_web": "old-access-token-0123456789",
                "refresh_token_web": "old-refresh-token-0123456789",
            },
        }
        new = {
            "access_token": "new-access-token-0123456789",
            "refresh_token": "new-refresh-token-0123456789",
        }

        def response(status, data, text=""):
            return Mock(
                status_code=status,
                text=text,
                json=Mock(return_value=data),
                headers={},
                cookies=requests.cookies.RequestsCookieJar(),
            )

        for status, payload, accepted in (
            (200, {"user": {"id": 99}}, True),
            (200, {"user": {"id": 100}}, False),
            (200, {"user": None}, False),
            (401, {"error": "invalid_token"}, False),
            (403, {"error": "user_blocked"}, False),
            (429, {}, False),
            (403, {"error": "captcha_required"}, False),
        ):
            with self.subTest(status=status, accepted=accepted):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute(
                        "UPDATE vinted_buyer SET session=?", (buyer.encrypt(old),)
                    )
                replies = [
                    response(401, {"error": "invalid_token"}),
                    response(
                        200,
                        {},
                        text='<meta name="csrf-token" content="fresh-csrf-token-0123456789">',
                    ),
                    response(200, new),
                    response(status, payload),
                ]
                with patch.object(
                    requests.Session, "request", side_effect=replies
                ) as request, patch.object(requests.Session, "close") as close:
                    if accepted:
                        client = buyer.connected_client()
                        self.assertEqual(client.csrf, "fresh-csrf-token-0123456789")
                        client.session.close()
                    else:
                        with self.assertRaises(buyer.BuyerError):
                            buyer.connected_client()
                self.assertEqual(
                    [(call.args[0], call.args[1]) for call in request.call_args_list],
                    [
                        ("GET", buyer.BASE + "/api/v2/users/current"),
                        ("GET", buyer.BASE + "/"),
                        ("POST", buyer.BASE + "/web/api/auth/refresh"),
                        ("GET", buyer.BASE + "/api/v2/users/current"),
                    ],
                )
                self.assertIsNone(request.call_args_list[2].kwargs["json"])
                self.assertIsNone(
                    request.call_args_list[2].kwargs["headers"]["Content-Type"]
                )
                self.assertTrue(
                    all(
                        not call.kwargs["allow_redirects"]
                        for call in request.call_args_list
                    )
                )
                self.assertEqual(close.call_count, 2)
                with closing(search_settings.connection()) as conn:
                    saved = conn.execute("SELECT session FROM vinted_buyer").fetchone()[
                        0
                    ]
                self.assertEqual(
                    buyer.decrypt(saved)["csrf"], "fresh-csrf-token-0123456789"
                )
                self.assertEqual(
                    buyer.decrypt(saved)["cookies"]["access_token_web"],
                    new["access_token"],
                )
                self.assertTrue(buyer.settings()["enabled"])
                self.assertEqual(buyer.settings()["user_id"], "99")

    def test_saved_connection_check_never_prepares_checkout_or_changes_permission(self):
        with patch.object(buyer, "connected_client", return_value=self.client) as check:
            self.assertIn("No checkout or payment", buyer.check_saved_connection())
            self.assertTrue(buyer.settings()["enabled"])
            self.client.request.assert_not_called()
            with self.assertRaisesRegex(buyer.BuyerError, "30 seconds"):
                buyer.check_saved_connection()
            check.assert_called_once()

    def test_failure_diagnostics_distinguish_explicit_challenge_from_bare_403(self):
        cases = (
            (
                {
                    "url": "https://geo.captcha-delivery.com/captcha/?secret=private-token"
                },
                "security_challenge",
            ),
            ({"error": "invalid_csrf_token"}, "csrf"),
            ({"error": "account_blocked"}, "account_restricted"),
            ({"error": "unrecognised", "password": "private-password"}, "forbidden"),
        )
        for data, expected in cases:
            with self.subTest(reason=expected):
                response = Mock(status_code=403, text="", json=Mock(return_value=data))
                client = buyer.Client()
                with patch.object(
                    client.session, "request", return_value=response
                ) as request, self.assertRaises(buyer.BuyerError) as error:
                    client.request("POST", "/web/api/auth/oauth", {})
                self.assertEqual(error.exception.reason, expected)
                self.assertEqual(error.exception.status, 403)
                self.assertEqual(error.exception.stage, "sign_in")
                self.assertNotIn("private", str(error.exception))
                request.assert_called_once()
                client.session.close()

    def test_diagnosis_uses_one_empty_request_and_persists_challenge_without_secrets(
        self,
    ):
        response = Mock(
            status_code=403,
            text="",
            json=Mock(
                return_value={
                    "url": "https://geo.captcha-delivery.com/captcha/?secret=do-not-store",
                }
            ),
        )
        with patch.object(buyer.Client, "homepage"), patch.object(
            requests.Session, "request", return_value=response
        ) as request, self.assertRaises(buyer.BuyerError):
            buyer.check_signin()
        self.assertEqual(request.call_count, 1)
        sent = request.call_args.kwargs["json"]
        self.assertEqual(sent["username"], "")
        self.assertEqual(sent["password"], "")
        access = buyer.settings()["access"]
        self.assertEqual(access["stage"], "Vinted sign-in endpoint")
        self.assertEqual(access["http_status"], 403)
        self.assertIn("security check", access["message"])
        self.assertNotIn(b"do-not-store", Path(db.DB_PATH).read_bytes())
        with patch.object(buyer.Client, "homepage") as homepage, self.assertRaises(
            buyer.BuyerError
        ):
            buyer.check_signin()
        homepage.assert_not_called()

    def test_homepage_success_never_reports_buyer_connected(self):
        for code in (400, 401, 422):
            with closing(search_settings.connection()) as conn, conn:
                conn.execute("UPDATE vinted_buyer_access SET last_probe=0")
            with patch.object(buyer.Client, "homepage"), patch.object(
                buyer.Client,
                "request",
                side_effect=buyer.BuyerError(
                    "Validation failed", code, reason="http_error", stage="sign_in"
                ),
            ):
                message = buyer.check_signin()
            self.assertIn("have not been tested", message)
        self.assertNotIn("connected", buyer.settings()["access"]["message"])

    def test_signin_failure_is_persistent_and_never_logs_credentials(self):
        with patch.object(buyer.Client, "homepage"), patch.object(
            buyer.Client,
            "request",
            side_effect=buyer.BuyerError(
                "Refused", 403, reason="forbidden", stage="sign_in"
            ),
        ), self.assertLogs("vinted_buyer", level="INFO") as logs, self.assertRaises(
            buyer.BuyerError
        ):
            buyer.start_login("owner@example.test", "private-password")
        self.assertIn("reason=forbidden", " ".join(logs.output))
        self.assertNotIn("private-password", " ".join(logs.output))
        self.assertNotIn("owner@example.test", " ".join(logs.output))
        self.assertFalse(buyer.settings()["enabled"])
        self.assertEqual(buyer.settings()["access"]["http_status"], 403)

    def test_malformed_verification_payload_stops_without_crashing_or_retrying(self):
        client = buyer.Client()
        response = Mock(
            status_code=401,
            text="",
            json=Mock(return_value={"payload": ["unexpected"]}),
        )
        with patch.object(
            client.session, "request", return_value=response
        ) as request, self.assertRaises(buyer.BuyerError):
            client.request("POST", "/web/api/auth/oauth", {}, allow_challenge=True)
        request.assert_called_once()
        client.session.close()


class CheckoutInspectionTests(DatabaseFixture, unittest.TestCase):
    url = "https://www.vinted.co.uk/checkout?purchase_id=checkout-123&order_id=456&order_type=transaction"

    def setUp(self):
        super().setUp()
        self.client = Mock()
        self.client.request.return_value = {"checkout": web_checkout()}
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("INSERT INTO vinted_search_budgets VALUES (1,2000,350)")
            conn.execute(
                "INSERT INTO vinted_buy_attempts(item_id,state,message,updated) VALUES ('123','unknown','Keep this payment result',1)"
            )

    def inspect(self):
        with patch.object(buyer, "connected_client", return_value=self.client):
            return buying.check_checkout(self.url)

    def test_existing_checkout_uses_one_normal_load_without_payment_or_state_changes(
        self,
    ):
        before = buying.result("123")
        enabled = buyer.settings()["enabled"]
        message = self.inspect()
        self.assertIn("item price £15.00", message)
        self.assertIn("total £18.84 including fees and delivery", message)
        self.assertIn("No payment was submitted", message)
        self.client.request.assert_called_once_with(
            "PUT", "/api/v2/purchases/checkout-123/checkout"
        )
        self.client.session.close.assert_called_once()
        self.assertEqual(buying.result("123"), before)
        self.assertEqual(buyer.settings()["enabled"], enabled)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT max_total FROM vinted_search_budgets WHERE query_id=1"
                ).fetchone()[0],
                2000,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM vinted_buy_attempts").fetchone()[0],
                1,
            )

    def test_empty_subtotal_uses_native_items_and_reports_the_confirmed_item(self):
        value = web_checkout("17.17")
        summary = value["components"]["order_summary_v2"]
        summary["subtotal"] = None
        item = summary["order_items"][0]
        item["price"]["amount"] = "13.50"
        item["pricing"]["final_price"]["amount"] = "13.50"
        self.client.request.return_value = {"checkout": value}
        message = self.inspect()
        self.assertIn("Fictional test item (item 123)", message)
        self.assertIn("item price £13.50; total £17.17", message)
        self.assertIn("No payment was submitted", message)
        self.client.request.assert_called_once()

    def test_invalid_checkout_links_are_rejected_before_connecting(self):
        for url in (
            "",
            None,
            "x" * 2049,
            self.url.replace("https:", "http:"),
            self.url.replace("www.vinted.co.uk", "foreign.example"),
            self.url.replace("www.vinted.co.uk", "owner@www.vinted.co.uk"),
            self.url.replace("/checkout?", "/other?"),
            self.url + "#payment",
            self.url + "&purchase_id=other",
            self.url + "&extra=private",
            self.url.replace("456", "١٢٣"),
            self.url.replace("transaction", "bundle"),
            self.url.replace("checkout-123", "../payment"),
            self.url.replace("&order_id=456", ""),
        ):
            with self.subTest(url_type=type(url).__name__), patch.object(
                buyer, "connected_client"
            ) as connect, self.assertRaises(buyer.BuyerError):
                buying.check_checkout(url)
            connect.assert_not_called()

    def test_submitted_checkouts_cannot_be_reinitialized(self):
        for state in ("paying", "unknown", "needs_action", "paid", "payment_failed"):
            with self.subTest(state=state):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute(
                        "UPDATE vinted_buy_attempts SET checkout_id='checkout-123',state=? WHERE item_id='123'",
                        (state,),
                    )
                before = buying.result("123")
                with patch.object(
                    buyer, "connected_client"
                ) as connect, self.assertRaises(buyer.BuyerError):
                    buying.check_checkout(self.url)
                connect.assert_not_called()
                self.client.request.assert_not_called()
                self.assertEqual(buying.result("123"), before)

    def test_unverifiable_checkout_identity_or_currency_stops_after_one_load(self):
        for value in (
            None,
            [],
            {"id": "another-checkout"},
            {"id": "checkout-123", "components": []},
            {"id": "checkout-123", "components": {"pay_button_v2": "invalid"}},
        ):
            with self.subTest(kind=type(value).__name__):
                self.client.reset_mock()
                self.client.request.return_value = {"checkout": value}
                with self.assertRaises(buyer.BuyerError):
                    self.inspect()
                self.client.request.assert_called_once()
                self.client.session.close.assert_called_once()
        value = web_checkout()
        value["components"]["pay_button_v2"]["total"]["price"]["currency_code"] = "EUR"
        self.client.reset_mock()
        self.client.request.return_value = {"checkout": value}
        with self.assertRaises(buyer.BuyerError):
            self.inspect()
        self.client.request.assert_called_once()

    def test_missing_saved_choices_report_the_total_without_private_details(self):
        value = web_checkout()
        value["components"]["payment_method"]["selected_payment_method"] = None
        value["private_address"] = "private-address-must-not-appear"
        self.client.request.return_value = {"checkout": value}
        with self.assertLogs("vinted_buying", level="INFO") as logs, self.assertRaises(
            buyer.BuyerError
        ) as error:
            self.inspect()
        self.assertIn("£18.84", str(error.exception))
        self.assertIn("payment method", str(error.exception))
        self.assertIn("No payment was submitted", str(error.exception))
        self.assertNotIn(
            "private-address", str(error.exception) + " ".join(logs.output)
        )
        self.assertNotIn("checkout-123", " ".join(logs.output))

    def test_price_layout_diagnostics_never_include_checkout_or_private_fields(self):
        value = web_checkout()
        c = value["components"]
        c["order_summary_v2"]["subtotal"] = None
        c["order_summary_v2"]["order_items"] = [
            {
                "id": "private-item-marker",
                "title": "private-title-marker",
                "price": {"amount": "15.00", "currency_code": "GBP"},
                "pricing": {
                    "final_price": {
                        "amount": "private-price-marker",
                        "currency_code": "private-currency-marker",
                    }
                },
            }
        ]
        c["single_item_presentation"] = {"payable_amount": "private-value-marker"}
        self.client.request.return_value = {"checkout": value}
        with self.assertLogs("vinted_buying", level="INFO") as logs, self.assertRaises(
            buyer.BuyerError
        ):
            self.inspect()
        output = " ".join(logs.output)
        self.assertIn("subtotal=absent total=GBP:1884", output)
        self.assertIn('"price":"GBP:1500","final":"unreadable"', output)
        self.assertNotIn("private-", output)
        self.assertNotIn("checkout-123", output)
        self.client.request.assert_called_once()

    def test_account_refusal_never_loads_or_pays_a_checkout(self):
        with patch.object(
            buyer,
            "connected_client",
            side_effect=buyer.BuyerError("Session expired", 401, reason="credentials"),
        ) as connect, self.assertRaises(buyer.BuyerError) as error:
            buying.check_checkout(self.url)
        connect.assert_called_once()
        self.client.request.assert_not_called()
        self.assertIn("checking your buyer account (HTTP 401)", str(error.exception))

    def test_checkout_refusals_do_not_retry_or_submit_payment(self):
        for status, reason in (
            (403, "security_challenge"),
            (404, "http_error"),
            (429, "rate_limited"),
        ):
            with self.subTest(status=status):
                self.client.reset_mock()
                self.client.request.side_effect = buyer.BuyerError(
                    "Vinted refused this request", status, reason=reason
                )
                with self.assertRaises(buyer.BuyerError) as error:
                    self.inspect()
                self.assertEqual(error.exception.status, status)
                self.assertEqual(error.exception.reason, reason)
                self.assertIn("loading the existing checkout", str(error.exception))
                self.client.request.assert_called_once()
                self.client.session.close.assert_called_once()


class SessionRotationFixture(DatabaseFixture):
    old_access = "old-access-token-0123456789"
    old_refresh = "old-refresh-token-0123456789"
    new_access = "rotated-access-token-0123456789"
    new_refresh = "rotated-refresh-token-0123456789"

    def setUp(self):
        super().setUp()
        seed = {
            "cookies": {
                "access_token_web": self.old_access,
                "refresh_token_web": self.old_refresh,
            },
            "csrf": "old-csrf-token-0123456789",
        }
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?,verified_at=1,user_id='99',username='owner',enabled=1 WHERE id=1",
                (buyer.encrypt(seed),),
            )
            conn.execute("INSERT INTO vinted_search_budgets VALUES (1,2000,350)")

    def response(self, data=None, *, status=200, text="", rotate=False):
        cookies = requests.cookies.RequestsCookieJar()
        if rotate:
            for name, value in (
                ("access_token_web", self.new_access),
                ("refresh_token_web", self.new_refresh),
            ):
                cookies.set(
                    name,
                    value,
                    domain=".www.vinted.co.uk",
                    path="/",
                    secure=True,
                    expires=4102444800,
                )
        return Mock(
            status_code=status,
            text=text,
            headers={},
            cookies=cookies,
            json=(
                Mock(return_value=data)
                if data is not None
                else Mock(side_effect=ValueError("HTML"))
            ),
        )

    def saved(self):
        with closing(search_settings.connection()) as conn:
            row = dict(conn.execute("SELECT * FROM vinted_buyer WHERE id=1").fetchone())
        return row, buyer.decrypt(row["session"])

    def connected(self):
        with patch.object(
            requests.Session,
            "request",
            return_value=self.response({"user": {"id": 99}}),
        ):
            client = buyer.connected_client()
        self.addCleanup(client.session.close)
        return client


class SessionRotationPersistenceTests(SessionRotationFixture, unittest.TestCase):
    def test_api_rotation_is_available_for_the_next_native_renewal(self):
        client = self.connected()
        with patch.object(
            client.session, "request", return_value=self.response({}, rotate=True)
        ):
            client.request("GET", "/api/v2/items/123")
        client.session.close()
        _, saved = self.saved()
        self.assertEqual(saved["cookies"]["refresh_token_web"], self.new_refresh)

        responses = iter(
            [
                self.response({"error": "invalid_token"}, status=401),
                self.response(
                    text='<meta name="csrf-token" content="fresh-csrf-token-0123456789">'
                ),
                self.response({}, rotate=True),
                self.response({"user": {"id": 99}}),
            ]
        )
        sent_refresh = []

        def native(session, method, url, **kwargs):
            if url.endswith("/web/api/auth/refresh"):
                prepared = session.prepare_request(requests.Request(method, url))
                sent_refresh.append(prepared.headers.get("Cookie", ""))
            return next(responses)

        with patch.object(
            requests.Session, "request", autospec=True, side_effect=native
        ) as wire:
            restored = buyer.connected_client()
        self.addCleanup(restored.session.close)
        self.assertEqual(wire.call_count, 4)
        self.assertEqual(len(sent_refresh), 1)
        self.assertIn(self.new_refresh, sent_refresh[0])
        self.assertNotIn(self.old_refresh, sent_refresh[0])
        self.assertTrue(buyer.settings()["enabled"])
        self.assertNotIn(self.new_refresh.encode(), Path(db.DB_PATH).read_bytes())

    def test_listing_rotation_survives_failed_item_parsing(self):
        client = self.connected()
        response = self.response(
            text='<meta name="csrf-token" content="page-csrf-token-0123456789">',
            rotate=True,
        )
        with patch.object(
            client.session, "get", return_value=response
        ) as get, self.assertRaises(buyer.BuyerError):
            client.listing_page(buyer.BASE + "/items/123", "123")
        get.assert_called_once()
        row, saved = self.saved()
        self.assertEqual(saved["cookies"]["access_token_web"], self.new_access)
        self.assertEqual(saved["cookies"]["refresh_token_web"], self.new_refresh)
        self.assertEqual(saved["csrf"], "page-csrf-token-0123456789")
        self.assertEqual((row["user_id"], row["enabled"]), ("99", 1))
        with closing(search_settings.connection()) as conn:
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

    def test_successful_payment_response_rotation_preserves_the_pending_result(self):
        client = self.connected()
        with patch.object(
            client.session,
            "request",
            return_value=self.response({"payment": {"status": "pending"}}, rotate=True),
        ) as wire:
            data = client.request(
                "POST",
                "/api/v2/purchases/checkout-123/checkout/payment",
                {"checksum": "fictional"},
            )
        wire.assert_called_once()
        self.assertEqual(data["payment"]["status"], "pending")
        _, saved = self.saved()
        self.assertEqual(saved["cookies"]["refresh_token_web"], self.new_refresh)

    def test_old_client_cannot_overwrite_a_replaced_or_disconnected_session(self):
        for changed_user in ("99", "100", None):
            with self.subTest(changed_user=changed_user):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute(
                        "UPDATE vinted_buyer SET user_id='99',verified_at=1,session=? WHERE id=1",
                        (
                            buyer.encrypt(
                                {"cookies": {"access_token_web": self.old_access}}
                            ),
                        ),
                    )
                client = self.connected()
                replacement = (
                    buyer.encrypt(
                        {
                            "cookies": {
                                "access_token_web": "replacement-access-token-0123456789"
                            }
                        }
                    )
                    if changed_user
                    else None
                )
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute(
                        "UPDATE vinted_buyer SET session=?,user_id=? WHERE id=1",
                        (replacement, changed_user),
                    )
                with patch.object(
                    client.session,
                    "request",
                    return_value=self.response({}, rotate=True),
                ) as wire, self.assertRaises(buyer.BuyerError):
                    client.request("GET", "/api/v2/items/123")
                wire.assert_called_once()
                row, _ = self.saved()
                self.assertEqual(row["session"], replacement)
                self.assertEqual(row["user_id"], changed_user)

    def test_refused_responses_never_persist_their_cookies_or_retry(self):
        client = self.connected()
        before, _ = self.saved()
        for response in (
            self.response({}, status=403, rotate=True),
            self.response({"error": "invalid_token"}, rotate=True),
            self.response(text="<html>Verify you are human</html>", rotate=True),
        ):
            with patch.object(
                client.session, "request", return_value=response
            ) as wire, self.assertRaises(buyer.BuyerError):
                client.request("GET", "/api/v2/items/123")
            wire.assert_called_once()
            after, _ = self.saved()
            self.assertEqual(after["session"], before["session"])

    def test_unchanged_and_unverified_clients_do_not_write_the_saved_session(self):
        client = self.connected()
        before, _ = self.saved()
        with patch.object(client.session, "request", return_value=self.response({})):
            client.request("GET", "/api/v2/items/123")
        self.assertEqual(self.saved()[0]["session"], before["session"])
        anonymous = buyer.Client()
        self.addCleanup(anonymous.session.close)
        with patch.object(
            anonymous.session, "request", return_value=self.response({}, rotate=True)
        ):
            anonymous.request("GET", "/api/v2/items/123")
        self.assertEqual(self.saved()[0]["session"], before["session"])


class NativeCookieTransportTests(SessionRotationFixture, unittest.TestCase):
    """Keep Session.request/send and automatic cookie extraction real."""

    def setUp(self):
        # These tests exercise Requests' native Set-Cookie handling through its
        # mocked adapter. The browser SDK transport is tested separately.
        native_session = patch.object(buyer, "BrowserSession", requests.Session)
        native_session.start()
        self.addCleanup(native_session.stop)
        super().setUp()

    def transport_response(
        self, prepared, data=None, *, status=200, text="", cookies=()
    ):
        headers = Message()
        for value in cookies:
            headers.add_header("Set-Cookie", value)
        response = requests.Response()
        response.status_code = status
        response.request = prepared
        response.url = prepared.url
        response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=headers))
        response._content = (
            json.dumps(data).encode() if data is not None else text.encode()
        )
        response._content_consumed = True
        requests.cookies.extract_cookies_to_jar(
            response.cookies, prepared, response.raw
        )
        return response

    def token_headers(self, access, refresh):
        return tuple(
            f"{name}={value}; Domain=www.vinted.co.uk; Path=/; Secure; HttpOnly"
            for name, value in (
                ("access_token_web", access),
                ("refresh_token_web", refresh),
            )
        )

    def test_rejected_identity_cookies_cannot_poison_the_normal_renewal(self):
        calls = []
        rejected_access = "rejected-access-token-0123456789"
        rejected_refresh = "rejected-refresh-token-0123456789"

        def native(adapter, prepared, **kwargs):
            path = prepared.url.removeprefix(buyer.BASE)
            calls.append((prepared.method, path))
            if len(calls) == 1:
                return self.transport_response(
                    prepared,
                    {"code": "unauthorized", "message": "Unauthorized"},
                    status=401,
                    cookies=self.token_headers(rejected_access, rejected_refresh),
                )
            if path == "/":
                self.assertNotIn("access_token_web", prepared.headers.get("Cookie", ""))
                return self.transport_response(
                    prepared,
                    text='<meta name="csrf-token" content="fresh-csrf-token-0123456789">',
                    cookies=(
                        "anon_id=public-anonymous-id; Domain=www.vinted.co.uk; Path=/; Secure",
                    ),
                )
            if path == "/web/api/auth/refresh":
                cookie = prepared.headers.get("Cookie", "")
                self.assertEqual(cookie.count("refresh_token_web="), 1)
                self.assertIn(self.old_refresh, cookie)
                self.assertNotIn(rejected_refresh, cookie)
                self.assertNotIn(rejected_access, cookie)
                self.assertNotIn("public-anonymous-id", cookie)
                self.assertIsNone(prepared.body)
                self.assertNotIn("Content-Type", prepared.headers)
                self.assertNotIn("Authorization", prepared.headers)
                return self.transport_response(
                    prepared,
                    {
                        "access_token": self.new_access,
                        "refresh_token": self.new_refresh,
                    },
                    cookies=self.token_headers(self.new_access, self.new_refresh),
                )
            self.assertEqual(path, "/api/v2/users/current")
            self.assertIn(self.new_access, prepared.headers["Cookie"])
            return self.transport_response(prepared, {"user": {"id": 99}})

        with patch.object(
            requests.adapters.HTTPAdapter, "send", autospec=True, side_effect=native
        ), self.assertLogs("vinted_buyer", level="INFO") as logs:
            self.assertIn("No checkout or payment", buyer.check_saved_connection())
        self.assertEqual(
            calls,
            [
                ("GET", "/api/v2/users/current"),
                ("GET", "/"),
                ("POST", "/web/api/auth/refresh"),
                ("GET", "/api/v2/users/current"),
            ],
        )
        row, saved = self.saved()
        self.assertEqual(saved["cookies"]["refresh_token_web"], self.new_refresh)
        self.assertEqual((row["user_id"], row["enabled"]), ("99", 1))
        self.assertIn(
            "access_changed=True refresh_changed=True restored=True",
            "\n".join(logs.output),
        )
        for value in (
            self.old_access,
            self.old_refresh,
            self.new_access,
            self.new_refresh,
            rejected_access,
            rejected_refresh,
        ):
            self.assertNotIn(value, "\n".join(logs.output))

    def test_rejected_api_and_page_cookies_are_rolled_back_after_real_auto_merge(self):
        cases = (
            ("api", 403, {"message": "Forbidden"}, ""),
            ("api", 200, {"error": "invalid_token"}, ""),
            ("api", 200, None, "<html>Verify you are human</html>"),
            ("homepage", 403, None, "Forbidden"),
            ("listing", 404, None, "Not found"),
        )
        for kind, status, data, text in cases:
            with self.subTest(kind=kind, status=status):
                client = self.connected()
                before = client.exported()
                sealed = self.saved()[0]["session"]

                def native(
                    adapter, prepared, *, data=data, status=status, text=text, **kwargs
                ):
                    return self.transport_response(
                        prepared,
                        data,
                        status=status,
                        text=text,
                        cookies=self.token_headers(self.new_access, self.new_refresh),
                    )

                with patch.object(
                    requests.adapters.HTTPAdapter,
                    "send",
                    autospec=True,
                    side_effect=native,
                ) as send, self.assertRaises(buyer.BuyerError):
                    if kind == "api":
                        client.request("GET", "/api/v2/items/123")
                    elif kind == "homepage":
                        client.homepage()
                    else:
                        client.listing_page(buyer.BASE + "/items/123", "123")
                send.assert_called_once()
                self.assertEqual(client.exported(), before)
                self.assertEqual(self.saved()[0]["session"], sealed)

    def test_explicit_two_step_login_keeps_its_accepted_challenge_cookie(self):
        client = buyer.Client()
        self.addCleanup(client.session.close)

        def native(adapter, prepared, **kwargs):
            return self.transport_response(
                prepared,
                {"payload": {"id": 123}},
                status=401,
                cookies=(
                    "anon_id=two-step-login-context; Domain=www.vinted.co.uk; Path=/; Secure",
                ),
            )

        with patch.object(
            requests.adapters.HTTPAdapter, "send", autospec=True, side_effect=native
        ) as send:
            self.assertEqual(
                client.request("POST", "/web/api/auth/oauth", {}, allow_challenge=True),
                {"challenge_id": "123"},
            )
        send.assert_called_once()
        self.assertEqual(
            client.exported()["cookies"]["anon_id"], "two-step-login-context"
        )

    def test_two_native_renewals_survive_encrypted_restoration_over_a_simulated_hour(
        self,
    ):
        clock = [2000000000]
        access, refresh = self.old_access, self.old_refresh
        valid_until = clock[0] + 1800
        renewals = []
        paths = []

        def native(adapter, prepared, **kwargs):
            nonlocal access, refresh, valid_until
            path = prepared.url.removeprefix(buyer.BASE)
            paths.append(path)
            cookie = prepared.headers.get("Cookie", "")
            if path == "/":
                return self.transport_response(
                    prepared,
                    text='<meta name="csrf-token" content="fresh-csrf-token-0123456789">',
                )
            if path == "/api/v2/users/current":
                self.assertIn("access_token_web=" + access, cookie)
                self.assertEqual(cookie.count("access_token_web="), 1)
                if clock[0] >= valid_until:
                    return self.transport_response(
                        prepared, {"code": "unauthorized"}, status=401
                    )
                return self.transport_response(prepared, {"user": {"id": 99}})
            self.assertEqual(path, "/web/api/auth/refresh")
            self.assertEqual(prepared.method, "POST")
            self.assertIsNone(prepared.body)
            self.assertNotIn("Content-Type", prepared.headers)
            self.assertEqual(cookie.count("refresh_token_web="), 1)
            self.assertIn("refresh_token_web=" + refresh, cookie)
            self.assertNotIn("Authorization", prepared.headers)
            renewals.append(refresh)
            access = f"rotated-access-cycle-{len(renewals)}-0123456789"
            refresh = f"rotated-refresh-cycle-{len(renewals)}-0123456789"
            valid_until = clock[0] + 1800
            return self.transport_response(
                prepared,
                {"access_token": access, "refresh_token": refresh},
                cookies=self.token_headers(access, refresh),
            )

        with patch.object(
            buyer.time, "time", side_effect=lambda: clock[0]
        ), patch.object(
            requests.adapters.HTTPAdapter, "send", autospec=True, side_effect=native
        ):
            for elapsed in (0, 1900, 3900):
                clock[0] = 2000000000 + elapsed
                self.assertIn("No checkout or payment", buyer.check_saved_connection())
                self.assertEqual(
                    self.saved()[1]["cookies"]["refresh_token_web"], refresh
                )
        self.assertEqual(
            renewals, [self.old_refresh, "rotated-refresh-cycle-1-0123456789"]
        )
        self.assertEqual(paths.count("/web/api/auth/refresh"), 2)
        self.assertEqual(len(paths), 9)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM vinted_buy_attempts").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT max_total FROM vinted_search_budgets WHERE query_id=1"
                ).fetchone()[0],
                2000,
            )
        self.assertTrue(buyer.settings()["enabled"])


class SessionLinkTests(DatabaseFixture, unittest.TestCase):
    access = "test-access-token-0123456789"
    refresh = "test-refresh-token-0123456789"

    def homepage(self):
        return Mock(
            status_code=200,
            text='{"CSRF_TOKEN":"test-csrf-0123456789"}',
        )

    def test_existing_session_is_verified_without_login_or_payment(self):
        identity = Mock(
            status_code=200,
            text="",
            json=Mock(return_value={"user": {"id": 99, "login": "owner"}}),
        )
        with patch.object(
            requests.Session, "request", side_effect=[self.homepage(), identity]
        ) as request, patch.object(requests.Session, "close") as close:
            message = buyer.link_session(self.access, self.refresh)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            [(c.args[0], c.args[1]) for c in request.call_args_list],
            [
                ("GET", buyer.BASE + "/"),
                ("GET", buyer.BASE + "/api/v2/users/current"),
            ],
        )
        for call in request.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])
        close.assert_called_once()
        self.assertIn("Autobuy is off", message)
        settings = buyer.settings()
        self.assertTrue(settings["connected"])
        self.assertEqual(settings["username"], "owner")
        self.assertFalse(settings["enabled"])
        with closing(search_settings.connection()) as conn:
            saved = conn.execute("SELECT session FROM vinted_buyer").fetchone()[0]
        cookies = buyer.decrypt(saved)["cookies"]
        self.assertEqual(cookies["access_token_web"], self.access)
        self.assertEqual(cookies["refresh_token_web"], self.refresh)
        for token in (self.access, self.refresh):
            self.assertNotIn(token, json.dumps(settings))
            self.assertNotIn(token.encode(), Path(db.DB_PATH).read_bytes())
        with patch.object(requests.Session, "request") as request, self.assertRaises(
            buyer.BuyerError
        ):
            buyer.start_login("owner@example.test", "private-password")
        request.assert_not_called()

    def test_session_input_rejects_cookie_headers_and_control_characters(self):
        for value in (
            "",
            "short",
            "a" * 8193,
            "access_token_web=" + self.access + "; datadome=not-accepted",
            self.access + "\r\nInjected: header",
            "<script>not-a-token</script>",
        ):
            with self.subTest(value_length=len(value)), patch.object(
                requests.Session, "request"
            ) as request, self.assertRaises(buyer.BuyerError) as error:
                buyer.link_session(value, self.refresh)
            request.assert_not_called()
            self.assertNotIn(self.access, str(error.exception))
        self.assertFalse(buyer.settings()["connected"])

    def test_refused_session_stops_without_refresh_retry_or_saving_tokens(self):
        for status, data, reason in (
            (
                403,
                {"url": "https://geo.captcha-delivery.com/captcha/"},
                "security_challenge",
            ),
            (401, {"error": "invalid_token"}, "credentials"),
            (429, {}, "rate_limited"),
            (200, {"user": None}, "not_confirmed"),
            (200, {"user": ["invalid"]}, "not_confirmed"),
        ):
            with self.subTest(status=status, reason=reason):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("UPDATE vinted_buyer SET login_attempt=0")
                identity = Mock(
                    status_code=status, text="", json=Mock(return_value=data)
                )
                with patch.object(
                    requests.Session,
                    "request",
                    side_effect=[self.homepage(), identity],
                ) as request, self.assertLogs(
                    "vinted_buyer", level="INFO"
                ) as logs, self.assertRaises(
                    buyer.BuyerError
                ) as error:
                    buyer.link_session(self.access, self.refresh)
                self.assertEqual(error.exception.reason, reason)
                self.assertEqual(request.call_count, 2)
                self.assertFalse(buyer.settings()["connected"])
                self.assertFalse(buyer.settings()["enabled"])
                for token in (self.access, self.refresh):
                    self.assertNotIn(token, " ".join(logs.output))
                    self.assertNotIn(token.encode(), Path(db.DB_PATH).read_bytes())

    def test_homepage_challenge_stops_before_account_request(self):
        response = Mock(status_code=403, text="<html>Verify you are human</html>")
        with patch.object(
            requests.Session, "request", return_value=response
        ) as request, self.assertRaises(buyer.BuyerError) as error:
            buyer.link_session(self.access, self.refresh)
        self.assertEqual(error.exception.reason, "security_challenge")
        request.assert_called_once()
        self.assertFalse(buyer.settings()["connected"])


class BuyerDashboardTests(DatabaseFixture, unittest.TestCase):
    owner = test_dashboard.DashboardTests.owner

    def setUp(self):
        super().setUp()
        from web_ui_plugin.web_ui import create_app

        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()

    def test_readonly_listing_check_requires_owner_and_csrf(self):
        data = {
            "csrf": "offline-csrf",
            "action": "buyer_listing_check",
            "buyer_item_id": "123",
        }
        with patch.object(buying, "check_listing") as check:
            self.assertEqual(
                self.client.post("/connections", data=data).status_code, 400
            )
            check.assert_not_called()
        self.owner()
        with patch.object(buying, "check_listing") as check:
            self.assertEqual(
                self.client.post(
                    "/connections", data=dict(data, csrf="invalid")
                ).status_code,
                400,
            )
            check.assert_not_called()
        with patch.object(
            buying, "check_listing", return_value="No checkout or payment was created."
        ) as check:
            response = self.client.post("/connections", data=data)
        check.assert_called_once_with("123")
        self.assertEqual(response.status_code, 302)
        response = self.client.get(response.headers["Location"])
        self.assertIn(b"No checkout or payment was created", response.data)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_existing_checkout_check_requires_owner_and_csrf(self):
        data = {
            "csrf": "offline-csrf",
            "action": "buyer_checkout_check",
            "buyer_checkout_url": CheckoutInspectionTests.url,
        }
        with patch.object(buying, "check_checkout") as check:
            self.assertEqual(
                self.client.post("/connections", data=data).status_code, 400
            )
            check.assert_not_called()
        self.owner()
        with patch.object(buying, "check_checkout") as check:
            self.assertEqual(
                self.client.post(
                    "/connections", data=dict(data, csrf="invalid")
                ).status_code,
                400,
            )
            check.assert_not_called()
        with patch.object(
            buying,
            "check_checkout",
            return_value="Checkout total £18.84. No payment was submitted.",
        ) as check:
            response = self.client.post("/connections", data=data)
        check.assert_called_once_with(CheckoutInspectionTests.url)
        self.assertEqual(response.status_code, 302)
        response = self.client.get(response.headers["Location"])
        self.assertIn(b"No payment was submitted", response.data)
        self.assertNotIn(b"purchase_id=checkout-123", response.data)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_checkout_check_failure_redirects_and_never_repeats_on_refresh(self):
        self.owner()
        with patch.object(
            buying,
            "check_checkout",
            side_effect=buyer.BuyerError("Checkout stopped (HTTP 404)"),
        ) as check:
            response = self.client.post(
                "/connections",
                data={
                    "csrf": "offline-csrf",
                    "action": "buyer_checkout_check",
                    "buyer_checkout_url": CheckoutInspectionTests.url,
                },
            )
            self.assertEqual(response.status_code, 303)
            response = self.client.get(response.headers["Location"])
            response = self.client.get("/connections")
        check.assert_called_once()
        self.assertNotIn(b"purchase_id=checkout-123", response.data)

    def test_readonly_listing_check_errors_redirect_without_retrying(self):
        self.owner()
        with patch.object(
            buying,
            "check_listing",
            side_effect=buyer.BuyerError("Listing check stopped (HTTP 404)"),
        ) as check:
            response = self.client.post(
                "/connections",
                data={
                    "csrf": "offline-csrf",
                    "action": "buyer_listing_check",
                    "buyer_item_id": "123",
                },
            )
            self.assertEqual(response.status_code, 303)
            response = self.client.get(response.headers["Location"])
            response = self.client.get("/connections")
        check.assert_called_once_with("123")
        self.assertNotIn(b"access_token_web=", response.data)

    def test_buyer_login_requires_owner_and_csrf_and_never_echoes_password(self):
        self.assertEqual(
            self.client.post(
                "/connections", data={"action": "buyer_login"}
            ).status_code,
            400,
        )
        self.owner()
        with patch.object(
            buyer,
            "start_login",
            side_effect=buyer.BuyerError("Vinted requires verification"),
        ) as login:
            response = self.client.post(
                "/connections",
                data={
                    "csrf": "offline-csrf",
                    "action": "buyer_login",
                    "buyer_email": "owner@example.test",
                    "buyer_password": "private-password",
                },
            )
        login.assert_called_once_with("owner@example.test", "private-password")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/connections")
        response = self.client.get("/connections")
        self.assertNotIn(b"private-password", response.data)
        self.assertIn(b"Vinted requires verification", response.data)

    def test_password_form_accepts_username_and_keeps_credentials_private(self):
        self.owner()
        response = self.client.get("/connections")
        self.assertIn(b"Vinted username or email", response.data)
        self.assertIn(
            b'name="buyer_email" type="text" autocomplete="username"', response.data
        )
        with patch.object(
            buyer, "start_login", return_value="Buyer connected"
        ) as login:
            response = self.client.post(
                "/connections",
                data={
                    "csrf": "offline-csrf",
                    "action": "buyer_login",
                    "buyer_email": "offline_buyer",
                    "buyer_password": "private-password",
                },
            )
        login.assert_called_once_with("offline_buyer", "private-password")
        response = self.client.get(response.headers["Location"])
        self.assertNotIn(b"private-password", response.data)

    def test_password_block_remains_visible_after_old_session_renewal_fails(self):
        self.owner()
        with patch.object(
            buyer,
            "start_login",
            side_effect=buyer.BuyerError(
                buyer.AUTH_REASONS["security_challenge"],
                403,
                reason="security_challenge",
                stage="sign_in",
            ),
        ) as login:
            response = self.client.post(
                "/connections",
                data={
                    "csrf": "offline-csrf",
                    "action": "buyer_login",
                    "buyer_email": "owner@example.test",
                    "buyer_password": "private-password",
                },
            )
        self.assertEqual(response.status_code, 303)
        login.assert_called_once()
        # A separate saved-account check must not overwrite this owner attempt.
        buyer.record_auth("renewal_failed", "renewal", 400)
        response = self.client.get("/connections")
        self.assertIn(b"Last account connection attempt", response.data)
        self.assertIn(
            b"This does not establish that your password is wrong", response.data
        )
        self.assertIn(b"HTTP 403", response.data)
        self.assertIn(b"Saved account check", response.data)
        self.assertIn(b"HTTP 400", response.data)
        self.assertEqual(
            buyer.public_connection_result()["reason"], "security_challenge"
        )
        self.assertFalse(buyer.settings()["enabled"])
        with closing(search_settings.connection()) as conn:
            saved = conn.execute(
                "SELECT value FROM parameters WHERE key='buyer_connection_result'"
            ).fetchone()[0]
            self.assertEqual(
                set(json.loads(saved)),
                {"kind", "reason", "stage", "http_status", "checked"},
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0], 44
            )
        for secret in ("owner@example.test", "private-password"):
            self.assertNotIn(secret.encode(), response.data)
            self.assertNotIn(secret, saved)

    def test_successful_password_connection_replaces_previous_block(self):
        self.owner()
        buyer.record_connection_result(
            "buyer_login", "security_challenge", "sign_in", 403
        )
        with patch.object(buyer, "Client") as client:
            client.return_value.request.return_value = {}
            client.return_value.identity.return_value = ("99", "owner")
            client.return_value.exported.return_value = {
                "cookies": {"access_token_web": "offline-private-token"}
            }
            response = self.client.post(
                "/connections",
                data={
                    "csrf": "offline-csrf",
                    "action": "buyer_login",
                    "buyer_email": "owner@example.test",
                    "buyer_password": "private-password",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(buyer.public_connection_result()["reason"], "connected")
        self.assertTrue(buyer.settings()["connected"])
        self.assertFalse(buyer.settings()["enabled"])
        response = self.client.get("/connections")
        self.assertNotIn(b"Vinted blocked this sign-in", response.data)
        self.assertNotIn(b"private-password", response.data)
        self.assertNotIn(b"offline-private-token", response.data)

    def test_connection_result_whitelists_diagnostics_without_secret_values(self):
        buyer.record_connection_result(
            "buyer_login", "private-password", "private-token", True
        )
        result = buyer.public_connection_result()
        self.assertEqual(result["reason"], "not_confirmed")
        self.assertEqual(result["stage"], "Vinted request")
        self.assertIsNone(result["http_status"])
        self.assertNotIn("private", json.dumps(result))
        with patch.object(buyer, "ZoneInfo", side_effect=buyer.ZoneInfoNotFoundError):
            self.assertIn("UTC", buyer.public_connection_result()["checked"])
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE parameters SET value=? WHERE key='buyer_connection_result'",
                ('{"kind":"private-password"}',),
            )
        self.assertIsNone(buyer.public_connection_result())

    def test_session_link_is_owner_csrf_protected_and_never_repopulates_secrets(self):
        data = {
            "csrf": "offline-csrf",
            "action": "buyer_session",
            "buyer_access_token": "access-secret-never-show",
            "buyer_refresh_token": "refresh-secret-never-show",
        }
        with patch.object(buyer, "link_session") as link:
            self.assertEqual(
                self.client.post("/connections", data=data).status_code, 400
            )
            link.assert_not_called()
        self.owner()
        with patch.object(buyer, "link_session") as link:
            invalid = dict(data, csrf="invalid")
            self.assertEqual(
                self.client.post("/connections", data=invalid).status_code, 400
            )
            link.assert_not_called()
        with patch.object(
            buyer, "link_session", side_effect=buyer.BuyerError("Connection refused")
        ) as link:
            response = self.client.post("/connections", data=data)
        link.assert_called_once_with(
            data["buyer_access_token"], data["buyer_refresh_token"]
        )
        self.assertEqual(response.status_code, 303)
        response = self.client.get(response.headers["Location"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn(b"Connection refused", response.data)
        self.assertIn(b"Verify and link existing session", response.data)
        for field in ("buyer_access_token", "buyer_refresh_token"):
            self.assertNotIn(data[field].encode(), response.data)
