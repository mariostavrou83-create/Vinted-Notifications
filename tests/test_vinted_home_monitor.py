"""Offline Home worker account, freshness and real outbox regression tests."""

import json
import multiprocessing
import signal
import sqlite3
import time
import unittest
from contextlib import closing, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_search_controls import DatabaseFixture

import db
import search_settings
import vinted_home_monitor as home


def run_stalled_home(entered):
    def stall():
        entered.set()
        time.sleep(30)

    with patch.object(home, "Monitor") as factory, patch.object(home, "enabled", return_value=True), patch.object(
        home, "WORKER_STALL_SECONDS", 1
    ):
        factory.return_value.tick.side_effect = stall
        home.run()


@unittest.skipUnless(hasattr(signal, "SIGALRM"), "requires a POSIX timer")
class HomeWorkerTests(unittest.TestCase):
    def test_real_stalled_child_exits_and_supervisor_starts_exactly_one_replacement(self):
        from process_watchdog import ensure_running

        entered = multiprocessing.Event()
        stopped = multiprocessing.Process(target=run_stalled_home, args=(entered,))
        stopped.start()
        try:
            self.assertTrue(entered.wait(5))
            stopped.join(5)
            self.assertEqual(stopped.exitcode, -signal.SIGALRM)
            stop = multiprocessing.Event()
            replacement = ensure_running(stopped, stop.wait, name="vinted-home-monitor")
            try:
                self.assertTrue(replacement.is_alive())
                self.assertIs(ensure_running(replacement, stop.wait, name="vinted-home-monitor"), replacement)
            finally:
                stop.set()
                replacement.join(5)
            self.assertEqual(replacement.exitcode, 0)
        finally:
            if stopped.is_alive():
                stopped.kill()
                stopped.join(5)

    def test_idle_ticks_renew_timer_and_exceptions_do_not(self):
        with patch.object(home, "Monitor") as factory, patch.object(home, "enabled", return_value=False), patch.object(
            home.signal, "alarm"
        ) as alarm, patch.object(home.time, "sleep"):
            factory.return_value.tick.side_effect = [None, None, OSError(), SystemExit()]
            home.run()
        self.assertEqual([call.args[0] for call in alarm.call_args_list], [180, 180, 180, 0])


class HomeMonitorTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT INTO parameters(key,value) VALUES ('home_monitor_enabled','1')"
            )
        self.now = 1000.0
        self.item = {
            "id": "500",
            "title": "Fitted vintage top",
            "category_ids": ["1904", "4", "12"],
            "url": "https://www.vinted.co.uk/items/500-top",
            "price": {"amount": "5.00", "currency_code": "GBP"},
            "brand_title": "Test",
            "size_title": "S",
            "description": "Full original seller description",
            "description_complete": True,
            "photos": ["https://images1.vinted.net/t/01_123_photo/123.jpg"],
        }
        self.age = {"verified": True, "checked_at": self.now, "max_age_seconds": 60}
        self.search = search_settings.get_search(1)
        self.match = {"query_id": 1, "reason": "brand_style"}
        self.fetch = Mock(return_value=("page", self.now))
        self.worker = home.Monitor(clock=lambda: self.now, fetch=self.fetch)
        self.patches = [
            patch.object(home, "_account_ready", return_value=None),
            patch.object(home, "active_searches", return_value=[self.search]),
            patch.object(
                home,
                "parse_home",
                return_value={
                    "reason": "ok",
                    "authenticated": True,
                    "items": [self.item],
                },
            ),
            patch.object(
                home,
                "parse_listing",
                side_effect=lambda *args, **kwargs: {
                    "item": self.item,
                    "age": self.age,
                    "reason": "ok",
                },
            ),
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

    def test_only_original_account_is_polled_across_multiple_intervals(self):
        self.worker.tick()
        self.assertEqual(self.fetch.call_args.args, ("seller", home.buyer.BASE + "/"))
        self.worker.pending["seller"].clear()
        self.now = 1029
        self.worker.tick()
        self.assertEqual(self.fetch.call_count, 1)
        self.now = 1030
        self.worker.tick()
        self.assertEqual(self.fetch.call_count, 1)
        self.now = 1061
        self.worker.tick()
        self.assertEqual(self.fetch.call_count, 2)
        self.assertTrue(all(call.args[0] == "seller" for call in self.fetch.call_args_list))

    def test_fast_home_only_accelerates_after_successful_checks(self):
        home.controls.save_target("1")
        self.worker = home.Monitor(clock=lambda: self.now, fetch=self.fetch)
        home.parse_home.return_value["items"] = []
        speeds = {self.worker.scheduled_seconds}
        for self.now in range(1000, 1301):
            self.fetch.return_value = ("page", self.now)
            self.worker.tick()
            speeds.add(self.worker.scheduled_seconds)

        self.assertEqual(speeds, {8, 4, 2, 1})
        self.assertEqual(self.status()["target_seconds"], 1)
        self.assertEqual(self.status()["measured_seconds"], 1)
        self.assertTrue(all(call.args[0] == "seller" for call in self.fetch.call_args_list))

    def test_fast_home_runs_listing_checks_and_restarts_into_provider_cooldown(self):
        home.controls.save_target("1")
        calls = []

        def fetch(role, url):
            calls.append((role, url, self.now))
            return "page", self.now

        self.fetch.side_effect = fetch
        self.worker = home.Monitor(clock=lambda: self.now, fetch=self.fetch)
        self.worker.scheduled_seconds = 1
        for quarter_second in range(240):
            self.now = 1000 + quarter_second / 4
            item_id = str(500 + quarter_second // 20)
            home.parse_home.return_value["items"] = [
                dict(self.item, id=item_id, url=f"{home.buyer.BASE}/items/{item_id}-top")
            ]
            self.worker.tick()
        listings = [at for _, url, at in calls if "/items/" in url]
        self.assertEqual(len(listings), home.MAX_DETAILS)
        self.assertLess(listings[0] - 1000, 1)
        self.assertGreater(sum(url == home.buyer.BASE + "/" for _, url, _ in calls), 50)

        self.now = 1061
        self.fetch.side_effect = home.buyer.BuyerError("Test cooldown", 429, reason="rate_limited", retry_after=950)
        self.worker.tick()
        self.assertEqual(self.status()["retry_at"], 2011)
        self.now = 2000
        self.worker = home.Monitor(clock=lambda: self.now, fetch=self.fetch)
        self.fetch.reset_mock()
        self.worker.tick()
        self.fetch.assert_not_called()
        self.assertEqual(self.worker.speedup_at, 2611)

    def test_fresh_alert_preserves_watermark_search_and_description(self):
        self.discover()
        row = self.rows()[0]
        self.assertEqual((row["query_id"], row["item_id"]), (1, "500"))
        self.assertEqual(search_settings.get_search(1), self.search)
        with closing(search_settings.connection()) as conn:
            details = json.loads(
                conn.execute(
                    "SELECT payload FROM vinted_alert_details WHERE item_id='500'"
                ).fetchone()[0]
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0], 44
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM vinted_buy_attempts").fetchone()[0],
                0,
            )
        self.assertEqual(details["description"], self.item["description"])
        self.assertFalse(details["description_pending"])
        self.assertEqual(details["home_discovery"]["expires_at"], 1120)
        self.assertEqual(details["home_discovery"]["source_role"], "seller")
        self.assertEqual(details["photos"], self.item["photos"])
        self.assertEqual(self.status()["alerts"], 1)

    def test_cached_listing_inside_three_minutes_keeps_its_true_delivery_deadline(self):
        from vinted_alerts import get_details, home_discovery_fresh
        from vinted_home_transport import response_time

        origin = 1795212000
        self.now = origin + 40
        observed = response_time({"Date": "Fri, 20 Nov 2026 22:00:00 GMT", "Age": "40"}, self.now)
        self.fetch.return_value = ("page", observed)
        self.age = {"verified": True, "checked_at": observed, "max_age_seconds": 120}
        self.discover()
        row, = self.rows()
        details = get_details(row)
        self.assertEqual(details["home_discovery"]["expires_at"], origin + 60)
        self.assertTrue(home_discovery_fresh(details, now=origin + 59))
        self.assertFalse(home_discovery_fresh(details, now=origin + 60))

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
        self.assertFalse(
            home._publish(
                self.item,
                self.search,
                self.match,
                {"verified": True, "checked_at": 2000, "max_age_seconds": 60},
                "seller",
                now=1000,
            )
        )
        self.assertFalse(
            home._publish(
                self.item,
                self.search,
                self.match,
                {"verified": True, "checked_at": 800, "max_age_seconds": 60},
                "seller",
                now=1000,
            )
        )

    def test_original_feed_and_searches_share_global_deduplication(self):
        self.discover()
        self.now = 1030
        self.worker.tick()
        self.assertNotIn("buyer", self.worker.pending)
        self.assertEqual(len(self.rows()), 1)
        self.assertFalse(
            db.add_item_to_db(
                id="500",
                title="same",
                query_id=2,
                price="5",
                timestamp=1000,
                photo_url=None,
            )
        )
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
        self.fetch.side_effect = home.buyer.BuyerError(
            "SECRET should not be logged", reason="security_challenge", status=403
        )
        self.worker.tick()
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(self.status()["code"], "security_challenge")
        self.assertNotIn("SECRET", db.get_parameter("home_monitor_status_seller"))
        self.now += 1
        self.worker.tick()
        self.assertEqual(self.fetch.call_count, 1)
        self.assertFalse(self.worker.pending["seller"])

    def test_busy_account_returns_unused_page_slot_and_rechecks_feed_in_five_seconds(self):
        self.fetch.side_effect = [home.buyer.BuyerError("Local account lock", reason="busy"),
                                  ("home", 1005.0)]
        self.worker.tick()
        self.assertEqual(self.status()["code"], "busy")
        self.assertFalse(self.worker.page_reads["seller"])
        self.now = 1004
        self.worker.tick()
        self.assertEqual(self.fetch.call_count, 1)
        self.now = 1005
        self.worker.tick()
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(self.fetch.call_args.args, ("seller", home.buyer.BASE + "/"))
        self.assertEqual(list(self.worker.page_reads["seller"]), [1005])
        self.assertEqual(self.status()["code"], "checked")

    def test_provider_cooldown_survives_worker_restart(self):
        self.fetch.side_effect = home.buyer.BuyerError(
            "private challenge", reason="rate_limited", status=429, retry_after=1800
        )
        self.worker.tick()
        self.assertEqual(self.status()["retry_at"], 2800)
        restarted_fetch = Mock(return_value=("page", self.now))
        restarted = home.Monitor(clock=lambda: self.now, fetch=restarted_fetch)
        restarted.tick()
        restarted_fetch.assert_not_called()
        self.now = 1030
        restarted.tick()
        restarted_fetch.assert_not_called()
        self.assertEqual(restarted.retry_at["seller"], 2800)

    def test_signed_in_empty_feed_retries_at_normal_cadence_and_backs_off_if_repeated(self):
        empty = {"reason": "home_cards_unavailable", "authenticated": True, "items": []}
        with patch.object(home, "parse_home", return_value=empty):
            for delay in (60, 120, 240, 300, 300):
                self.worker.tick()
                self.assertEqual(self.status()["retry_at"], self.now + delay)
                self.assertEqual(self.status()["code"], "source_unavailable")
                self.worker = home.Monitor(clock=lambda: self.now, fetch=self.fetch)
                before = self.fetch.call_count
                self.now += delay - 1
                self.worker.tick()
                self.assertEqual(self.fetch.call_count, before)
                self.now += 1
        self.worker.tick()
        self.assertEqual(self.status()["code"], "checked")
        self.assertEqual(self.status()["read_delays"]["home"], 0)
        self.assertFalse(self.rows())
        self.now += home.INTERVAL
        with patch.object(home, "parse_home", return_value=empty):
            self.worker.tick()
        self.assertEqual(self.status()["retry_at"], self.now + 60)

    def test_empty_feed_without_confirmed_signin_keeps_full_pause(self):
        with patch.object(home, "parse_home", return_value={"reason": "home_cards_unavailable", "items": []}):
            self.worker.tick()
        self.assertEqual(self.status()["retry_at"], self.now + 300)
        self.assertEqual(self.status()["read_delays"]["home"], 0)

    def test_transient_read_failures_back_off_per_stage_and_honour_provider_delay(self):
        self.fetch.side_effect = home.buyer.BuyerError("temporary", reason="network")
        self.worker.tick()
        self.assertEqual(self.status()["retry_at"], 1060)
        self.now = 1060
        self.fetch.side_effect = None
        self.worker.tick()
        self.assertEqual(self.status()["read_delays"]["home"], 0)
        for identity, (expected_delay, requested) in enumerate(((60, 0), (120, 0), (310, 310)), 501):
            self.now += 1
            self.fetch.side_effect = home.buyer.BuyerError("unavailable", status=503, reason="unreadable", retry_after=requested)
            self.worker.tick()
            self.assertEqual(self.status()["retry_at"], self.now + expected_delay)
            self.worker = home.Monitor(clock=lambda: self.now, fetch=self.fetch)
            self.now += expected_delay
            self.fetch.side_effect = None
            # A new recommendation still has to be checked after each wait.
            self.item.update(id=str(identity), url=f"https://www.vinted.co.uk/items/{identity}")
            self.worker.tick()
            self.assertEqual(self.status()["stage"], "home")
        self.now += 1
        self.worker.tick()
        self.assertEqual(self.status()["read_delays"], {"home": 0, "listing": 0})

    def test_identity_network_error_keeps_full_account_cooldown(self):
        self.fetch.side_effect = home.buyer.BuyerError("temporary", reason="network", stage="identity")
        self.worker.tick()
        self.assertEqual(self.status()["retry_at"], 1300)

    def test_optional_listing_failures_cannot_silence_verified_home_cards(self):
        first = dict(self.item, id="700", home_available=True, home_created_at=1055)
        second = dict(first, id="701", home_created_at=1115)
        stale = dict(first, id="702", home_created_at=800)
        book = dict(first, id="703", category_ids=["2309", "3037"])
        cards = [self.item]
        self.fetch.side_effect = lambda role, url: ("page", self.now)
        with patch.object(home, "parse_home", return_value={"reason": "ok", "items": cards}):
            self.worker.tick()
            for at, delay in ((1001, 60), (1061, 120)):
                self.now = at
                with patch.object(self.worker, "fetch", side_effect=home.buyer.BuyerError(
                    "temporary", reason="network", stage="listing_page"
                )):
                    self.worker.tick()
                self.assertEqual(self.status()["code"], "listing_deferred")
                self.assertEqual(self.status()["detail_retry_at"], at + delay)
                self.assertEqual(self.status()["retry_at"], 0)
                if at == 1001:
                    cards.extend([first, stale, book])
                    self.now = 1060
                    self.worker.tick()
                    self.assertEqual([r["item_id"] for r in self.rows()], ["700"])
            self.worker = home.Monitor(clock=lambda: self.now, fetch=self.fetch)
            self.now = 1120
            cards.append(second)
            self.worker.tick()
            self.now = 1121
            self.worker.tick()
            self.assertEqual({r["item_id"] for r in self.rows()}, {"700", "701"})
            self.assertTrue(all(call.args == ("seller", home.buyer.BASE + "/") for call in self.fetch.call_args_list))
            self.now = 1180
            newly_listed = dict(self.item, id="704", url="https://www.vinted.co.uk/items/704")
            cards.append(newly_listed)
            self.worker.tick()
            self.now = 1181
            self.worker.tick()
            self.assertEqual(self.fetch.call_args.args, ("seller", newly_listed["url"]))
            self.assertEqual(self.status()["detail_retry_at"], 0)
            self.assertEqual(self.status()["last_detail_ok"], 1181)

    def test_wait_or_account_denial_from_a_listing_still_stops_every_seller_read(self):
        for reason, status, wait in (("rate_limited", 429, 420), ("unreadable", 503, 420),
                                     ("security_challenge", 503, 0), ("credentials", 401, 0),
                                     ("unreadable", 404, 420)):
            with self.subTest(reason=reason, status=status):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("DELETE FROM parameters WHERE key='home_monitor_status_seller'")
                    conn.execute("DELETE FROM vinted_home_pages WHERE role='seller'")
                self.now += 1000
                self.worker = home.Monitor(clock=lambda: self.now, fetch=self.fetch)
                self.fetch.side_effect = None
                self.worker.tick()
                self.now += 1
                self.fetch.side_effect = home.buyer.BuyerError("refused", reason=reason, status=status,
                                                              stage="listing_page", retry_after=wait)
                self.worker.tick()
                self.assertEqual(self.status()["retry_at"], self.now + (wait or 300))
                restart = home.Monitor(clock=lambda: self.now, fetch=self.fetch)
                self.now += (wait or 300) - 1
                count = self.fetch.call_count
                restart.tick()
                self.assertEqual(self.fetch.call_count, count)

    def test_listing_limit_resumes_with_home_then_spaces_details_across_restart(self):
        self.worker.tick()
        self.now = 1001
        self.fetch.side_effect = home.buyer.BuyerError("limited", status=429, reason="rate_limited", retry_after=85)
        self.worker.tick()
        self.assertEqual(self.status()["retry_at"], 1086)
        self.assertEqual(self.status()["rate_limit_level"], 1)
        self.worker = home.Monitor(clock=lambda: self.now, fetch=self.fetch)
        self.fetch.reset_mock(side_effect=True)
        self.now = 1085
        self.worker.tick()
        self.fetch.assert_not_called()
        self.now = 1086
        self.worker.tick()
        self.fetch.assert_called_once_with("seller", home.buyer.BASE + "/")
        self.now = 1095
        self.worker.tick()
        self.assertEqual(self.fetch.call_count, 1)
        self.now = 1096
        self.fetch.side_effect = home.buyer.BuyerError("limited", status=429, reason="rate_limited")
        self.worker.tick()
        self.assertEqual(self.fetch.call_args.args, ("seller", self.item["url"]))
        self.assertEqual(self.status()["retry_at"], 1216)
        self.assertEqual(self.status()["rate_limit_level"], 2)
        self.fetch.reset_mock(side_effect=True)
        self.now = 1215
        self.worker.tick()
        self.fetch.assert_not_called()
        self.now = 1216
        self.worker.tick()
        self.fetch.assert_called_once_with("seller", home.buyer.BASE + "/")
        self.assertEqual(self.status()["rate_limit_level"], 2)
        self.now = 1816
        self.worker.tick()
        self.assertEqual(self.status()["rate_limit_level"], 1)

    def test_single_429_without_a_delay_and_verified_card_after_recovery(self):
        self.fetch.side_effect = home.buyer.BuyerError("limited", status=429, reason="rate_limited")
        self.worker.tick()
        self.assertEqual(self.status()["retry_at"], 1060)
        self.fetch.side_effect = None
        self.now = 1060
        self.fetch.return_value = ("home", self.now)
        card = dict(self.item, home_available=True, home_created_at=1050)
        unknown = dict(self.item, id="501", category_ids=[])
        unavailable = dict(self.item, id="504", home_available=False)
        known = dict(self.item, id="499")
        with patch.object(home, "parse_home", return_value={"reason": "ok", "items": [unknown, unavailable, known, card]}):
            self.worker.tick()
        self.assertEqual([row["item_id"] for row in self.rows()], ["500"])
        self.assertEqual([item["id"] for item in self.worker.pending["seller"]], ["499", "501"])
        self.assertEqual(self.fetch.call_count, 2)

    def test_busy_optional_guide_read_still_queues_a_verified_home_recommendation(self):
        with patch.object(home, "active_searches", side_effect=sqlite3.OperationalError("busy")) as guides:
            self.discover()
        guides.assert_called_once()
        row, = self.rows()
        self.assertEqual(row["item_id"], "500")
        self.assertIsNone(row["query_id"])
        self.assertEqual(self.worker.retry_at["seller"], 0)
        self.assertEqual(self.status()["code"], "checked")

    def test_expiring_reads_allow_a_second_home_page_to_find_a_new_listing(self):
        cards = [dict(self.item, id=str(i), url=f"https://www.vinted.co.uk/items/{i}")
                 for i in range(500, 525)]
        self.worker.examined.update({c["id"]: 980 for c in cards[:23]})
        previous_reads = [946, 951, 956, 961]
        self.worker.page_reads["seller"].extend(previous_reads)
        read_times = list(previous_reads)
        def fetch(role, url):
            self.assertEqual(role, "seller")
            read_times.append(self.now)
            return "page", self.now

        fresh = dict(self.item, id="625", url="https://www.vinted.co.uk/items/625",
                     home_available=True, home_created_at=1030)
        self.age["max_age_seconds"] = 600
        self.fetch.side_effect = fetch
        with patch.object(home, "parse_home", side_effect=[
            {"reason": "ok", "items": cards}, {"reason": "ok", "items": cards[1:] + [fresh]}
        ]) as pages:
            for instant in range(1000, 1033):
                self.now = instant
                self.worker.tick()
        self.assertEqual(pages.call_count, 2)
        self.assertEqual([r["item_id"] for r in self.rows()], ["625"])
        for instant in read_times:
            self.assertLessEqual(sum(instant - 60 < t <= instant for t in read_times), home.PAGE_BUDGET)

    def test_restart_skips_proven_old_cards_and_checks_the_new_card_first(self):
        old = [dict(self.item, id=str(i), url=f"https://www.vinted.co.uk/items/{i}")
               for i in range(900, 924)]
        home.activity.save_feed("seller", old, 998, seen_at=1000)
        home.activity.save_feed("seller", old, 1177, seen_at=1179)
        self.now = 1180
        self.worker = home.Monitor(clock=lambda: self.now, fetch=self.fetch)
        self.fetch.side_effect = lambda *args: ("page", self.now)
        self.age = {"verified": True, "checked_at": 1181, "max_age_seconds": 60}
        with patch.object(home, "parse_home", return_value={"reason": "ok", "items": old + [self.item]}):
            self.worker.tick()
        self.assertEqual(self.status()["previously_seen_expired"], 24)
        self.assertEqual([card["id"] for card in self.worker.pending["seller"]], ["500"])
        self.assertEqual(self.rows(), [])
        self.now += 1
        self.worker.tick()
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(self.fetch.call_args.args, ("seller", self.item["url"]))
        self.assertEqual([r["item_id"] for r in self.rows()], ["500"])

    def test_prior_page_time_or_recent_first_sighting_never_establishes_age(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("INSERT INTO vinted_home_pages VALUES ('seller', 600, ?)",
                         (json.dumps([{"id": "500"}]),))
        seen = home.activity.save_feed("seller", [self.item], 800, seen_at=1000)
        self.assertEqual(seen, {"500": 1000})
        self.now = 1179
        self.fetch.side_effect = lambda *args: ("page", self.now)
        self.age = {"verified": False}
        self.worker.tick()
        self.assertEqual(self.status()["previously_seen_expired"], 0)
        self.assertEqual([card["id"] for card in self.worker.pending["seller"]], ["500"])
        self.worker.tick()
        self.assertEqual(self.status()["age_unverified"], 1)
        self.assertEqual(self.rows(), [])

    def test_failed_snapshot_does_not_block_current_home_checks(self):
        with patch.object(home.activity, "save_feed", side_effect=sqlite3.OperationalError("busy")):
            self.discover()
        self.assertEqual([row["item_id"] for row in self.rows()], ["500"])

    def test_fourth_new_card_on_a_repeat_heavy_page_reaches_the_outbox(self):
        cards = [dict(self.item, id=str(i), url=f"https://www.vinted.co.uk/items/{i}")
                 for i in range(500, 525)]
        self.worker.examined.update({card["id"]: 990 for card in cards[:20]})

        def listing(_html, identity, observed, *, home_card):
            self.assertEqual(home_card["id"], identity)
            age = {"verified": True, "checked_at": observed,
                   "max_age_seconds": 60 if identity == "521" else 600}
            item = dict(self.item, id=identity, url=f"https://www.vinted.co.uk/items/{identity}")
            return {"reason": "ok", "item": item, "age": age}

        self.fetch.side_effect = lambda *args: ("page", self.now)
        with patch.object(home, "parse_home", return_value={"reason": "ok", "items": cards}), \
             patch.object(home, "parse_listing", side_effect=listing):
            for offset in (0, 1, 6, 11, 16, 21, 32):
                self.now = 1000 + offset
                self.worker.tick()
        self.assertEqual([r["item_id"] for r in self.rows()], ["521"])
        self.assertEqual(self.fetch.call_count, home.PAGE_BUDGET)
        self.assertEqual(self.fetch.call_args.args, ("seller", home.buyer.BASE + "/"))

    def test_recommended_incomplete_listing_can_qualify_on_one_later_check(self):
        self.age = {"verified": False}
        self.discover()
        self.assertEqual(self.rows(), [])

        self.now = 1060
        self.age = {"verified": True, "checked_at": self.now, "max_age_seconds": 70}
        self.fetch.return_value = ("page", self.now)
        self.discover()
        self.assertEqual([r["item_id"] for r in self.rows()], ["500"])
        self.now = 1120
        self.worker.tick()
        self.assertFalse(self.worker.pending["seller"])
        self.assertEqual(len(self.rows()), 1)

    def test_inconclusive_recheck_uses_only_spare_slots_and_expires(self):
        self.age = {"verified": False}
        self.discover()
        other = dict(self.item, id="499")
        with patch.object(home, "parse_home", return_value={"reason": "ok", "items": [self.item, other]}):
            self.now = 1060
            self.worker.tick()
        self.assertEqual([c["id"] for c in self.worker.pending["seller"]], ["499", "500"])
        self.worker.pending["seller"].popleft()
        self.now += 1
        self.worker.tick()
        self.assertEqual(self.rows(), [])
        self.now = 1120
        self.worker.tick()
        self.assertFalse(self.worker.pending["seller"])
        self.worker.recheck_until["500"] = self.now + 20
        self.now += 60
        self.worker.tick()
        self.assertFalse(self.worker.pending["seller"])
        self.assertFalse(self.worker.recheck_until)

    def test_removed_listing_does_not_pause_remaining_home_items(self):
        self.worker.tick()
        self.worker.pending["seller"].append(dict(self.item,id="501"))
        self.fetch.side_effect = home.buyer.BuyerError("Gone", status=404, reason="http_error")
        self.now += 1
        self.worker.tick()
        self.assertEqual(self.worker.retry_at["seller"],0)
        self.assertEqual(self.status()["listing_unavailable"],1)
        self.assertEqual(self.worker.pending["seller"][0]["id"],"501")

    def test_listing_redirect_skips_only_item_without_following_or_cooldown(self):
        with patch.object(
            home,
            "parse_home",
            return_value={
                "reason": "ok",
                "authenticated": True,
                "items": [
                    self.item,
                    dict(
                        self.item,
                        id="501",
                        url="https://www.vinted.co.uk/items/501-top",
                    ),
                ],
            },
        ):
            self.worker.tick()
        self.fetch.side_effect = home.buyer.BuyerError(
            "private location must not persist", status=307, reason="listing_redirect"
        )
        self.now += 1
        self.worker.tick()
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(self.status()["skipped_redirect"], 1)
        self.assertEqual(self.status()["http_status"], 307)
        self.assertEqual(self.status()["retry_at"], 0)
        self.assertEqual(self.status()["code"], "checked")
        self.assertEqual(
            [item["id"] for item in self.worker.pending["seller"]], ["500"]
        )
        self.assertIn("501", self.worker.examined)
        self.assertNotIn(
            "private location", db.get_parameter("home_monitor_status_seller")
        )
        self.fetch.side_effect = None
        self.now += 1
        self.worker.tick()
        self.assertEqual(self.fetch.call_count, 2)
        self.now += home.DETAIL_SPACING_SECONDS - 1
        self.worker.tick()
        self.assertEqual(self.fetch.call_count, 3)
        self.assertEqual(len(self.rows()), 1)

    def test_security_or_auth_redirect_still_pauses_role(self):
        for reason in (
            "security_challenge",
            "session_refresh",
            "signin_redirect",
            "home_redirect",
            "redirect",
        ):
            with self.subTest(reason=reason):
                self.worker = home.Monitor(clock=lambda: self.now, fetch=self.fetch)
                self.worker.retry_at = dict.fromkeys(home.ROLES, 0)
                self.fetch.side_effect = None
                self.worker.tick()
                self.fetch.side_effect = home.buyer.BuyerError(
                    "private", status=307, reason=reason
                )
                self.now += 1
                self.worker.tick()
                self.assertEqual(self.status()["skipped_redirect"], 0)
                self.assertEqual(self.status()["retry_at"], self.now + 300)
                self.assertFalse(self.worker.pending["seller"])

    def test_age_counts_distinguish_missing_from_old(self):
        self.age = {"verified": False, "checked_at": 1000, "max_age_seconds": None}
        self.discover()
        self.assertEqual(
            (self.status()["age_unverified"], self.status()["age_expired"]), (1, 0)
        )
        self.worker = home.Monitor(clock=lambda: self.now, fetch=self.fetch)
        self.age = {"verified": True, "checked_at": 1000, "max_age_seconds": 240}
        self.discover()
        self.assertEqual(
            (self.status()["age_unverified"], self.status()["age_expired"]), (0, 1)
        )

    def test_rounded_age_at_the_cutoff_is_unconfirmed_and_never_queued(self):
        self.age = {"verified": True, "checked_at": 1000, "min_age_seconds": 120, "max_age_seconds": 180}
        self.discover()
        self.assertEqual((self.status()["age_unverified"], self.status()["age_expired"]), (1, 0))
        self.assertFalse(self.rows())

    def test_older_womens_listing_counts_only_include_verified_strict_age_bounds(self):
        for upper, categories, expected in [
            (220, ["4"], (1, 0, 1, 1)),
            (299, ["4"], (1, 0, 0, 1)),
            (599, ["4"], (1, 0, 0, 0)),
            (220, ["2309"], (0, 0, 0, 0)),
            (None, ["4"], (0, 0, 0, 0)),
        ]:
            with self.subTest(upper=upper, categories=categories):
                self.now = 1000
                self.item["category_ids"] = categories
                self.age = {"verified": upper is not None, "checked_at": 1000, "max_age_seconds": upper}
                self.worker = home.Monitor(clock=lambda: self.now, fetch=self.fetch)
                self.discover()
                status = self.status()
                self.assertEqual(tuple(status[key] for key in (
                    "womens_listing_age_checked", "womens_listing_under_3m",
                    "womens_listing_under_5m", "womens_listing_under_10m")), expected)
                self.assertFalse(self.rows())

    def test_source_failure_preserves_only_fixed_detail_code(self):
        with patch.object(
            home,
            "parse_home",
            return_value={"reason": "home_not_authenticated", "items": []},
        ):
            self.worker.tick()
        self.assertEqual(self.status()["detail_code"], "home_not_authenticated")
        self.assertEqual(self.status()["retry_at"], 1300)

    def test_detail_budget_and_deadline_are_bounded(self):
        with patch.object(
            home,
            "parse_home",
            return_value={
                "reason": "ok",
                "items": [
                    dict(
                        self.item, id=str(i), url=f"https://www.vinted.co.uk/items/{i}"
                    )
                    for i in range(500, 600)
                ],
            },
        ):
            self.worker.tick()
        self.assertEqual(len(self.worker.pending["seller"]), 6)
        self.now = 1030
        self.worker.tick()
        self.assertFalse(self.worker.pending["seller"])
        self.assertEqual(self.fetch.call_count, 1)
        self.now = 1061
        self.worker.tick()
        self.assertEqual(self.fetch.call_count, 2)
        self.assertTrue(all(call.args[0] == "seller" for call in self.fetch.call_args_list))

    def test_global_banwords_and_country_settings_are_preserved(self):
        db.set_parameter("banwords", "fitted|||aviator")
        self.assertFalse(home.global_filters_allow(self.item))
        db.set_parameter("banwords", "")
        db.add_to_allowlist("GB")
        self.assertFalse(home.global_filters_allow(self.item))
        self.assertTrue(home.global_filters_allow(dict(self.item, seller_country="GB")))

    def test_page_read_keeps_role_and_no_solver_or_renewal(self):
        client = Mock(account_role="seller")
        account = SimpleNamespace(
            exclusive=Mock(return_value=nullcontext()),
            connected_client=Mock(return_value=client),
        )
        with patch.object(home, "seller", account), patch.object(
            home, "read_html", return_value=("html", 1000)
        ) as read, patch.object(home, "yield_to_purchase") as priority:
            self.assertEqual(
                home._page("seller", home.buyer.BASE + "/"), ("html", 1000)
            )
        account.connected_client.assert_called_once_with(
            solve_challenges=False, allow_refresh=False
        )
        read.assert_called_once_with(client, home.buyer.BASE + "/")
        self.assertEqual(priority.call_count, 2)
        client.session.close.assert_called_once()

    def test_page_role_mismatch_closes_client_before_read(self):
        client = Mock(account_role="buyer")
        account = SimpleNamespace(
            exclusive=Mock(return_value=nullcontext()),
            connected_client=Mock(return_value=client),
        )
        with patch.object(home, "seller", account), patch.object(
            home, "read_html"
        ) as read, patch.object(home, "yield_to_purchase"), self.assertRaises(
            home.buyer.BuyerError
        ):
            home._page("seller", home.buyer.BASE + "/")
        read.assert_not_called()
        client.session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
