"""Offline identity reads keep cooldown identity unless credentials change."""

import time
import unittest
from contextlib import closing
from unittest.mock import patch

import requests
import test_vinted_buying as buying_tests
from test_vinted_session_worker import token

import search_settings
import vinted_buyer as buyer
import vinted_session_worker as worker


class IdentityCooldownPreservationTests(
    buying_tests.SessionRotationFixture, unittest.TestCase
):
    transport_response = buying_tests.NativeCookieTransportTests.transport_response
    token_headers = buying_tests.NativeCookieTransportTests.token_headers

    def setUp(self):
        native = patch.object(buyer, "BrowserSession", requests.Session)
        native.start()
        self.addCleanup(native.stop)
        self.now = int(time.time())
        self.old_access = token(self.now + 900, "valid-early-due-access")
        self.old_refresh = token(self.now + 7 * 86400, "saved-refresh")
        self.new_access = token(self.now + 7200, "accepted-identity-access")
        self.new_refresh = token(self.now + 8 * 86400, "accepted-identity-refresh")
        self.calls = []
        self.responses = []
        super().setUp()
        # Export the canonical records once, so a no-op identity response has
        # literally unchanged content rather than legacy restoration metadata.
        seed = buyer.Client(self.saved()[1])
        try:
            canonical = seed.exported()
        finally:
            seed.session.close()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=? WHERE id=1",
                (buyer.encrypt(canonical),),
            )

    def native(self, adapter, prepared, **kwargs):
        request = (prepared.method, prepared.url.removeprefix(buyer.BASE))
        self.calls.append(request)
        response = self.responses.pop(0)
        self.assertEqual(request, response["request"])
        return self.transport_response(
            prepared,
            response.get("data"),
            status=response.get("status", 200),
            text=response.get("text", ""),
            cookies=response.get("cookies", ()),
        )

    def identity(self, *, rotate=False):
        return {
            "request": ("GET", "/api/v2/users/current"),
            "data": {"user": {"id": 99}},
            "cookies": (
                self.token_headers(self.new_access, self.new_refresh) if rotate else ()
            ),
        }

    def refused_renewal(self):
        return [
            self.identity(),
            {
                "request": ("GET", "/"),
                "text": '<meta name="csrf-token" content="offline-fresh-csrf-0123456789">',
            },
            {
                "request": ("POST", "/web/api/auth/refresh"),
                "status": 400,
                "data": {"message": "Bad request"},
            },
        ]

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

    def maintenance_state(self):
        with closing(search_settings.connection()) as conn:
            return dict(
                conn.execute(
                    "SELECT * FROM vinted_buyer_maintenance WHERE id=1"
                ).fetchone()
            )

    def connect(self):
        with buyer.exclusive():
            client = buyer.connected_client()
        self.addCleanup(client.session.close)
        return client

    def transport(self):
        return patch.object(
            requests.adapters.HTTPAdapter,
            "send",
            autospec=True,
            side_effect=self.native,
        )

    def test_unchanged_identity_keeps_seal_and_advances_verification(self):
        before, contents = self.saved()
        controls = self.controls()
        self.responses = [self.identity()]
        with self.transport(), patch.object(buyer.time, "time", return_value=self.now):
            client = self.connect()
        after, saved = self.saved()
        self.assertEqual(after["session"], before["session"])
        self.assertEqual(saved, contents)
        self.assertEqual(after["verified_at"], self.now)
        self.assertEqual(client._verified_session[1], after["session"])
        self.assertEqual(self.controls(), controls)
        self.assertEqual(self.calls, [("GET", "/api/v2/users/current")])
        self.assertEqual(self.responses, [])

    def test_owner_identity_cannot_bypass_refused_renewal_cooldown(self):
        original = self.saved()[0]["session"]
        controls = self.controls()
        self.responses = self.refused_renewal() + [self.identity()]
        with self.transport(), patch.object(worker.time, "time", return_value=self.now):
            self.assertEqual(worker.maintain_connection(), "cooldown")
            state = self.maintenance_state()
            self.connect()
            self.assertEqual(worker.maintain_connection(), "cooldown")
        self.assertEqual(self.saved()[0]["session"], original)
        self.assertEqual(self.saved()[0]["verified_at"], self.now)
        self.assertEqual(self.maintenance_state(), state)
        self.assertEqual(state["retry_at"], self.now + worker.AMBIGUOUS_RENEWAL_DELAY)
        self.assertEqual(self.calls.count(("POST", "/web/api/auth/refresh")), 1)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.controls(), controls)
        self.assertEqual(self.responses, [])

    def test_real_identity_rotation_changes_seal_and_rechecks_freshness(self):
        controls = self.controls()
        self.responses = self.refused_renewal() + [self.identity(rotate=True)]
        with self.transport(), patch.object(worker.time, "time", return_value=self.now):
            self.assertEqual(worker.maintain_connection(), "cooldown")
            sealed = self.saved()[0]["session"]
            old_state = self.maintenance_state()
            client = self.connect()
            self.assertEqual(worker.maintain_connection(), "fresh")
        after, saved = self.saved()
        self.assertNotEqual(after["session"], sealed)
        self.assertEqual(after["verified_at"], self.now)
        self.assertEqual(saved["cookies"]["access_token_web"], self.new_access)
        self.assertEqual(saved["cookies"]["refresh_token_web"], self.new_refresh)
        self.assertEqual(client._verified_session[1], after["session"])
        # The old session's retry reservation remains attributed to that old
        # seal; it neither blocks fresh credentials nor forces another request.
        self.assertEqual(self.maintenance_state(), old_state)
        self.assertEqual(self.calls.count(("POST", "/web/api/auth/refresh")), 1)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.controls(), controls)
        self.assertEqual(self.responses, [])


if __name__ == "__main__":
    unittest.main()
