"""Offline safety and real contention checks for temporary writer telemetry."""

import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import db
import sqlite_write_audit as audit


class WriteAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = str(Path(self.temp.name) / "private-database.sqlite3")
        self.flag = patch.dict(os.environ, {"SQLITE_WRITE_AUDIT": "1"})
        self.flag.start()
        self.old_window, audit._window = audit._window, audit._Window()
        self.clock = [100.0]
        self.clock_patch = patch.object(audit, "_now", side_effect=lambda: self.clock[0])
        self.clock_patch.start()
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.executescript("CREATE TABLE parameters(key TEXT PRIMARY KEY,value TEXT);"
                               "INSERT INTO parameters VALUES ('test','old');")

    def tearDown(self):
        self.clock_patch.stop()
        audit._window = self.old_window
        self.flag.stop()
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def test_direct_legacy_write_and_cursor_capture_one_commit_without_values(self):
        with self.assertLogs(audit.logger, "INFO") as logged:
            with closing(db.get_db_connection()) as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE parameters SET value=? /* secret-sql */ WHERE key='test'",
                               ("private-token-value",))
                self.clock[0] += 0.25
                conn.commit()
            db.set_parameter("test", "legacy-private-value")
        self.assertEqual(len(logged.output), 1)
        line = logged.output[0]
        self.assertIn("table=parameters", line)
        self.assertIn("held_after_write_ms=250.000", line)
        for secret in ("private-token-value", "legacy-private-value", "secret-sql", db.DB_PATH, "UPDATE"):
            self.assertNotIn(secret, line)
        self.assertEqual(db.get_parameter("test"), "legacy-private-value")
        self.assertEqual(sum(r[0] for r in audit._window.rows.values()), 2)

    def test_connection_executemany_and_context_commit_are_measured_once(self):
        with self.assertLogs(audit.logger, "INFO") as logged:
            with db.connection_scope() as conn:
                with conn:
                    cursor = conn.executemany("INSERT INTO parameters VALUES (?,?)", [("a", "1"), ("b", "2")])
                    self.assertIsInstance(cursor, audit.AuditedCursor)
                    conn.close()  # Helper-owned close must defer to batch owner.
                    self.clock[0] += 0.2
        self.assertEqual(len(logged.output), 1)
        self.assertIn("outcome=commit", logged.output[0])
        self.assertEqual(db.get_parameter("a"), "1")
        self.assertEqual(db.get_parameter("b"), "2")

    def test_context_rollback_and_sql_commit_preserve_database_behavior(self):
        with closing(db.get_db_connection()) as conn:
            with self.assertLogs(audit.logger, "INFO") as logged:
                with self.assertRaisesRegex(RuntimeError, "original"):
                    with conn:
                        conn.execute("UPDATE parameters SET value='discard' WHERE key='test'")
                        self.clock[0] += 0.2
                        raise RuntimeError("original")
            self.assertIn("outcome=rollback", logged.output[0])
            self.assertIsNone(conn._audit_tx)
            conn.execute("UPDATE parameters SET value='accepted' WHERE key='test'")
            conn.execute("COMMIT")
            self.assertIsNone(conn._audit_tx)
        self.assertEqual(db.get_parameter("test"), "accepted")

    def test_explicit_begin_tracks_post_acquisition_hold_separately(self):
        with closing(db.get_db_connection()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.clock[0] += 0.25
            conn.execute("UPDATE parameters SET value='new' WHERE key='test'")
            with self.assertLogs(audit.logger, "INFO") as logged:
                conn.commit()
        line = logged.output[0]
        self.assertIn("first_write_ms=0.000", line)
        self.assertIn("held_after_write_ms=250.000", line)
        self.assertIn("table=parameters", line)
        self.assertIn("first_op=begin", line)

    def test_disabled_shared_connection_uses_native_cursors_without_audit_clock(self):
        with patch.dict(os.environ, {"SQLITE_WRITE_AUDIT": "0"}), patch.object(
            audit, "_now", side_effect=AssertionError("disabled clock called")
        ) as clock:
            with db.connection_scope() as conn:
                with conn:
                    cursor = conn.execute("UPDATE parameters SET value='off' WHERE key='test'")
                    self.assertNotIsInstance(cursor, audit.AuditedCursor)
                    self.assertNotIsInstance(conn.cursor(), audit.AuditedCursor)
                    conn.executemany("INSERT INTO parameters VALUES (?,?)", [("off", "1")])
                conn.rollback()
            clock.assert_not_called()
        self.assertEqual(db.get_parameter("test"), "off")

    def test_logging_occurs_after_release_and_script_does_not_log_before_commit(self):
        def log_after_release(*args):
            with closing(sqlite3.connect(db.DB_PATH, timeout=0)) as observer:
                observer.execute("BEGIN IMMEDIATE")
                observer.rollback()

        with closing(db.get_db_connection()) as conn:
            conn.execute("UPDATE parameters SET value='committed' WHERE key='test'")
            self.clock[0] += 0.2
            with patch.object(audit.logger, "info", side_effect=log_after_release) as logger:
                conn.commit()
                self.assertEqual(logger.call_count, 1)
            conn.execute("UPDATE parameters SET value='script' WHERE key='test'")
            self.clock[0] += 0.2
            with patch.object(audit.logger, "info") as logger:
                conn.executescript("INSERT INTO parameters VALUES ('script','accepted');")
                logger.assert_not_called()
        self.assertEqual(db.get_parameter("test"), "script")

    def test_real_clock_failure_cannot_prevent_or_undo_writes(self):
        self.clock_patch.stop()
        try:
            with patch.object(audit.time, "monotonic", side_effect=RuntimeError("clock")):
                with closing(db.get_db_connection()) as conn, conn:
                    conn.execute("UPDATE parameters SET value='clock-safe' WHERE key='test'")
            self.assertEqual(db.get_parameter("test"), "clock-safe")
        finally:
            self.clock_patch.start()

    def test_diagnostic_failures_never_undo_success_or_replace_sql_errors(self):
        with closing(db.get_db_connection()) as conn:
            with patch.object(audit, "_source", side_effect=ValueError("diagnostic")):
                conn.execute("UPDATE parameters SET value='new' WHERE key='test'")
                conn.commit()
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("UPDATE missing_table SET value='private'")
            conn.execute("UPDATE parameters SET value='newer' WHERE key='test'")
            self.clock[0] += 0.2
            with patch.object(audit.logger, "info", side_effect=ValueError("logger")):
                conn.commit()
        self.assertEqual(db.get_parameter("test"), "newer")

    def test_rate_bound_summary_and_automatic_ten_minute_expiry(self):
        with closing(db.get_db_connection()) as conn:
            with self.assertLogs(audit.logger, "INFO") as logged:
                for _ in range(8):
                    conn.execute("UPDATE parameters SET value='new' WHERE key='test'")
                    self.clock[0] += 0.11
                    conn.commit()
                self.clock[0] = 161
                conn.execute("UPDATE parameters SET value='summary' WHERE key='test'")
                conn.commit()
            self.assertEqual(len(logged.output), 6)  # Five slow lines + one summary.
            self.assertIn("transactions=9 suppressed=3", logged.output[-1])
            self.clock[0] = 701
            with patch.object(audit.logger, "info") as logger:
                conn.execute("UPDATE parameters SET value='after-expiry' WHERE key='test'")
                conn.commit()
                logger.assert_not_called()
            self.assertFalse(conn._audit_enabled)
            self.assertFalse(audit.enabled())
        with closing(db.get_db_connection()) as conn:
            self.assertNotIsInstance(conn, audit.AuditedConnection)
        self.assertEqual(db.get_parameter("test"), "after-expiry")

    def test_failed_commit_keeps_transaction_until_explicit_rollback(self):
        with closing(db.get_db_connection()) as conn:
            conn.executescript("CREATE TABLE parent(id INTEGER PRIMARY KEY);"
                               "CREATE TABLE child(id REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED);")
            conn.execute("INSERT INTO child VALUES (1)")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.commit()
            self.assertTrue(conn.in_transaction)
            self.assertIsNotNone(conn._audit_tx)
            conn.rollback()
            self.assertIsNone(conn._audit_tx)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM child").fetchone()[0], 0)

    def test_unknown_table_names_are_replaced_with_fixed_other_label(self):
        with closing(db.get_db_connection()) as conn:
            conn.execute("CREATE TABLE secret_customer_name(value)")
            conn.execute("INSERT INTO secret_customer_name VALUES ('secret@example.test')")
            self.clock[0] += 0.2
            with self.assertLogs(audit.logger, "INFO") as logged:
                conn.commit()
        self.assertIn("table=other", logged.output[0])
        self.assertNotIn("secret", logged.output[0])

    def test_real_writer_wait_is_not_reported_as_transaction_hold(self):
        self.clock_patch.stop()
        audit._window = audit._Window()
        entered, release = threading.Event(), threading.Event()
        failures = []

        def writer():
            try:
                with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
                    conn.execute("UPDATE parameters SET value='owner' WHERE key='test'")
                    entered.set()
                    if not release.wait(2):
                        raise AssertionError("release timed out")
            except Exception as exc:
                failures.append(exc)
                entered.set()

        thread = threading.Thread(target=writer)
        thread.start()
        timer = None
        try:
            self.assertTrue(entered.wait(2))
            with closing(db.get_db_connection()) as conn:
                timer = threading.Timer(0.2, release.set)
                timer.start()
                with self.assertLogs(audit.logger, "INFO") as logged:
                    conn.execute("UPDATE parameters SET value='buyer' WHERE key='test'")
                    self.assertGreater(conn._audit_tx["first_ms"], 100)
                    conn.commit()
            line = logged.output[0]
            held = float(line.split("held_after_write_ms=")[1].split()[0])
            self.assertLess(held, 100)
            self.assertIn("outcome=commit", line)
        finally:
            release.set()
            thread.join(2)
            if timer:
                timer.cancel()
        self.assertEqual(failures, [])
        self.clock_patch.start()


if __name__ == "__main__":
    unittest.main()
