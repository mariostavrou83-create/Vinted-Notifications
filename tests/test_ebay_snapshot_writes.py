"""Real SQLite snapshot deduplication and seller-deletion ordering checks."""

import json
import sqlite3
import threading
import unittest
from contextlib import closing
from unittest.mock import patch

from test_ebay_monitor import EbayFixture, item

import db
import ebay_monitor as monitor
import ebay_privacy as privacy
import search_settings


class SnapshotWriteTests(EbayFixture, unittest.TestCase):
    def traced_snapshot(self, search, rows, now):
        statements = []
        connect = sqlite3.connect

        def traced(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(db.sqlite3, "connect", side_effect=traced):
            result = self.snapshot(search, rows, now)
        return result, statements

    def test_unchanged_hundred_item_page_has_no_item_dml_and_new_item_still_alerts(self):
        search = self.enable()
        rows = [item(i, seller={"username": "seller-one"}) for i in range(1000, 1100)]
        self.snapshot(search, rows, 1000)
        result, sql = self.traced_snapshot(search, rows, 1020)
        self.assertEqual(result, 0)
        self.assertFalse(any(s.startswith(("INSERT INTO ebay_item_owners", "INSERT OR REPLACE INTO ebay_item_owners",
                                          "INSERT OR IGNORE INTO ebay_seen")) for s in sql))
        self.assertEqual(sum("SELECT value FROM parameters WHERE key='ebay_deletion_verification'" in s for s in sql), 1)
        self.assertEqual(sum(s.startswith("SELECT raw_id,item_id,seller_hash") for s in sql), 1)
        self.assertEqual(sum(s.startswith("SELECT digest FROM ebay_deleted_sellers") for s in sql), 1)
        self.assertEqual(sum(s.startswith("SELECT item_id FROM ebay_seen WHERE query_id=") for s in sql), 1)
        fresh = item(1100, created=1021, seller={"username": "seller-one"})
        self.assertEqual(self.snapshot(search, [fresh, *rows], 1022), 1)
        self.assertEqual([row["item_id"] for row in self.outbox()], ["ebay:1100"])

    def test_changed_owner_is_updated_even_when_item_was_already_seen(self):
        search = self.enable()
        self.snapshot(search, [item(123, seller={"username": "old-seller"})], 1000)
        self.assertEqual(self.snapshot(search, [item(123, seller={"username": "new-seller"})], 1020), 0)
        with closing(search_settings.connection()) as conn:
            salt = conn.execute("SELECT value FROM parameters WHERE key='ebay_deletion_verification'").fetchone()[0]
            saved = conn.execute("SELECT seller_hash FROM ebay_item_owners WHERE raw_id='v1|123|0'").fetchone()[0]
        self.assertEqual(saved, privacy.identifier_hash("new-seller", salt))

    def test_deleted_seller_is_checked_before_already_seen_shortcut(self):
        search = self.enable()
        self.snapshot(search, [item(123, seller={"username": "original"})], 1000)
        with closing(search_settings.connection()) as conn, conn:
            salt = conn.execute("SELECT value FROM parameters WHERE key='ebay_deletion_verification'").fetchone()[0]
            conn.execute("INSERT INTO ebay_deleted_sellers VALUES (?)", (privacy.identifier_hash("deleted", salt),))
            before = conn.execute("SELECT * FROM ebay_item_owners").fetchall()
        self.assertEqual(self.snapshot(search, [item(123, seller={"username": "deleted"})], 1020), 0)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(conn.execute("SELECT * FROM ebay_item_owners").fetchall(), before)
            flow = json.loads(conn.execute("SELECT value FROM parameters WHERE key LIKE 'ebay_flow:%:1'").fetchone()[0])
        self.assertEqual(flow["privacy_excluded"], 1)
        self.assertNotIn("already_seen", flow)

    def test_duplicate_identity_keeps_sequential_owner_updates_and_single_alert(self):
        search = self.enable()
        self.snapshot(search, [], 1000)
        rows = [item(123, seller={"username": "first"}), item(123, seller={"username": "last"})]
        self.assertEqual(self.snapshot(search, rows, 1020), 1)
        with closing(search_settings.connection()) as conn:
            salt = conn.execute("SELECT value FROM parameters WHERE key='ebay_deletion_verification'").fetchone()[0]
            owner = conn.execute("SELECT seller_hash FROM ebay_item_owners WHERE raw_id='v1|123|0'").fetchone()[0]
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ebay_seen WHERE item_id='v1|123|0'").fetchone()[0], 1)
        self.assertEqual(owner, privacy.identifier_hash("last", salt))
        self.assertEqual(len(self.outbox()), 1)

    def test_large_batch_uses_bounded_queries_and_preserves_all_baseline_ids(self):
        search = self.enable()
        rows = [item(i, seller={"username": f"seller-{i}"}) for i in range(2000, 3205)]
        self.snapshot(search, rows, 1000)
        result, sql = self.traced_snapshot(search, rows, 1020)
        self.assertEqual(result, 0)
        for prefix in ("SELECT raw_id,item_id,seller_hash", "SELECT digest FROM ebay_deleted_sellers",
                       "SELECT item_id FROM ebay_seen WHERE query_id="):
            self.assertEqual(sum(s.startswith(prefix) for s in sql), 3)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ebay_seen WHERE query_id=1").fetchone()[0], len(rows))

    def test_competing_snapshots_recheck_seen_under_the_same_writer_reservation(self):
        search = self.enable()
        self.snapshot(search, [], 1000)
        start = threading.Barrier(2)
        results, errors = [], []

        def snapshot():
            try:
                start.wait(2)
                results.append(monitor.record_snapshot(search, [item(123)], 1020))
            except Exception as exc:
                errors.append(exc)

        with patch.object(monitor.time, "time", return_value=1020):
            threads = [threading.Thread(target=snapshot) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [0, 1])
        self.assertEqual(len(self.outbox()), 1)

    def test_snapshot_failure_rolls_back_owner_seen_and_alert_together(self):
        search = self.enable()
        self.snapshot(search, [], 1000)
        with patch.object(monitor, "parse_item", side_effect=RuntimeError("injected")), self.assertRaises(RuntimeError):
            self.snapshot(search, [item(123, seller={"username": "seller"})], 1020)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ebay_item_owners").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ebay_seen").fetchone()[0], 0)
        self.assertEqual(self.outbox(), [])


if __name__ == "__main__":
    unittest.main()
