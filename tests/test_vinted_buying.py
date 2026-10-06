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
