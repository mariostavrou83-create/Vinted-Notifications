"""Owner settings save waits for background account work; no marketplace calls."""

import fcntl
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import db
import vinted_buyer as buyer
import vinted_buyer_priority as priority


class BuyerSettingsLockTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = str(Path(self.directory.name) / "buyer.sqlite3")
        self.db_patch = patch.object(db, "DB_PATH", path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        with closing(sqlite3.connect(path)) as conn, conn:
            buyer.migrate(conn)
            conn.execute("UPDATE vinted_buyer SET session=?,user_id='111',verified_at=100,"
                         "enabled=0,max_total=7000,max_extra=500,pickup_mode='nearest',"
                         "preferred_card_last4='4242' WHERE id=1", (b"existing-session",))
        self.form = {
            "buyer_enabled": "yes", "buyer_pickup_mode": "nearest", "buyer_card_last4": "4242",
            "buyer_browser_info": json.dumps({"color_depth":24,"java_enabled":False,
                "language":"en-GB","screen_height":900,"screen_width":1440,"timezone_offset":0}),
        }
        self.settings_patch = patch.object(buyer, "settings", return_value={
            "connected":True,"pickup_mode":"nearest","preferred_card_last4":"4242"})
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.holder = os.open(Path(self.directory.name) / "vinted-buyer.lock", os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, self.holder)
        fcntl.flock(self.holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def row(self):
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            return dict(conn.execute("SELECT * FROM vinted_buyer WHERE id=1").fetchone())

    def test_save_yields_background_work_and_preserves_existing_connection_and_limits(self):
        blocked, done = threading.Event(), threading.Event()
        errors = []
        real_flock = fcntl.flock
        held_inode = os.fstat(self.holder).st_ino

        def observed_flock(fd, operation):
            try:
                return real_flock(fd, operation)
            except BlockingIOError:
                if operation == (fcntl.LOCK_EX | fcntl.LOCK_NB) and os.fstat(fd).st_ino == held_inode:
                    blocked.set()
                raise

        def save():
            try:
                buyer.save_limits(self.form)
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        before = self.row()
        with patch.object(buyer.fcntl, "flock", side_effect=observed_flock):
            thread = threading.Thread(target=save)
            thread.start()
            try:
                self.assertTrue(blocked.wait(2), "The test must encounter a held background lock")
                self.assertTrue(priority.purchase_waiting(), "Background workers must see the owner's wait")
                self.assertFalse(done.is_set())
            finally:
                real_flock(self.holder, fcntl.LOCK_UN)
                thread.join(2)
        self.assertTrue(done.is_set())
        self.assertEqual(errors, [])
        after = self.row()
        self.assertEqual(after["enabled"], 1)
        for field in ("session", "user_id", "verified_at", "max_total", "max_extra",
                      "pickup_mode", "preferred_card_last4"):
            self.assertEqual(after[field], before[field], field)
        self.assertFalse(priority.purchase_waiting())

    def test_wait_timeout_changes_no_settings_and_releases_the_owner_marker(self):
        before = self.row()
        with patch.object(buyer.time, "monotonic", side_effect=[0, 6]):
            with self.assertRaises(buyer.BuyerError) as raised:
                buyer.save_limits(self.form)
        self.assertEqual(raised.exception.reason, "busy")
        self.assertEqual(self.row(), before)
        self.assertFalse(priority.purchase_waiting())


if __name__ == "__main__":
    unittest.main()
