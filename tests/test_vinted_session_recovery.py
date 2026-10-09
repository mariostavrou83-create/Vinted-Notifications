"""Offline recovery: ordinary renewal and explicit activation never purchase."""

import json
import os
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

import search_settings
import vinted_buyer as buyer
import vinted_buying as buying
import vinted_session_recovery as recovery
from test_search_controls import DatabaseFixture
from test_vinted_buying import DEVICE


class RecoveryTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.env = patch.dict(
            os.environ,
            {
                "MSJ_BUYER_RECOVERY_ON_LINK": "offline-recovery",
                "MSJ_BUYER_RECOVERY_AFTER": "1000",
                "MSJ_TELEGRAM_REVIEW_ONLY": "0",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.saved = {
            "cookies": {"access_token_web": "private-access-token"},
            "csrf": "private-csrf",
        }
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?,verified_at=1,user_id='99',username='owner',enabled=0,pickup_mode='nearest',preferred_card_last4='1234',browser_info=?",
                (buyer.encrypt(self.saved), json.dumps(DEVICE)),
            )
            conn.execute("INSERT INTO vinted_search_budgets VALUES (1,2000,350)")
        self.client = Mock()
        self.client.network = buyer.network_configuration()
        self.client.identity.return_value = ("99", "owner")
        self.client.exported.return_value = self.saved

    def controls(self):
        with closing(search_settings.connection()) as conn:
            return (
                tuple(
                    conn.execute(
                        "SELECT user_id,max_total,max_extra,pickup_mode,preferred_card_last4,browser_info FROM vinted_buyer"
                    ).fetchone()
                ),
                [tuple(r) for r in conn.execute("SELECT * FROM vinted_search_budgets")],
                [tuple(r) for r in conn.execute("SELECT * FROM queries")],
                [tuple(r) for r in conn.execute("SELECT * FROM vinted_buy_attempts")],
            )

    def test_verified_native_renewal_enables_once_without_checkout_or_payment(self):
        buying.claim({"item_id": "987"})
        buying.record("987", "unknown", "Previous payment is unconfirmed.")
        before = self.controls()
        with patch.object(buyer, "renew_saved_client") as renew, buyer.exclusive():
            result = recovery.recover_linked_client(self.client)
            self.assertIsNone(recovery.recover_linked_client(self.client))
        renew.assert_called_once_with(self.client)
        self.client.identity.assert_called_once()
        self.client.request.assert_not_called()
        self.assertEqual(result["outcome"], "enabled")
        self.assertTrue(result["normal_renewal_verified"])
        self.assertTrue(result["same_buyer_account"])
        self.assertFalse(result["checkout_created"])
        self.assertFalse(result["payment_submitted"])
        self.assertTrue(buyer.settings()["enabled"])
        self.assertEqual(self.controls(), before)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET enabled=0")
        with buyer.exclusive():
            self.assertIsNone(recovery.recover_linked_client(self.client))
        self.assertFalse(buyer.settings()["enabled"])

    def test_rejected_renewal_keeps_fresh_session_and_blocks_restart_retry(self):
        with closing(search_settings.connection()) as conn:
            original = conn.execute("SELECT session FROM vinted_buyer").fetchone()[0]
        with patch.object(
            buyer,
            "renew_saved_client",
            side_effect=buyer.BuyerError(
                buyer.AUTH_REASONS["renewal_failed"],
                400,
                reason="renewal_failed",
                stage="renewal",
            ),
        ) as renew, buyer.exclusive():
            result = recovery.recover_linked_client(self.client)
            self.assertIsNone(recovery.recover_linked_client(self.client))
        renew.assert_called_once()
        self.client.identity.assert_not_called()
        self.assertEqual(result["http_status"], 400)
        self.assertFalse(buyer.settings()["enabled"])
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute("SELECT session FROM vinted_buyer").fetchone()[0], original
            )
            raw = conn.execute(
                "SELECT value FROM parameters WHERE key=?", (recovery.RESULT,)
            ).fetchone()[0]
        for secret in ("private-access-token", "private-csrf"):
            self.assertNotIn(secret, raw)

    def test_different_buyer_never_enables(self):
        self.client.identity.return_value = ("100", "other")
        with patch.object(buyer, "renew_saved_client"), buyer.exclusive():
            result = recovery.recover_linked_client(self.client)
        self.assertEqual(result["reason"], "account_changed")
        self.assertFalse(buyer.settings()["enabled"])
        self.assertEqual(buyer.settings()["user_id"], "99")

    def test_startup_checks_current_account_instead_of_inferring_from_old_form_event(
        self,
    ):
        for event in (
            {"kind": "buyer_login", "reason": "connected", "checked": 2000},
            {"kind": "buyer_session", "reason": "connected", "checked": 999},
            {"kind": "buyer_session", "reason": "not_confirmed", "checked": 2000},
            {"kind": "buyer_session", "reason": "connected", "checked": True},
        ):
            with closing(search_settings.connection()) as conn, conn:
                conn.execute(
                    "INSERT OR REPLACE INTO parameters VALUES ('buyer_connection_result', ?)",
                    (json.dumps(event),),
                )
            with patch.object(
                buyer,
                "connected_client",
                side_effect=buyer.BuyerError(
                    buyer.AUTH_REASONS["credentials"],
                    401,
                    reason="credentials",
                    stage="identity",
                ),
            ) as connect, patch.object(recovery, "recover_linked_client") as recover:
                self.assertIsNone(recovery.run_if_ready())
            connect.assert_called_once_with(allow_refresh=False)
            recover.assert_not_called()

    def test_startup_recovers_just_submitted_owner_session_without_implicit_refresh(
        self,
    ):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO parameters VALUES ('buyer_connection_result', ?)",
                (
                    json.dumps(
                        {
                            "kind": "buyer_session",
                            "reason": "connected",
                            "checked": 2000,
                        }
                    ),
                ),
            )
        with patch.object(
            buyer, "connected_client", return_value=self.client
        ) as connect, patch.object(buyer, "renew_saved_client") as renew:
            result = recovery.run_if_ready()
        connect.assert_called_once_with(allow_refresh=False)
        renew.assert_called_once_with(self.client)
        self.assertEqual(result["outcome"], "enabled")
        self.client.session.close.assert_called_once()
