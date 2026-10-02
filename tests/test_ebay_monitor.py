"""Offline integration tests: real SQLite, fake network/Telegram, no live alerts."""

import sqlite3
import threading
import time
import unittest
from concurrent.futures import Future
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_search_controls import DatabaseFixture

import alert_delivery
import dashboard_store
import db
import ebay_monitor as monitor
import ebay_store as store
import search_settings


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def item(number, created=1010, price="15.00", **fields):
    result = {
        "itemId": f"v1|{number}|0",
        "legacyItemId": str(number),
        "title": "Hollister fur gilet",
        "itemCreationDate": iso(created),
        "itemOriginDate": iso(created),
        "itemWebUrl": f"https://www.ebay.co.uk/itm/{number}",
        "buyingOptions": ["FIXED_PRICE"],
        "price": {"value": price, "currency": "GBP"},
        "shippingOptions": [{"shippingCost": {"value": "3.00", "currency": "GBP"}}],
        "image": {"imageUrl": "https://i.ebayimg.com/images/g/example/s-l500.jpg"},
    }
    result.update(fields)
    return result


class EbayFixture(DatabaseFixture):
    def enable(self, query_id=1, **changes):
        old = search_settings.get_search(query_id)
        form = {
            "query_name": old["query_name"],
            "query": old["query"],
            "revision": str(old["revision"]),
            "platform_mode": "both",
            "ebay_keywords": "hollister fur",
            "ebay_buying": "fixed",
            "ebay_condition": "any",
            "ebay_uk_only": "yes",
            "ebay_max_price": "20.00",
        }
        form.update(changes)
        dashboard_store.save_search(query_id, form)
        return search_settings.get_search(query_id)

    def snapshot(self, search, items, now=1000):
        with patch.object(monitor.time, "time", return_value=now):
            return monitor.record_snapshot(search, items, now)

    def outbox(self):
        with closing(search_settings.connection()) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM alert_outbox")]


class PlatformTests(EbayFixture, unittest.TestCase):
    def test_upgrade_from_schema_5_adds_timing_without_resetting_searches(self):
        search = self.enable()
        self.snapshot(search, [], 1000)
        self.snapshot(search, [item(123)], 1020)
        before = search_settings.get_search(1)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DROP TABLE ebay_alert_timing")
            conn.execute(
                "UPDATE parameters SET value='5' WHERE key='msj_search_schema'"
            )
        self.assertTrue(search_settings.ensure_schema())
        self.assertEqual(search_settings.get_search(1), before)
        self.assertEqual(self.outbox()[0]["item_id"], "ebay:123")
        self.assertEqual(store.delivery_timing(1030)["sample_size"], 0)

    def test_upgrade_from_schema_4_preserves_vinted_outbox_and_queries(self):
        self.batch(1, [123])
        before = db.get_queries()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DROP INDEX idx_outbox_platform")
            conn.execute("ALTER TABLE alert_outbox DROP COLUMN platform")
            for table in (
                "search_platforms",
                "ebay_state",
                "ebay_seen",
                "ebay_call_buckets",
                "platform_media_cache",
                "ebay_alert_timing",
            ):
                conn.execute("DROP TABLE " + table)
            conn.execute(
                "UPDATE parameters SET value='4' WHERE key='msj_search_schema'"
            )
        backup = search_settings.ensure_schema()
        self.assertTrue(backup)
        self.assertEqual(db.get_queries(), before)
        self.assertEqual(self.outbox()[0]["platform"], "vinted")
        self.assertEqual(len(search_settings.active_queries()), 44)
        self.assertEqual(store.active_searches(), [])
        self.assertIsNone(search_settings.ensure_schema())

    def test_existing_44_searches_default_vinted_and_keep_watermarks(self):
        before = db.get_queries()
        for row in dashboard_store.list_searches():
            self.assertEqual(row["platform_mode"], "vinted")
        self.assertEqual(store.active_searches(), [])
        search = self.enable()
        self.assertEqual(search["platform_mode"], "both")
        self.assertEqual(db.get_queries(), before)
        self.assertEqual(len(search_settings.active_queries()), 44)
        self.assertEqual(len(store.active_searches()), 1)

    def test_ebay_only_needs_no_vinted_link_and_vinted_discards_inflight(self):
        self.enable(platform_mode="ebay")
        self.assertEqual(len(search_settings.active_queries()), 43)
        self.assertEqual(self.batch(1, [105]), [])
        self.assertEqual(db.get_queries()[0][2], 100)
        for n in range(2):
            dashboard_store.save_search(
                None,
                {
                    "query_name": f"eBay only {n}",
                    "platform_mode": "ebay",
                    "ebay_keywords": "ralph lauren",
                    "ebay_buying": "fixed",
                    "ebay_condition": "used",
                },
            )
        self.assertEqual(len(store.active_searches()), 3)
        self.assertEqual(len(search_settings.active_queries()), 43)
        self.enable(platform_mode="both")
        self.assertEqual(
            self.batch(1, [110]), []
        )  # Reenabled platform baselines quietly.

    def test_ebay_edit_does_not_rebaseline_vinted_and_stale_response_is_dropped(self):
        old = self.enable()
        self.snapshot(old, [], 1000)
        self.enable(ebay_max_price="30.00")
        self.assertEqual(search_settings.get_search(1)["rebaseline"], 0)
        self.assertEqual(self.snapshot(old, [item(123)], 1020), 0)
        self.assertEqual(self.outbox(), [])

    def test_shared_metadata_updates_without_resetting_ebay_baseline(self):
        search = self.enable(reminder="First note")
        self.snapshot(search, [], 1000)
        updated = self.enable(reminder="New <note>", exclusions="teddy")
        self.assertEqual(search["ebay_generation"], updated["ebay_generation"])
        self.assertEqual(self.snapshot(updated, [item(123)], 1020), 1)
        self.assertIn("New &lt;note&gt;", self.outbox()[0]["content"])

    def test_pause_archive_resume_reset_only_ebay_state_and_preserve_history(self):
        search = self.enable()
        self.snapshot(search, [], 1000)
        self.snapshot(search, [item(123)], 1020)
        dashboard_store.change_state(1, "pause", search["revision"])
        self.assertEqual(store.active_searches(), [])
        self.assertEqual(self.outbox()[0]["status"], "cancelled")
        self.assertEqual(self.snapshot(search, [item(124)], 1021), 0)
        dashboard_store.change_state(1, "resume", search["revision"] + 1)
        fresh = search_settings.get_search(1)
        self.assertEqual(self.snapshot(fresh, [item(125)], 1030), 0)
        self.assertTrue(db.is_item_in_db_by_id(99))

    def test_validation_and_stale_form_are_atomic(self):
        for bad in [
            {"ebay_keywords": ""},
            {"ebay_min_price": "30", "ebay_max_price": "20"},
            {"ebay_category": "bad"},
            {"ebay_buying": "invalid"},
        ]:
            with self.assertRaises(ValueError):
                self.enable(**bad)
        self.assertEqual(store.active_searches(), [])


class NewListingTests(EbayFixture, unittest.TestCase):
    def test_quiet_empty_baseline_new_items_dedup_restart_and_platform_id_collision(
        self,
    ):
        search = self.enable()
        self.assertEqual(self.snapshot(search, [], 1000), 0)
        self.assertEqual(self.snapshot(search, [item(99)], 1020), 1)
        self.assertEqual(self.snapshot(search, [item(99, price="5.00")], 1030), 0)
        restarted = search_settings.get_search(1)
        self.assertEqual(self.snapshot(restarted, [item(99)], 1040), 0)
        self.assertTrue(db.is_item_in_db_by_id(99))
        self.assertEqual(self.outbox()[0]["item_id"], "ebay:99")

    def test_baseline_old_items_origin_date_price_drops_and_missing_dates_are_silent(
        self,
    ):
        search = self.enable()
        self.snapshot(search, [item(1, created=900)], 1000)
        candidates = [
            item(2, created=900),
            item(3, itemOriginDate=iso(800), itemCreationDate=iso(1010)),
            item(4, itemOriginDate=None, itemCreationDate=None),
            item(5, price="30.00"),
        ]
        self.assertEqual(self.snapshot(search, candidates, 1020), 0)
        self.assertEqual(self.snapshot(search, [item(5, price="10.00")], 1030), 0)
        self.assertEqual(self.snapshot(search, [item(6, itemOriginDate=None)], 1040), 1)

    def test_exclusion_is_local_and_overlap_only_delivers_once(self):
        first = self.enable(exclusions="gilet")
        second = self.enable(2)
        for search in (first, second):
            self.snapshot(search, [], 1000)
        self.assertEqual(self.snapshot(first, [item(123)], 1020), 0)
        self.assertEqual(self.snapshot(second, [item(123)], 1020), 1)
        self.assertEqual(len(self.outbox()), 1)

    def test_postage_currency_end_date_auction_price_and_unsafe_links(self):
        config = dict(store.DEFAULTS, max_price=1700, include_shipping=True)
        self.assertIsNone(monitor.parse_item(item(1), config, 1020))
        self.assertIsNone(monitor.parse_item(item(1, shippingOptions=[]), config, 1020))
        config["include_shipping"] = False
        self.assertIsNotNone(monitor.parse_item(item(1), config, 1020))
        for fields in [
            {"itemEndDate": iso(1010)},
            {"price": {"value": "10", "currency": "USD"}},
            {"itemWebUrl": "https://evil.test/itm/1"},
            {"itemOriginDate": iso(2000)},
            {"buyingOptions": ["AUCTION"]},
        ]:
            self.assertIsNone(monitor.parse_item(item(1, **fields), config, 1020))
        auction = monitor.parse_item(
            item(
                1,
                buyingOptions=["AUCTION"],
                currentBidPrice={"value": "7", "currency": "GBP"},
            ),
            dict(config, buying="auction"),
            1020,
        )
        self.assertEqual(auction["price"], 700)
        self.assertTrue(auction["auction"])

    def test_atomic_outbox_failure_retries_without_consuming_seen_id(self):
        search = self.enable()
        self.snapshot(search, [], 1000)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "CREATE TRIGGER fail_ebay BEFORE INSERT ON alert_outbox BEGIN SELECT RAISE(ABORT,'disk failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.snapshot(search, [item(123)], 1020)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DROP TRIGGER fail_ebay")
        self.assertEqual(self.snapshot(search, [item(123)], 1030), 1)


class QuotaTests(EbayFixture, unittest.TestCase):
    def test_owner_diagnostic_is_budgeted_throttled_and_respects_cooldown(self):
        config = {"daily_budget": 5000}
        self.assertIsNone(store.reserve_call(config, 1000))
        self.assertIsNone(store.reserve_call(config, 1002, diagnostic=True))
        self.assertEqual(store.reserve_call(config, 1003, diagnostic=True), 1032)
        self.assertGreater(store.reserve_call(config, 1003), 1020)
        with closing(search_settings.connection()) as conn, conn:
            self.assertEqual(
                conn.execute("SELECT SUM(calls) FROM ebay_call_buckets").fetchone()[0],
                2,
            )
            conn.execute(
                "INSERT INTO delivery_runtime VALUES ('ebay_api_cooldown',1100)"
            )
        self.assertEqual(store.reserve_call(config, 1040, diagnostic=True), 1100)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE ebay_call_buckets SET calls=4500")
        self.assertGreater(store.reserve_call(config, 1200, diagnostic=True), 86400)

    def test_rolling_budget_pacing_restart_and_429_cooldown(self):
        config = {"daily_budget": 100}
        self.assertIsNone(store.reserve_call(config, 1000))
        self.assertGreater(store.reserve_call(config, 1001), 1001)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE ebay_call_buckets SET calls=90")
        self.assertEqual(store.reserve_call(config, 2000), 87420)
        self.assertIsNone(store.reserve_call(config, 87421))
        search = self.enable()
        monitor.record_failure(
            search, monitor.EbayError("Rate limited", 120, global_cooldown=True), 88000
        )
        self.assertGreaterEqual(
            store.reserve_call({"daily_budget": 5000}, 88001), 88120
        )

    def test_grouping_shares_equivalent_requests_without_losing_price_rules(self):
        self.enable(1, ebay_max_price="10")
        self.enable(2, ebay_max_price="20", exclusions="teddy")
        self.enable(3, ebay_condition="used")
        groups = monitor.grouped_searches(store.active_searches())
        self.assertEqual(sorted(map(len, groups)), [1, 2])
        self.assertNotIn("price", monitor.search_params(groups[0][0]["ebay"])["filter"])

    def test_100_search_15_second_capacity_is_640000_with_headroom(self):
        self.assertAlmostEqual(store.call_spacing({"daily_budget": 640000}) * 100, 15)
        self.assertGreater(store.call_spacing({"daily_budget": 50000}) * 100, 190)

    def test_5_second_poll_default_and_approved_capacity_for_100_searches(self):
        self.assertEqual(store.configuration()["target_interval"], 5)
        store.save_configuration({"daily_budget": "1920000"})
        self.assertAlmostEqual(store.call_spacing(store.configuration()) * 100, 5)
        self.assertEqual(store.connection_summary()["alert_target"], 15)

    def test_one_query_failure_does_not_cool_down_other_queries(self):
        search = self.enable()
        self.enable(2, ebay_keywords="ralph lauren")
        monitor.record_failure(search, monitor.EbayError("Network failure"), 1000)
        self.assertEqual(
            search_settings.get_search(1)["ebay_health"]["next_poll"], 1005
        )
        self.assertIsNone(store.reserve_call({"daily_budget": 1920000}, 1001))
        self.assertFalse(search_settings.get_search(2)["ebay_health"])

    def test_alert_timing_counts_deadline_misses_and_excludes_invalid_clocks(self):
        search = self.enable()
        self.snapshot(search, [], 1000)
        records = [item(n) for n in (201, 202, 203)]
        records += [
            item(204, created=1040),
            item(205, created=1011, itemOriginDate=None),
        ]
        self.snapshot(search, records, 1014)
        for accepted in (1025, 1025.01):
            row = alert_delivery.claim(now=accepted, platform="ebay")
            alert_delivery.finish(row, message_id=1, now=accepted)
        failed = alert_delivery.claim(now=1026, platform="ebay")
        alert_delivery.finish(failed, failure="Forbidden", permanent=True, now=1026)
        timing = store.delivery_timing(1060)
        self.assertEqual(timing["sent"], 2)
        self.assertEqual(timing["within_target"], 1)
        self.assertEqual(timing["late"], 1)
        self.assertEqual(timing["pending_late"], 1)
        self.assertEqual(timing["invalid_timestamps"], 1)
        self.assertEqual(timing["fallback_timestamps"], 1)
        self.assertEqual(timing["failed"], 1)
        self.assertEqual(timing["p95_total"], 15.01)
        self.assertEqual(timing["p95_discovery"], 4)
        self.assertEqual(timing["p95_dispatch"], 11.01)
        self.assertEqual(store.delivery_timing(100000)["sent"], 0)

    def test_slow_request_does_not_block_another_search(self):
        self.enable(1)
        self.enable(2, ebay_keywords="ralph lauren")
        config = {
            "client_id": "id",
            "client_secret": "secret",
            "telegram_token": "12345:" + "x" * 25,
            "chat_id": "123",
            "daily_budget": 640000,
            "target_interval": 15,
        }
        gate, started = threading.Event(), []

        def slow(group, config, now, interval):
            started.append(group[0]["id"])
            gate.wait(2)

        poller = monitor.Poller(workers=2)
        try:
            with (
                patch.object(store, "configuration", return_value=config),
                patch.object(poller, "fetch_group", side_effect=slow),
            ):
                begin = time.monotonic()
                poller.tick(now=1000)
                poller.tick(now=1001)
                self.assertLess(time.monotonic() - begin, 0.5)
                self.assertEqual(len(poller.inflight), 2)
        finally:
            gate.set()
            poller.close()
        self.assertEqual(set(started), {1, 2})

    def test_100_distinct_requests_are_dispatched_every_5_seconds_at_sufficient_quota(
        self,
    ):
        # Simulate successful immediate network responses; rate gate still uses real SQLite.
        searches = [
            {
                "id": i,
                "ebay": dict(store.DEFAULTS, keywords=f"brand{i}"),
                "ebay_health": {},
            }
            for i in range(100)
        ]
        config = {
            "client_id": "id",
            "client_secret": "secret",
            "telegram_token": "12345:" + "x" * 25,
            "chat_id": "123",
            "daily_budget": 2000000,
            "target_interval": 5,
        }
        calls = {i: [] for i in range(100)}

        class ImmediateExecutor:
            def submit(self, fn, group, config, now, interval):
                calls[group[0]["id"]].append(now)
                f = Future()
                f.set_result(None)
                return f

            def shutdown(self, **kwargs):
                pass

        poller = monitor.Poller()
        poller.executor.shutdown()
        poller.executor = ImmediateExecutor()
        with (
            patch.object(store, "configuration", return_value=config),
            patch.object(store, "active_searches", return_value=searches),
        ):
            for tick in range(1301):
                poller.tick(now=1000 + tick * 0.01)
        self.assertTrue(all(len(c) >= 2 for c in calls.values()))
        self.assertTrue(all(4.99 <= c[1] - c[0] <= 5.02 for c in calls.values()))

    def test_settings_reload_during_request_preserves_poll_reservation(self):
        self.enable(1)
        config = {
            "client_id": "id",
            "client_secret": "secret",
            "telegram_token": "12345:" + "x" * 25,
            "chat_id": "123",
            "daily_budget": 100000,
            "target_interval": 5,
        }
        jobs = []

        class ControlledExecutor:
            def submit(self, fn, group, config, now, interval):
                future = Future()
                jobs.append((now, future))
                return future

            def shutdown(self, **kwargs):
                pass

        poller = monitor.Poller()
        poller.executor.shutdown()
        poller.executor = ControlledExecutor()
        with patch.object(store, "configuration", return_value=config):
            poller.tick(now=1000)
            # Reads fresh dictionaries from SQLite before the worker has
            # committed its new next_poll. The database still says 'due'.
            poller.tick(now=1001)
            self.assertEqual(len(jobs), 1)
            jobs[0][1].set_result(None)
            poller.tick(now=1001.1)
            poller.tick(now=1004.99)
            self.assertEqual(len(jobs), 1)
            poller.tick(now=1005)
            self.assertEqual([job[0] for job in jobs], [1000, 1005])
            # A slow failure finishes after the normal polling deadline.
            # Its returned retry deadline must win over a stale cache too.
            jobs[1][1].set_result(1020)
            poller.tick(now=1011)
            poller.tick(now=1019.99)
            self.assertEqual(len(jobs), 2)
            poller.tick(now=1020)
            self.assertEqual([job[0] for job in jobs], [1000, 1005, 1020])
            jobs[2][1].set_result(None)


class ClientTests(unittest.TestCase):
    def client(self, response):
        session = Mock()
        session.post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: {"access_token": "test-token", "expires_in": 7200},
        )
        session.get.return_value = response
        return (
            monitor.BrowseClient(
                {"client_id": "id", "client_secret": "secret"}, session
            ),
            session,
        )

    def test_uk_newly_listed_oauth_cached_and_200_result_warning(self):
        client, session = self.client(
            SimpleNamespace(
                status_code=200, json=lambda: {"itemSummaries": [item(1)] * 200}
            )
        )
        config = dict(store.DEFAULTS, keywords="hollister")
        _, warning = client.search(config)
        client.search(config)
        self.assertEqual(session.post.call_count, 1)
        kwargs = session.get.call_args.kwargs
        self.assertEqual(kwargs["headers"]["X-EBAY-C-MARKETPLACE-ID"], "EBAY_GB")
        self.assertEqual(kwargs["params"]["sort"], "newlyListed")
        self.assertIn("200", warning)

    def test_rate_limit_retry_after_and_no_secrets_in_error(self):
        client, _session = self.client(
            SimpleNamespace(status_code=429, headers={"Retry-After": "123"})
        )
        with self.assertRaises(monitor.EbayError) as caught:
            client.search(dict(store.DEFAULTS, keywords="hollister"))
        self.assertEqual(caught.exception.retry_after, 123)
        self.assertTrue(caught.exception.global_cooldown)
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(
            monitor.retry_delay("Thu, 01 Jan 1970 00:20:00 GMT", now=1000), 200
        )


class SeparateBotTests(EbayFixture, unittest.IsolatedAsyncioTestCase):
    async def test_each_bot_claims_own_platform_and_rate_limits_are_independent(self):
        search = self.enable()
        self.snapshot(search, [], 1000)
        self.snapshot(search, [item(123)], 1020)
        self.batch(1, [123])
        ebay = alert_delivery.claim(now=1100, platform="ebay")
        vinted = alert_delivery.claim(now=1100)
        self.assertEqual(ebay["item_id"], "ebay:123")
        self.assertEqual(vinted["item_id"], "123")
        alert_delivery.finish(
            ebay, failure="Rate limit", delay=120, cooldown=True, now=1100
        )
        alert_delivery.finish(vinted, failure="Retry", delay=1, now=1100)
        self.assertIsNone(alert_delivery.claim(now=1110, platform="ebay"))
        self.assertIsNotNone(alert_delivery.claim(now=1110))

    async def test_ebay_link_sends_without_image_fetch_and_reference_cache_is_bot_specific(
        self,
    ):
        search = self.enable()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT INTO dashboard_media VALUES ('photo',?,'vinted-file-id',1)",
                (b"image",),
            )
        search["reference_id"] = "photo"
        self.snapshot(search, [], 1000)
        self.snapshot(search, [item(123)], 1020)
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
            send_photo=AsyncMock(
                return_value=SimpleNamespace(
                    photo=[SimpleNamespace(file_id="ebay-file-id")]
                )
            ),
        )
        worker = alert_delivery.DeliveryWorker(
            bot, "123", platform="ebay", bot_id="56789"
        )
        await worker.tick(now=1100)
        args = bot.send_message.call_args.kwargs
        self.assertEqual(args["reply_markup"].inline_keyboard[0][0].text, "Open eBay")
        self.assertTrue(args["link_preview_options"].is_disabled)
        await worker.tick(now=1102)
        self.assertNotEqual(bot.send_photo.call_args.kwargs["photo"], "vinted-file-id")
        self.assertEqual(
            dashboard_store.get_media("photo")["telegram_file_id"], "vinted-file-id"
        )
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute("SELECT file_id FROM platform_media_cache").fetchone()[0],
                "ebay-file-id",
            )
