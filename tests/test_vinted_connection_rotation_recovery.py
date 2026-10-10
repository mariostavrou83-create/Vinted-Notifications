"""Offline accepted rotations survive a later connection failure; no purchases."""

import hashlib
import time
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

import requests
import test_vinted_buying as buying_tests
from test_vinted_session_worker import token

import search_settings
import vinted_buyer as buyer


class ConnectionRotationRecoveryTests(
    buying_tests.SessionRotationFixture, unittest.TestCase
):
    transport_response = buying_tests.NativeCookieTransportTests.transport_response
    token_headers = buying_tests.NativeCookieTransportTests.token_headers

    def setUp(self):
        native = patch.object(buyer, "BrowserSession", requests.Session)
        native.start()
        self.addCleanup(native.stop)
        self.now = int(time.time())
        self.new_access = token(self.now + 60, "accepted-identity-access")
        self.new_refresh = token(self.now + 7 * 86400, "accepted-identity-refresh")
        self.final_access = token(self.now + 3600, "accepted-renewal-access")
        self.final_refresh = token(self.now + 8 * 86400, "accepted-renewal-refresh")
        self.clients = []
        self.calls = []
        self.responses = []
        self.real_client = buyer.Client
        super().setUp()

    def client(self, *args, **kwargs):
        client = self.real_client(*args, **kwargs)
        client.session.close = Mock(wraps=client.session.close)
        self.clients.append(client)
        return client

    def native(self, adapter, prepared, **kwargs):
        self.calls.append((prepared.method, prepared.url.removeprefix(buyer.BASE)))
        response = self.responses.pop(0)
        self.assertEqual(self.calls[-1], response["request"])
        if response.get("before"):
            response["before"]()
        if response.get("error"):
            raise response["error"]
        return self.transport_response(
            prepared,
            response.get("data"),
            status=response.get("status", 200),
            text=response.get("text", ""),
            cookies=response.get("cookies", ()),
        )

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

    def identity(self, *, user=99, rotate=True, **extra):
        return {
            "request": ("GET", "/api/v2/users/current"),
            "data": {"user": {"id": user}},
            "cookies": (
                self.token_headers(self.new_access, self.new_refresh) if rotate else ()
            ),
            **extra,
        }

    def bootstrap(self, **extra):
        return {
            "request": ("GET", "/"),
            "text": '<meta name="csrf-token" content="offline-fresh-csrf-0123456789">',
            **extra,
        }

    def renewal(self, **extra):
        return {
            "request": ("POST", "/web/api/auth/refresh"),
            "data": {},
            "cookies": self.token_headers(self.final_access, self.final_refresh),
            **extra,
        }

    def connect(self, *, renew_before=120, check=False):
        with (
            patch.object(buyer, "Client", side_effect=self.client),
            patch.object(
                requests.adapters.HTTPAdapter,
                "send",
                autospec=True,
                side_effect=self.native,
            ),
        ):
            if check:
                return buyer.check_saved_connection()
            with buyer.exclusive():
                return buyer.connected_client(renew_before=renew_before)

    def assert_preserved_failure(self, reason):
        before = self.controls()
        with self.assertRaises(buyer.BuyerError) as failure:
            self.connect()
        self.assertEqual(failure.exception.reason, reason)
        row, saved = self.saved()
        self.assertEqual(saved["cookies"]["refresh_token_web"], self.new_refresh)
        self.assertEqual(row["verified_at"], 1)
        self.assertEqual(self.controls(), before)
        self.assertEqual(self.responses, [])
        for client in self.clients:
            client.session.close.assert_called_once()

    def test_initial_identity_rotation_survives_failed_bootstrap(self):
        self.responses = [self.identity(), self.bootstrap(status=503, data={})]
        self.assert_preserved_failure("unreadable")

    def test_rejected_renewal_restores_to_accepted_identity_rotation(self):
        self.responses = [
            self.identity(),
            self.bootstrap(),
            self.renewal(status=400, data={"message": "Bad request"}),
        ]
        self.assert_preserved_failure("renewal_failed")
        self.assertNotEqual(
            self.saved()[1]["cookies"]["refresh_token_web"], self.final_refresh
        )

    def test_initial_identity_rotation_survives_renewal_network_failure(self):
        self.responses = [
            self.identity(),
            self.bootstrap(),
            self.renewal(error=requests.ConnectionError("offline failure")),
        ]
        self.assert_preserved_failure("network")

    def test_initial_identity_rotation_survives_bootstrap_network_failure(self):
        self.responses = [
            self.identity(),
            self.bootstrap(error=requests.ConnectionError("offline failure")),
        ]
        self.assert_preserved_failure("network")

    def test_accepted_renewal_survives_later_identity_network_failure(self):
        before = self.controls()
        self.responses = [
            self.identity(),
            self.bootstrap(),
            self.renewal(),
            self.identity(error=requests.ConnectionError("offline failure")),
        ]
        with self.assertRaises(buyer.BuyerError) as failure:
            self.connect()
        self.assertEqual(failure.exception.reason, "network")
        row, saved = self.saved()
        self.assertEqual(saved["cookies"]["refresh_token_web"], self.final_refresh)
        self.assertEqual(row["verified_at"], 1)
        self.assertEqual(self.controls(), before)

    def test_initial_wrong_buyer_cannot_persist_or_start_proactive_renewal(self):
        before = self.saved()[0]["session"]
        self.responses = [self.identity(user=100)]
        with self.assertRaises(buyer.BuyerError) as failure:
            self.connect()
        self.assertEqual(failure.exception.reason, "account_changed")
        self.assertEqual(failure.exception.stage, "identity")
        self.assertEqual(self.saved()[0]["session"], before)
        self.assertEqual(self.calls, [("GET", "/api/v2/users/current")])
        self.clients[0].session.close.assert_called_once()

    def test_wrong_buyer_after_renewal_never_updates_verification_or_controls(self):
        before = self.controls()
        self.responses = [
            self.identity(),
            self.bootstrap(),
            self.renewal(),
            self.identity(
                user=100,
                cookies=self.token_headers(
                    "offline-foreign-access-0123456789",
                    "offline-foreign-refresh-0123456789",
                ),
            ),
        ]
        with self.assertRaises(buyer.BuyerError) as failure:
            self.connect()
        self.assertEqual(failure.exception.reason, "account_changed")
        self.assertEqual(failure.exception.stage, "identity")
        self.assertEqual(self.saved()[0]["verified_at"], 1)
        self.assertEqual(
            self.saved()[1]["cookies"]["refresh_token_web"], self.final_refresh
        )
        self.assertEqual(
            self.clients[0].exported()["cookies"]["refresh_token_web"],
            self.final_refresh,
        )
        self.assertEqual(self.controls(), before)

    def test_unusable_identity_after_renewal_cannot_persist_returned_tokens(self):
        for data in ({}, {"user": []}, {"user": {"id": "unusable"}}):
            with self.subTest(data=data):
                before = self.controls()
                self.responses = [
                    self.identity(),
                    self.bootstrap(),
                    self.renewal(),
                    self.identity(
                        data=data,
                        cookies=self.token_headers(
                            "offline-unverified-access-0123456789",
                            "offline-unverified-refresh-0123456789",
                        ),
                    ),
                ]
                with self.assertRaises(buyer.BuyerError) as failure:
                    self.connect()
                self.assertEqual(failure.exception.reason, "not_confirmed")
                self.assertEqual(failure.exception.stage, "identity")
                self.assertEqual(
                    self.saved()[1]["cookies"]["refresh_token_web"],
                    self.final_refresh,
                )
                self.assertEqual(self.saved()[0]["verified_at"], 1)
                self.assertEqual(self.controls(), before)

    def replacement(self):
        return buyer.encrypt(
            {"cookies": {"access_token_web": "offline-newer-connection-0123456789"}}
        )

    def replace(self, sealed):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET session=? WHERE id=1", (sealed,))

    def test_accepted_initial_rotation_cannot_overwrite_a_newer_connection(self):
        replacement = self.replacement()
        self.responses = [self.identity(before=lambda: self.replace(replacement))]
        with self.assertRaises(buyer.BuyerError) as failure:
            self.connect()
        self.assertEqual(failure.exception.reason, "saved_session")
        self.assertEqual(self.saved()[0]["session"], replacement)
        self.assertEqual(self.saved()[0]["verified_at"], 1)
        self.assertEqual(len(self.calls), 1)
        self.clients[0].session.close.assert_called_once()

    def test_final_verification_cas_cannot_overwrite_a_newer_connection(self):
        replacement = self.replacement()
        self.new_access = token(self.now + 3600, "accepted-fresh-identity-access")
        self.responses = [self.identity()]
        real_encrypt = buyer.encrypt
        encryption_count = 0

        def encrypt(data):
            nonlocal encryption_count
            encryption_count += 1
            if encryption_count == 2:
                self.replace(replacement)
            return real_encrypt(data)

        with (
            patch.object(buyer, "encrypt", side_effect=encrypt),
            self.assertRaises(buyer.BuyerError) as failure,
        ):
            self.connect()
        self.assertEqual(failure.exception.reason, "saved_session")
        self.assertEqual(self.saved()[0]["session"], replacement)
        self.assertEqual(self.saved()[0]["verified_at"], 1)

    def test_unbound_expired_recovery_cas_preserves_a_newer_connection(self):
        replacement = self.replacement()
        self.responses = [
            self.identity(status=401, data={"code": "unauthorized"}, rotate=False),
            self.bootstrap(),
            self.renewal(before=lambda: self.replace(replacement)),
        ]
        with self.assertRaises(buyer.BuyerError) as failure:
            self.connect()
        self.assertEqual(failure.exception.reason, "saved_session")
        self.assertEqual(self.saved()[0]["session"], replacement)
        self.assertEqual(self.saved()[0]["verified_at"], 1)
        self.assertEqual(len(self.calls), 3)

    def test_expired_recovery_still_persists_accepted_rotation_and_verifies_buyer(self):
        before = self.controls()
        self.responses = [
            self.identity(status=401, data={"code": "unauthorized"}, rotate=False),
            self.bootstrap(),
            self.renewal(),
            self.identity(rotate=False),
        ]
        client = self.connect()
        client.session.close()
        row, saved = self.saved()
        self.assertEqual(saved["cookies"]["refresh_token_web"], self.final_refresh)
        self.assertGreater(row["verified_at"], 1)
        self.assertEqual(client._verified_session[1], row["session"])
        self.assertEqual(self.controls(), before)
        self.assertEqual(self.responses, [])

    def test_owner_check_failure_has_only_current_encrypted_session_fingerprint(self):
        sealed = self.saved()[0]["session"]
        self.responses = [
            self.identity(status=401, data={"code": "unauthorized"}, rotate=False),
            self.bootstrap(),
            self.renewal(status=400, data={"message": "Bad request"}),
        ]
        with self.assertRaises(buyer.BuyerError) as failure:
            self.connect(check=True)
        self.assertEqual(
            failure.exception.session_fingerprint, hashlib.sha256(sealed).hexdigest()
        )
        self.assertNotIn(failure.exception.session_fingerprint, str(failure.exception))
        self.assertEqual(self.saved()[0]["session"], sealed)

    def test_old_failed_renewal_does_not_adopt_newer_connection_fingerprint(self):
        replacement = self.replacement()
        self.responses = [
            self.identity(status=401, data={"code": "unauthorized"}, rotate=False),
            self.bootstrap(),
            self.renewal(
                status=400,
                data={"message": "Bad request"},
                before=lambda: self.replace(replacement),
            ),
        ]
        with self.assertRaises(buyer.BuyerError) as failure:
            self.connect(check=True)
        self.assertEqual(failure.exception.reason, "renewal_failed")
        self.assertIsNone(failure.exception.session_fingerprint)
        self.assertEqual(self.saved()[0]["session"], replacement)


if __name__ == "__main__":
    unittest.main()
