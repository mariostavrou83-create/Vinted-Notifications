"""Trial expiry, shared evidence, private-feed and real SQLite integration."""

import os
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from test_search_controls import DatabaseFixture
from werkzeug.security import generate_password_hash

import db
import research_trial as trial
import research_trial_bridge as bridge
from web_ui_plugin.web_ui import create_app


def raw(identity, price="45", **kw):
    result = {
        "id": identity,
        "title": "Example Label tattoo print mesh top",
        "brand_title": "Example Label",
        "size_title": "S / UK 8-10",
        "price": {"amount": price, "currency_code": "GBP"},
        "user": {"id": int(identity) + 100},
    }
    result.update(kw)
    return result


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = trial.Store(Path(self.directory.name) / "trial.sqlite3")
        self.now = int(time.time() // trial.DAY) * trial.DAY + 23 * 3600
        self.store.feed(
            {"version": 2, "exclusions": {}, "items": [], "health": {}}, self.now
        )

    def tearDown(self):
        self.directory.cleanup()

    def add(self, arm="hybrid", count=4):
        items = [trial.normalize(raw(str(i)), "12") for i in range(1, count + 1)]
        self.store.ingest(arm, items, self.now, source="focus")
        return items

    def test_exact_fourteen_day_stop_and_restart_keeps_original_dates(self):
        state = self.store.state(self.now)
        self.assertEqual(state["ends"] - state["started"], 14 * 86400)
        restarted = trial.Store(self.store.path)
        restarted.feed(
            {"version": 2, "exclusions": {}, "items": [], "health": {}}, self.now + 100
        )
        self.assertEqual(restarted.state(self.now + 100)["ends"], state["ends"])
        worker = trial.Worker(restarted, "https://unused.invalid", "x" * 40)
        worker.fetch_feed = Mock()
        worker.step(state["ends"])
        worker.fetch_feed.assert_not_called()
        self.assertEqual(restarted.state(state["ends"])["phase"], "completed")
        restarted.control("resume", state["ends"] + 1)
        self.assertEqual(restarted.state(state["ends"] + 1)["phase"], "completed")

    def test_shared_item_is_tracked_once_but_each_arm_has_its_own_discoveries(self):
        self.add()
        self.add("broad")
        snapshot = self.store.snapshot(self.now)
        self.assertEqual(snapshot["shared_tracked"], 4)
        self.assertEqual([a["seen"] for a in snapshot["arms"]], [4, 0, 4])
        with closing(self.store.connect()) as c:
            self.assertEqual(
                c.execute(
                    "SELECT COUNT(*) FROM items WHERE admitted IS NOT NULL"
                ).fetchone()[0],
                4,
            )

    def test_admission_and_request_limits_are_persisted_before_fetch(self):
        self.add(count=30)
        with closing(self.store.connect()) as c:
            self.assertEqual(
                c.execute(
                    "SELECT COUNT(*) FROM items WHERE admitted IS NOT NULL"
                ).fetchone()[0],
                4,
            )
        identity = self.store.reserve_read("hybrid", "explore", now=self.now)
        self.store.finish_read(
            identity, "ok", trial.BYTE_LIMIT, trial.BYTE_LIMIT, self.now
        )
        self.assertFalse(self.store.can_read("outcome", self.now))
        with self.assertRaises(RuntimeError):
            self.store.reserve_read("hybrid", "explore", now=self.now)

    def test_tracking_reserves_equal_space_for_both_price_bands(self):
        for band, price in [("low", "5"), ("high", "90")]:
            items = [
                trial.normalize(raw(str(i), price), "12")
                for i in range(1 if band == "low" else 51, 31 if band == "low" else 81)
            ]
            self.store.ingest("hybrid", items, self.now, source="focus")
        with closing(self.store.connect()) as c:
            counts = dict(
                c.execute(
                    "SELECT CASE WHEN entry_price<=2500 THEN 'low' ELSE 'high' END,COUNT(*) FROM items WHERE admitted IS NOT NULL GROUP BY 1"
                )
            )
        self.assertEqual(counts, {"low": 4, "high": 4})

    def test_focused_price_bands_follow_the_candidate_instead_of_a_fixed_price(self):
        for start, price, band in [(1, "45", "low"), (51, "100", "high")]:
            items = [
                trial.normalize(raw(str(i), price), "12")
                for i in range(start, start + 12)
            ]
            self.store.ingest(
                "hybrid", items, self.now, source="focus", price_band=band
            )
        with closing(self.store.connect()) as c:
            counts = dict(
                c.execute(
                    "SELECT admission_band,COUNT(*) FROM items WHERE admitted IS NOT NULL GROUP BY admission_band"
                )
            )
        self.assertEqual(counts, {"low": 4, "high": 4})

    def test_tracking_is_spread_through_the_day_and_reserves_hybrid_focus(self):
        early = int(self.now // trial.DAY) * trial.DAY + 3600
        low = [trial.normalize(raw(str(i), "5"), "12") for i in range(1, 31)]
        high = [trial.normalize(raw(str(i), "90"), "12") for i in range(51, 81)]
        self.store.ingest("hybrid", low + high, early, source="explore")
        with closing(self.store.connect()) as c:
            self.assertEqual(
                c.execute(
                    "SELECT COUNT(*) FROM items WHERE admitted IS NOT NULL"
                ).fetchone()[0],
                2,
            )
        self.store.ingest("hybrid", low + high, self.now, source="explore")
        with closing(self.store.connect()) as c:
            self.assertEqual(
                c.execute(
                    "SELECT COUNT(*) FROM items WHERE admitted IS NOT NULL"
                ).fetchone()[0],
                4,
            )
        self.store.ingest("hybrid", low + high, self.now, source="focus")
        with closing(self.store.connect()) as c:
            self.assertEqual(
                c.execute(
                    "SELECT COUNT(*) FROM items WHERE admitted IS NOT NULL"
                ).fetchone()[0],
                8,
            )

    def test_seller_panels_cover_more_than_the_first_category(self):
        for offset, category in enumerate(list(trial.CATEGORIES)[:6]):
            items = [
                trial.normalize(raw(str(offset * 100 + i), "10"), category)
                for i in range(1, 11)
            ]
            self.store.ingest("broad", items, self.now)
        with closing(self.store.connect()) as c:
            counts = [
                r[0]
                for r in c.execute("SELECT COUNT(*) FROM sellers GROUP BY category")
            ]
        self.assertGreaterEqual(len(counts), 4)
        self.assertLessEqual(max(counts), 2)
        self.assertEqual(sum(counts), 8)

    def test_upgrade_releases_only_unread_legacy_choices_and_redundant_seeds(self):
        self.add(count=4)
        self.store.record_outcome("1", {"state": "active"}, self.now)
        ends = self.store.state(self.now)["ends"]
        with closing(self.store.connect()) as c, c:
            c.execute("UPDATE trial SET admission_version=1")
            c.execute("UPDATE items SET admission_policy=1")
            c.execute("DELETE FROM sellers")
            c.executemany(
                "INSERT INTO sellers(id,category) VALUES(?,'1037')",
                [(str(i),) for i in range(101, 109)],
            )
        upgraded = trial.Store(self.store.path)
        with closing(upgraded.connect()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM items").fetchone()[0], 4)
            self.assertEqual(
                c.execute(
                    "SELECT COUNT(*) FROM items WHERE admitted IS NOT NULL"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(c.execute("SELECT COUNT(*) FROM sellers").fetchone()[0], 2)
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM unread_admission_archive").fetchone()[
                    0
                ],
                3,
            )
        self.assertEqual(upgraded.state(self.now)["ends"], ends)
        # A later restart must keep new balanced admissions.
        upgraded.ingest(
            "hybrid", [trial.normalize(raw("20"), "12")], self.now, source="focus"
        )
        restarted = trial.Store(self.store.path)
        with closing(restarted.connect()) as c:
            self.assertEqual(
                c.execute(
                    "SELECT admission_policy FROM items WHERE id='20'"
                ).fetchone()[0],
                2,
            )
            self.assertIsNotNone(
                c.execute("SELECT admitted FROM items WHERE id='20'").fetchone()[0]
            )

    def test_focus_requires_supply_in_both_economically_viable_bands(self):
        items = [
            trial.normalize(raw(str(i), "10" if i <= 6 else "100"), "12")
            for i in range(1, 13)
        ]
        items += [
            trial.normalize(raw(str(i), "5", brand_title="Low Margin Label"), "12")
            for i in range(51, 81)
        ]
        self.store.ingest("hybrid", items, self.now)
        focus = self.store.focus_candidates(self.now)
        self.assertTrue(focus)
        self.assertTrue(all("Example Label" in x["name"] for x in focus))

    def test_existing_brands_are_excluded_at_cheap_and_premium_prices_in_every_arm(
        self,
    ):
        self.store.feed(
            {
                "version": 2,
                "health": {},
                "exclusions": {
                    "names": ["001 example label mesh tops"],
                    "brand_ids": [],
                },
            },
            self.now,
        )
        for arm in trial.ARMS:
            self.store.ingest(
                arm,
                [
                    trial.normalize(raw("1", "5"), "12"),
                    trial.normalize(raw("2", "90"), "12"),
                ],
                self.now,
            )
        self.assertTrue(
            all(
                a["seen"] == 0 and a["tracked"] == 0
                for a in self.store.snapshot(self.now)["arms"]
            )
        )

    def test_new_brand_and_modern_single_word_model_can_be_discovered(self):
        r = raw("91")
        r.update(title="Fresh Label Horizon jacket", brand_title="Fresh Label")
        item = trial.normalize(r, "1037")
        self.assertIn("horizon", trial.phrases(item))
        self.store.ingest("broad", [item], self.now)
        self.assertEqual(self.store.snapshot(self.now)["arms"][0]["seen"], 1)

    def test_new_saved_search_retires_existing_research_cohort(self):
        self.add()
        self.store.feed(
            {
                "version": 2,
                "health": {},
                "exclusions": {"names": ["example label graphic tops"]},
            },
            self.now + 60,
        )
        with closing(self.store.connect()) as c:
            self.assertEqual(
                c.execute(
                    "SELECT COUNT(*) FROM items WHERE state='excluded'"
                ).fetchone()[0],
                4,
            )
        self.assertEqual(self.store.snapshot(self.now + 60)["arms"][2]["seen"], 0)

    def test_stale_health_pauses_research_and_manual_pause_has_no_fetch(self):
        self.assertFalse(self.store.can_read("explore", self.now + 181))
        self.store.control("pause", self.now)
        worker = trial.Worker(self.store, "https://unused.invalid", "x" * 40)
        worker.fetch_feed = Mock()
        worker.step(self.now)
        worker.fetch_feed.assert_not_called()

    def test_two_slow_windows_pause_research_without_changing_trial_end(self):
        end = self.store.state(self.now)["ends"]
        for offset, p95 in [(0, 2.5), (60, 2.9), (120, 3.1)]:
            self.store.feed(
                {
                    "version": 2,
                    "exclusions": {},
                    "items": [],
                    "health": {"interval_p95": p95, "samples": 100},
                },
                self.now + offset,
            )
        state = self.store.state(self.now + 120)
        self.assertEqual(state["reason"], "Alert performance guard")
        self.assertGreater(state["pause_until"], self.now + 120)
        self.assertEqual(state["ends"], end)

    def feed_health(self, now, p95):
        self.store.feed(
            {
                "version": 2,
                "exclusions": {},
                "health": {"interval_p95": p95, "samples": 100},
            },
            now,
        )

    def test_two_healthy_windows_recover_without_waiting_ten_more_minutes(self):
        self.feed_health(self.now, 2.5)
        self.feed_health(self.now + 60, 3.1)
        self.assertFalse(self.store.can_read("outcome", self.now + 60))
        self.feed_health(self.now + 120, 2.5)
        self.assertFalse(self.store.can_read("outcome", self.now + 120))
        self.feed_health(self.now + 180, 2.5)
        self.assertTrue(self.store.can_read("outcome", self.now + 180))

    def test_health_recovery_never_shortens_marketplace_access_backoff(self):
        identity = self.store.reserve_read("hybrid", "explore", now=self.now)
        self.store.finish_read(identity, "http_429", 0, 0, self.now)
        for offset, p95 in [(60, 3.2), (120, 2.5), (180, 2.5)]:
            self.feed_health(self.now + offset, p95)
        state = self.store.state(self.now + 180)
        self.assertEqual(state["reason"], "Marketplace access backoff")
        self.assertEqual(state["pause_until"], self.now + 3600)
        self.assertFalse(self.store.can_read("outcome", self.now + 180))

    def test_baseline_can_only_recalibrate_during_stable_research_idle_period(self):
        self.feed_health(self.now, 2.5)
        identity = self.store.reserve_read("hybrid", "explore", now=self.now)
        self.store.finish_read(identity, "ok", 1, 1, self.now)
        # A busy research window cannot raise its own comparison baseline.
        for offset in range(60, 541, 60):
            self.feed_health(self.now + offset, 2.8)
        self.assertEqual(self.store.state(self.now + 540)["baseline"], 2.5)
        for offset in range(3601, 4202, 60):
            self.feed_health(self.now + offset, 2.8)
        self.assertEqual(self.store.state(self.now + 4201)["baseline"], 2.8)
        self.assertTrue(self.store.can_read("outcome", self.now + 4201))
        self.feed_health(self.now + 4261, 3.01)
        self.assertFalse(self.store.can_read("outcome", self.now + 4261))

    def test_recent_read_does_not_hide_an_earlier_clean_idle_baseline(self):
        self.feed_health(self.now, 2.5)
        with closing(self.store.connect()) as c, c:
            # A recent timestamp from an earlier deployment is not proof that
            # its single startup baseline was actually calibrated.
            c.execute("UPDATE trial SET baseline_at=?", (self.now - 300,))
            c.executemany(
                "INSERT INTO health VALUES(?,?,0,0)",
                [(self.now - 900 + i * 60, 2.8) for i in range(8)],
            )
            c.execute(
                "INSERT INTO reads(started,arm,kind,status) VALUES(?,'hybrid','explore','ok')",
                (self.now - 60,),
            )
            # Four later idle readings do not yet form a complete baseline;
            # select the earlier contiguous eight rather than mixing the gap.
            c.executemany(
                "INSERT INTO health VALUES(?,?,0,0)",
                [(self.now + 600 + i * 60, 2.9) for i in range(4)],
            )
        self.feed_health(self.now + 801, 2.9)
        self.assertEqual(self.store.state(self.now + 801)["baseline"], 2.8)
        self.assertEqual(
            self.store.state(self.now + 801)["baseline_calibration_windows"], 8
        )
        with closing(self.store.connect()) as c, c:
            c.executemany(
                "INSERT INTO health VALUES(?,?,0,0)",
                [(self.now + 1200 + i * 60, 2.9) for i in range(8)],
            )
        self.feed_health(self.now + 2000, 2.9)
        self.assertEqual(self.store.state(self.now + 2000)["baseline"], 2.8)

    def test_sold_status_requires_native_age_and_price_not_first_seen(self):
        self.add()
        self.store.record_outcome("1", {"state": "sold", "price": 4500}, self.now)
        with closing(self.store.connect()) as c:
            row = c.execute("SELECT * FROM items WHERE id='1'").fetchone()
            self.assertIsNone(row["age_upper"])
        self.assertEqual(self.store.snapshot(self.now)["arms"][2]["fast_sold"], 0)
        self.store.record_outcome(
            "2",
            {
                "state": "sold",
                "price": 4500,
                "age_bounds": (3600, 7200),
                "seller": "102",
            },
            self.now,
        )
        self.assertEqual(self.store.snapshot(self.now)["arms"][2]["fast_sold"], 1)
        self.assertTrue(
            all(
                x["stage"] == "Watching"
                for x in self.store.candidates("hybrid", self.now)
            )
        )

    def test_missing_reserved_and_removed_never_become_sales(self):
        self.add()
        for identity, state in [
            ("1", "unavailable"),
            ("2", "reserved"),
            ("3", "unknown"),
        ]:
            self.store.record_outcome(
                identity, {"state": state, "age_bounds": (1, 60)}, self.now
            )
        self.assertEqual(self.store.snapshot(self.now)["arms"][2]["fast_sold"], 0)

    def test_slow_and_unverifiable_items_stop_consuming_followups_after_one_day(self):
        self.add()
        later = self.now + trial.DAY + 1
        self.store.record_outcome(
            "1",
            {"state": "active", "age_bounds": (2 * trial.DAY, 3 * trial.DAY)},
            later,
        )
        self.store.record_outcome("2", {"state": "unknown"}, later)
        with closing(self.store.connect()) as c:
            states = dict(c.execute("SELECT id,state FROM items WHERE id IN ('1','2')"))
        self.assertEqual(states, {"1": "unsold_24h", "2": "unverified"})
        self.assertEqual(self.store.snapshot(later)["arms"][2]["fast_sold"], 0)

    def test_external_identity_and_wrong_sizes_are_rejected(self):
        self.assertIsNone(trial.item_url("https://evil.invalid/items/123", "123"))
        self.assertIsNone(trial.item_url("https://www.vinted.co.uk/items/124", "123"))
        r = raw("1")
        r["size_title"] = "L / UK 16"
        self.assertIsNone(trial.normalize(r, "12"))
        self.assertEqual(trial.age_bounds("10 hours ago"), (36000, 39600))

    def test_native_sidebar_proof_is_bound_to_the_correct_vinted_item(self):
        markup = """<html><head><link rel="canonical" href="https://www.vinted.co.uk/items/123-mesh-top"></head><body>
        <div class="item-page-sidebar-content"><div class="web_ui__Cell__success">Sold</div>
        <div data-testid="item-page-summary-plugin"><h1>Mesh top</h1></div>
        <div data-testid="item-price">£45.00</div><div itemprop="upload_date">10 hours ago</div>
        <a href="/member/456">Seller</a></div></body></html>"""
        with patch("vinted_page_data.parse_purchase_item", return_value=None):
            parsed = trial.parse_listing(markup, "123")
            self.assertEqual(parsed["state"], "sold")
            self.assertEqual(parsed["price"], 4500)
            self.assertEqual(parsed["age_bounds"], (36000, 39600))
            self.assertEqual(
                trial.parse_listing(
                    markup.replace("www.vinted.co.uk", "evil.invalid"), "123"
                )["state"],
                "identity_unverified",
            )
            self.assertEqual(
                trial.parse_listing(markup, "124")["state"], "identity_unverified"
            )

    def test_worker_api_is_private_and_never_exports_network_credentials(self):
        app = trial.make_app(self.store, "x" * 40)
        client = app.test_client()
        self.assertEqual(client.get("/snapshot.json").status_code, 401)
        good = client.get(
            "/snapshot.json", headers={"Authorization": "Bearer " + "x" * 40}
        )
        self.assertEqual(good.status_code, 200)
        self.assertNotIn("network", good.json)
        self.assertEqual(client.get("/healthz").json, {"status": "ok"})


class BridgeTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()

    def test_daily_stock_route_requires_owner_and_controls_require_csrf(self):
        self.assertEqual(self.client.get("/sourcing").status_code, 302)
        with closing(db.get_db_connection()) as c, c:
            c.execute(
                "UPDATE dashboard_auth SET password_hash=?",
                (generate_password_hash("offline sourcing password"),),
            )
        with self.client.session_transaction() as session:
            session["owner"] = True
            session["csrf"] = "source-csrf"
        with tempfile.TemporaryDirectory() as directory:
            store = trial.Store(Path(directory) / "daily.db", daily_enabled=True)
            from daily_sourcing import DailySourcing

            data = DailySourcing(store).snapshot(time.time())
            with patch.object(bridge, "worker_request", return_value=data):
                response = self.client.get("/sourcing")
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    b"No stock has cleared the evidence checks yet", response.data
                )
                self.assertIn(b"Daily sourcing", response.data)
                data["ready"] = [
                    {
                        "id": "6",
                        "group_id": "x",
                        "pattern": "Example · Horizon",
                        "title": "Horizon <jacket>",
                        "url": "https://www.vinted.co.uk/items/6",
                        "photo": "",
                        "price": 1200,
                        "all_in": 1550,
                        "buy_limit": 3250,
                        "resale_estimate": 5850,
                        "estimated_margin": 4000,
                        "checked_at": time.time(),
                        "size": "S",
                        "condition": "Very good",
                        "cheap_quick_sales": 2,
                        "premium_quick_sales": 3,
                        "seller_count": 5,
                        "comparables": [],
                    }
                ]
                card = self.client.get("/sourcing")
                self.assertEqual(card.status_code, 200)
                self.assertIn("£12.00", card.get_data(as_text=True))
                self.assertIn("Horizon &lt;jacket&gt;", card.get_data(as_text=True))
        self.assertEqual(
            self.client.post("/sourcing/control", data={"action": "pause"}).status_code,
            400,
        )
        with patch.object(
            bridge, "worker_request", return_value={"phase": "paused"}
        ) as request:
            self.assertEqual(
                self.client.post(
                    "/sourcing/control", data={"csrf": "source-csrf", "action": "pause"}
                ).status_code,
                302,
            )
            request.assert_called_once_with("/control", action="pause")

    def test_private_feed_key_is_separate_from_owner_login(self):
        with patch.dict(
            os.environ, {"MSJ_RESEARCH_TOKEN": "x" * 40}, clear=False
        ), patch.object(
            bridge,
            "feed",
            return_value={"version": 2, "exclusions": {}, "health": {}, "items": []},
        ):
            self.assertEqual(
                self.client.get("/internal/research-feed").status_code, 401
            )
            self.assertEqual(
                self.client.get(
                    "/internal/research-feed",
                    headers={"Authorization": "Bearer " + "x" * 40},
                ).status_code,
                200,
            )
            self.assertEqual(
                self.client.get(
                    "/internal/research-feed?since=nan",
                    headers={"Authorization": "Bearer " + "x" * 40},
                ).status_code,
                400,
            )
        self.assertEqual(self.client.get("/research").status_code, 302)

    def test_real_feed_sql_is_read_only_and_exports_no_account_or_solver_credentials(
        self,
    ):
        now = time.time()
        db.add_item_to_db(
            "321",
            "Hollister fur hood",
            1,
            "12.50",
            now,
            None,
            currency="GBP",
            alert={
                "search_name": "Example",
                "content": "Example",
                "url": "https://www.vinted.co.uk/items/321",
                "vinted_details": {"brand": "Hollister"},
            },
        )
        original_queries = db.get_queries()
        with patch(
            "vinted_buyer.network_configuration",
            return_value={
                "proxy": "http://proxy.invalid:1234",
                "api_key": "do-not-export",
                "cookies": "do-not-export",
            },
        ):
            data = bridge.feed(0, now + 1)
        self.assertEqual(data["items"], [])
        self.assertEqual(data["version"], 2)
        self.assertTrue(data["exclusions"]["names"])
        self.assertEqual(data["network"], {"proxy": "http://proxy.invalid:1234"})
        self.assertNotIn("do-not-export", str(data))
        self.assertEqual(db.get_queries(), original_queries)

    def test_comparison_page_is_owner_only_and_research_control_needs_csrf(self):
        before = db.get_queries()
        with closing(db.get_db_connection()) as c, c:
            c.execute(
                "UPDATE dashboard_auth SET password_hash=?",
                (generate_password_hash("offline research password"),),
            )
        with self.client.session_transaction() as s:
            s["owner"] = True
            s["csrf"] = "trial-csrf"
        with tempfile.TemporaryDirectory() as d:
            store = trial.Store(Path(d) / "t.db")
            data = store.snapshot()
            with patch.object(bridge, "snapshot", return_value=data):
                page = self.client.get("/research")
                self.assertEqual(page.status_code, 200)
                for name in trial.ARMS.values():
                    self.assertIn(name.encode(), page.data)
            self.assertEqual(
                self.client.post(
                    "/research/control", data={"action": "pause"}
                ).status_code,
                400,
            )
            with patch.object(
                bridge, "worker_request", return_value={"phase": "paused"}
            ) as call:
                response = self.client.post(
                    "/research/control", data={"action": "pause", "csrf": "trial-csrf"}
                )
                self.assertEqual(response.status_code, 302)
                call.assert_called_once_with("/control", action="pause")
        self.assertEqual(db.get_queries(), before)
