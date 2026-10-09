"""Real SQLite checks for cheaper polling without lost or delayed new alerts."""

import sqlite3
import threading
import unittest
from contextlib import closing
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock, patch

import dashboard_store
import db
import polling
import resource_controls
import search_settings
import vinted_keywords
from test_search_controls import DatabaseFixture


def listing(item_id):
    return SimpleNamespace(
        id=item_id, title="Hollister jacket", brand_title="Hollister",
        price="15", currency="GBP", photo=None,
        url=f"https://www.vinted.co.uk/items/{item_id}",
        has_real_timestamp=False, raw_timestamp=200, observed_at=200,
        is_new_item=lambda: True,
    )


class EfficientPollingTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE queries SET last_item=NULL WHERE id=1")
        self.queue, self.output = Queue(), Queue()
        with patch.object(polling, "ThreadPoolExecutor"):
            self.poller = polling.Poller(self.queue)

    def page(self, ids, now, scheduler_id=1, process=True):
        queries = vinted_keywords.expand(search_settings.active_queries())
        query = queries[scheduler_id]
        self.poller.processing_states = search_settings.processing_states()
        items = [listing(item_id) for item_id in ids]
        accepted = self.poller.process_page(scheduler_id, query[1], items, now)
        if accepted:
            self.queue.put((items, query[0], query[1], query[4] if len(query) > 4 else None))
            if process:
                self.core.clear_item_queue(self.queue, self.output)
        return accepted

    def test_unchanged_pages_are_reused_but_new_listing_alerts_immediately(self):
        self.assertTrue(self.page([100], 100))  # quiet baseline
        for now in range(101, 110):
            self.assertFalse(self.page([100], now))
        self.assertTrue(self.page([101, 100], 109.1))
        self.assertEqual(self.output.qsize(), 1)
        self.assertIn("/items/101", self.output.get()[1])
        self.assertFalse(self.page([101, 100], 110))
        self.assertTrue(self.page([101, 100], 119.1))  # freshness heartbeat
        self.assertTrue(self.output.empty())

    def test_failed_item_commit_is_retried_instead_of_cached_as_processed(self):
        self.page([100], 100)
        with patch.object(db, "add_item_to_db", side_effect=sqlite3.OperationalError("busy")):
            with self.assertRaises(sqlite3.OperationalError):
                self.page([101, 100], 101)
        self.assertTrue(self.page([101, 100], 102))
        self.assertEqual(self.output.qsize(), 1)
        self.assertIn("/items/101", self.output.get()[1])

    def test_pending_processing_keeps_new_pages_deliverable(self):
        self.page([100], 100)
        self.assertTrue(self.page([101, 100], 101, process=False))
        self.assertTrue(self.page([101, 100], 102, process=False))
        while not self.queue.empty():
            self.core.clear_item_queue(self.queue, self.output)
        self.assertEqual(self.output.qsize(), 1)

    def test_empty_first_page_does_not_silence_first_new_listing(self):
        self.assertTrue(self.page([], 100))
        self.assertFalse(self.page([], 101))
        self.assertTrue(self.page([101], 102))
        self.assertEqual(self.output.qsize(), 1)

    def test_edited_search_and_resumed_search_keep_their_quiet_baseline(self):
        self.page([100], 100)
        old = search_settings.get_search(1)
        dashboard_store.save_search(1, {
            "query_name": "Edited", "query": old["query"] + "&brand_ids[]=30",
            "revision": str(old["revision"]),
        })
        self.assertTrue(self.page([102, 100], 101))
        self.assertTrue(self.output.empty())
        self.assertTrue(self.page([103, 102, 100], 102))
        self.assertEqual(self.output.qsize(), 1)
        self.output.get()
        old = search_settings.get_search(1)
        dashboard_store.change_state(1, "pause", old["revision"])
        old = search_settings.get_search(1)
        dashboard_store.change_state(1, "resume", old["revision"])
        self.assertTrue(self.page([104, 103], 103))
        self.assertTrue(self.output.empty())

    def test_keyword_alternatives_keep_independent_baselines(self):
        old = search_settings.get_search(1)
        dashboard_store.save_search(1, {
            "query_name": "Keywords", "query": old["query"],
            "revision": str(old["revision"]), "vinted_keywords": "fur,sherpa",
        })
        first, second = vinted_keywords.rows(1)
        a, b = -first["id"], -second["id"]
        self.assertTrue(self.page([100], 100, a))
        self.assertFalse(self.page([100], 101, a))
        self.assertTrue(self.page([102], 102, b))  # own quiet baseline
        self.assertTrue(self.output.empty())
        self.assertTrue(self.page([103, 102], 103, b))
        self.assertEqual(self.output.qsize(), 1)

    def test_duplicate_transaction_does_not_block_next_item_in_shared_connection(self):
        with db.connection_scope():
            self.assertFalse(db.add_item_to_db(99, "Duplicate", 1, 10, 200, None, "GBP"))
            self.assertTrue(db.add_item_to_db(101, "New", 1, 10, 200, None, "GBP"))
        self.assertTrue(db.is_item_in_db_by_id(101))

    def test_shared_connection_closes_and_rolls_back_uncommitted_work(self):
        with self.assertRaises(RuntimeError):
            with db.connection_scope() as conn:
                with db.connection_scope() as nested:
                    self.assertIs(nested, conn)
                db.set_parameter("query_refresh_delay", "3")
                conn.execute("INSERT INTO parameters VALUES ('unfinished_test','discard')")
                raise RuntimeError("interrupted batch")
        self.assertEqual(db.get_parameter("query_refresh_delay"), "3")
        self.assertIsNone(db.get_parameter("unfinished_test"))
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
        with closing(db.get_db_connection()) as fresh:
            self.assertIsNot(fresh, conn)

    def test_batch_connection_is_not_shared_between_threads(self):
        used = []
        def worker():
            with db.connection_scope() as conn:
                used.append(conn)
                used.append(conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0])
        with db.connection_scope() as main:
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertIsNot(used[0], main)
            self.assertEqual(used[1], 44)

    def test_health_success_writes_are_sampled_and_failures_and_recovery_are_immediate(self):
        clock = [100.0]
        executor = SimpleNamespace(submit=lambda *args: SimpleNamespace(done=lambda: False))
        with patch.object(polling, "ThreadPoolExecutor", return_value=executor), patch.object(
            polling.time, "monotonic", side_effect=lambda: clock[0]
        ), patch.object(polling, "budget", polling.RequestBudget()), patch.object(
            search_settings, "record_health", wraps=search_settings.record_health
        ) as health:
            poller = polling.Poller(Queue())
            for now, error in [(100, None), (101, None), (102, None),
                               (103, ValueError("invalid response")), (104, None),
                               (105, None), (109, None)]:
                clock[0] = now
                future = Mock()
                future.done.return_value = True
                future.result.side_effect = error
                future.result.return_value = []
                future.search_url = search_settings.get_search(1)["query"]
                poller.pending = {1: (future, now - .1, 1000 + now, 1)}
                poller.tick()
            self.assertEqual(health.call_count, 4)
            self.assertEqual([call.args[-1] for call in health.call_args_list],
                             ["", "ValueError", "", ""])
        self.assertEqual(search_settings.health_rows()[0]["failures"], 0)

    def test_speed_choices_show_real_workload_and_do_not_change_saved_mode(self):
        resource_controls.save_rate("0")
        choices = resource_controls.summary()
        self.assertEqual([(o["name"], o["cycle"]) for o in choices["options"]],
                         [("Fast", 1), ("Balanced", 4.4), ("Budget", 8.8)])
        self.assertEqual(resource_controls.request_rate(), 0)


if __name__ == "__main__":
    unittest.main()
