"""Public-source contracts, failure handling and new-listing-only delivery."""

import unittest
from contextlib import closing
from datetime import datetime
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from test_ebay_monitor import EbayFixture, item

import ebay_connections as connections
import ebay_monitor as monitor
import ebay_public as public
import ebay_store as store
import search_settings

NOW = datetime(2026, 9, 30, 9, 33, 40, tzinfo=ZoneInfo("Europe/London")).timestamp()


def card(
    number="237098542046",
    title="Hollister fur jacket",
    price="£15.00",
    date="30-Sep 09:33",
    extra="",
    domain="www.ebay.co.uk",
):
    return f"""<li class="s-card" data-listingid="{number}"><a class="s-card__link" href="https://{domain}/itm/{number}?tracking=ignored"><div class="s-card__title">{title}</div></a><span>Pre-owned</span><span class="s-card__price">{price}</span><span>Buy It Now</span><span>+£3.50 delivery</span><span>{date}</span>{extra}</li>"""


def page(cards, sort="Newly listed", heading="60 results"):
    return f'<html><div class="srp-sort">Sort: {sort}</div><h1 class="srp-controls__count-heading">{heading}</h1><ul class="srp-results">{cards}</ul></html>'.encode()


class PublicParserTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(store.DEFAULTS, keywords="hollister jacket")

    def test_current_card_markup_price_postage_and_minute_precision(self):
        results, warning = public.parse_page(page(card()), self.config, NOW)
        self.assertEqual(len(results), 1)
        result = monitor.parse_item(results[0], self.config, NOW)
        self.assertEqual(result["item_id"], "ebay:237098542046")
        self.assertEqual((result["price"], result["shipping"]), (1500, 350))
        self.assertEqual(result["url"], "https://www.ebay.co.uk/itm/237098542046")
        self.assertTrue(result["public"])
        self.assertEqual(result["listed_label"], "30-Sep 09:33")
        self.assertEqual(warning, "")

    def test_price_filters_stay_local_and_requested_remote_filters_are_present(self):
        config = dict(
            self.config,
            max_price=2000,
            min_price=500,
            condition="used",
            category="57988",
        )
        query = parse_qs(urlparse(public.search_url(config)).query)
        self.assertEqual(query["_sop"], ["10"])
        self.assertEqual(query["LH_PrefLoc"], ["1"])
        self.assertEqual(query["LH_ItemCondition"], ["3000"])
        self.assertNotIn("_udhi", query)
        self.assertNotIn("_udlo", query)

    def test_challenge_wrong_sort_missing_dates_foreign_currency_and_range_fail_closed(
        self,
    ):
        samples = [
            b"Verify you are human",
            page(card(), sort="Best Match"),
            page(card(date="")),
            page(card(price="$15.00")),
            page(card(price="£10.00 to £20.00")),
            page(card(domain="example.com")),
            b"<html>unexpected layout</html>",
        ]
        for sample in samples:
            with self.subTest(sample=sample[:60]), self.assertRaises(monitor.EbayError):
                public.parse_page(sample, self.config, NOW)
        with self.assertRaises(monitor.EbayError) as raised:
            public.parse_page(samples[0], self.config, NOW)
        self.assertTrue(raised.exception.halt)

    def test_zero_results_and_relaxed_recommendations_do_not_become_matches(self):
        self.assertEqual(
            public.parse_page(page(card(), heading="0 results"), self.config, NOW)[0],
            [],
        )
        results, _ = public.parse_page(
            page(
                card() + "<li>Results matching fewer words</li>" + card("237098542047")
            ),
            self.config,
            NOW,
        )
        self.assertEqual(len(results), 1)

    def test_placeholder_and_foreign_location_do_not_alert(self):
        results, _ = public.parse_page(
            page(card("237098542047", extra="<span>from United States</span>")),
            self.config,
            NOW,
        )
        self.assertEqual(results, [])
        results, _ = public.parse_page(page(card("123456") + card()), self.config, NOW)
        self.assertEqual(len(results), 1)

    def test_year_boundary_and_leap_day(self):
        now = datetime(2027, 1, 1, 0, 1, tzinfo=ZoneInfo("Europe/London")).timestamp()
        self.assertTrue(
            public.listing_date("31-Dec 23:59", now).startswith("2026-12-31")
        )
        leap = datetime(2028, 2, 29, 12, tzinfo=ZoneInfo("Europe/London")).timestamp()
        self.assertTrue(
            public.listing_date("29-Feb 11:59", leap).startswith("2028-02-29")
        )

    def test_transport_respects_rate_limit_and_halts_on_access_denial(self):
        for status, halt in [(429, False), (401, True), (403, True)]:
            response = MagicMock()
            response.status_code = status
            response.headers = {"Retry-After": "120"}
            response.url = public.search_url(self.config)
            response.history = []
            response.iter_content.return_value = iter([b"Access denied"])
            response.__enter__.return_value = response
            session = MagicMock()
            session.get.return_value = response
            with self.subTest(status=status), self.assertRaises(
                monitor.EbayError
            ) as raised:
                public.PublicClient(session=session).search(self.config)
            self.assertEqual(raised.exception.halt, halt)
            self.assertTrue(raised.exception.global_cooldown)
            session.get.assert_called_once()
            if status == 429:
                self.assertEqual(raised.exception.retry_after, 120)
            else:
                self.assertIn(f"HTTP {status}", str(raised.exception))
                self.assertIn("access-denied", str(raised.exception))

    def test_diagnostic_classifies_bounded_sample_without_leaking_response_data(self):
        response = MagicMock()
        response.status_code = 403
        response.url = "https://www.ebay.co.uk/sch/i.html?secret=private-canary"
        response.history = []
        response.headers = {
            "Content-Type": "text/html; private-canary",
            "Server": "AkamaiGHost",
            "Set-Cookie": "private-canary",
            "WWW-Authenticate": "Bearer private-canary",
        }
        response.iter_content.return_value = iter(
            [b"Verify you are human private-canary" + b"x" * 4000] * 100
        )
        with self.assertLogs(public.logger, level="WARNING") as captured:
            diagnostic = public.response_diagnostic(response, public.time.monotonic())
        self.assertEqual(diagnostic["page_kind"], "human-verification")
        self.assertEqual(diagnostic["sample_bytes"], public.DIAGNOSTIC_BYTES)
        self.assertEqual(diagnostic["sample_state"], "limited")
        self.assertNotIn("private-canary", str(diagnostic) + str(captured.output))

    def test_denial_keeps_status_and_pause_when_error_body_times_out(self):
        response = MagicMock()
        response.status_code = 401
        response.url = public.search_url(self.config)
        response.history = []
        response.headers = {}
        response.iter_content.side_effect = public.requests.ReadTimeout(
            "private-canary"
        )
        response.__enter__.return_value = response
        session = MagicMock()
        session.get.return_value = response
        with self.assertRaises(monitor.EbayError) as raised:
            public.PublicClient(session=session).search(self.config)
        self.assertIn("HTTP 401", str(raised.exception))
        self.assertNotIn("private-canary", str(raised.exception))
        self.assertTrue(raised.exception.halt)
        session.get.assert_called_once()


class PublicDeliveryTests(EbayFixture, unittest.TestCase):
    def test_manual_denial_persists_pause_and_success_clears_it_without_alerts(self):
        before = search_settings.active_queries()
        with patch.object(
            connections, "configuration", return_value={"source": "public"}
        ):
            with patch.object(
                public.PublicClient,
                "search",
                side_effect=monitor.EbayError("HTTP 403", 3600, True, halt=True),
            ) as check, self.assertRaisesRegex(ValueError, "HTTP 403"):
                connections.test_connection("ebay")
            check.assert_called_once()
            self.assertTrue(store.connection_summary()["public_paused"])
            with patch.object(public.PublicClient, "search", return_value=([], "")):
                connections.test_connection("ebay")
            self.assertFalse(store.connection_summary()["public_paused"])
        self.assertEqual(search_settings.active_queries(), before)
        self.assertEqual(self.outbox(), [])

    def snapshot_public(self, search, items, now):
        with patch.object(monitor.time, "time", return_value=now):
            return monitor.record_snapshot(search, items, now, source="public")

    def test_public_source_needs_only_separate_telegram_connection(self):
        config = {
            "source": "public",
            "client_id": "",
            "client_secret": "",
            "telegram_token": "87654321:fake",
            "chat_id": "123",
        }
        self.assertEqual(store.missing_configuration(config), [])
        config["source"] = "browse"
        self.assertIn("eBay App ID", store.missing_configuration(config))

    def test_quiet_baseline_same_minute_arrival_old_stock_and_price_drop(self):
        search = self.enable()
        old = item(
            123, created=900, _dateSource="publicSearchMinute", _listedLabel="old"
        )
        expensive = item(
            124,
            created=1020,
            price="30.00",
            _dateSource="publicSearchMinute",
            _listedLabel="new",
        )
        self.assertEqual(self.snapshot_public(search, [old], 1030), 0)
        self.assertEqual(self.snapshot_public(search, [old, expensive], 1035), 0)
        fresh = item(
            125,
            created=1020,
            _dateSource="publicSearchMinute",
            _listedLabel="same minute",
        )
        older_unseen = item(
            126, created=900, _dateSource="publicSearchMinute", _listedLabel="old"
        )
        expensive["price"]["value"] = "10.00"
        self.assertEqual(
            self.snapshot_public(search, [fresh, old, expensive, older_unseen], 1040), 1
        )
        self.assertEqual(self.snapshot_public(search, [fresh], 1045), 0)
        self.assertEqual(self.outbox()[0]["item_id"], "ebay:125")
        self.assertIn("minute precision", self.outbox()[0]["content"])
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE alert_outbox SET status='sent',sent_at=1042")
        timing = store.delivery_timing(1050)
        self.assertEqual(timing["public_records"], 1)
        self.assertEqual(timing["sent"], 0)
        self.assertEqual(timing["within_target"], 0)
        self.assertEqual(timing["p95_dispatch"], 2)

    def test_source_switch_rebaselines_and_stale_inflight_results_are_ignored(self):
        search = self.enable()
        self.snapshot_public(search, [], 1000)
        store.save_configuration({"source": "public"})
        self.assertEqual(self.snapshot_public(search, [item(123)], 1020), 0)
        fresh = search_settings.get_search(1)
        self.assertGreater(fresh["ebay_generation"], search["ebay_generation"])
        self.assertEqual(self.snapshot_public(fresh, [item(123)], 1020), 0)

    def test_public_calls_ignore_api_quota_but_keep_spacing_and_persistent_pause(self):
        config = {"source": "public", "daily_budget": 5000}
        self.assertIsNone(store.reserve_call(config, 1000))
        self.assertAlmostEqual(store.reserve_call(config, 1000.01), 1000.1)
        self.assertIsNone(store.reserve_call(config, 1000.11))
        search = self.enable()
        monitor.record_failure(
            search, monitor.EbayError("Access denied", 3600, True, halt=True), 1010
        )
        self.assertGreater(store.reserve_call(config, 9000), 9000)
