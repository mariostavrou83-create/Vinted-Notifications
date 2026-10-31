"""Real SQLite checks for fewer writer reservations without lost alert state."""

import sqlite3
import time
import unittest
from contextlib import closing, contextmanager
from queue import Queue
from unittest.mock import patch

from test_polling_efficiency import listing
from test_search_controls import DatabaseFixture

import db
import search_settings
import vinted_keywords


class MetadataWriteTests(DatabaseFixture, unittest.TestCase):
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

    def metadata_batch(self, variant, ids, now):
        source, output = Queue(), Queue()
        source.put(([listing(i) for i in ids], 1, variant["url"], variant["id"]))
        with patch.object(self.core, "time", return_value=now):
            self.core.clear_item_queue(source, output)
        return output

    def variant(self):
        with closing(search_settings.connection()) as conn, conn:
            vinted_keywords.save(
                conn, 1, search_settings.get_search(1)["query"], ["Hollister"]
            )
        return vinted_keywords.rows(1)[0]

    def test_known_filtered_ids_and_equal_watermark_do_not_wait_for_writer(self):
        search_settings.remember_filtered(1, [101, 102])
        with closing(search_settings.connection()) as writer:
            writer.execute("BEGIN IMMEDIATE")
            with self.traced() as sql:
                started = time.monotonic()
                search_settings.remember_filtered(1, [102, 101, 102])
                db.update_last_timestamp(1, 100)
                elapsed = time.monotonic() - started
            writer.rollback()
        self.assertLess(elapsed, 0.5)
        self.assertFalse(any(s.startswith(("BEGIN", "INSERT", "UPDATE")) for s in sql))

    def test_missing_filtered_ids_are_durable_deduplicated_and_bounded(self):
        identities = list(range(2000, 3205))
        with self.traced() as sql:
            search_settings.remember_filtered(1, identities + identities)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM search_filtered_items WHERE query_id=1"
                ).fetchone()[0],
                len(identities),
            )
        probes = [s for s in sql if s.startswith("SELECT item FROM search_filtered_items")]
        self.assertEqual(len(probes), 3)
        self.assertEqual(sum(s == "COMMIT" for s in sql), 1)

    def test_filter_probe_insert_race_remains_idempotent(self):
        original = search_settings.connection
        races = []

        def racing_connection():
            conn = original()
            raced = False

            def trace(statement):
                nonlocal raced
                if statement.startswith("BEGIN") and not raced:
                    raced = True
                    with closing(original()) as competitor, competitor:
                        competitor.execute(
                            "INSERT INTO search_filtered_items VALUES (1,'500')"
                        )
                    races.append("inserted")

            conn.set_trace_callback(trace)
            return conn

        with patch.object(search_settings, "connection", side_effect=racing_connection):
            search_settings.remember_filtered(1, [500, 501])
        self.assertEqual(races, ["inserted"])
        self.assertEqual(search_settings.filtered_ids(1), {"500", "501"})

    def test_deleted_search_between_probe_and_insert_is_not_resurrected(self):
        original = search_settings.connection
        races = []

        def racing_connection():
            conn = original()
            raced = False

            def trace(statement):
                nonlocal raced
                if statement.startswith("BEGIN") and not raced:
                    raced = True
                    with closing(original()) as competitor, competitor:
                        competitor.execute("DELETE FROM queries WHERE id=2")
                    races.append("deleted")

            conn.set_trace_callback(trace)
            return conn

        with patch.object(search_settings, "connection", side_effect=racing_connection):
            search_settings.remember_filtered(2, [500])
        self.assertEqual(races, ["deleted"])
        self.assertEqual(search_settings.filtered_ids(2), set())
        self.assertIsNone(search_settings.get_search(2))

    def test_variant_heartbeat_commits_metadata_once_and_preserves_freshness(self):
        variant = self.variant()
        self.assertTrue(self.metadata_batch(variant, [500], 1000).empty())
        self.assertIn(-variant["id"], vinted_keywords.expand(search_settings.active_queries()))
        with self.traced() as sql:
            self.assertTrue(self.metadata_batch(variant, [500], 1050).empty())
        self.assertEqual(sum(s == "COMMIT" for s in sql), 1)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2)
            frontier = conn.execute(
                "SELECT max_item_id,last_check FROM listing_frontiers WHERE query_id=1"
            ).fetchone()
            current = vinted_keywords.rows(1, conn)[0]
        self.assertEqual(tuple(frontier), (500, 1050))
        self.assertEqual((current["max_item_id"], current["last_check"]), (500, 1050))
        self.assertEqual(search_settings.filtered_ids(1), {"500"})
        # Advancing freshness on heartbeats preserves the 20-minute outage rule.
        self.assertEqual(vinted_keywords.cutoff(current, [501], 2200), 500)

    def test_failed_metadata_rolls_back_together_but_keeps_new_alert_durable(self):
        variant = self.variant()
        self.metadata_batch(variant, [500], 1000)
        before = vinted_keywords.rows(1)[0]
        with closing(search_settings.connection()) as conn:
            checkpoints = [tuple(row) for row in conn.execute("SELECT * FROM listing_checkpoints")]
        with patch.object(
            vinted_keywords, "finish", side_effect=sqlite3.OperationalError("injected")
        ), self.assertRaises(sqlite3.OperationalError):
            self.metadata_batch(variant, [501, 500], 1050)
        with closing(search_settings.connection()) as conn:
            frontier = conn.execute(
                "SELECT max_item_id,last_check FROM listing_frontiers WHERE query_id=1"
            ).fetchone()
            alerts = conn.execute(
                "SELECT COUNT(*) FROM alert_outbox WHERE item_id='501'"
            ).fetchone()[0]
            self.assertEqual(
                [tuple(row) for row in conn.execute("SELECT * FROM listing_checkpoints")],
                checkpoints,
            )
        self.assertEqual(tuple(frontier), (500, 1000))
        self.assertEqual(vinted_keywords.rows(1)[0], before)
        self.assertEqual(search_settings.filtered_ids(1), set())
        self.assertTrue(db.is_item_in_db_by_id(501))
        self.assertEqual(alerts, 1)
        # Retry advances metadata without consuming a second durable alert.
        self.metadata_batch(variant, [501, 500], 1051)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM alert_outbox WHERE item_id='501'").fetchone()[0],
                1,
            )
        self.assertEqual(vinted_keywords.rows(1)[0]["max_item_id"], 501)

    def test_failed_new_item_never_advances_frontier(self):
        variant = self.variant()
        self.metadata_batch(variant, [500], 1000)
        with patch.object(db, "add_item_to_db", side_effect=sqlite3.OperationalError("busy")), self.assertRaises(sqlite3.OperationalError):
            self.metadata_batch(variant, [501, 500], 1050)
        self.assertEqual(vinted_keywords.rows(1)[0]["max_item_id"], 500)
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute("SELECT max_item_id FROM listing_frontiers WHERE query_id=1").fetchone()[0],
                500,
            )

    def test_changed_watermark_still_commits_durably(self):
        db.update_last_timestamp(1, 300)
        self.assertEqual(db.get_last_timestamp(1), 300)


if __name__ == "__main__":
    unittest.main()
