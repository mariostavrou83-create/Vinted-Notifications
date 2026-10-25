"""Live stock requires matched native sales, fresh availability and bounded work."""

import json
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import research_trial as trial
from daily_sourcing import FRESH, LIMITS, DailySourcing, scope_matches


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = int(time.time() // trial.DAY) * trial.DAY + 23 * 3600
        self.store = trial.Store(
            Path(self.directory.name) / "trial.sqlite3", daily_enabled=True
        )
        self.engine = DailySourcing(self.store)
        self.feed(self.now)

    def tearDown(self):
        self.directory.cleanup()

    def feed(self, now, p95=2.5, pending=0):
        self.store.feed(
            {
                "version": 2,
                "exclusions": {"scopes": []},
                "health": {"interval_p95": p95, "samples": 100, "pending_age": pending},
            },
            now,
        )

    def item(self, identity, price, title="Example Label Horizon zip jacket"):
        return trial.normalize(
            {
                "id": str(identity),
                "title": title,
                "brand_title": "Example Label",
                "brand_id": 123,
                "catalog_id": 1037,
                "size_title": "S / UK 8-10",
                "status": "Very good",
                "price": {"amount": str(price), "currency_code": "GBP"},
                "user_id": identity + 1000,
            },
            "1037",
        )

    def evidence(self):
        low = [self.item(i, 8) for i in (1, 2)]
        high = [self.item(i, 65) for i in (3, 4, 5)]
        current = self.item(6, 12)
        self.store.ingest(
            "hybrid", low + [current], self.now, source="focus", price_band="low"
        )
        self.store.ingest("hybrid", high, self.now, source="focus", price_band="high")
        for item in low + high + [current]:
            self.store.record_outcome(
                item["id"],
                {
                    "state": "active" if item["id"] == "6" else "sold",
                    "price": item["price"],
                    "seller": item["seller"],
                    "condition": "Very good",
                    "size": item["size"],
                    "age_bounds": (3600, 7200),
                },
                self.now,
            )

    def test_qualifies_a_fresh_available_bargain_using_both_sold_bands(self):
        self.evidence()
        snapshot = self.engine.snapshot(self.now)
        self.assertEqual(len(snapshot["ready"]), 1)
        card = snapshot["ready"][0]
        self.assertEqual(card["id"], "6")
        self.assertGreaterEqual(card["estimated_margin"], 1800)
        self.assertEqual(card["cheap_quick_sales"], 2)
        self.assertEqual(card["premium_quick_sales"], 3)
        self.assertEqual(len(card["comparables"]), 5)
        self.assertEqual(snapshot["stats"]["new_qualified_today"], 1)
        self.assertEqual(
            self.engine.snapshot(self.now + 1)["stats"]["new_qualified_today"], 1
        )

    def test_expensive_asking_prices_unknowns_and_disappearances_do_not_make_sales(
        self,
    ):
        self.evidence()
        self.store.record_outcome("3", {"state": "unavailable"}, self.now)
        self.assertFalse(self.engine.snapshot(self.now)["ready"])
        self.store.record_outcome("3", {"state": "sold", "price": 6500}, self.now)
        # Native age from the earlier confirmed check remains; remove all age
        # proof to model a sold page whose publication time was never verified.
        with closing(self.store.connect()) as c, c:
            c.execute("UPDATE items SET age_upper=NULL WHERE id='3'")
        self.assertFalse(self.engine.snapshot(self.now)["ready"])

    def test_stale_or_unavailable_stock_is_not_recommended_and_gets_rechecked(self):
        self.evidence()
        self.assertFalse(self.engine.snapshot(self.now + FRESH + 1)["ready"])
        self.assertEqual(self.engine.priority_item(self.now + FRESH + 1)["id"], "6")
        self.store.record_outcome("6", {"state": "unavailable"}, self.now + FRESH + 1)
        self.assertFalse(self.engine.snapshot(self.now + FRESH + 1)["ready"])

    def test_different_models_and_generic_garment_titles_do_not_match(self):
        self.evidence()
        with closing(self.store.connect()) as c, c:
            row = c.execute("SELECT payload FROM items WHERE id='6'").fetchone()
            payload = json.loads(row[0])
            payload["title"] = "Example Label Summit zip jacket"
            c.execute("UPDATE items SET payload=? WHERE id='6'", (json.dumps(payload),))
        self.assertFalse(self.engine.snapshot(self.now)["ready"])

    def test_premium_new_tag_prices_cannot_value_a_used_item(self):
        self.evidence()
        for identity in ("3", "4", "5"):
            self.store.record_outcome(
                identity,
                {
                    "state": "sold",
                    "price": 6500,
                    "condition": "New with tags",
                    "age_bounds": (3600, 7200),
                },
                self.now,
            )
        self.assertFalse(self.engine.snapshot(self.now)["ready"])

    def test_same_brand_new_type_is_allowed_but_existing_type_is_excluded_at_every_price(
        self,
    ):
        scope = {
            "brand_ids": ["123"],
            "brand_name": "Example Label",
            "categories": ["1037"],
            "keywords": ["fur hood"],
        }
        for price in (8, 90):
            self.assertTrue(
                scope_matches(self.item(1, price, "Example Label fur hood coat"), scope)
            )
            self.assertFalse(
                scope_matches(
                    self.item(2, price, "Example Label Horizon zip jacket"), scope
                )
            )
        self.assertFalse(
            scope_matches(
                dict(self.item(3, 8), category="12", title="Fur hood top"), scope
            )
        )

    def test_research_looks_for_bargains_before_three_bargains_are_already_observed(
        self,
    ):
        for brand in range(5):
            items = []
            for offset, price in enumerate((30, 40, 40)):
                item = self.item(100 + brand * 10 + offset, price)
                item.update(
                    brand="Label" + str(brand),
                    title="Label" + str(brand) + " Horizon zip jacket",
                )
                items.append(item)
            self.store.ingest("broad", items, self.now, price_band="high")
        targets = self.store.focus_candidates(self.now)
        self.assertEqual(len(targets), 4)
        self.assertTrue(all(x["cheap_arrivals"] == 0 for x in targets))
        self.assertFalse(self.engine.snapshot(self.now)["ready"])

    def test_daily_cohort_budget_and_reserved_stock_slots_survive_restart(self):
        items = [self.item(i, 10) for i in range(100, 230)]
        self.store.ingest("hybrid", items, self.now, source="focus", price_band="low")
        self.store.ingest("hybrid", items, self.now, source="focus", price_band="high")
        with closing(self.store.connect()) as c, c:
            enrolled = c.execute(
                "SELECT COUNT(*) FROM items WHERE admitted IS NOT NULL"
            ).fetchone()[0]
            self.assertLessEqual(enrolled, 64)
        for item in items:
            self.store.admit_stock(item["id"], self.now)
        restarted = trial.Store(self.store.path)
        with closing(restarted.connect()) as c:
            self.assertEqual(
                c.execute(
                    "SELECT COUNT(*) FROM items WHERE admitted IS NOT NULL"
                ).fetchone()[0],
                LIMITS["admissions"],
            )
        self.assertTrue(restarted.daily())

    def test_stock_refresh_reads_share_the_outcome_budget(self):
        with closing(self.store.connect()) as c, c:
            c.executemany(
                "INSERT INTO reads(started,arm,kind,status) VALUES(?,'shared','stock','ok')",
                [(self.now,)] * LIMITS["outcome"],
            )
        self.assertFalse(self.store.can_read("outcome", self.now))
        self.assertFalse(self.store.can_read("stock", self.now))

    def test_daily_collection_continues_after_benchmark_and_pause_is_persistent(self):
        ends = self.store.state(self.now)["ends"]
        self.feed(ends + 1)
        self.assertEqual(self.store.state(ends + 1)["phase"], "completed")
        self.assertTrue(self.store.can_read("explore", ends + 1))
        first = self.store.snapshot(ends + 1)
        self.store.control("pause", ends + 2)
        restarted = trial.Store(self.store.path)
        self.assertFalse(restarted.can_read("explore", ends + 2))
        restarted.control("resume", ends + 3)
        self.assertTrue(restarted.can_read("explore", ends + 3))
        self.assertEqual(self.store.snapshot(ends + 5), first)

    def test_absent_health_or_a_three_second_breach_stops_daily_reads(self):
        self.feed(self.now + 60, 3.01)
        self.assertFalse(self.store.can_read("broad", self.now + 60))
        self.feed(self.now + 120, 2.5)
        self.feed(self.now + 180, 2.5)
        self.assertTrue(self.store.can_read("broad", self.now + 180))
        self.store.feed({"version": 2, "exclusions": {}, "health": {}}, self.now + 240)
        self.assertFalse(self.store.can_read("broad", self.now + 240))

    def test_alert_queue_delay_also_blocks_daily_reads(self):
        self.feed(self.now + 60, pending=3.01)
        self.assertFalse(self.store.can_read("broad", self.now + 60))

    def test_network_backoff_does_not_get_shortened_by_healthy_alert_samples(self):
        for i in range(5):
            with closing(self.store.connect()) as c, c:
                identity = c.execute(
                    "INSERT INTO reads(started,arm,kind) VALUES(?,'shared','outcome')",
                    (self.now,),
                ).lastrowid
            self.store.finish_read(identity, "read_unavailable", 0, 0, self.now)
        deadline = self.store.state(self.now)["pause_until"]
        self.assertGreater(deadline, self.now + 180)
        for offset, p95 in [(60, 3.1), (120, 2.5), (180, 2.5)]:
            self.feed(self.now + offset, p95)
        self.assertGreaterEqual(
            self.store.state(self.now + 180)["pause_until"], deadline
        )
        self.assertFalse(self.store.can_read("broad", self.now + 180))

    def test_scheduler_keeps_one_network_job_and_a_persisted_gap(self):
        worker = trial.Worker(self.store, "https://unused.invalid", "x" * 40)
        worker.transport = Mock()
        worker.transport.catalogue.return_value = []
        worker.fetch_feed = Mock()
        worker.feed_due = self.now + 180
        worker.step(self.now)
        self.assertEqual(worker.transport.catalogue.call_count, 1)
        worker.step(self.now + 24)
        self.assertEqual(worker.transport.catalogue.call_count, 1)
        self.assertEqual(self.store.state(self.now)["next_network"], self.now + 25)

    def test_private_snapshot_never_exposes_credentials(self):
        client = trial.make_app(self.store, "x" * 40).test_client()
        self.assertEqual(client.get("/sourcing.json").status_code, 401)
        body = client.get(
            "/sourcing.json", headers={"Authorization": "Bearer " + "x" * 40}
        )
        self.assertEqual(body.status_code, 200)
        self.assertNotIn("proxy", body.get_data(as_text=True))
        self.assertNotIn("token", body.get_data(as_text=True))

    def test_uncertain_publication_age_stops_wasting_checks_after_one_day(self):
        item = self.item(1, 10)
        self.store.ingest("hybrid", [item], self.now, source="focus")
        self.store.record_outcome(
            "1", {"state": "active", "price": 1000}, self.now + trial.DAY + 1
        )
        with closing(self.store.connect()) as c:
            self.assertEqual(
                c.execute("SELECT state FROM items WHERE id='1'").fetchone()[0],
                "unverified",
            )
