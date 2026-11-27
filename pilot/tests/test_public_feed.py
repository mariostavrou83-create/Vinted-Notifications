import json
import sqlite3
import unittest
from unittest.mock import patch

from flask import Flask

from pilot import db, flow, market, pricing
from pilot import public_feed as feed


class PublicFeedTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            PILOT_SCAVIO_API_KEY="test",
            PILOT_PUBLIC_SEARCH_ENABLED="1",
            PILOT_PUBLIC_SEARCH_MAX_REQUESTS=10,
            PILOT_PUBLIC_SEARCH_DAILY_LIMIT=10,
        )
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(self.conn.close)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA + market.SCHEMA + flow.SCHEMA + feed.SCHEMA)
        self.conn.execute(
            "CREATE TABLE pilot_buyer_link(user_id INTEGER,account_ref TEXT,revoke_pending INTEGER)"
        )
        self.now = 1795773600  # Public tests disable quiet hours explicitly below.

    def search(self, uid=1, term="camera", include=None, exclude=None, budget=None, prefs=True):
        self.conn.execute("INSERT OR IGNORE INTO users VALUES(?,?,?)", (uid, "owner" + str(uid), "hash"))
        if prefs:
            self.conn.execute(
                "INSERT OR IGNORE INTO settings VALUES(?,?)", (uid, '{"quiet_start":"","quiet_end":""}')
            )
        return self.conn.execute(
            "INSERT INTO searches VALUES(NULL,?,?,?,?,?,?,1)",
            (
                uid,
                "Find " + term,
                "https://www.vinted.co.uk/catalog?search_text=" + term,
                json.dumps(include or []),
                json.dumps(exclude or []),
                budget,
            ),
        ).lastrowid

    def check(self, after, items):
        with patch.object(feed, "_search", return_value=items) as fetch:
            worked = feed.public_discover(self.conn, self.now + after)
            return worked, fetch.call_count

    def listing(self, id, title="camera lens"):
        return {
            "id": id,
            "currency": "GBP",
            "price": "6.00",
            "total_price": "7.50",
            "title": title,
            "seller": {"id": 3456},
            "url": f"https://www.vinted.co.uk/items/{id}-camera",
            "condition": "Good",
        }

    def test_filtered_url_mapping_rejects_unsupported_or_ignored_filters(self):
        parsed = feed.parameters(
            "https://www.vinted.co.uk/catalog/10-coats?search_text=blue&brand_ids[]=8&brand_ids[]=4&price_to=12&currency=GBP&size_ids=9&order=relevance"
        )
        self.assertEqual(
            parsed,
            {
                "country": "uk",
                "page": 1,
                "sort": "newest_first",
                "category_id": 10,
                "query": "blue",
                "brand_ids": [4, 8],
                "size_ids": [9],
                "price_to": 12,
            },
        )
        response = {
            "items": [],
            "country": "uk",
            "currency": "GBP",
            "query": "blue",
            "filters": {"brand_ids": [8, 4], "category_id": 10, "size_ids": [9], "price_to": "12"},
        }
        self.assertTrue(feed._confirmed(parsed, response))
        self.assertFalse(feed._confirmed(parsed, dict(response, filters={})))
        with self.assertRaises(ValueError):
            feed.parameters(
                "https://www.vinted.co.uk/catalog?search_text=blue&additional_filter=fragile"
            )
        with self.assertRaises(ValueError):
            feed.parameters("https://www.vinted.co.uk/catalog?catalog[]=10&catalog[]=12")

    def test_first_snapshot_baseline_followup_fans_out_privately_and_finds_no_duplicates(self):
        self.search(uid=1)
        self.search(uid=2)
        self.assertEqual(self.check(0, [self.listing(10)]), (True, 1))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM pilot_alerts").fetchone()[0], 0)
        self.assertEqual(self.check(2, [self.listing(11)]), (False, 0))
        self.check(6, [self.listing(11), self.listing(10)])
        self.check(12, [self.listing(11), self.listing(10)])
        rows = self.conn.execute("SELECT * FROM pilot_alerts ORDER BY user_id").fetchall()
        self.assertEqual(
            [(r["user_id"], r["listing_id"], r["account_ref"]) for r in rows],
            [(1, "11", "public"), (2, "11", "public")],
        )
        self.assertIsNone(json.loads(rows[0]["listing"])["total_pence"])
        self.assertEqual(json.loads(rows[0]["listing"])["subtotal_pence"], 750)
        self.assertEqual(self.conn.execute("SELECT used FROM public_budget").fetchone()[0], 3)

    def test_paid_discovery_supplies_independent_resale_evidence_without_another_lookup(self):
        self.search(term="camera", include=["private sourcing keyword"])
        first = {
            **self.listing(22),
            "price": "19.00",
            "user_id": "private owner",
            "attributes": {"buyer_reference": "private shopping note"},
        }
        second = {**self.listing(23), "seller": {"id": 4567}, "price": "20.00"}
        own_target = dict(self.listing(24), price="1.00", seller={"id": 9000})
        same_seller = dict(self.listing(25), price="1.00", seller={"id": 9000})
        self.assertEqual(self.check(0, [first, second, own_target, same_seller]), (True, 1))
        target = {**feed._listing(own_target), "all_in_pence": 350}
        self.app.config.update(PILOT_SCAVIO_ENABLED=True, PILOT_SCAVIO_MAX_SEARCHES=10)
        with patch.object(market, "_search") as extra_lookup:
            comps = market.comparisons_for(self.conn, target, now=self.now + 2)
            value = pricing.estimate_resale(target, comps, now=self.now + 2)
            self.assertEqual(value["resale_pence"], 1850)
            self.assertEqual(value["profit_pence"], 1500)
            self.assertEqual({item["id"] for item in value["evidence"]}, {"22", "23"})
            extra_lookup.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT used FROM market_budget").fetchone()[0], 0)
        payloads = " ".join(r[0] for r in self.conn.execute("SELECT payload FROM market_offers"))
        self.assertNotIn("private", payloads)
        self.assertNotIn("user_id", payloads)
        # Only explicit unavailability invalidates a listing. Omission from the first
        # page alone cannot tell us that the seller withdrew it.
        self.check(7, [dict(first, is_reserved=True)])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM market_offers WHERE id='22'").fetchone())
        self.assertTrue(self.conn.execute("SELECT 1 FROM market_offers WHERE id='23'").fetchone())
        with db.transaction(self.conn):
            market.remember_public_results(self.conn, [first], self.now + 1)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM market_offers WHERE id='22'").fetchone())

    def test_daily_and_lifetime_caps_apply_with_ambiguous_timeouts_and_restart(self):
        import requests

        self.search()
        self.app.config["PILOT_PUBLIC_SEARCH_DAILY_LIMIT"] = 1
        with patch.object(feed, "_search", side_effect=requests.Timeout):
            self.assertTrue(feed.public_discover(self.conn, self.now))
        self.assertEqual(self.check(500, []), (False, 0))
        self.assertEqual(self.check(86500, []), (True, 1))
        self.app.config["PILOT_PUBLIC_SEARCH_MAX_REQUESTS"] = 2
        self.assertEqual(self.check(200000, []), (False, 0))

    def test_keywords_budget_and_rebaseline_after_change_or_long_gap(self):
        sid = self.search(include=["lens"], exclude=["case"], budget=1250)
        self.check(0, [self.listing(4)])
        self.check(
            7,
            [
                self.listing(9, "lens case"),
                self.listing(8, "camera body"),
                self.listing(7),
                dict(self.listing(6), total_price="8.00"),
            ],
        )
        self.assertEqual([r[0] for r in self.conn.execute("SELECT listing_id FROM pilot_alerts")], ["7"])
        self.check(1300, [self.listing(100)])
        self.conn.execute("UPDATE searches SET exclude_keywords='[\"bundle\"]' WHERE id=?", (sid,))
        self.check(1310, [self.listing(101)])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM pilot_alerts").fetchone()[0], 1)

    def test_fairness_off_switch_and_provider_account_overlap(self):
        self.search(uid=1, term="camera")
        self.search(uid=2, term="guitar")
        self.search(uid=3, term="other")
        self.conn.execute("INSERT INTO pilot_buyer_link VALUES(3,'verified',0)")
        used = []
        with patch.object(feed, "_search", side_effect=lambda b: used.append(b["query"]) or []):
            self.app.config["PILOT_PUBLIC_SEARCH_ENABLED"] = False
            self.assertFalse(feed.public_discover(self.conn, self.now))
            self.app.config["PILOT_PUBLIC_SEARCH_ENABLED"] = True
            for seconds in (1, 3, 5, 7):
                feed.public_discover(self.conn, self.now + seconds)
        self.assertEqual(set(used), {"camera", "guitar"})
        self.assertNotEqual(used[0], used[1])

    def test_inflight_search_edits_or_deletion_drop_result(self):
        sid = self.search()
        self.check(0, [self.listing(80)])

        def change_during_request(body):
            self.conn.execute("DELETE FROM searches WHERE id=?", (sid,))
            return [self.listing(85)]

        with patch.object(feed, "_search", side_effect=change_during_request):
            self.assertTrue(feed.public_discover(self.conn, self.now + 8))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM pilot_alerts").fetchone()[0], 0)
