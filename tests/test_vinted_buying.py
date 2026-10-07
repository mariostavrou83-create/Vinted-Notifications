"""No live transactions: buyer auth, price gates and uncertain-payment recovery."""

import copy
import json
import stat
import unittest
from contextlib import closing
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


class BuyingTests(DatabaseFixture, unittest.TestCase):
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

    def test_exact_search_budget_is_allowed_regardless_of_old_global_caps(self):
        self.final = checkout("20.00")
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET max_total=100,max_extra=0")
        self.assertEqual(self.run_buy()["state"], "paid")
        self.assertEqual(len(self.payments()), 1)

    def test_missing_budget_or_inactive_search_cannot_prepare_checkout(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DELETE FROM vinted_search_budgets")
        with self.assertRaisesRegex(buyer.BuyerError, "maximum total"):
            self.run_buy()
        self.client.request.assert_not_called()
        self.row["query_id"] = None
        with self.assertRaisesRegex(buyer.BuyerError, "no longer active"):
            self.run_buy()
        self.client.request.assert_not_called()

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
            self.assertEqual(client.session.headers["Authorization"], "Bearer " + old)
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
                self.assertEqual(
                    client.session.headers["Authorization"], "Bearer " + new
                )
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
        self.assertEqual(client.exported()["cookies"]["access_token_web"], body)
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
        self.assertEqual(
            client.session.headers["Authorization"], "Bearer " + new_access
        )
        prepared = client.session.prepare_request(
            requests.Request("GET", buyer.BASE + "/api/v2/users/current")
        )
        self.assertNotIn(old, prepared.headers["Cookie"])
        self.assertEqual(prepared.headers["Cookie"].count("access_token_web="), 1)
        client.session.close()

    def test_json_refresh_tokens_are_validated_before_header_update(self):
        old = "old-test-token-0123456789"
        client = buyer.Client({"cookies": {"access_token_web": old}})
        for invalid in (
            "short",
            "bad\r\nheader-value",
            {"secret": "invalid"},
            "x" * 8193,
        ):
            client.update_tokens(Mock(), {"access_token": invalid})
            self.assertEqual(client.session.headers["Authorization"], "Bearer " + old)
        new = "new-test-token-0123456789"
        client.update_tokens(Mock(), {"access_token": new})
        self.assertEqual(client.session.headers["Authorization"], "Bearer " + new)
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
        self.assertEqual(
            client.request.call_args.args[2],
            {"refresh_token": old_session["cookies"]["refresh_token_web"]},
        )
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
                    if reason == "session_refresh":
                        client.request.assert_called_once_with(
                            "POST",
                            "/web/api/auth/refresh",
                            {"refresh_token": "refresh"},
                        )
                    else:
                        client.request.assert_called_once_with(
                            "POST",
                            "/web/api/auth/oauth",
                            {
                                "client_id": "web",
                                "scope": "user",
                                "grant_type": "refresh_token",
                                "refresh_token": "refresh",
                            },
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
                        ("POST", buyer.BASE + "/web/api/auth/refresh"),
                        ("GET", buyer.BASE + "/api/v2/users/current"),
                    ],
                )
                self.assertEqual(
                    request.call_args_list[1].kwargs["json"],
                    {"refresh_token": old["cookies"]["refresh_token_web"]},
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
                self.assertEqual(buyer.decrypt(saved)["csrf"], old["csrf"])
                self.assertTrue(buyer.settings()["enabled"])
                self.assertEqual(buyer.settings()["user_id"], "99")
                for value in (
                    *old["cookies"].values(),
                    *new.values(),
                    "never-log-this",
                ):
                    self.assertNotIn(value, " ".join(logs.output))
                    self.assertNotIn(value.encode(), Path(db.DB_PATH).read_bytes())

    def test_oauth_renewal_uses_saved_csrf_and_rejects_unverified_identity(self):
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

        def response(status, data):
            return Mock(
                status_code=status,
                text="",
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
                    response(200, new),
                    response(status, payload),
                ]
                with patch.object(
                    requests.Session, "request", side_effect=replies
                ) as request, patch.object(requests.Session, "close") as close:
                    if accepted:
                        client = buyer.connected_client()
                        self.assertEqual(client.csrf, old["csrf"])
                        client.session.close()
                    else:
                        with self.assertRaises(buyer.BuyerError):
                            buyer.connected_client()
                self.assertEqual(
                    [(call.args[0], call.args[1]) for call in request.call_args_list],
                    [
                        ("GET", buyer.BASE + "/api/v2/users/current"),
                        ("POST", buyer.BASE + "/web/api/auth/oauth"),
                        ("GET", buyer.BASE + "/api/v2/users/current"),
                    ],
                )
                self.assertEqual(
                    request.call_args_list[1].kwargs["json"],
                    {
                        "client_id": "web",
                        "scope": "user",
                        "grant_type": "refresh_token",
                        "refresh_token": old["cookies"]["refresh_token_web"],
                    },
                )
                self.assertTrue(
                    all(
                        not call.kwargs["allow_redirects"]
                        for call in request.call_args_list
                    )
                )
                close.assert_called_once()
                with closing(search_settings.connection()) as conn:
                    saved = conn.execute("SELECT session FROM vinted_buyer").fetchone()[
                        0
                    ]
                self.assertEqual(buyer.decrypt(saved)["csrf"], old["csrf"])
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
