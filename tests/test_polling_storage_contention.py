"""Advisory status writes cannot stop fast polling when SQLite is busy."""

import sqlite3
import time
import unittest
from concurrent.futures import Future
from contextlib import ExitStack, closing
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_search_controls import DatabaseFixture

import db
import polling
import search_settings


class PollingStorageContentionTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.clock = [100.0]
        self.executor = SimpleNamespace(submit=Mock(side_effect=lambda *a: Future()))
        self.stack.enter_context(patch.object(polling, "ThreadPoolExecutor", return_value=self.executor))
        self.stack.enter_context(patch.object(polling.time, "monotonic", side_effect=lambda: self.clock[0]))
        self.stack.enter_context(patch.object(polling, "budget", polling.RequestBudget()))
        self.queue = Queue()
        self.poller = polling.Poller(self.queue)
        self.poller.queries = {row[0]: row for row in search_settings.active_queries()[:3]}
        self.poller.config_checked = 1000
        self.poller.target = 1
        self.poller.request_rate = 0
        self.poller.last_health_write = 99
        self.poller.next_due = {1: 200, 2: 200, 3: 100}

    def complete(self, query_id=1, *, item=None):
        result = Future()
        result.set_result([] if item is None else [SimpleNamespace(id=item, is_new_item=lambda: True)])
        result.search_url = self.poller.queries[query_id][1]
        self.poller.pending[query_id] = (result, self.clock[0] - 0.1, 1000 + self.clock[0], 1)

    def rows(self):
        with closing(search_settings.connection()) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM search_health ORDER BY query_id")]

    def test_real_competing_writer_keeps_new_items_and_dispatch_immediate(self):
        self.complete(item=101)
        with closing(sqlite3.connect(db.DB_PATH, timeout=0.1)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            started = time.perf_counter()
            self.poller.tick()
            self.assertLess(time.perf_counter() - started, 0.3)
            self.assertEqual(self.queue.get_nowait()[0][0].id, 101)
            self.assertEqual(self.executor.submit.call_count, 1)
            self.assertEqual(self.executor.submit.call_args.args[1][0], 3)
            self.assertIn(1, self.poller.pending_health)
            self.assertNotIn(1, self.poller.health_written)
            self.assertEqual(self.poller.next_due[1], 100.9)
            self.assertEqual(self.rows(), [])
            writer.rollback()
        self.clock[0] = 101.1
        self.poller.tick()
        self.assertFalse(self.poller.pending_health)
        self.assertEqual(self.poller.health_written[1], 101.1)
        self.assertEqual(self.rows()[0]["last_attempt"], 1100)

    def test_urgent_results_do_not_bypass_one_second_retry_and_latest_wins(self):
        self.complete()
        with closing(sqlite3.connect(db.DB_PATH, timeout=0.1)) as writer, patch.object(
            search_settings, "connection", wraps=search_settings.connection,
        ) as opened:
            writer.execute("BEGIN IMMEDIATE")
            self.poller.tick()
            self.assertEqual(opened.call_count, 1)
            self.clock[0] = 100.2
            self.complete()
            self.poller.tick()
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(len(self.poller.pending_health), 1)
            writer.rollback()
            self.clock[0] = 101.1
            self.poller.tick()
            self.assertEqual(opened.call_count, 2)
        self.assertEqual(self.rows()[0]["last_attempt"], 1100.2)

    def test_busy_after_first_row_rolls_back_entire_batch_before_retry(self):
        self.complete(1)
        self.complete(2)
        record = search_settings.record_health

        def blocked_second(query_id, *args, **kwargs):
            record(query_id, *args, **kwargs)
            if query_id == 2:
                error = sqlite3.OperationalError("busy snapshot")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY | (2 << 8)
                raise error

        with patch.object(search_settings, "record_health", side_effect=blocked_second):
            self.poller.tick()
        self.assertEqual(self.rows(), [])
        self.assertEqual(set(self.poller.pending_health), {1, 2})
        self.assertEqual(self.poller.health_written, {})
        self.clock[0] = 101.1
        self.poller.tick()
        self.assertEqual([row["query_id"] for row in self.rows()], [1, 2])
        self.assertEqual(set(self.poller.health_written), {1, 2})

    def test_locked_extended_code_is_retried_but_other_sql_errors_are_visible(self):
        self.complete()
        error = sqlite3.OperationalError("shared cache contention")
        error.sqlite_errorcode = sqlite3.SQLITE_LOCKED | (1 << 8)
        with patch.object(search_settings, "record_health", side_effect=error):
            self.poller.tick()
        self.assertTrue(self.poller.pending_health)
        self.clock[0] = 101.1
        with patch.object(search_settings, "record_health", side_effect=sqlite3.OperationalError("no such table")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "no such table"):
                self.poller.tick()
        self.assertTrue(self.poller.pending_health)
        self.assertEqual(self.rows(), [])

    def test_removed_or_edited_queries_are_pruned_during_busy_recovery(self):
        self.complete()
        with patch.object(search_settings, "record_health", side_effect=sqlite3.OperationalError("database is locked")):
            self.poller.tick()
        self.assertTrue(self.poller.pending_health)
        self.clock[0] = 101.1
        self.poller.queries.pop(1)
        self.poller.tick()
        self.assertFalse(self.poller.pending_health)
        self.assertEqual(self.rows(), [])

    def test_advisory_timeout_is_restored_on_borrowed_connection_after_failure(self):
        self.complete()
        with db.connection_scope() as borrowed:
            borrowed.execute("PRAGMA busy_timeout=1234")
            with patch.object(search_settings, "record_health", side_effect=sqlite3.OperationalError("database is locked")):
                self.poller.tick()
            self.assertEqual(borrowed.execute("PRAGMA busy_timeout").fetchone()[0], 1234)
            self.assertFalse(borrowed.in_transaction)
        with closing(db.get_db_connection()) as critical:
            self.assertEqual(critical.execute("PRAGMA busy_timeout").fetchone()[0], 15000)
