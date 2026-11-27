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

    def search(
        self, uid=1, term="camera", include=None, exclude=None, budget=None, prefs=True
    ):
        self.conn.execute(
            "INSERT OR IGNORE INTO users VALUES(?,?,?)",
            (uid, "owner" + str(uid), "hash"),
        )
        if prefs:
            self.conn.execute(
                "INSERT OR IGNORE INTO settings VALUES(?,?)",
                (uid, '{"quiet_start":"","quiet_end":""}'),
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
            "filters": {
                "brand_ids": [8, 4],
                "category_id": 10,
                "size_ids": [9],
                "price_to": "12",
            },
        }
        self.assertTrue(feed._confirmed(parsed, response))
        self.assertFalse(feed._confirmed(parsed, dict(response, filters={})))
        with self.assertRaises(ValueError):
            feed.parameters(
                "https://www.vinted.co.uk/catalog?search_text=blue&additional_filter=fragile"
            )
        with self.assertRaises(ValueError):
            feed.parameters(
                "https://www.vinted.co.uk/catalog?catalog[]=10&catalog[]=12"
            )

    def test_first_snapshot_baseline_followup_fans_out_privately_and_finds_no_duplicates(
        self,
    ):
        self.search(uid=1)
        self.search(uid=2)
        self.assertEqual(self.check(0, [self.listing(10)]), (True, 1))
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM pilot_alerts").fetchone()[0], 0
        )
        self.assertEqual(self.check(2, [self.listing(11)]), (False, 0))
        self.check(6, [self.listing(11), self.listing(10)])
        self.check(12, [self.listing(11), self.listing(10)])
        rows = self.conn.execute(
            "SELECT * FROM pilot_alerts ORDER BY user_id"
        ).fetchall()
        self.assertEqual(
            [(r["user_id"], r["listing_id"], r["account_ref"]) for r in rows],
            [(1, "11", "public"), (2, "11", "public")],
        )
        self.assertIsNone(json.loads(rows[0]["listing"])["total_pence"])
        self.assertEqual(json.loads(rows[0]["listing"])["subtotal_pence"], 750)
        self.assertEqual(
            self.conn.execute("SELECT used FROM public_budget").fetchone()[0], 3
        )

    def test_paid_discovery_supplies_independent_resale_evidence_without_another_lookup(
        self,
    ):
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
        self.assertEqual(
            self.check(0, [first, second, own_target, same_seller]), (True, 1)
        )
        target = {**feed._listing(own_target), "all_in_pence": 350}
        self.app.config.update(PILOT_SCAVIO_ENABLED=True, PILOT_SCAVIO_MAX_SEARCHES=10)
        with patch.object(market, "_search") as extra_lookup:
            comps = market.comparisons_for(self.conn, target, now=self.now + 2)
            value = pricing.estimate_resale(target, comps, now=self.now + 2)
            self.assertEqual(value["resale_pence"], 1850)
            self.assertEqual(value["profit_pence"], 1500)
            self.assertEqual({item["id"] for item in value["evidence"]}, {"22", "23"})
            extra_lookup.assert_not_called()
        self.assertEqual(
            self.conn.execute("SELECT used FROM market_budget").fetchone()[0], 0
        )
        payloads = " ".join(
            r[0] for r in self.conn.execute("SELECT payload FROM market_offers")
        )
        self.assertNotIn("private", payloads)
        self.assertNotIn("user_id", payloads)
        # Only explicit unavailability invalidates a listing. Omission from the first
        # page alone cannot tell us that the seller withdrew it.
        self.check(7, [dict(first, is_reserved=True)])
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM market_offers WHERE id='22'").fetchone()
        )
        self.assertTrue(
            self.conn.execute("SELECT 1 FROM market_offers WHERE id='23'").fetchone()
        )
        with db.transaction(self.conn):
            market.remember_public_results(self.conn, [first], self.now + 1)
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM market_offers WHERE id='22'").fetchone()
        )

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
        self.assertEqual(
            [r[0] for r in self.conn.execute("SELECT listing_id FROM pilot_alerts")],
            ["7"],
        )
        self.check(1300, [self.listing(100)])
        self.conn.execute(
            "UPDATE searches SET exclude_keywords='[\"bundle\"]' WHERE id=?", (sid,)
        )
        self.check(1310, [self.listing(101)])
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM pilot_alerts").fetchone()[0], 1
        )

    def test_fairness_off_switch_and_provider_account_overlap(self):
        self.search(uid=1, term="camera")
        self.search(uid=2, term="guitar")
        self.search(uid=3, term="other")
        self.conn.execute("INSERT INTO pilot_buyer_link VALUES(3,'verified',0)")
        used = []
        with patch.object(
            feed, "_search", side_effect=lambda b: used.append(b["query"]) or []
        ):
            self.app.config["PILOT_PUBLIC_SEARCH_ENABLED"] = False
            self.assertFalse(feed.public_discover(self.conn, self.now))
            self.app.config["PILOT_PUBLIC_SEARCH_ENABLED"] = True
            for seconds in (1, 3, 5, 7):
                feed.public_discover(self.conn, self.now + seconds)
        self.assertEqual(set(used), {"camera", "guitar"})
        self.assertNotEqual(used[0], used[1])

    def test_all_100_distinct_source_urls_share_each_five_second_round(self):
        for i in range(100):
            self.search(term=f"camera{i}", include=[f"lens{i}:"])
        self.app.config.update(
            PILOT_PUBLIC_SEARCH_REQUESTS_PER_SECOND=100,
            PILOT_PUBLIC_SEARCH_CONCURRENCY=10,
            PILOT_PUBLIC_SEARCH_MAX_REQUESTS=205,
            PILOT_PUBLIC_SEARCH_DAILY_LIMIT=205,
        )
        calls = []
        batch = 1

        def provider(body):
            idx = int(body["query"].removeprefix("camera"))
            calls.append((batch, idx))
            return [self.listing(1000 + batch * 100 + idx, f"lens{idx}: wide angle")]

        with patch.object(feed, "_search", side_effect=provider):
            self.assertTrue(feed.public_discover(self.conn, self.now))
            self.assertFalse(feed.public_discover(self.conn, self.now + 4.9))
            self.assertEqual(
                self.conn.execute("SELECT count(*) FROM pilot_alerts").fetchone()[0], 0
            )
            batch = 2
            self.assertTrue(feed.public_discover(self.conn, self.now + 5))
        self.assertEqual(sorted(calls), [(b, i) for b in (1, 2) for i in range(100)])
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM pilot_alerts").fetchone()[0], 100
        )
        health = feed.status(self.conn, 1, self.now + 6)["searches"]
        self.assertEqual({h["state"] for h in health.values()}, {"checked"})
        self.assertEqual({h["target_seconds"] for h in health.values()}, {5})

    def test_100_private_rules_on_shared_source_evaluated_on_every_round(self):
        ids = [
            self.search(uid=i + 1, term="shoes", include=[f"private{i}."])
            for i in range(100)
        ]
        with patch.object(
            feed,
            "_search",
            side_effect=[
                [self.listing(400)],
                [self.listing(401, "Shoes private32.")],
                [
                    self.listing(402, "Shoes private17."),
                    self.listing(401, "Shoes private32."),
                ],
            ],
        ) as fetch:
            for delta in (0, 5, 10):
                self.assertTrue(feed.public_discover(self.conn, self.now + delta))
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM public_search_checkpoint"
            ).fetchone()[0],
            100,
        )
        alerts = [
            tuple(r)
            for r in self.conn.execute(
                "SELECT user_id,listing_id FROM pilot_alerts ORDER BY user_id"
            )
        ]
        self.assertEqual(alerts, [(18, "402"), (33, "401")])
        self.assertEqual(
            feed.status(self.conn, 33, self.now + 11)["searches"][str(ids[32])][
                "state"
            ],
            "checked",
        )

    def test_stable_excess_capacity_is_visible_without_ever_spending_on_a_slow_rotation(
        self,
    ):
        ids = [self.search(term=f"coat{i}") for i in range(7)]
        self.app.config.update(
            PILOT_PUBLIC_SEARCH_REQUESTS_PER_SECOND=1, PILOT_PUBLIC_SEARCH_CONCURRENCY=1
        )
        fake_clock = [100.0]

        def elapsed():
            return fake_clock[0]

        def sleep(seconds):
            fake_clock[0] += seconds + 0.00001

        # Simulate sequential sub-second responses on a one-request-per-second key.
        with (
            patch.object(feed.time, "monotonic", side_effect=elapsed),
            patch.object(feed.time, "sleep", side_effect=sleep),
            patch.object(feed, "_search", return_value=[]) as fetch,
        ):
            self.assertTrue(feed.public_discover(self.conn, self.now))
            self.assertEqual(fetch.call_count, 5)
            health = feed.status(self.conn, 1, self.now + 4.2)["searches"]
            self.assertEqual(
                [health[str(i)]["state"] for i in ids],
                ["checked"] * 5 + ["capacity"] * 2,
            )
            self.assertTrue(feed.public_discover(self.conn, self.now + 5))
            self.assertEqual(fetch.call_count, 10)
            self.assertEqual(len({c.args[0]["query"] for c in fetch.call_args_list}), 5)
            self.assertFalse(feed.public_discover(self.conn, self.now + 10))
            self.assertEqual(
                feed.status(self.conn, 1, self.now + 10)["searches"][str(ids[0])][
                    "state"
                ],
                "budget",
            )

    def test_runtime_disable_stops_new_requests_in_current_round(self):
        for i in range(5):
            self.search(term=f"coat{i}")
        self.app.config.update(
            PILOT_PUBLIC_SEARCH_REQUESTS_PER_SECOND=5, PILOT_PUBLIC_SEARCH_CONCURRENCY=1
        )
        normal_config = feed.config_for
        with patch.object(feed, "_search", return_value=[]) as fetch:

            def settings(conn, base_config):
                options = normal_config(conn, base_config)
                if fetch.call_count >= 2:
                    options["PILOT_PUBLIC_SEARCH_ENABLED"] = "0"
                    options["_market_generation"] += 1
                return options

            with patch.object(feed, "config_for", side_effect=settings):
                self.assertTrue(feed.public_discover(self.conn, self.now))
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(
            self.conn.execute("SELECT used FROM public_budget").fetchone()[0], 2
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM public_search_checkpoint"
            ).fetchone()[0],
            2,
        )

    def test_missing_tick_failed_fetch_and_late_response_never_marked_checked(self):
        ident = self.search()
        self.check(0, [self.listing(5)])
        state = lambda when: feed.status(self.conn, 1, self.now + when)["searches"][
            str(ident)
        ]
        self.assertEqual(state(5.5)["state"], "stale")
        prior = state(5.5)["last_checked_at"]
        import requests

        with patch.object(feed, "_search", side_effect=requests.Timeout):
            self.assertTrue(feed.public_discover(self.conn, self.now + 6))
        self.assertEqual(state(6.5)["state"], "failed")
        self.assertEqual(state(6.5)["last_checked_at"], prior)
        self.assertFalse(self.check(10, [])[0])
        self.assertEqual(state(10)["state"], "backoff")

    def test_public_photos_are_vinted_cdn_only_and_publish_time_is_unknown(self):
        raw = self.listing(5)
        candidate = "https://images1.vinted.net/t/01_abcd/image.webp"
        item = feed._listing(
            dict(
                raw,
                photo={"url": "https://not-vinted.test/i.jpg"},
                photos=[{"url": candidate}],
            )
        )
        self.assertEqual(item["photo_url"], candidate)
        self.assertEqual(item["listing_url"], "https://www.vinted.co.uk/items/5")
        self.assertFalse(item["posted_at_exact"])

    def test_inflight_search_edits_or_deletion_drop_result(self):
        sid = self.search()
        self.check(0, [self.listing(80)])

        def change_during_request(body):
            self.conn.execute("DELETE FROM searches WHERE id=?", (sid,))
            return [self.listing(85)]

        with patch.object(feed, "_search", side_effect=change_during_request):
            self.assertTrue(feed.public_discover(self.conn, self.now + 8))
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM pilot_alerts").fetchone()[0], 0
        )
