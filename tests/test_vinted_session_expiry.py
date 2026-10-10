"""Offline expiry and disabled-buyer maintenance; no live auth or payments."""

import time
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
import test_vinted_buying as buying_tests
from test_vinted_session_worker import token

import search_settings
import vinted_buyer as buyer
import vinted_session_worker as worker


def record(name, value, expiry=None, **kwargs):
    return {
        "name": name,
        "value": value,
        "domain": "www.vinted.co.uk",
        "path": "/",
        "secure": True,
        "expires": expiry,
        **kwargs,
    }


def saved(access="opaque-access", refresh="opaque-refresh", *, records=None):
    result = {"cookies": {"access_token_web": access, "refresh_token_web": refresh}}
    if records is not None:
        result["cookie_records"] = records
    return result


class CookieExpiryTests(unittest.TestCase):
    now = 1800000000

    def test_legacy_jwt_scheduling_and_opaque_cookie_fallback(self):
        session = saved(
            token(self.now + 60, "access"), token(self.now + 700000, "refresh")
        )
        self.assertTrue(worker.due(session, self.now))
        self.assertEqual(
            worker.session_cookie_expiry(session, "access_token_web"), self.now + 60
        )
        session = saved(
            records=[
                record("access_token_web", "opaque-access", self.now + 60),
                record("refresh_token_web", "opaque-refresh", self.now + 700000),
            ]
        )
        self.assertTrue(worker.due(session, self.now))
        self.assertEqual(
            worker.session_cookie_expiry(session, "access_token_web"), self.now + 60
        )

    def test_scheduling_uses_earlier_cookie_or_jwt_expiry(self):
        value = token(self.now + 3600, "access")
        session = saved(
            value,
            records=[
                record("access_token_web", value, self.now + 60),
                record("refresh_token_web", "opaque-refresh", self.now + 700000),
            ],
        )
        self.assertEqual(
            worker.session_cookie_expiry(session, "access_token_web"), self.now + 60
        )
        self.assertTrue(worker.due(session, self.now))
        session["cookie_records"][0]["value"] = token(self.now + 30, "access")
        session["cookie_records"][0]["expires"] = self.now + 3600
        self.assertEqual(
            worker.session_cookie_expiry(session, "access_token_web"), self.now + 30
        )
        self.assertTrue(worker.due(session, self.now))

    def test_invalid_jwt_claims_fall_back_to_valid_cookie_metadata(self):
        for value in (
            "opaque-token",
            token("invalid", "access"),
            token(True, "access"),
            token(float("inf"), "access"),
        ):
            with self.subTest(value_type=type(value).__name__):
                session = saved(
                    records=[record("access_token_web", value, self.now + 60)]
                )
                self.assertEqual(
                    worker.session_cookie_expiry(session, "access_token_web"),
                    self.now + 60,
                )

    def test_opaque_session_cookies_and_unknown_expiry_do_not_start_authentication(
        self,
    ):
        session = saved(
            records=[
                record("access_token_web", "opaque-access"),
                record("refresh_token_web", "opaque-refresh"),
            ]
        )
        self.assertIsNone(worker.session_cookie_expiry(session, "access_token_web"))
        self.assertFalse(worker.due(session, self.now))
        self.assertFalse(worker.due(saved(), self.now))

    def test_refresh_cookie_margin_and_expired_access_survive_restart(self):
        session = saved(
            records=[
                record("access_token_web", "opaque-access", self.now + 3600),
                record(
                    "refresh_token_web",
                    "opaque-refresh",
                    self.now + worker.REFRESH_MARGIN,
                ),
            ]
        )
        self.assertTrue(worker.due(session, self.now))
        session["cookie_records"][0]["expires"] = self.now - 60
        session["cookie_records"][1]["expires"] = self.now + 700000
        self.assertTrue(worker.due(session, self.now))

    def test_canonical_domain_and_request_path_scope_are_required(self):
        for metadata in (
            {"domain": "www.vinted.fr"},
            {"domain": "other.vinted.co.uk"},
            {"domain": "evil.test"},
            {"domain": ".vinted.co.uk", "domain_specified": False},
            {"path": "/checkout"},
            {"path": "/api/v2/users/currently"},
            {"path": "relative"},
            {"path": "/invalid\r\npath"},
        ):
            with self.subTest(metadata=metadata):
                session = saved(
                    token(self.now + 60, "stale-flat-view"),
                    records=[
                        record(
                            "access_token_web",
                            "opaque-access",
                            self.now + 60,
                            **metadata,
                        ),
                        record(
                            "refresh_token_web", "opaque-refresh", self.now + 700000
                        ),
                    ],
                )
                self.assertIsNone(
                    worker.session_cookie_expiry(session, "access_token_web")
                )
                self.assertFalse(worker.due(session, self.now))

    def test_legitimate_parent_domain_and_scoped_paths_are_applicable(self):
        for domain in (
            "www.vinted.co.uk",
            ".www.vinted.co.uk",
            "vinted.co.uk",
            ".vinted.co.uk",
        ):
            with self.subTest(domain=domain):
                session = saved(
                    records=[
                        record(
                            "access_token_web",
                            "opaque-access",
                            self.now + 60,
                            domain=domain,
                            path="/api/v2/users",
                        ),
                        record(
                            "refresh_token_web",
                            "opaque-refresh",
                            self.now + 700000,
                            domain=domain,
                            path="/web/api/auth",
                        ),
                    ]
                )
                self.assertTrue(worker.due(session, self.now))

    def test_missing_or_inapplicable_refresh_cookie_does_not_start_authentication(self):
        for refresh in (
            None,
            record(
                "refresh_token_web",
                "opaque-refresh",
                self.now + 700000,
                path="/checkout",
            ),
        ):
            records = [record("access_token_web", "opaque-access", self.now + 60)]
            if refresh:
                records.append(refresh)
            session = saved(records=records)
            self.assertFalse(worker.due(session, self.now))
        self.assertFalse(
            worker.due(saved(token(self.now + 60, "access"), ""), self.now)
        )

    def test_malformed_metadata_never_supplies_expiry(self):
        for metadata in (
            {"expires": True},
            {"expires": "1800000060"},
            {"expires": 1800000060.0},
            {"expires": float("nan")},
            {"expires": float("inf")},
            {"expires": -1},
            {"expires": 253402300800},
            {"domain": []},
            {"path": None},
            {"secure": "true"},
            {"domain_specified": 1},
            {"discard": "false"},
            {"value": "secret\r\nvalue"},
        ):
            with self.subTest(metadata_type=next(iter(metadata))):
                session = saved(
                    records=[
                        dict(
                            record("access_token_web", "opaque-access", self.now + 60),
                            **metadata,
                        )
                    ]
                )
                self.assertIsNone(
                    worker.session_cookie_expiry(session, "access_token_web")
                )
        for session in (
            None,
            [],
            {"cookie_records": {}},
            {"cookie_records": [None]},
            {"cookies": []},
        ):
            self.assertFalse(worker.due(session, self.now))

    def test_record_view_is_authoritative_and_ambiguous_values_are_ignored(self):
        session = saved(
            "foreign-flat-value",
            records=[
                record("access_token_web", "real-canonical-access", self.now + 60),
                record(
                    "access_token_web",
                    "foreign-flat-value",
                    self.now + 700000,
                    domain="other.vinted.co.uk",
                ),
                record("refresh_token_web", "opaque-refresh", self.now + 700000),
            ],
        )
        self.assertEqual(
            worker.session_cookie_expiry(session, "access_token_web"), self.now + 60
        )
        session["cookie_records"].append(
            record("access_token_web", "ambiguous-access", self.now + 30, path="/api")
        )
        self.assertIsNone(worker.session_cookie_expiry(session, "access_token_web"))
        self.assertFalse(worker.due(session, self.now))


class DisabledMaintenanceTests(buying_tests.SessionRotationFixture, unittest.TestCase):
    transport_response = buying_tests.NativeCookieTransportTests.transport_response

    def setUp(self):
        transport = patch.object(buyer, "BrowserSession", requests.Session)
        transport.start()
        self.addCleanup(transport.stop)
        self.now = int(time.time())
        self.calls = []
        self.renew_status = 200
        self.old_access = "offline-opaque-access-token-0123456789"
        self.old_refresh = "offline-opaque-refresh-token-0123456789"
        self.new_access = "offline-renewed-access-token-0123456789"
        self.new_refresh = "offline-renewed-refresh-token-0123456789"
        super().setUp()
        self.store_opaque_expiries(self.now + 60, self.now + 7 * 86400)

    def store_opaque_expiries(self, access_expiry, refresh_expiry):
        session = saved(
            self.old_access,
            self.old_refresh,
            records=[
                record("access_token_web", self.old_access, access_expiry),
                record("refresh_token_web", self.old_refresh, refresh_expiry),
            ],
        )
        client = buyer.Client(session)
        try:
            session = client.exported()
        finally:
            client.session.close()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?,enabled=0", (buyer.encrypt(session),)
            )

    def native(self, adapter, prepared, **kwargs):
        path = prepared.url.removeprefix(buyer.BASE)
        self.calls.append((prepared.method, path))
        if path == "/api/v2/users/current":
            return self.transport_response(prepared, {"user": {"id": 99}})
        if path == "/":
            self.assertNotIn("access_token_web", prepared.headers.get("Cookie", ""))
            return self.transport_response(
                prepared,
                text='<meta name="csrf-token" content="offline-fresh-csrf-0123456789">',
            )
        self.assertEqual(path, "/web/api/auth/refresh")
        self.assertEqual(prepared.method, "POST")
        self.assertIsNone(prepared.body)
        self.assertNotIn("Content-Type", prepared.headers)
        self.assertIn(self.old_refresh, prepared.headers.get("Cookie", ""))
        self.assertEqual(worker.maintain_connection(), "busy")
        cookies = (
            ()
            if self.renew_status != 200
            else (
                f"access_token_web={self.new_access}; Domain=www.vinted.co.uk; Path=/; Secure; Max-Age=1900",
                f"refresh_token_web={self.new_refresh}; Domain=www.vinted.co.uk; Path=/; Secure; Max-Age=604800",
            )
        )
        return self.transport_response(
            prepared,
            {} if self.renew_status == 200 else {"error": "private-upstream-rejection"},
            status=self.renew_status,
            cookies=cookies,
        )

    def run_cycle(self, at=None):
        with patch.object(
            worker.time, "time", return_value=self.now if at is None else at
        ), patch.object(
            requests.adapters.HTTPAdapter,
            "send",
            autospec=True,
            side_effect=self.native,
        ):
            return worker.maintain_connection()

    def controls(self):
        with closing(search_settings.connection()) as conn:
            return (
                tuple(
                    conn.execute(
                        "SELECT user_id,enabled,max_total,max_extra,pickup_mode,preferred_card_last4 FROM vinted_buyer"
                    ).fetchone()
                ),
                [
                    tuple(row)
                    for row in conn.execute("SELECT * FROM vinted_search_budgets")
                ],
                [
                    tuple(row)
                    for row in conn.execute("SELECT * FROM vinted_buy_attempts")
                ],
            )

    def test_verified_disabled_buyer_with_opaque_expiry_renews_and_stays_disabled(self):
        controls = self.controls()
        self.assertEqual(self.run_cycle(), "verified")
        self.assertEqual(
            self.calls,
            [
                ("GET", "/api/v2/users/current"),
                ("GET", "/"),
                ("POST", "/web/api/auth/refresh"),
                ("GET", "/api/v2/users/current"),
            ],
        )
        self.assertEqual(self.controls(), controls)
        self.assertFalse(self.saved()[0]["enabled"])
        self.assertEqual(
            self.saved()[1]["cookies"]["access_token_web"], self.new_access
        )
        self.assertEqual(self.run_cycle(), "cooldown")
        self.assertEqual(self.run_cycle(self.now + 300), "fresh")
        self.assertEqual(len(self.calls), 4)

    def test_unknown_or_not_due_opaque_expiry_uses_no_authentication_request(self):
        for expiries in ((None, None), (self.now + 3600, self.now + 7 * 86400)):
            with self.subTest(expiries=expiries):
                self.store_opaque_expiries(*expiries)
                self.assertEqual(self.run_cycle(), "fresh")
        self.assertEqual(self.calls, [])
        self.assertFalse(self.saved()[0]["enabled"])

    def test_ambiguous_renewal_retries_after_cooldown_and_never_enables(self):
        self.renew_status = 400
        original = self.saved()[0]["session"]
        self.assertEqual(self.run_cycle(), "cooldown")
        self.assertEqual(self.run_cycle(self.now + 899), "cooldown")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.saved()[0]["session"], original)
        self.assertFalse(self.saved()[0]["enabled"])
        self.assertEqual(self.controls()[2], [])
        self.renew_status = 200
        self.assertEqual(self.run_cycle(self.now + 900), "verified")
        self.assertFalse(self.saved()[0]["enabled"])
        self.assertEqual(self.controls()[2], [])

    def test_transient_error_waits_for_cooldown_with_autobuy_off(self):
        self.renew_status = 503
        self.assertEqual(self.run_cycle(), "cooldown")
        self.assertEqual(self.run_cycle(self.now + 299), "cooldown")
        self.assertEqual(len(self.calls), 3)
        self.renew_status = 200
        self.assertEqual(self.run_cycle(self.now + 300), "verified")
        self.assertFalse(self.saved()[0]["enabled"])

    def test_unverified_account_is_not_maintained_even_with_due_metadata(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET verified_at=NULL")
        self.assertEqual(self.run_cycle(), "idle")
        self.assertEqual(self.calls, [])

    def test_bootstrap_and_renewal_share_one_solver_allowance(self):
        private = buyer.Client()
        self.addCleanup(private.session.close)
        public = SimpleNamespace(
            solver_attempted=False,
            csrf="offline-csrf",
            session=SimpleNamespace(close=Mock()),
        )

        def homepage():
            self.assertFalse(public.solver_attempted)
            public.solver_attempted = True

        public.homepage = Mock(side_effect=homepage)
        with patch.object(buyer, "Client", return_value=public):
            private.refresh_security_token()
        self.assertTrue(private.solver_attempted)
        public.session.close.assert_called_once()

    def test_prior_solver_use_is_propagated_to_bootstrap_even_on_failure(self):
        private = buyer.Client()
        self.addCleanup(private.session.close)
        private.solver_attempted = True
        public = SimpleNamespace(
            solver_attempted=False, csrf="", session=SimpleNamespace(close=Mock())
        )

        def homepage():
            self.assertTrue(public.solver_attempted)
            raise buyer.BuyerError(
                "Security challenge", 403, reason="security_challenge", stage="homepage"
            )

        public.homepage = Mock(side_effect=homepage)
        with patch.object(buyer, "Client", return_value=public), self.assertRaises(
            buyer.BuyerError
        ):
            private.refresh_security_token()
        self.assertTrue(private.solver_attempted)
        public.session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
