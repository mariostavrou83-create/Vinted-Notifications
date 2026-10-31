"""An approved incident window cannot reopen unrelated pauses or offers."""

import os
import unittest
from contextlib import closing
from unittest.mock import patch

from test_search_controls import DatabaseFixture

import search_settings
import vinted_offer_storage_recovery as recovery
import vinted_offers as offers

NOW = 2000000
WINDOW = [NOW - 110, NOW - 90]


class StorageIncidentRecoveryTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET user_id='111',session=?,verified_at=? WHERE id=1", (b"offline-session", NOW - 10))
            conn.execute("UPDATE vinted_offer_settings SET account_id='111',mode='paused',pause_reason='shape',rounding='half_pound',history='three_months',next_send=?,cooldown_until=? WHERE id=1", (NOW + 180, NOW + 60))
            offers.event(conn, "live", now=NOW - 200)
            offers.event(conn, "paused", "shape", now=NOW - 100)

    def test_only_approved_incident_recovers_once_and_preserves_limits(self):
        before = offers.state()
        self.assertEqual(recovery.run_once(window=WINDOW, now=NOW), "applied")
        after = offers.state()
        self.assertEqual(after["mode"], "live")
        self.assertEqual(after["pause_reason"], "")
        for key in ("account_id", "rounding", "history", "since", "next_send", "cooldown_until", "scan_page", "scan_watermark"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(recovery.run_once(window=WINDOW, now=NOW), "already_applied")
        with closing(search_settings.connection()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM vinted_offer_events WHERE action='resumed'").fetchone()[0], 1)

    def test_manual_auth_or_new_shape_pause_is_preserved(self):
        for reason in ("", "auth", "restricted", "shape"):
            with self.subTest(reason=reason):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("UPDATE vinted_offer_settings SET mode='paused',pause_reason=? WHERE id=1", (reason,))
                    offers.event(conn, "paused", reason, now=NOW - 20)
                before = offers.state()
                self.assertEqual(recovery.run_once(window=WINDOW, now=NOW), "state_changed_or_unverified")
                self.assertEqual(offers.state(), before)

    def test_changed_or_unverified_account_is_not_resumed(self):
        for user, verified in (("222", NOW - 10), ("111", NOW - 150), ("111", None)):
            with self.subTest(user=user, verified=verified):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("UPDATE vinted_buyer SET user_id=?,verified_at=? WHERE id=1", (user, verified))
                self.assertEqual(recovery.run_once(window=WINDOW, now=NOW), "state_changed_or_unverified")
                self.assertEqual(offers.state()["mode"], "paused")

    def add_uncertain(self, status):
        with closing(search_settings.connection()) as conn, conn:
            return conn.execute("INSERT INTO vinted_offer_jobs(account_id,item_id,recipient_id,notification_id,liked_at,created,status) VALUES ('111','123','456','offline',?,?,?)", (NOW - 1000, NOW - 500, status)).lastrowid

    def test_current_sending_or_unrecorded_uncertainty_blocks_recovery(self):
        for status in ("sending", "uncertain"):
            with self.subTest(status=status):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("DELETE FROM vinted_offer_jobs")
                job = self.add_uncertain(status)
                self.assertEqual(recovery.run_once(window=WINDOW, now=NOW), "uncertain_offer")
                with closing(search_settings.connection()) as conn:
                    self.assertEqual(conn.execute("SELECT status FROM vinted_offer_jobs WHERE id=?", (job,)).fetchone()[0], status)

    def test_previously_acknowledged_quarantine_is_never_requeued(self):
        job = self.add_uncertain("uncertain")
        with closing(search_settings.connection()) as conn, conn:
            # The explicit owner resume follows this old uncertain receipt.
            offers.event(conn, "uncertain", "uncertain", job, now=NOW - 500)
            offers.event(conn, "live", now=NOW - 200)
            offers.event(conn, "paused", "shape", now=NOW - 100)
        self.assertEqual(recovery.run_once(window=WINDOW, now=NOW), "applied")
        with closing(search_settings.connection()) as conn:
            self.assertEqual(conn.execute("SELECT status FROM vinted_offer_jobs WHERE id=?", (job,)).fetchone()[0], "uncertain")

    def test_absent_invalid_future_or_overbroad_configuration_does_nothing(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(recovery.run_once(now=NOW), "not_requested")
        for window in (True, [], [False, NOW], [NOW, NOW + 10], [NOW - 100, NOW], [NOW - 100000, NOW - 99950], [float("nan"), NOW]):
            self.assertEqual(recovery.run_once(window=window, now=NOW), "invalid_window")
        self.assertEqual(offers.state()["mode"], "paused")
