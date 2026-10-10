"""Real SQLite checks for cheaper polling without lost or delayed new alerts."""

import sqlite3
import threading
import unittest
from contextlib import closing
from itertools import pairwise
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_search_controls import DatabaseFixture

import dashboard_store
import db
import polling
import resource_controls
import search_settings
import vinted_keywords


def listing(item_id):
    return SimpleNamespace(
        id=item_id,
        title="Hollister jacket",
        brand_title="Hollister",
        price="15",
        currency="GBP",
        photo=None,
        url=f"https://www.vinted.co.uk/items/{item_id}",
        has_real_timestamp=False,
        raw_timestamp=200,
        observed_at=200,
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
            self.queue.put(
                (items, query[0], query[1], query[4] if len(query) > 4 else None)
            )
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
        with patch.object(
            db, "add_item_to_db", side_effect=sqlite3.OperationalError("busy")
        ), self.assertRaises(sqlite3.OperationalError):
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
        dashboard_store.save_search(
            1,
            {
                "query_name": "Edited",
                "query": old["query"] + "&brand_ids[]=30",
                "revision": str(old["revision"]),
            },
        )
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
        dashboard_store.save_search(
            1,
            {
                "query_name": "Keywords",
                "query": old["query"],
                "revision": str(old["revision"]),
                "vinted_keywords": "fur,sherpa",
            },
        )
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
            self.assertFalse(
                db.add_item_to_db(99, "Duplicate", 1, 10, 200, None, "GBP")
            )
            self.assertTrue(db.add_item_to_db(101, "New", 1, 10, 200, None, "GBP"))
        self.assertTrue(db.is_item_in_db_by_id(101))

    def test_shared_connection_closes_and_rolls_back_uncommitted_work(self):
        with self.assertRaises(RuntimeError), db.connection_scope() as conn:
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

    def test_health_success_writes_are_sampled_and_failures_and_recovery_are_immediate(
        self,
    ):
        clock = [100.0]
        executor = SimpleNamespace(
            submit=lambda *args: SimpleNamespace(done=lambda: False)
        )
        with patch.object(
            polling, "ThreadPoolExecutor", return_value=executor
        ), patch.object(
            polling.time, "monotonic", side_effect=lambda: clock[0]
        ), patch.object(
            polling, "budget", polling.RequestBudget()
        ), patch.object(
            search_settings, "record_health", wraps=search_settings.record_health
        ) as health:
            poller = polling.Poller(Queue())
            for now, error in [
                (100, None),
                (101, None),
                (102, None),
                (103, ValueError("invalid response")),
                (104, None),
                (105, None),
                (109, None),
            ]:
                clock[0] = now
                future = Mock()
                future.done.return_value = True
                future.result.side_effect = error
                future.result.return_value = []
                future.search_url = search_settings.get_search(1)["query"]
                poller.pending = {1: (future, now - 0.1, 1000 + now, 1)}
                poller.tick()
            self.assertEqual(health.call_count, 4)
            self.assertEqual(
                [call.args[-1] for call in health.call_args_list],
                ["", "ValueError", "", ""],
            )
        self.assertEqual(search_settings.health_rows()[0]["failures"], 0)

    def test_speed_choices_show_real_workload_and_do_not_change_saved_mode(self):
        resource_controls.save_rate("0")
        choices = resource_controls.summary()
        self.assertEqual(
            [(o["name"], o["cycle"]) for o in choices["options"]],
            [("Fast", 1), ("Balanced", 4.4), ("Budget", 8.8)],
        )
        self.assertEqual(resource_controls.request_rate(), 0)


class CatalogueHeaderBatchTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from pyVintedVN.requester import Requester

        self.requester = Requester()

    def tearDown(self):
        self.requester.session.close()
        super().tearDown()

    def test_each_locale_update_reads_fresh_headers_with_one_closed_connection(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO parameters VALUES ('user_agents', ?)",
                ('["Fresh catalogue agent"]',),
            )
        db.set_parameter("default_headers", '{"Locale":"cy-GB","X-Test":"new"}')
        opened = []
        connect = sqlite3.connect

        def counted_connect(*args, **kwargs):
            conn = connect(*args, **kwargs)
            opened.append(conn)
            return conn

        with patch.object(
            db.sqlite3, "connect", side_effect=counted_connect
        ), patch.object(self.requester.session, "get") as get, patch.object(
            self.requester.session, "head"
        ) as head:
            self.requester.set_locale("www.vinted.co.uk")
        self.assertEqual(len(opened), 1)
        self.assertEqual(self.requester.HEADER["User-Agent"], "Fresh catalogue agent")
        self.assertEqual(self.requester.session.headers["X-Test"], "new")
        self.assertEqual(self.requester.VINTED_AUTH_URL, "https://www.vinted.co.uk/")
        get.assert_not_called()
        head.assert_not_called()
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

        # A repeated locale must still see the owner's latest configuration.
        db.set_parameter("default_headers", '{"Locale":"en-GB","X-Test":"newer"}')
        self.requester.set_locale("www.vinted.co.uk")
        self.assertEqual(self.requester.session.headers["X-Test"], "newer")

    def test_failed_second_read_closes_connection_and_preserves_previous_headers(self):
        previous = dict(self.requester.HEADER)
        opened = []
        connect = sqlite3.connect

        def counted_connect(*args, **kwargs):
            conn = connect(*args, **kwargs)
            opened.append(conn)
            return conn

        with patch.object(
            db.sqlite3, "connect", side_effect=counted_connect
        ), patch.object(
            db,
            "get_parameter",
            side_effect=['["new agent"]', sqlite3.OperationalError("busy")],
        ), self.assertRaises(
            sqlite3.OperationalError
        ):
            self.requester.set_locale("www.vinted.co.uk")
        self.assertEqual(self.requester.HEADER, previous)
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")
        # Failure must not leak a thread-local batch into the next refresh.
        self.requester.set_locale("www.vinted.co.uk")
        self.assertEqual(self.requester.HEADER, previous)


class ScaledPollingCapacityTests(DatabaseFixture, unittest.TestCase):
    def test_250_searches_stay_fair_bounded_and_nonoverlapping_at_varied_latencies(
        self,
    ):
        with closing(search_settings.connection()) as conn, conn:
            conn.executemany(
                "INSERT INTO queries VALUES (?,?,100,?)",
                [
                    (
                        i,
                        f"https://www.vinted.co.uk/catalog?search_text=test{i}",
                        f"Search {i}",
                    )
                    for i in range(45, 251)
                ],
            )
        for duration, rate, seconds in (
            (0.1, 0, 20),
            (0.24, 0, 20),
            (0.5, 0, 30),
            (0.24, 10, 60),
        ):
            with self.subTest(fetch_seconds=duration, rate=rate):
                clock, starts, active = [100.0], [], set()
                queue = Queue(maxsize=64)
                resource_controls.save_rate(str(rate))
                db.set_parameter("query_refresh_delay", "1")
                test = self

                class FakeFuture:
                    def __init__(
                        self,
                        query,
                        started,
                        clock=clock,
                        duration=duration,
                        active=active,
                    ):
                        self.query, self.started = query, started
                        self.clock, self.duration, self.active = clock, duration, active

                    def done(self):
                        return self.clock[0] >= self.started + self.duration

                    def result(self):
                        self.active.remove(self.query[0])
                        return []

                def submit(
                    _fn,
                    query,
                    _count,
                    test=test,
                    active=active,
                    starts=starts,
                    clock=clock,
                    future_class=FakeFuture,
                ):
                    test.assertNotIn(query[0], active)
                    active.add(query[0])
                    starts.append((query[0], clock[0]))
                    return future_class(query, clock[0])

                with patch.object(
                    polling,
                    "ThreadPoolExecutor",
                    return_value=SimpleNamespace(submit=submit),
                ), patch.object(
                    polling.time, "monotonic", side_effect=lambda clock=clock: clock[0]
                ), patch.object(
                    polling, "budget", polling.RequestBudget()
                ), patch.object(
                    polling.logger, "info"
                ):
                    poller = polling.Poller(queue, workers=12)
                    for step in range(int(seconds / 0.025)):
                        clock[0] = 100 + step * 0.025
                        poller.tick()
                        self.assertLessEqual(len(active), 12)
                        self.assertLessEqual(len(poller.pending), 12)
                        while not queue.empty():
                            queue.get_nowait()
                per_query = {q: [] for q in range(1, 251)}
                for query_id, started in starts:
                    per_query[query_id].append(started)
                self.assertTrue(all(len(times) >= 2 for times in per_query.values()))
                self.assertTrue(
                    all(
                        b - a >= 0.999
                        for times in per_query.values()
                        for a, b in pairwise(times)
                    )
                )
                if rate:
                    self.assertLessEqual(len(starts), rate * seconds)
                    self.assertTrue(
                        all(
                            b[1] - a[1] >= 1 / rate - 0.0001
                            for a, b in pairwise(starts)
                        )
                    )


if __name__ == "__main__":
    unittest.main()
