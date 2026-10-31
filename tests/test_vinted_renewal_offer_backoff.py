"""A local renewal guard shares its actual remaining deadline with offers."""

import hashlib
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_search_controls import DatabaseFixture

import search_settings
import vinted_buyer as buyer
import vinted_offers as offers
import vinted_session_worker as worker

NOW = 2000000000


class RenewalOfferBackoffTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.sealed = buyer.encrypt({"cookies": {"access_token_web": "offline-access-token-0123456789"}})
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET user_id='99',verified_at=?,session=?",
                (NOW - 1, self.sealed),
            )
            conn.execute(
                "UPDATE vinted_buyer_maintenance SET session_fingerprint=?,retry_at=?,blocked=0",
                (hashlib.sha256(self.sealed).hexdigest(), NOW + 73),
            )
        offers.configure("live", "half_pound", "today", now=NOW)

    def local_error(self):
        client = SimpleNamespace(_verified_session=("99", self.sealed, {}), _loaded_session_reference=None)
        with patch.object(worker.time, "time", return_value=NOW):
            with self.assertRaises(buyer.BuyerError) as caught:
                worker.reserve_renewal(client)
        return caught.exception

    def test_real_renewal_admission_error_retains_exact_remaining_backoff(self):
        error = self.local_error()
        self.assertEqual(error.reason, "renewal_cooldown")
        self.assertEqual(error.retry_after, 73)
        self.assertEqual(error.retry_after_seconds, 73)
        offers._wait_after_failure(error, NOW)
        self.assertEqual(offers.state()["cooldown_until"], NOW + 73)
        self.assertEqual(offers.state()["mode"], "live")

    def test_worker_waits_until_real_deadline_then_checks_without_an_extra_fifteen_minutes(self):
        offers._wait_after_failure(self.local_error(), NOW)
        client = SimpleNamespace(
            _verified_session=("99", self.sealed, {}),
            session=SimpleNamespace(close=Mock()),
            request=Mock(return_value={"notifications": []}),
        )
        factory = Mock(return_value=client)
        offers.OfferWorker(clock=lambda: NOW + 72, client_factory=factory).tick()
        factory.assert_not_called()
        offers.OfferWorker(clock=lambda: NOW + 73, client_factory=factory).tick()
        factory.assert_called_once()
        self.assertEqual(client.request.call_args.args[0:2], ("GET", buyer.NOTIFICATIONS_PATH))

    def test_upstream_rate_limit_still_has_its_existing_minimum(self):
        error = buyer.BuyerError("Offline rate limit", 429, reason="rate_limited", retry_after=73)
        offers._wait_after_failure(error, NOW)
        self.assertEqual(offers.state()["cooldown_until"], NOW + 900)

    def test_local_deadline_never_shortens_an_existing_upstream_pause(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_offer_settings SET cooldown_until=?,next_scan=?,next_send=?", (NOW + 1000,) * 3)
        offers._wait_after_failure(self.local_error(), NOW)
        state = offers.state()
        self.assertEqual((state["cooldown_until"], state["next_scan"], state["next_send"]), (NOW + 1000,) * 3)

    def test_unknown_or_invalid_local_delay_keeps_the_conservative_fallback(self):
        for delay in (0, -1, float("nan"), float("inf"), "73"):
            with self.subTest(delay=delay):
                error = buyer.BuyerError("Offline local guard", reason="renewal_cooldown", retry_after=delay)
                offers._wait_after_failure(error, NOW)
                self.assertEqual(offers.state()["cooldown_until"], NOW + 900)
