"""Bounded freshness commits preserve frontier, baseline and outage decisions."""

import sqlite3
import unittest
from contextlib import closing, contextmanager
from queue import Queue
from unittest.mock import patch

from test_search_controls import DatabaseFixture

import db
import frontier_heartbeats as heartbeats
import search_settings
import vinted_keywords


class HeartbeatTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        heartbeats._current()
        self.clock = 0.0
        self.timer = patch.object(heartbeats.time, "monotonic", side_effect=lambda: self.clock)
        self.timer.start()
        self.addCleanup(self.timer.stop)

    def seed(self, query_id, maximum=100, checked=1000):
        search_settings.remember_listing_frontier(query_id, [maximum], checked)

    def checked(self, query_id):
        with closing(search_settings.connection()) as conn:
            return tuple(conn.execute(
                "SELECT max_item_id,last_check FROM listing_frontiers WHERE query_id=?",
                (query_id,),
            ).fetchone())

    def empty(self, query_id, now, variant=None):
        source, output = Queue(), Queue()
        search = search_settings.get_search(query_id)
        source.put(([], query_id, variant["url"] if variant else search["query"],
                    variant["id"] if variant else None))
        with patch.object(self.core, "time", return_value=now):
            self.core.clear_item_queue(source, output)
        self.assertTrue(output.empty())

    @contextmanager
    def traced(self):
        statements = []
        connect = sqlite3.connect

        def opened(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(db.sqlite3, "connect", side_effect=opened):
            yield statements

    def variants(self):
        with closing(search_settings.connection()) as conn, conn:
            vinted_keywords.save(conn, 1, search_settings.get_search(1)["query"], ["fur", "lined"])
            conn.execute(
                "UPDATE vinted_keyword_variants SET primed=1,last_item=100,max_item_id=100,last_check=1000"
            )
            conn.execute("INSERT OR REPLACE INTO search_dashboard(query_id,rebaseline) VALUES (1,1)")
        return vinted_keywords.rows(1)

    def test_twenty_searches_share_one_idle_commit_with_full_durability(self):
        for query_id in range(1, 21):
            self.seed(query_id)
        with self.traced() as statements:
            for query_id in range(1, 21):
                self.empty(query_id, 1100 + query_id)
            self.assertEqual(sum(sql == "COMMIT" for sql in statements), 0)
            self.assertEqual(self.checked(1), (100, 1000))
            self.clock = 0.51
            self.core.clear_item_queue(Queue(), Queue())
        self.assertEqual(sum(sql == "COMMIT" for sql in statements), 1)
        self.assertEqual(self.checked(1), (100, 1101))
        self.assertEqual(self.checked(20), (100, 1120))
        self.assertFalse(heartbeats._pending)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_cap_flushes_before_accepting_twenty_first_heartbeat(self):
        for query_id in range(1, 22):
            self.seed(query_id)
        with self.traced() as statements:
            for query_id in range(1, 22):
                self.empty(query_id, 1100 + query_id)
        self.assertEqual(sum(sql == "COMMIT" for sql in statements), 1)
        self.assertEqual(len(heartbeats._pending), 1)
        self.assertEqual(self.checked(20), (100, 1120))
        self.assertEqual(self.checked(21), (100, 1000))

    def test_sibling_variants_and_exact_key_flush_keep_all_freshness(self):
        self.seed(1)
        variants = self.variants()
        for index, variant in enumerate(variants):
            self.empty(1, 1100 + index, variant)
        self.assertEqual(len(heartbeats._pending), 2)
        with self.traced() as statements:
            heartbeats.flush(query_id=1, variant_id=variants[0]["id"])
            self.assertEqual(len(heartbeats._pending), 1)
            heartbeats.flush()
        self.assertEqual(sum(sql == "COMMIT" for sql in statements), 2)
        self.assertEqual(self.checked(1), (100, 1101))
        current = vinted_keywords.rows(1)
        self.assertEqual([row["last_check"] for row in current], [1100, 1101])
        self.assertTrue(all(row["max_item_id"] == row["last_item"] == 100 for row in current))
        # Validate all siblings before this batch updates their shared parent.
        for index, variant in enumerate(current):
            self.empty(1, 1150 + index, variant)
        with self.traced() as statements:
            heartbeats.flush()
        self.assertEqual(sum(sql == "COMMIT" for sql in statements), 1)
        self.assertEqual([row["last_check"] for row in vinted_keywords.rows(1)], [1150, 1151])

    def test_failed_flush_rolls_back_all_rows_and_retains_retry(self):
        for query_id in (1, 2):
            self.seed(query_id)
            self.empty(query_id, 1100)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("""CREATE TRIGGER fail_heartbeat BEFORE UPDATE ON listing_frontiers
                WHEN NEW.query_id=2 BEGIN SELECT RAISE(ABORT,'injected'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            heartbeats.flush()
        self.assertEqual(len(heartbeats._pending), 2)
        self.assertEqual(self.checked(1), (100, 1000))
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DROP TRIGGER fail_heartbeat")
        heartbeats.flush()
        self.assertEqual(self.checked(1), (100, 1100))
        self.assertEqual(self.checked(2), (100, 1100))

    def test_edits_and_recreated_frontiers_cannot_receive_old_heartbeat(self):
        self.seed(1)
        self.seed(2)
        self.empty(1, 1100)
        self.empty(2, 1100)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO search_dashboard(query_id,revision) VALUES (1,1)")
            conn.execute("DELETE FROM listing_frontiers WHERE query_id=2")
            conn.execute("INSERT INTO listing_frontiers VALUES (2,100,1200)")
        heartbeats.flush()
        self.assertEqual(self.checked(1), (100, 1000))
        self.assertEqual(self.checked(2), (100, 1200))
        self.assertFalse(heartbeats._pending)

    def test_nonempty_batch_flushes_before_global_outage_cutoff_and_advances_immediately(self):
        self.seed(1, maximum=500)
        self.seed(2, maximum=100)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE listing_frontiers SET last_check=3500 WHERE query_id=2")
        self.empty(1, 2300)
        with closing(search_settings.connection()) as conn:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM listing_checkpoints WHERE minute=?", (int(2300 / 60),)
            ).fetchone())
        floor = search_settings.listing_cutoff
        observed = []

        def checked_floor(query_id, item_ids, now):
            with closing(search_settings.connection()) as conn:
                self.assertIsNotNone(conn.execute(
                    "SELECT 1 FROM listing_checkpoints WHERE minute=?", (int(2300 / 60),)
                ).fetchone())
            observed.append(True)
            return floor(query_id, item_ids, now)

        # The buffered checkpoint is now exactly across the twenty-minute
        # boundary, while this particular search is fresh and can alert.
        with patch.object(self.core, "time", return_value=3501), patch.object(
            search_settings, "listing_cutoff", side_effect=checked_floor
        ):
            self.assertEqual(len(self.batch(2, [600])), 1)
        self.assertEqual(observed, [True])
        self.assertFalse(heartbeats._pending)
        self.assertEqual(self.checked(2), (600, 3501))

    def test_empty_first_baseline_is_immediate_and_next_same_key_flushes_before_read(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE queries SET last_item=NULL WHERE id=3")
        self.empty(3, 1100)
        self.assertEqual(self.checked(3), (0, 1100))
        self.assertFalse(heartbeats._pending)
        self.empty(3, 1150)
        self.assertEqual(self.checked(3), (0, 1100))
        self.empty(3, 1200)
        self.assertEqual(self.checked(3), (0, 1150))
        self.assertEqual(len(heartbeats._pending), 1)


if __name__ == "__main__":
    unittest.main()
