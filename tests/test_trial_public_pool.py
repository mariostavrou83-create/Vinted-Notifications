import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import trial_public_pool as pool


class PublicComparisonPool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "vinted_notifications.db"
        self.now = pool.EARLIEST + 90
        with patch.object(pool, "eligible_service", return_value=True), patch.object(pool.time, "time", return_value=self.now):
            pool.activate(self.db, self.now + 3600)
        self.conn = sqlite3.connect(pool._path(self.db))
        self.addCleanup(self.conn.close)
        self.target = {"id":"100", "seller_id":"44", "brand":"Next", "size":"S"}

    def item(self, identity="200", seller="55", **extra):
        return dict(dict(id=identity, title="Next cropped pink cami top", url=f"/items/{identity}-next",
                         price={"amount":"7.00","currency_code":"GBP"}, user={"id":seller},
                         item_box={"first_line":"Next","second_line":"S · Very good"}), **extra)

    def rows(self, at=None):
        return pool.candidates(self.db, self.target, self.now if at is None else at)

    def test_only_known_public_sellers_active_and_uk(self):
        items = [self.item(), self.item("100","123"), self.item("201","44"), self.item("202",""),
                 self.item("203","58", can_buy=False), self.item("204","59", seller_country_code="FR"),
                 self.item("205","60", price={"amount":"6","currency_code":"EUR"}),
                 self.item("206","61", item_box={"first_line":"Next","second_line":"S · unknown"}),
                 self.item("207","62", url="https://www.vinted.fr/items/207")]
        pool._store(self.conn, self.now, items, self.now)
        rows = self.rows()
        self.assertEqual([r["id"] for r in rows], ["200"])
        self.assertEqual((rows[0]["observed_at"], rows[0]["ask_pence"]), (self.now, 700))
        self.assertEqual(rows[0]["url"], "https://www.vinted.co.uk/items/200")
        self.assertNotIn("user", rows[0])

    def test_withdrawal_newer_observation_and_time_bound(self):
        pool._store(self.conn, self.now, [self.item()], self.now)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows(self.now-.01), [])
        self.assertEqual(self.rows(self.now+pool.MAX_AGE+1), [])
        pool._store(self.conn, self.now+1, [self.item(is_sold=True)], self.now+1)
        self.assertEqual(self.rows(self.now+2), [])
        pool._store(self.conn, self.now, [self.item()], self.now+2)
        self.assertEqual(self.rows(self.now+2), [])
        pool._store(self.conn, self.now+3601, [self.item()], self.now+3601)
        self.assertEqual(self.rows(self.now+3601), [])

    def test_unactivated_service_has_no_pool(self):
        unused = self.db.parent / "fresh" / "original.db"
        with patch.object(pool,"eligible_service",return_value=False):
            pool.activate(unused,self.now+3600)
        self.assertFalse(pool._path(unused).exists())
        self.assertEqual(pool.candidates(unused,self.target,self.now),[])
