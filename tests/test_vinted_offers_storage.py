"""Real SQLite writer contention; Vinted traffic uses the offline offer fixture."""

import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

import test_vinted_offers as fixture
import search_settings
import vinted_offers as offers


class OfferStorageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.OfferTests(
            "test_busy_storage_before_account_access_waits_without_pausing"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.enable()
        self.job_id = self.fixture.add()
        self.blocker = None
        connect = search_settings.connection

        def immediate_lock_error():
            conn = connect()
            conn.execute("PRAGMA busy_timeout=0")
            return conn

        self.connections = patch.object(
            offers, "connection", side_effect=immediate_lock_error
        )
        self.connections.start()
        self.addCleanup(self.connections.stop)
        self.addCleanup(self.release_lock)

    def hold_lock(self):
        self.assertIsNone(self.blocker)
        self.blocker = search_settings.connection()
        self.blocker.execute("BEGIN IMMEDIATE")

    def release_lock(self):
        if self.blocker is not None:
            self.blocker.rollback()
            self.blocker.close()
            self.blocker = None

    def test_storage_lock_before_account_access_waits_then_recovers_without_pause(self):
        self.hold_lock()
        self.fixture.worker.tick()
        self.fixture.factory.assert_not_called()
        self.assertEqual(offers.state()["mode"], "live")
        self.assertEqual(self.fixture.jobs()[0]["status"], "queued")
        self.release_lock()
        self.fixture.worker.tick()
        self.assertEqual(len(self.fixture.client.writes()), 1)
        self.assertEqual(self.fixture.jobs()[0]["status"], "sent")

    def test_storage_lock_at_scan_commit_stops_tick_without_more_marketplace_calls(self):
        def lock_after_notifications(method, path, body):
            if path == offers.buyer.NOTIFICATIONS_PATH:
                self.hold_lock()

        self.fixture.client.fail = lock_after_notifications
        self.fixture.worker.tick()
        self.assertEqual(len(self.fixture.client.calls), 1)
        self.assertEqual(self.fixture.jobs()[0]["status"], "queued")
        self.assertEqual(offers.state()["mode"], "live")
        self.assertFalse(self.fixture.client.writes())

    def test_storage_lock_before_send_reservation_does_not_post_and_checking_recovers(self):
        def lock_after_checked_conversation(method, path, body):
            if method == "GET" and path == "/api/v2/conversations/7001":
                self.hold_lock()

        self.fixture.client.fail = lock_after_checked_conversation
        self.fixture.worker.tick()
        job = self.fixture.jobs()[0]
        self.assertEqual(job["status"], "checking")
        self.assertFalse(self.fixture.client.writes())
        self.assertEqual(offers.state()["mode"], "live")
        self.release_lock()
        self.fixture.client.fail = None
        self.fixture.now += offers.LEASE_SECONDS + 1
        self.fixture.worker.tick()
        self.assertEqual(len(self.fixture.client.writes()), 1)
        self.assertEqual(self.fixture.jobs()[0]["status"], "sent")

    def test_storage_lock_after_accepted_post_keeps_send_lease_and_never_replays(self):
        self.fixture.client.before_offer = self.hold_lock
        self.fixture.worker.tick()
        self.assertEqual(len(self.fixture.client.writes()), 1)
        self.assertEqual(self.fixture.jobs()[0]["status"], "sending")
        self.assertEqual(offers.state()["mode"], "live")
        calls = list(self.fixture.client.calls)
        self.release_lock()
        self.fixture.client.before_offer = None

        # Even before the durable lease expires, no identity, scan or offer
        # call may run while the accepted POST's local receipt is missing.
        self.fixture.worker.tick()
        self.assertEqual(self.fixture.client.calls, calls)
        self.assertEqual(self.fixture.factory.call_count, 1)
        self.fixture.now += offers.LEASE_SECONDS + 1
        self.fixture.worker.tick()
        self.assertEqual(self.fixture.jobs()[0]["status"], "uncertain")
        self.assertEqual(offers.state()["pause_reason"], "uncertain")
        restarted = offers.OfferWorker(
            client_factory=self.fixture.factory,
            clock=lambda: self.fixture.now,
            item_reader=self.fixture.item_read,
        )
        restarted.tick()
        self.assertEqual(self.fixture.client.calls, calls)
        self.assertEqual(len(self.fixture.client.writes()), 1)

    def test_storage_lock_after_sent_receipt_preserves_sent_and_reserved_spacing(self):
        finish = offers._finish

        def finish_then_lock(job_id, status, reason="", **fields):
            finish(job_id, status, reason, **fields)
            if status == "sent":
                self.hold_lock()

        with patch.object(offers, "_finish", side_effect=finish_then_lock):
            self.fixture.worker.tick()
        self.assertEqual(self.fixture.jobs()[0]["status"], "sent")
        self.assertGreater(offers.state()["next_send"], self.fixture.now)
        self.release_lock()
        self.fixture.now += offers.LEASE_SECONDS + 1
        self.fixture.worker.tick()
        self.assertEqual(self.fixture.jobs()[0]["status"], "sent")
        self.assertEqual(len(self.fixture.client.writes()), 1)
        self.assertEqual(offers.state()["mode"], "live")

    def test_pending_send_from_previous_worker_blocks_all_account_work(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_offer_jobs SET status='sending',lease_until=? WHERE id=?",
                (self.fixture.now + offers.LEASE_SECONDS, self.job_id),
            )
        self.fixture.worker.tick()
        self.fixture.factory.assert_not_called()
        self.assertFalse(self.fixture.client.calls)

    def test_existing_protocol_shape_pause_is_never_automatically_reopened(self):
        offers.pause("shape")
        self.fixture.now += offers.LEASE_SECONDS + 1
        self.fixture.worker.tick()
        self.fixture.factory.assert_not_called()
        self.assertEqual(offers.state()["pause_reason"], "shape")
        self.assertEqual(offers.state()["mode"], "paused")

    def test_non_busy_sqlite_error_remains_a_visible_fault_without_network(self):
        with patch.object(
            offers, "_recover", side_effect=sqlite3.OperationalError("disk I/O error")
        ), self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O error"):
            self.fixture.worker.tick()
        self.fixture.factory.assert_not_called()

    def test_only_busy_or_locked_base_codes_are_transient(self):
        for code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY | 256):
            with self.subTest(code=code):
                exc = sqlite3.OperationalError("fixed diagnostic")
                exc.sqlite_errorcode = code
                self.assertTrue(offers._storage_busy(exc))
        for code in (sqlite3.SQLITE_IOERR, sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_ERROR):
            with self.subTest(code=code):
                exc = sqlite3.OperationalError("database is locked")
                exc.sqlite_errorcode = code
                self.assertFalse(offers._storage_busy(exc))


if __name__ == "__main__":
    unittest.main()
