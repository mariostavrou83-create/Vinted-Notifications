"""Persistent live selection, quota preservation, and bounded Vinted scaling."""

import itertools
import unittest
from contextlib import closing

import test_ebay_dashboard
import test_search_controls
from test_ebay_monitor import EbayFixture, item
from werkzeug.datastructures import MultiDict

import db
import ebay_store
import resource_controls
import search_settings
from web_ui_plugin.web_ui import create_app


class LiveSelectionTests(EbayFixture, unittest.TestCase):
    def test_ten_only_swap_persists_and_does_not_reset_budget(self):
        for i in range(1, 13):
            self.enable(i)
        self.assertEqual(
            [s["id"] for s in ebay_store.active_searches()], list(range(1, 11))
        )
        config = ebay_store.configuration()
        self.assertIsNone(ebay_store.reserve_call(config, 1000))
        with self.assertRaisesRegex(ValueError, "up to 10"):
            ebay_store.save_live_selection(range(1, 12))
        ebay_store.save_live_selection(range(3, 13))
        self.assertEqual(
            [s["id"] for s in ebay_store.active_searches()], list(range(3, 13))
        )
        self.assertGreater(ebay_store.reserve_call(config, 1001), 1001)
        self.assertEqual(len(search_settings.active_queries()), 44)
        ebay_store.save_live_selection([])
        self.assertEqual(ebay_store.active_searches(), [])

    def test_swap_rejects_inflight_old_results_and_silently_baselines_new_selection(
        self,
    ):
        first = self.enable(1)
        self.enable(2)
        ebay_store.save_live_selection([1])
        first = search_settings.get_search(1)
        self.snapshot(first, [], 1000)
        self.snapshot(first, [item(101)], 1020)
        ebay_store.save_live_selection([2])
        self.assertEqual(self.outbox()[0]["status"], "cancelled")
        self.assertEqual(self.snapshot(first, [item(102)], 1030), 0)
        second = search_settings.get_search(2)
        self.assertEqual(self.snapshot(second, [item(103)], 1040), 0)
        self.assertEqual(self.snapshot(second, [item(104, created=1041)], 1050), 1)
        ebay_store.save_live_selection([1])
        self.assertEqual(
            self.snapshot(search_settings.get_search(1), [item(105)], 1060), 0
        )

    def test_paused_selected_search_cannot_fill_an_extra_slot_on_resume(self):
        for i in range(1, 12):
            self.enable(i)
        ebay_store.save_live_selection(range(1, 11))
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE search_dashboard SET paused=1 WHERE query_id=1")
        self.assertEqual(len(ebay_store.active_searches()), 9)
        with self.assertRaises(ValueError):
            ebay_store.save_live_selection(range(1, 12))


class ControlsDashboardTests(EbayFixture, unittest.TestCase):
    login = test_ebay_dashboard.SharedDashboardTests.login

    def setUp(self):
        super().setUp()
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()

    def test_selection_and_workload_forms_require_auth_and_validate(self):
        for i in range(1, 12):
            self.enable(i)
        self.login()
        form = MultiDict(
            [("csrf", "test-csrf"), ("action", "live_searches")]
            + [("live_search", str(i)) for i in range(1, 12)]
        )
        self.assertIn(
            "Choose up to 10", self.client.post("/connections", data=form).text
        )
        form.setlist("live_search", ["11"])
        self.assertEqual(self.client.post("/connections", data=form).status_code, 302)
        self.assertEqual([s["id"] for s in ebay_store.active_searches()], [11])
        self.assertIn("Standby", self.client.get("/").text)
        for rate in ("-1", "21", "bad"):
            with self.assertRaises(ValueError):
                resource_controls.save_rate(rate)
        resource_controls.save_rate("5")
        self.assertEqual(resource_controls.request_rate(), 5)


class ScalingTests(test_search_controls.DatabaseFixture, unittest.TestCase):
    simulate = test_search_controls.SchedulerTests.simulate

    def test_fast_mode_restores_one_second_checks_without_overlap(self):
        resource_controls.save_rate("0")
        starts, pending = self.simulate(workers=12, target=1)
        self.assertEqual(resource_controls.summary()["rate"], 0)
        self.assertEqual(resource_controls.summary()["cycle"], 1)
        self.assertLessEqual(pending, 12)
        for query_id in range(1, 45):
            times = [t for q, t in starts if q == query_id]
            self.assertGreaterEqual(len(times), 10)
            self.assertTrue(
                all(0.999 <= b - a <= 1.1 for a, b in itertools.pairwise(times))
            )

    def test_200_searches_are_fair_and_do_not_multiply_request_rate(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.executemany(
                "INSERT INTO queries(id,query,last_item,query_name) VALUES (?,?,100,?)",
                [
                    (
                        i,
                        f"https://www.vinted.co.uk/catalog?search_text=test{i}",
                        f"Search {i}",
                    )
                    for i in range(45, 201)
                ],
            )
        starts, pending = self.simulate(seconds=48, workers=12, target=1)
        self.assertEqual({q for q, t in starts}, set(range(1, 201)))
        self.assertLessEqual(len(starts), 480)
        self.assertLessEqual(pending, 12)
        self.assertTrue(
            all(b[1] - a[1] >= 0.0999 for a, b in itertools.pairwise(starts))
        )
        db.set_parameter("query_refresh_delay", "1")
        self.assertEqual(resource_controls.summary()["cycle"], 20)


if __name__ == "__main__":
    unittest.main()
