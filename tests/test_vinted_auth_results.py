"""Offline evidence separation; no login, solver or payment is sent."""

import json
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape
from test_search_controls import DatabaseFixture

import db
import search_settings
import vinted_buyer as buyer


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def event(kind, checked, reason="connected", stage="identity", status=200):
    return {
        "kind": kind,
        "reason": reason,
        "stage": stage,
        "http_status": status,
        "checked": checked,
    }


class AuthResultTests(DatabaseFixture, unittest.TestCase):
    def render(self):
        templates = Path(__file__).resolve().parents[1] / "web_ui_plugin/templates"
        template = Environment(
            loader=FileSystemLoader(templates), autoescape=select_autoescape(["html"])
        ).get_template("msj_buyer.html")
        return template.render(
            buyer=buyer.settings(),
            buyer_auth_results=buyer.public_auth_results(),
            buyer_connection_result=buyer.public_connection_result(),
            csrf="offline-csrf",
        )

    def test_password_block_and_native_renewal_remain_distinct_after_fresh_session_link(
        self,
    ):
        sign_in = timestamp("2026-10-09T15:37:52.995Z")
        renewal = timestamp("2026-10-09T15:38:31.953Z")
        fresh_link = timestamp("2026-10-09T16:05:01Z")
        with patch.object(buyer.time, "time", return_value=sign_in):
            buyer.record_connection_result(
                "buyer_login", "security_challenge", "sign_in", 403
            )
        with patch.object(buyer.time, "time", return_value=renewal):
            buyer.record_auth("renewal_failed", "renewal", 400)
        with patch.object(buyer.time, "time", return_value=fresh_link):
            buyer.record_connection_result(
                "buyer_session", "connected", "identity", 200
            )
            buyer.record_auth("connected", "identity", 200)
        results = buyer.public_auth_results()
        self.assertEqual(results["buyer_login"]["checked_at"], sign_in)
        self.assertEqual(results["buyer_login"]["http_status"], 403)
        self.assertEqual(results["buyer_login"]["stage_code"], "sign_in")
        self.assertEqual(results["renewal"]["checked_at"], renewal)
        self.assertEqual(results["renewal"]["http_status"], 400)
        self.assertEqual(results["renewal"]["stage_code"], "renewal")
        self.assertEqual(results["buyer_session"]["checked_at"], fresh_link)
        self.assertEqual(results["saved_check"]["checked_at"], fresh_link)
        self.assertEqual(buyer.public_connection_result()["kind_code"], "buyer_session")
        html = self.render()
        for text in (
            "16:37:52 BST",
            "16:38:31 BST",
            "17:05:01 BST",
            "HTTP 403",
            "HTTP 400",
        ):
            self.assertIn(text, html)
        self.assertIn(
            "A CapSolver solution does not establish that Vinted accepted login", html
        )
        self.assertIn('data-buyer-auth-result="buyer_login"', html)
        self.assertIn('data-buyer-auth-result="renewal"', html)
        self.assertFalse(buyer.settings()["enabled"])

    def test_older_import_and_late_result_cannot_replace_newer_kind_or_legacy(self):
        with patch.object(buyer.time, "time", return_value=200):
            buyer.record_connection_result("buyer_login", "connected", "identity", 200)
        with patch.object(buyer.time, "time", return_value=100):
            buyer.record_connection_result(
                "buyer_login", "security_challenge", "sign_in", 403
            )
        payload = [event("buyer_login", 150, "security_challenge", "sign_in", 403)]
        with patch.dict(
            buyer.os.environ,
            {"MSJ_BUYER_AUTH_RESULTS_IMPORT_ON_START": json.dumps(payload)},
        ):
            self.assertTrue(buyer.import_auth_results_once())
        self.assertEqual(
            buyer.public_auth_results()["buyer_login"]["reason"], "connected"
        )
        self.assertEqual(buyer.public_connection_result()["checked_at"], 200)

    def test_import_consumes_metadata_once_without_auth_calls_or_other_changes(self):
        payload = [
            event("buyer_login", 100, "security_challenge", "sign_in", 403),
            event("renewal", 101, "renewal_failed", "renewal", 400),
        ]
        with closing(search_settings.connection()) as conn:
            before = tuple(conn.execute("SELECT * FROM vinted_buyer").fetchone())
            access_before = tuple(
                conn.execute("SELECT * FROM vinted_buyer_access").fetchone()
            )
        with patch.object(
            buyer, "Client", side_effect=AssertionError("No auth calls")
        ) as client, patch.dict(
            buyer.os.environ,
            {"MSJ_BUYER_AUTH_RESULTS_IMPORT_ON_START": json.dumps(payload)},
        ):
            self.assertTrue(buyer.import_auth_results_once())
            self.assertFalse(buyer.import_auth_results_once())
            client.assert_not_called()
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                tuple(conn.execute("SELECT * FROM vinted_buyer").fetchone()), before
            )
            self.assertEqual(
                tuple(conn.execute("SELECT * FROM vinted_buyer_access").fetchone()),
                access_before,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0], 44
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM parameters WHERE key LIKE 'buyer_auth_results_import_%'"
                ).fetchone()[0],
                1,
            )

    def test_invalid_events_never_persist_or_echo_private_fields(self):
        valid = event("buyer_login", 100, "security_challenge", "sign_in", 403)
        invalid = [
            dict(valid, password="private-secret-marker"),
            dict(valid, kind="private-secret-marker"),
            dict(valid, reason="private-secret-marker"),
            dict(valid, stage="private-secret-marker"),
            dict(valid, checked=True),
            dict(valid, checked=float("nan")),
            dict(valid, checked=float("inf")),
            dict(valid, checked=10**1000),
            dict(valid, checked=4102444801),
            dict(valid, http_status=True),
            dict(valid, http_status=99),
            dict(valid, kind=[]),
            dict(valid, reason={}),
            event("renewal", 100, "renewal_failed", "identity", 400),
            event("buyer_login", 100, "renewed", "sign_in", 200),
        ]
        with patch.dict(
            buyer.os.environ,
            {"MSJ_BUYER_AUTH_RESULTS_IMPORT_ON_START": json.dumps(invalid)},
        ):
            self.assertFalse(buyer.import_auth_results_once())
        self.assertEqual(buyer.public_auth_results(), {})
        with patch.dict(
            buyer.os.environ,
            {"MSJ_BUYER_AUTH_RESULTS_IMPORT_ON_START": json.dumps(invalid + [valid])},
        ):
            self.assertTrue(buyer.import_auth_results_once())
        self.assertNotIn(
            "private-secret-marker", json.dumps(buyer.public_auth_results())
        )
        self.assertNotIn("private-secret-marker", self.render())
        self.assertNotIn(b"private-secret-marker", Path(db.DB_PATH).read_bytes())

    def test_legacy_result_and_shared_native_renewal_are_formatted_without_mutation(
        self,
    ):
        legacy = event("buyer_login", 100, "security_challenge", "sign_in", 403)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT INTO parameters(key,value) VALUES ('buyer_connection_result',?)",
                (json.dumps(legacy),),
            )
            conn.execute(
                "UPDATE vinted_buyer_access SET checked=101,reason='renewal_failed',stage='renewal',http_status=400 WHERE id=1"
            )
        with patch.object(buyer, "ZoneInfo", side_effect=buyer.ZoneInfoNotFoundError):
            results = buyer.public_auth_results()
            self.assertIn("00:01:40 UTC", results["buyer_login"]["checked"])
            self.assertIn("00:01:41 UTC", results["renewal"]["checked"])
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM parameters WHERE key LIKE 'buyer_auth_result_%'"
                ).fetchone()[0],
                0,
            )

    def test_successful_native_renewal_is_recorded_only_after_usable_access_response(
        self,
    ):
        for status, data, accepted in (
            (201, {"access_token": "rotated-private-access-token-0123456789"}, True),
            (200, {}, False),
            (403, {"error": "forbidden"}, False),
        ):
            with self.subTest(status=status, accepted=accepted):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute(
                        "DELETE FROM parameters WHERE key='buyer_auth_result_renewal'"
                    )
                    conn.execute(
                        "UPDATE vinted_buyer_access SET checked=NULL,reason=NULL,stage=NULL,http_status=NULL"
                    )
                client = buyer.Client(
                    {
                        "cookies": {
                            "access_token_web": "old-private-access-token-0123456789",
                            "refresh_token_web": "old-private-refresh-token-0123456789",
                        }
                    }
                )
                response = Mock(
                    status_code=status,
                    headers={},
                    text="",
                    cookies=requests.cookies.RequestsCookieJar(),
                    json=Mock(return_value=data),
                )
                with patch.object(
                    client.session, "request", return_value=response
                ) as request, patch.object(
                    client, "retry_security_check", return_value=False
                ):
                    if accepted:
                        client.request("POST", "/web/api/auth/refresh")
                    else:
                        with self.assertRaises(buyer.BuyerError):
                            client.request("POST", "/web/api/auth/refresh")
                    request.assert_called_once()
                results = buyer.public_auth_results()
                if accepted:
                    self.assertEqual(results["renewal"]["reason"], "renewed")
                    self.assertEqual(results["renewal"]["http_status"], 201)
                    self.assertIn(
                        "Account verification is reported separately",
                        results["renewal"]["message"],
                    )
                else:
                    self.assertNotIn("renewal", results)
                self.assertFalse(buyer.settings()["enabled"])
                client.session.close()


if __name__ == "__main__":
    unittest.main()
