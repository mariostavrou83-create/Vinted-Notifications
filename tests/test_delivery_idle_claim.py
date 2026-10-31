"""Idle delivery never takes the writer; real leases still recheck atomically."""

import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from test_search_controls import DatabaseFixture

import alert_delivery as delivery
import db
import search_settings


class DeliveryIdleClaimTests(DatabaseFixture, unittest.TestCase):
    def add(self, item_id="110", **fields):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                """INSERT INTO alert_outbox
                (item_id,query_id,search_name,content,url,title,price,currency,found_at)
                VALUES (?,1,'Search','Alert','https://example.test/item','Item','10','GBP',1)""",
                (item_id,),
            )
            for field, value in fields.items():
                conn.execute(
                    f"UPDATE alert_outbox SET {field}=? WHERE item_id=?",
                    (value, item_id),
                )

    def traced_connection(self, statements, before_write=None):
        class Connection(sqlite3.Connection):
            def execute(opened, sql, parameters=()):
                if sql == "BEGIN IMMEDIATE" and before_write:
                    before_write()
                return super().execute(sql, parameters)

        conn = sqlite3.connect(db.DB_PATH, timeout=0, factory=Connection)
        conn.row_factory = sqlite3.Row
        conn.set_trace_callback(statements.append)
        return conn

    def assert_read_only(self, statements):
        self.assertTrue(statements)
        self.assertFalse(
            any(
                sql.lstrip().upper().startswith(("BEGIN", "UPDATE", "INSERT", "DELETE"))
                for sql in statements
            ),
            statements,
        )

    def test_empty_dispatcher_can_poll_while_another_writer_is_held(self):
        statements = []
        with closing(search_settings.connection()) as owner:
            owner.execute("BEGIN IMMEDIATE")
            with patch.object(
                delivery,
                "connection",
                side_effect=lambda: self.traced_connection(statements),
            ):
                self.assertIsNone(delivery.claim(now=1000))
                self.assertIsNone(delivery.claim(now=1000, platform="ebay"))
            owner.rollback()
        self.assert_read_only(statements)

    def test_ineligible_jobs_and_cooldown_never_take_the_writer(self):
        self.add("110", next_attempt=1001)
        self.add("111", leased_until=1001)
        self.add(
            "112", status="sent", photo_status="pending", photo_next_attempt=1001
        )
        self.add("113", status="sent", photo_status="pending", leased_until=1001)
        self.add("114", status="failed")
        self.add("ebay:115", platform="ebay")
        statements = []
        with closing(search_settings.connection()) as owner:
            owner.execute("BEGIN IMMEDIATE")
            with patch.object(
                delivery,
                "connection",
                side_effect=lambda: self.traced_connection(statements),
            ):
                self.assertIsNone(delivery.claim(now=1000, preferred_photo="112"))
            owner.rollback()
        self.assert_read_only(statements)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE alert_outbox SET next_attempt=0 WHERE item_id='110'")
            conn.execute("INSERT INTO delivery_runtime VALUES ('cooldown_until',1001)")
        statements.clear()
        with patch.object(
            delivery,
            "connection",
            side_effect=lambda: self.traced_connection(statements),
        ):
            self.assertIsNone(delivery.claim(now=1000))
        self.assert_read_only(statements)

    def test_due_listings_and_photos_keep_eligibility_and_lease_recovery(self):
        cases = (
            ({}, {}, "listing"),
            ({"leased_until": 1000}, {}, "listing"),
            ({"platform": "ebay"}, {"platform": "ebay"}, "listing"),
            ({"status": "sent", "photo_status": "pending"}, {}, "photo"),
            (
                {
                    "status": "sent",
                    "photo_status": "pending",
                    "photo_attempts": 2,
                    "leased_until": 1000,
                },
                {"preferred_photo": "110"},
                "photo",
            ),
        )
        for fields, options, kind in cases:
            with self.subTest(fields=fields, options=options):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("DELETE FROM alert_outbox")
                self.add(**fields)
                claimed = delivery.claim(now=1000, **options)
                self.assertEqual(claimed["kind"], kind)
                self.assertTrue(claimed["lease_token"])
                self.assertIsNone(delivery.claim(now=1001, **options))

    def test_photos_disabled_stays_read_only_until_a_listing_is_due(self):
        self.add(status="sent", photo_status="pending")
        statements = []
        with patch.object(
            delivery,
            "connection",
            side_effect=lambda: self.traced_connection(statements),
        ):
            self.assertIsNone(
                delivery.claim(now=1000, preferred_photo="110", allow_photos=False)
            )
        self.assert_read_only(statements)
        self.add("111")
        self.assertEqual(
            delivery.claim(now=1000, allow_photos=False)["item_id"], "111"
        )

    def test_lease_and_cooldown_changes_between_probe_and_lock_are_rechecked(self):
        for change in ("lease", "cooldown"):
            with self.subTest(change=change):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("DELETE FROM alert_outbox")
                    conn.execute("DELETE FROM delivery_runtime")
                self.add()

                def another_worker():
                    with closing(search_settings.connection()) as conn, conn:
                        if change == "lease":
                            conn.execute(
                                "UPDATE alert_outbox SET leased_until=1120,lease_token='other'"
                            )
                        else:
                            conn.execute(
                                "INSERT INTO delivery_runtime VALUES ('cooldown_until',1030)"
                            )

                statements = []
                with patch.object(
                    delivery,
                    "connection",
                    side_effect=lambda: self.traced_connection(statements, another_worker),
                ):
                    self.assertIsNone(delivery.claim(now=1000))
                self.assertIn("BEGIN IMMEDIATE", statements)
                with closing(search_settings.connection()) as conn:
                    self.assertEqual(
                        conn.execute("SELECT lease_token FROM alert_outbox").fetchone()[0],
                        "other" if change == "lease" else None,
                    )

    def test_new_listing_arriving_before_write_lock_wins_over_photo(self):
        self.add(status="sent", photo_status="pending")
        statements = []
        with patch.object(
            delivery,
            "connection",
            side_effect=lambda: self.traced_connection(statements, lambda: self.add("111")),
        ):
            claimed = delivery.claim(now=1000, preferred_photo="110")
        self.assertEqual((claimed["item_id"], claimed["kind"]), ("111", "listing"))


if __name__ == "__main__":
    unittest.main()
