import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from lxml import html

import trial_market_hour as hourly
import trial_market_search as search
import trial_public_pool as pool
from resale_pilot import market_cache


class MarketProductFactsTest(unittest.TestCase):
    title = "Mango denim jacket"
    prose = "Girls size 9-11 years. Slight rip in the lining. Personal contact details omitted."

    def item(self, *, detail):
        raw = {
            "id": "101",
            "title": self.title,
            "url": "/items/101-mango",
            "user": {"id": "501"},
            "item_box": {"first_line": "Mango", "second_line": "S · Good"},
            "price": {"amount": "7.00", "currency_code": "GBP"},
        }
        if detail:
            raw.update(description=self.prose, department="Girls")
        return raw

    def assert_documented(self, item):
        self.assertEqual(item["audience_age"], "child")
        self.assertEqual(item["audience_gender"], "female")
        self.assertEqual(item["audience_age_band"], "108-143")
        self.assertEqual(item["reported_wear"], ["rip or tear"])
        self.assertTrue(item["wear_description_observed"])
        self.assertNotIn("Personal contact", json.dumps(item))
        self.assertNotIn("Girls size", json.dumps(item))

    def test_catalogue_pool_retains_product_facts_across_a_later_skim(self):
        now = time.time()
        with (
            tempfile.TemporaryDirectory() as tmp,
            sqlite3.connect(Path(tmp) / "pool.db") as conn,
        ):
            conn.executescript("""
              CREATE TABLE window (id INTEGER PRIMARY KEY, ends REAL);
              CREATE TABLE offers (id TEXT PRIMARY KEY,observed REAL,brand TEXT,size TEXT,seller TEXT,payload TEXT);
            """)
            conn.execute("INSERT INTO window VALUES (1,?)", (now + 1000,))
            for index, detailed in enumerate((True, False)):
                pool._store(
                    conn, now + index, [self.item(detail=detailed)], now + index
                )
                self.assert_documented(
                    json.loads(conn.execute("SELECT payload FROM offers").fetchone()[0])
                )
            replacement = self.item(detail=False)
            replacement["user"] = {"id": "600"}
            pool._store(conn, now + 2, [replacement], now + 2)
            new = json.loads(conn.execute("SELECT payload FROM offers").fetchone()[0])
            self.assertFalse(new["reported_wear"])
            self.assertNotIn("wear_description_observed", new)
            self.assertNotIn("audience_age", new)

    def test_target_and_sent_comparables_read_their_own_public_listing_descriptions(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "original.db"
            with sqlite3.connect(db) as conn:
                conn.executescript("""
                  CREATE TABLE alert_outbox (item_id TEXT,title TEXT,url TEXT,platform TEXT,status TEXT,currency TEXT,found_at REAL);
                  CREATE TABLE vinted_alert_details (item_id TEXT,payload TEXT);
                """)
                for identity in ("101", "102"):
                    item = {
                        "id": identity,
                        "title": self.title,
                        "size": "S",
                        "brand": "Mango",
                        "seller_id": identity,
                    }
                    conn.execute(
                        "INSERT INTO alert_outbox VALUES(?,?,?,'vinted','sent','GBP',?)",
                        (
                            identity,
                            self.title,
                            "https://www.vinted.co.uk/items/" + identity,
                            time.time(),
                        ),
                    )
                    conn.execute(
                        "INSERT INTO vinted_alert_details VALUES(?,?)",
                        (
                            identity,
                            json.dumps(
                                {"trial_listing": item, "description": self.prose}
                            ),
                        ),
                    )
            target, comparisons = hourly._live_rows(db, "101")
            self.assert_documented(target)
            self.assert_documented(comparisons[0])

    def test_audience_conflict_stops_before_photo_or_comparison_requests(self):
        item = {
            "id": "101",
            "title": "Mango Kids denim jacket",
            "size": "S / UK 8-10",
            "active": True,
        }
        with (
            patch.object(hourly, "_live_rows", return_value=(item, [])),
            patch.object(hourly, "_first_price", new_callable=AsyncMock) as photo,
            patch(
                "trial_market_search.search_marketplaces", new_callable=AsyncMock
            ) as public,
        ):
            result = asyncio.run(hourly.evaluate("101", "irrelevant", None, None))
        self.assertEqual(result["market_status"], "conflicting_audience")
        self.assertIsNone(result["resale_pence"])
        self.assertEqual(result["market_calls"], 0)
        self.assertEqual(result["model_calls"], 0)
        photo.assert_not_awaited()
        public.assert_not_awaited()

    def test_provider_cache_preserves_only_checked_facts(self):
        now = time.time()
        raw = {
            "id": "101",
            "seller": {"id": "501"},
            "brand": "Mango",
            "size": "S",
            "title": self.title,
            "price": 7,
            "condition": "Good",
            "description": self.prose,
        }
        checked = hourly._checked_market(raw, "GBP")
        self.assert_documented(checked)
        with (
            tempfile.TemporaryDirectory() as tmp,
            market_cache(Path(tmp) / "cache") as conn,
        ):
            conn.execute(
                "INSERT INTO market VALUES(?,?,?)",
                (json.dumps(["mango", "jacket"]), now, json.dumps([checked])),
            )
            rows = hourly._market_rows(
                conn, {"brand": "Mango", "title": self.title}, now
            )
            self.assert_documented(rows[0])

    def test_public_item_page_size_age_condition_and_seller_notes(self):
        link = "https://www.ebay.co.uk/itm/123458123458"
        product = {
            "@type": "Product",
            "url": link,
            "name": self.title,
            "brand": {"name": "Mango"},
            "size": "S",
            "audience": {
                "@type": "PeopleAudience",
                "suggestedGender": "female",
                "suggestedMinAge": 9,
                "suggestedMaxAge": 11,
            },
            "additionalProperty": [
                {"name": "Condition", "value": "Good"},
                {"name": "Department", "value": "Girls"},
                {
                    "name": "Seller Notes",
                    "value": "Slight rip in the lining. Personal contact details omitted.",
                },
            ],
            "offers": {
                "priceCurrency": "GBP",
                "price": "7.00",
                "availability": "https://schema.org/InStock",
                "seller": {"name": "publicseller", "address": {"addressCountry": "GB"}},
            },
        }
        doc = html.fromstring(
            '<script type="application/ld+json">' + json.dumps(product) + "</script>"
        )
        evidence = search._detail(doc, "ebay_uk_active", ("123458123458", link), None)
        self.assert_documented(evidence)
        adult = dict(
            evidence,
            id="777",
            seller_id="another",
            audience_age="adult",
            audience_gender="female",
            reported_wear=[],
        )
        adult.pop("audience_age_band")
        self.assertFalse(search._match(adult, evidence))
        same_child_without_defect = dict(
            evidence, id="778", seller_id="another", reported_wear=[]
        )
        self.assertFalse(search._match(same_child_without_defect, evidence))
        product["audience"] = {"@type": "PeopleAudience"}
        product["additionalProperty"] = [{"name": "Condition", "value": "Good"}]
        doc = html.fromstring(
            '<script type="application/ld+json">' + json.dumps(product) + "</script>"
        )
        incomplete = search._detail(doc, "ebay_uk_active", ("123458123458", link), None)
        self.assertNotIn("audience_age", incomplete)
        self.assertNotIn("wear_description_observed", incomplete)
