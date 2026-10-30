"""Accepted body-only token rotations retain the original cookie destinations."""

import base64
import json
import unittest
from contextlib import closing
from unittest.mock import patch

import requests
from test_search_controls import DatabaseFixture

import search_settings
import vinted_buyer as buyer
from vinted_session_worker import session_cookie_expiry

NOW = 2000000000
OLD_ACCESS = "old-private-access-token-0123456789"
OLD_REFRESH = "old-private-refresh-token-0123456789"


def token(expiry):
    claims = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
    return "eyJhbGciOiJub25lIn0." + claims + ".offline-signature"


class RotationCookieScopeTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.clock = patch.object(buyer.time, "time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def client(self, *, session=False):
        client = buyer.Client({"cookie_records": [
            {
                "name": "access_token_web", "value": OLD_ACCESS,
                "domain": ".vinted.co.uk", "path": "/", "secure": True,
                "expires": None if session else NOW + 60,
                "domain_specified": True, "domain_initial_dot": True,
                "path_specified": True, "discard": session,
            },
            {
                "name": "refresh_token_web", "value": OLD_REFRESH,
                "domain": "www.vinted.co.uk", "path": "/web/api/auth",
                "secure": True, "expires": NOW + 86400,
                "domain_specified": False, "domain_initial_dot": False,
                "path_specified": True, "discard": False,
            },
        ]})
        self.addCleanup(client.session.close)
        return client

    def response(self, data=None, *, cookies=None):
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(data or {}).encode()
        response._content_consumed = True
        if cookies is not None:
            response.cookies = cookies
        return response

    def records(self, client):
        return {record["name"]: record for record in client.exported()["cookie_records"]}

    def api_cookie(self, client):
        prepared = client.session.prepare_request(requests.Request(
            "GET", buyer.PICKUP_BASE + buyer.NOTIFICATIONS_PATH,
        ))
        return prepared.headers.get("Cookie", "")

    def test_body_only_rotation_retains_parent_access_and_host_only_refresh_scope(self):
        client = self.client()
        before = self.records(client)
        access, refresh = token(NOW + 3600), "new-opaque-refresh-0123456789"
        self.assertTrue(client.update_tokens(self.response(), {
            "access_token": access, "refresh_token": refresh,
        }))
        after = self.records(client)
        for name in before:
            old = dict(before[name])
            new = dict(after[name])
            old.pop("value")
            new.pop("value")
            if name == "access_token_web":
                old["expires"] = NOW + 3600
            self.assertEqual(new, old)
        cookie = self.api_cookie(client)
        self.assertEqual(cookie.count("access_token_web="), 1)
        self.assertIn("access_token_web=" + access, cookie)
        self.assertNotIn("refresh_token_web=", cookie)

    def test_persistent_expiry_tracks_new_accepted_claim_instead_of_old_due_time(self):
        client = self.client()
        client.update_tokens(self.response(), {"access_token": token(NOW + 7200)})
        self.assertEqual(self.records(client)["access_token_web"]["expires"], NOW + 7200)
        self.assertEqual(session_cookie_expiry(client.exported(), "access_token_web"), NOW + 7200)

    def test_session_cookie_stays_session_only_and_maintenance_uses_new_claim(self):
        client = self.client(session=True)
        client.update_tokens(self.response(), {"access_token": token(NOW + 3600)})
        record = self.records(client)["access_token_web"]
        self.assertIsNone(record["expires"])
        self.assertTrue(record["discard"])
        self.assertEqual(session_cookie_expiry(client.exported(), "access_token_web"), NOW + 3600)

    def test_explicit_set_cookie_is_authoritative_over_body_and_original_scope(self):
        client = self.client()
        returned = requests.cookies.RequestsCookieJar()
        returned.set("access_token_web", "set-cookie-access-0123456789",
                     domain="www.vinted.co.uk", path="/api", secure=True, expires=NOW + 1800)
        expected = next(iter(returned))
        self.assertTrue(client.update_tokens(self.response(cookies=returned), {
            "access_token": token(NOW + 7200),
        }))
        current = next(cookie for cookie in client.session.cookies if cookie.name == "access_token_web")
        for field in ("value", "domain", "path", "secure", "expires",
                      "domain_specified", "domain_initial_dot", "path_specified", "discard"):
            self.assertEqual(getattr(current, field), getattr(expected, field))
        self.assertNotIn("access_token_web=", self.api_cookie(client))

    def test_opaque_and_malformed_claims_do_not_invent_a_new_cookie_lifetime(self):
        for value in ("new-opaque-access-0123456789", token(True), token("later"),
                      token(4102444801), token(None)):
            with self.subTest(value=value):
                client = self.client()
                client.update_tokens(self.response(), {"access_token": value})
                self.assertEqual(self.records(client)["access_token_web"]["expires"], NOW + 60)
                self.assertEqual(self.records(client)["access_token_web"]["domain"], ".vinted.co.uk")

    def test_expired_claim_does_not_replace_the_working_credential_or_expiry(self):
        client = self.client()
        before = client.exported()
        self.assertFalse(client.update_tokens(self.response(), {"access_token": token(NOW - 1)}))
        self.assertEqual(client.exported(), before)

    def test_opaque_rotation_cannot_claim_an_already_expired_cookie_is_usable(self):
        client = self.client()
        for cookie in client.session.cookies:
            if cookie.name == "access_token_web":
                cookie.expires = NOW - 1
        before = client.exported()
        self.assertFalse(client.update_tokens(self.response(), {
            "access_token": "new-opaque-access-0123456789",
        }))
        self.assertEqual(client.exported(), before)

    def test_fractional_claim_cannot_produce_an_immediately_expired_cookie(self):
        client = self.client()
        before = client.exported()
        self.assertFalse(client.update_tokens(self.response(), {"access_token": token(NOW + 0.5)}))
        self.assertEqual(client.exported(), before)

    def test_missing_cookie_retains_existing_canonical_fallback(self):
        client = buyer.Client()
        self.addCleanup(client.session.close)
        client.update_tokens(self.response(), {"access_token": "new-opaque-access-0123456789"})
        self.assertEqual(self.records(client)["access_token_web"]["domain"], "www.vinted.co.uk")
        self.assertNotIn("access_token_web=", self.api_cookie(client))

    def test_ambiguous_body_rotation_rolls_back_response_without_retry(self):
        client = self.client()
        client.session.cookies.set("refresh_token_web", "duplicate-refresh-0123456789",
                                   domain=".vinted.co.uk", secure=True)
        original = client.exported()

        def accepted(*args, **kwargs):
            client.session.cookies.set("auxiliary", "private-auxiliary", domain="www.vinted.co.uk", secure=True)
            return self.response({
                "user": {"id": 99}, "access_token": token(NOW + 3600),
                "refresh_token": "rotated-refresh-0123456789",
            })

        with patch.object(client.session, "request", side_effect=accepted) as request:
            with self.assertRaises(buyer.BuyerError) as error:
                client.request("GET", "/api/v2/users/current")
        self.assertEqual(error.exception.reason, "saved_session")
        self.assertEqual(client.exported(), original)
        self.assertNotIn("duplicate-refresh", str(error.exception))
        request.assert_called_once()

    def test_accepted_rotation_persists_and_restores_original_scope(self):
        client = self.client()
        original = client.exported()
        sealed = buyer.encrypt(original)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET user_id='99',verified_at=?,session=? WHERE id=1", (NOW, sealed))
        client.bind_verified_session("99", sealed, original)
        access = token(NOW + 3600)
        with patch.object(client.session, "request", return_value=self.response({
            "user": {"id": 99}, "access_token": access,
        })) as request:
            client.request("GET", "/api/v2/users/current")
        with closing(search_settings.connection()) as conn:
            saved = buyer.decrypt(conn.execute("SELECT session FROM vinted_buyer WHERE id=1").fetchone()[0])
        restored = buyer.Client(saved)
        self.addCleanup(restored.session.close)
        self.assertEqual(self.records(restored)["access_token_web"]["domain"], ".vinted.co.uk")
        self.assertEqual(self.records(restored)["refresh_token_web"]["domain_specified"], False)
        self.assertIn("access_token_web=" + access, self.api_cookie(restored))
        self.assertEqual(session_cookie_expiry(saved, "access_token_web"), NOW + 3600)
        request.assert_called_once()
