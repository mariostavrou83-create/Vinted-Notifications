"""Seeded boundary matrices and real-SQLite contention, with no live network.

These exercise varied inputs, not repeated identical requests to marketplaces.
The case counts below are deliberately stable and run in ordinary CI.
"""

import random
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from decimal import ROUND_HALF_UP, Decimal
from html import escape
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from test_ebay_monitor import item
from test_search_controls import DatabaseFixture
from test_vinted_buying import checkout

import ebay_monitor
import ebay_search_link
import ebay_store
import photo_cards
import search_settings
import vinted_alerts
import vinted_budget
import vinted_buyer
import vinted_buying
import vinted_keywords


def pounds(pence):
    return f"{pence // 100}.{pence % 100:02d}"


class BoundaryMatrices(unittest.TestCase):
    def test_5000_estimates_and_checkout_boundary_cases(self):
        rng = random.Random(20261006)
        for case in range(5000):
            with self.subTest(case=case):
                price = rng.randrange(1, 100001)
                postage = rng.randrange(0, 10001)
                fee = (
                    int(
                        (Decimal(price) * Decimal(".05")).quantize(
                            Decimal(1), rounding=ROUND_HALF_UP
                        )
                    )
                    + 70
                )
                supplied = bool(case % 2)
                if supplied:
                    fee = rng.randrange(0, 2500)
                total = price + postage + fee
                cap = total + rng.choice((-1, 0, 1, 200))
                listing = SimpleNamespace(
                    price=pounds(price), currency="GBP", raw_data={}
                )
                if supplied:
                    listing.raw_data["total_item_price"] = {
                        "amount": pounds(price + fee),
                        "currency_code": "GBP",
                    }
                estimate = vinted_budget.estimate(
                    listing,
                    {"vinted_max_total": cap, "vinted_postage_estimate": postage},
                )
                self.assertEqual(estimate["total"], total)
                self.assertEqual(estimate["within_budget"], total <= cap)
                self.assertEqual(estimate["buyer_protection_estimated"], not supplied)
                quoted = checkout(pounds(total))
                changed_price = price + rng.choice((-1, 0, 1))
                quoted["components"]["order_summary_v2"]["subtotal"]["price"][
                    "amount"
                ] = pounds(changed_price)
                valid = total <= cap and changed_price <= price
                if valid:
                    self.assertEqual(
                        vinted_buying.checkout_prices(quoted, price, cap), total
                    )
                else:
                    with self.assertRaises(vinted_buyer.BuyerError):
                        vinted_buying.checkout_prices(quoted, price, cap)

    def test_1000_unicode_captions_and_every_notes_page_fit_telegram(self):
        rng = random.Random(51450)
        words = ["jeans", "🧥", "<b>tag</b>", "£", "&", "é", "漢字", "👨‍👩‍👧‍👦"]
        for case in range(1000):

            def value(length):
                return " ".join(rng.choices(words, k=length))

            row = {
                "query_id": case + 1,
                "search_name": value(20),
                "title": value(rng.randrange(1, 180)),
                "price": "15.00",
                "currency": "GBP",
                "url": "https://www.vinted.co.uk/items/123",
            }
            details = {
                "name": value(20)[:100],
                "brand": value(20)[:120],
                "description": value(rng.randrange(0, 600))[:6000],
                "description_checked": True,
                "guide": escape(value(60)),
                "reminder": escape(value(80)[:800]),
            }
            if case % 2:
                details["platform"] = "ebay"
                details["shipping"] = 220
            elif case % 3:
                details["buy_feedback"] = {"message": value(130), "state": "unknown"}
            with self.subTest(case=case):
                text, pages = photo_cards.captions(row, details)
                self.assertLessEqual(photo_cards.units(photo_cards.plain(text)), 1024)
                self.assertNotIn("<b>tag</b>", text)
                if pages:
                    expected = "\n\n".join(
                        photo_cards.plain(p)
                        for p in vinted_alerts.sections(row, details)[3:]
                        if p
                    )
                    self.assertEqual("".join(pages), expected)
                for index in range(len(pages)):
                    self.assertLessEqual(
                        photo_cards.units(
                            photo_cards.plain(
                                photo_cards.notes_caption(details, index, pages)
                            )
                        ),
                        1024,
                    )

    def test_2000_keyword_urls_preserve_all_non_keyword_filters(self):
        rng = random.Random(13)
        for case in range(2000):
            params = [
                ("catalog[]", str(rng.randrange(1, 3000))),
                ("size_ids[]", "2"),
                ("size_ids[]", "4"),
                ("status_ids[]", "2"),
                ("brand_ids[]", str(rng.randrange(1, 10000))),
                ("order", "newest_first"),
                ("search_text", "old"),
            ]
            url = "https://www.vinted.co.uk/catalog?" + urlencode(params)
            keyword = rng.choice(
                ["fur", "sherpa", "fleece lined", "hood & zip", "é"]
            ) + str(case)
            actual = parse_qs(
                urlsplit(vinted_keywords.with_keyword(url, keyword)).query
            )
            original = parse_qs(urlsplit(url).query)
            original["search_text"] = [keyword]
            self.assertEqual(actual, original)

    def test_2000_ebay_filter_imports_keep_category_brand_size_and_prices(self):
        rng = random.Random(50)
        for case in range(2000):
            cap = rng.randrange(100, 100000)
            category = str(rng.randrange(1, 100000))
            brand = rng.choice(["Bench", "7 For All Mankind", "Hollister"])
            size = str(rng.randrange(4, 24))
            url = "https://www.ebay.co.uk/sch/i.html?" + urlencode(
                {
                    "_nkw": "jeans",
                    "_sacat": category,
                    "Brand": brand,
                    "Size": size,
                    "_udhi": pounds(cap),
                    "LH_BIN": "1",
                    "LH_PrefLoc": "1",
                    "_sop": "10",
                }
            )
            parsed = ebay_search_link.parse_link(url)
            query = ebay_monitor.search_params(parsed)
            self.assertEqual(parsed["max_price"], cap)
            self.assertEqual(query["category_ids"], category)
            self.assertEqual(query["sort"], "newlyListed")
            self.assertEqual(parsed["aspects"], {"Brand": [brand], "Size": [size]})
            self.assertEqual(query["q"], "jeans")

    def test_invalid_prices_and_malformed_optional_ebay_data_cannot_crash_a_batch(self):
        for value in [
            "NaN",
            "Infinity",
            "-1",
            "1.999",
            "1e999999999",
            True,
            None,
            {},
            [],
        ]:
            self.assertIsNone(
                vinted_budget.money({"amount": value, "currency_code": "GBP"})
            )
            self.assertIsNone(ebay_monitor.money({"value": value, "currency": "GBP"}))
            with self.assertRaises(vinted_buyer.BuyerError):
                vinted_buying.cents({"amount": value, "currency_code": "GBP"})
        for field in ("shippingOptions", "additionalImages", "thumbnailImages"):
            for value in (None, {}, 1, "bad", [None, 1, {}]):
                raw = item(123, **{field: value})
                self.assertIsNotNone(
                    ebay_monitor.parse_item(raw, dict(ebay_store.DEFAULTS), 1020)
                )
        for raw in (None, [], "", 1):
            self.assertIsNone(
                ebay_monitor.parse_item(raw, dict(ebay_store.DEFAULTS), 1020)
            )

    def test_checkout_delivery_errors_and_unverified_item_subtotal_stop_payment(self):
        for field in (
            "shipping_pickup_details",
            "shipping_pickup_options",
            "shipping_address",
            "payment_method",
        ):
            data = checkout()
            data["components"][field]["errors"] = ["delivery unavailable"]
            with self.assertRaises(vinted_buyer.BuyerError):
                vinted_buying.checkout_prices(data, 1500, 2000)
        data = checkout()
        del data["components"]["order_summary_v2"]["subtotal"]
        with self.assertRaises(vinted_buyer.BuyerError):
            vinted_buying.checkout_prices(data, 1500, 2000)


class ContentionTests(DatabaseFixture, unittest.TestCase):
    def test_malformed_vinted_record_does_not_drop_other_new_items(self):
        from pyVintedVN.items.items import Items

        client = Mock()
        valid = {
            "id": 123,
            "title": "Bench coat",
            "url": "/items/123",
            "price": {"amount": "15", "currency_code": "GBP"},
        }
        client.get.return_value.json.return_value = {
            "items": [None, {}, dict(valid, price=None), valid]
        }
        with patch("pyVintedVN.items.items.report_catalogue_cache"):
            rows = Items(client=client).search("https://www.vinted.co.uk/catalog")
            self.assertEqual([row.id for row in rows], [123])
            client.get.return_value.json.return_value = {"items": [None, {}]}
            with self.assertRaises(ValueError):
                Items(client=client).search("https://www.vinted.co.uk/catalog")

    def test_250_fast_searches_are_fair_with_bounded_workers(self):
        from test_search_controls import SchedulerTests

        import resource_controls

        with closing(search_settings.connection()) as conn, conn:
            conn.executemany(
                "INSERT INTO queries(id,query,last_item,query_name) VALUES (?,?,100,?)",
                [
                    (
                        i,
                        f"https://www.vinted.co.uk/catalog?search_text=test{i}",
                        f"Search {i}",
                    )
                    for i in range(45, 251)
                ],
            )
        resource_controls.save_rate("0")
        starts, pending = SchedulerTests.simulate(
            self, seconds=30, workers=12, target=1
        )
        self.assertLessEqual(pending, 12)
        counts = [sum(q == query_id for q, _ in starts) for query_id in range(1, 251)]
        self.assertGreaterEqual(min(counts), 4)
        self.assertLessEqual(max(counts) - min(counts), 1)

    def test_1000_concurrent_claims_prepare_one_purchase_and_never_retry_unknown(self):
        row = {"item_id": "123"}
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: vinted_buying.claim(row), range(1000)))
        self.assertEqual(results.count(True), 1)
        vinted_buying.record("123", "unknown", "Payment unconfirmed")
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: vinted_buying.claim(row), range(1000)))
        self.assertFalse(any(results))

    def test_1000_shared_budget_attempts_never_exceed_reserve_across_callers(self):
        config = dict(ebay_store.configuration(), source="browse", daily_budget=100)
        accepted = 0
        for index in range(1000):
            mode = ({}, {"media": True}, {"diagnostic": True})[index % 3]
            accepted += (
                ebay_store.reserve_call(config, 100000 + index * 100, **mode) is None
            )
            with closing(search_settings.connection()) as conn:
                count = (
                    conn.execute("SELECT SUM(calls) FROM ebay_call_buckets").fetchone()[
                        0
                    ]
                    or 0
                )
            self.assertLessEqual(count, 90)
        self.assertGreater(accepted, 90)  # The rolling window legitimately renews.
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(
                pool.map(
                    lambda _: ebay_store.reserve_call(config, 200000, media=True),
                    range(100),
                )
            )
        self.assertLessEqual(results.count(None), 1)

    def test_production_upload_limit_matches_four_photo_form(self):
        from web_ui_plugin.web_ui import create_app, web_ui_process

        app = create_app({"TESTING": True})
        with patch("waitress.serve") as serve, patch(
            "web_ui_plugin.web_ui.create_app", return_value=app
        ):
            web_ui_process()
        self.assertEqual(
            serve.call_args.kwargs["max_request_body_size"],
            app.config["MAX_CONTENT_LENGTH"],
        )
        self.assertGreater(
            serve.call_args.kwargs["max_request_body_size"], 4 * 8 * 1024 * 1024
        )


class WatchdogTests(unittest.TestCase):
    def test_real_child_crash_recovers_without_duplicate_worker(self):
        import multiprocessing
        import os

        from process_watchdog import ensure_running

        stopped = multiprocessing.Process(target=os._exit, args=(7,))
        stopped.start()
        stopped.join(timeout=3)
        self.assertEqual(stopped.exitcode, 7)
        stop = multiprocessing.Event()
        recovered = ensure_running(stopped, stop.wait, name="audit-recovery")
        try:
            self.assertTrue(recovered.is_alive())
            self.assertIs(
                ensure_running(recovered, stop.wait, name="audit-recovery"), recovered
            )
        finally:
            stop.set()
            recovered.join(timeout=3)
            if recovered.is_alive():
                recovered.terminate()
                recovered.join(timeout=3)
        self.assertEqual(recovered.exitcode, 0)

    def test_dead_worker_restarts_once_then_healthy_process_is_retained(self):
        from process_watchdog import ensure_running

        dead = Mock(exitcode=1)
        dead.is_alive.return_value = False
        replacement = Mock()
        replacement.is_alive.return_value = True
        target = Mock()
        with patch(
            "process_watchdog.multiprocessing.Process", return_value=replacement
        ) as factory:
            active = ensure_running(dead, target, ("queue",), name="test")
            self.assertIs(
                ensure_running(active, target, ("queue",), name="test"), active
            )
        factory.assert_called_once_with(target=target, args=("queue",), name="test")
        replacement.start.assert_called_once()
        dead.join.assert_called_once_with(timeout=0)

    def test_main_monitor_covers_every_critical_process_and_bounded_queues(self):
        import ast
        from pathlib import Path

        from test_alert_reliability import functions

        root = Path(__file__).resolve().parents[1]
        namespace = {
            "db": Mock(get_parameter=Mock(return_value="")),
            "telegram_process": None,
            "rss_process": None,
            "ebay_worker_process": Mock(),
            "scrape_process": Mock(),
            "item_extractor_process": Mock(),
            "dispatcher_process": Mock(),
            "web_ui_process_instance": Mock(),
            "scraper_process": Mock(),
            "item_extractor": Mock(),
            "dispatcher_function": Mock(),
            "web_ui_process": Mock(),
        }
        monitor = functions(
            "vinted_notifications.py", {"monitor_processes"}, namespace
        )["monitor_processes"]
        with patch("process_watchdog.ensure_running", return_value=Mock()) as ensure:
            monitor("items", "telegram", "rss", "new")
        self.assertEqual(
            {c.kwargs["name"] for c in ensure.call_args_list},
            {"vinted-poller", "item-extractor", "dispatcher", "dashboard"},
        )
        tree = ast.parse((root / "vinted_notifications.py").read_text())
        queues = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "Queue"
        ]
        self.assertEqual(len(queues), 4)
        self.assertTrue(
            all(
                any(
                    k.arg == "maxsize"
                    and isinstance(k.value, ast.Constant)
                    and 0 < k.value.value <= 128
                    for k in n.keywords
                )
                for n in queues
            )
        )
