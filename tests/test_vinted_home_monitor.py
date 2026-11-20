"""Offline Home worker account, freshness and real outbox regression tests."""

import json
import unittest
from contextlib import closing, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_search_controls import DatabaseFixture

import db
import search_settings
import vinted_home_monitor as home


class HomeMonitorTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("INSERT INTO parameters(key,value) VALUES ('home_monitor_enabled','1')")
        self.now = 1000.0
        self.item = {"id": "500", "title": "Fitted vintage top", "url": "https://www.vinted.co.uk/items/500-top",
                     "price": {"amount": "5.00", "currency_code": "GBP"},
                     "brand_title": "Test", "size_title": "S", "description": "Full original seller description",
                     "description_complete": True,
                     "photos": ["https://images1.vinted.net/t/01_123_photo/123.jpg"]}
        self.age = {"verified": True, "checked_at": self.now, "max_age_seconds": 60}
        self.search = search_settings.get_search(1)
        self.match = {"query_id": 1, "reason": "brand_style"}
        self.fetch = Mock(return_value=("page", self.now))
        self.worker = home.Monitor(clock=lambda: self.now, fetch=self.fetch)
        self.patches = [
            patch.object(home, "_account_ready", return_value=None),
            patch.object(home, "active_searches", return_value=[self.search]),
            patch.object(home, "parse_home", return_value={"reason": "ok", "authenticated": True, "items": [self.item]}),
            patch.object(home, "parse_listing", side_effect=lambda *args: {"item": self.item, "age": self.age, "reason": "ok"}),
            patch.object(home, "match_candidate", return_value=self.match),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def rows(self):
        with closing(search_settings.connection()) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM alert_outbox")]

    def status(self, role="seller"):
        return json.loads(db.get_parameter("home_monitor_status_" + role))

    def discover(self):
        self.worker.tick()
        self.now += 1
        self.worker.tick()

    def test_original_account_first_buyer_at_half_minute(self):
        self.worker.tick()
        self.assertEqual(self.fetch.call_args.args, ("seller", home.buyer.BASE + "/"))
        self.worker.pending["seller"].clear()
        self.now = 1029
        self.worker.tick()
        self.assertEqual(self.fetch.call_count, 1)
        self.now = 1030
        self.worker.tick()
        self.assertEqual(self.fetch.call_args.args, ("buyer", home.buyer.BASE + "/"))

    def test_fresh_alert_preserves_watermark_search_and_description(self):
        self.discover()
        row = self.rows()[0]
        self.assertEqual((row["query_id"], row["item_id"]), (1, "500"))
        self.assertEqual(search_settings.get_search(1), self.search)
        with closing(search_settings.connection()) as conn:
            details = json.loads(conn.execute("SELECT payload FROM vinted_alert_details WHERE item_id='500'").fetchone()[0])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0], 44)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM vinted_buy_attempts").fetchone()[0], 0)
        self.assertEqual(details["description"], self.item["description"])
        self.assertFalse(details["description_pending"])
        self.assertEqual(details["home_discovery"]["expires_at"], 1120)
        self.assertEqual(details["home_discovery"]["source_role"], "seller")
        self.assertEqual(details["photos"], self.item["photos"])
        self.assertEqual(self.status()["alerts"], 1)

    def test_old_unknown_future_and_cached_age_cannot_alert(self):
        for age in (
            {"verified": False, "checked_at": 1000, "max_age_seconds": None},
            {"verified": True, "checked_at": 1000, "max_age_seconds": 240},
            {"verified": True, "checked_at": 1000, "max_age_seconds": 180},
            {"verified": True, "checked_at": 1000, "max_age_seconds": float("nan")},
        ):
            with self.subTest(age=age):
                self.age = age
                self.worker = home.Monitor(clock=lambda: self.now, fetch=self.fetch)
                self.discover()
                self.assertEqual(self.rows(), [])
        self.assertFalse(home._publish(self.item, self.search, self.match,
                                       {"verified": True, "checked_at": 2000, "max_age_seconds": 60}, "seller", now=1000))
        self.assertFalse(home._publish(self.item, self.search, self.match,
                                       {"verified": True, "checked_at": 800, "max_age_seconds": 60}, "seller", now=1000))

    def test_both_feeds_and_searches_share_global_deduplication(self):
        self.discover()
        self.now = 1030
        self.worker.tick()
        self.assertFalse(self.worker.pending["buyer"])
        self.assertEqual(len(self.rows()), 1)
        self.assertFalse(db.add_item_to_db(id="500", title="same", query_id=2, price="5", timestamp=1000, photo_url=None))
        self.assertEqual(len(self.rows()), 1)

    def test_disabled_performs_no_reads(self):
        db.set_parameter("home_monitor_enabled", "0")
        self.worker.tick()
        self.fetch.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_disable_during_detail_does_not_enqueue(self):
        self.worker.tick()
        def disable(*args):
            db.set_parameter("home_monitor_enabled", "0")
            return "page", 1000
        self.fetch.side_effect = disable
        self.now += 1
        self.worker.tick()
        self.assertEqual(self.rows(), [])

    def test_challenge_stops_role_without_replay_or_raw_error(self):
        self.fetch.side_effect = home.buyer.BuyerError("SECRET should not be logged", reason="security_challenge", status=403)
        self.worker.tick()
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(self.status()["code"], "security_challenge")
        self.assertNotIn("SECRET", db.get_parameter("home_monitor_status_seller"))
        self.now += 1
        self.worker.tick()
        self.assertEqual(self.fetch.call_count, 1)
        self.assertFalse(self.worker.pending["seller"])

    def test_provider_cooldown_survives_worker_restart(self):
        self.fetch.side_effect = home.buyer.BuyerError("private challenge", reason="rate_limited", status=429, retry_after=1800)
        self.worker.tick()
        self.assertEqual(self.status()["retry_at"], 2800)
        restarted_fetch = Mock(return_value=("page", self.now))
        restarted = home.Monitor(clock=lambda: self.now, fetch=restarted_fetch)
        restarted.tick()
        restarted_fetch.assert_not_called()
        self.now = 1030
        restarted.tick()
        self.assertEqual(restarted_fetch.call_args.args[0], "buyer")
        self.assertEqual(restarted.retry_at["seller"], 2800)

    def test_source_failure_preserves_only_fixed_detail_code(self):
        with patch.object(home, "parse_home", return_value={"reason": "home_not_authenticated", "items": []}):
            self.worker.tick()
        self.assertEqual(self.status()["detail_code"], "home_not_authenticated")
        self.assertEqual(self.status()["retry_at"], 1300)

    def test_detail_budget_and_deadline_are_bounded(self):
        with patch.object(home, "parse_home", return_value={"reason": "ok", "items": [dict(self.item, id=str(i), url=f"https://www.vinted.co.uk/items/{i}") for i in range(500, 600)]}):
            self.worker.tick()
        self.assertEqual(len(self.worker.pending["seller"]), 6)
        self.now = 1030
        self.worker.tick()
        self.assertFalse(self.worker.pending["seller"])
        self.assertEqual(self.fetch.call_args.args, ("buyer", home.buyer.BASE + "/"))

    def test_global_banwords_and_country_settings_are_preserved(self):
        db.set_parameter("banwords", "fitted|||aviator")
        self.assertFalse(home.global_filters_allow(self.item))
        db.set_parameter("banwords", "")
        db.add_to_allowlist("GB")
        self.assertFalse(home.global_filters_allow(self.item))
        self.assertTrue(home.global_filters_allow(dict(self.item, seller_country="GB")))

    def test_page_read_keeps_role_and_no_solver_or_renewal(self):
        client = Mock(account_role="seller")
        account = SimpleNamespace(exclusive=Mock(return_value=nullcontext()), connected_client=Mock(return_value=client))
        with patch.object(home, "seller", account), patch.object(home, "read_html", return_value=("html", 1000)) as read, patch.object(home, "yield_to_purchase") as priority:
            self.assertEqual(home._page("seller", home.buyer.BASE + "/"), ("html", 1000))
        account.connected_client.assert_called_once_with(solve_challenges=False, allow_refresh=False)
        read.assert_called_once_with(client, home.buyer.BASE + "/")
        self.assertEqual(priority.call_count, 2)
        client.session.close.assert_called_once()

    def test_page_role_mismatch_closes_client_before_read(self):
        client = Mock(account_role="buyer")
        account = SimpleNamespace(exclusive=Mock(return_value=nullcontext()), connected_client=Mock(return_value=client))
        with patch.object(home, "seller", account), patch.object(home, "read_html") as read, patch.object(home, "yield_to_purchase"), self.assertRaises(home.buyer.BuyerError):
            home._page("seller", home.buyer.BASE + "/")
        read.assert_not_called()
        client.session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
