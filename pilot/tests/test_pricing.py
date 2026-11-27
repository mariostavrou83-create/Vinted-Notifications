"""Invented listings exercise the decision rules without marketplace calls or paid model usage."""

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from pilot.pricing import estimate_resale


NOW = datetime(2026, 11, 27, 18, tzinfo=timezone.utc)
TARGET = {
    "id": "1", "seller_id": "company-a", "title": "Oasis leather bomber jacket",
    "brand": "Oasis", "category": "Jackets", "subtype": "bomber", "material": "leather",
    "condition": "Very good", "size": "8", "all_in_pence": 700,
}


def comparable(identifier, price, target=TARGET, **overrides):
    return dict(dict(target, id=str(identifier), seller_id=f"seller-{identifier}",
                     ask_pence=price, observed_at=NOW.isoformat(), currency="GBP",
                     url=f"https://www.vinted.co.uk/items/{identifier}-description?ref=personal"), **overrides)


class PricingTests(unittest.TestCase):
    def evaluate(self, rows, target=TARGET, **kw):
        return estimate_resale(target, rows, now=NOW, **kw)

    def test_first_credible_comp_exclusions_and_exact_profit_threshold(self):
        results = self.evaluate([
            comparable(2, 950, material="faux leather"),
            comparable(3, 1500, condition="Satisfactory"),
            comparable(4, 2250, material="genuine leather", size="UK 8"), comparable(5, 2399), comparable(6, 2099, size="16"),
            comparable(7, 750, seller_id=TARGET["seller_id"]),
        ])
        self.assertEqual(results["resale_pence"], 2200)
        self.assertEqual(results["profit_pence"], 1500)
        self.assertTrue(results["meets_target"])
        self.assertEqual([x["id"] for x in results["evidence"]], ["4", "5"])
        self.assertNotIn("ref=", results["evidence"][0]["url"])
        just_below = self.evaluate([comparable(2, 2250), comparable(3, 2300)],
                                  dict(TARGET, selling_cost_pence=1))
        self.assertEqual(just_below["profit_pence"], 1499)
        self.assertFalse(just_below["meets_target"])

    def test_cheaper_better_condition_caps_suggested_price(self):
        value = self.evaluate([comparable(2, 1900, condition="New with tags"), comparable(3, 2100)])
        self.assertEqual(value["resale_pence"], 1850)
        self.assertIn("upper limit", value["evidence"][0]["reason"])

    def test_unrelated_or_insufficient_comps_cannot_produce_an_estimate(self):
        rows = [
            comparable(15, 2250), comparable(16, 2200, seller_id="seller-15"),
            comparable(17, 1600, seller_id=""), comparable(18, 1900, status="sold"),
            comparable(19, 1700, observed_at=(NOW - timedelta(hours=73)).isoformat()),
            comparable(20, 1800, country="DE"), comparable(21, 1700, currency="EUR"),
            comparable(22, 1550, url="https://example.org/items/22"),
            comparable(23, 900, url="https://www.vinted.co.uk/items/1"),
        ]
        value = self.evaluate(rows)
        self.assertIsNone(value["resale_pence"])
        self.assertIsNone(value["meets_target"])
        self.assertEqual(len(value["evidence"]), 1)
        self.assertTrue(value["exclusions"])

    def test_category_independence_and_model_variants(self):
        camera = {"id": "1", "seller_id": "21", "title": "Canon EOS 2000D body", "brand": "Canon",
                  "category": "Digital cameras", "model": "EOS 2000D", "condition": "Good"}
        rows = [
            comparable(2, 22500, camera), comparable(3, 24000, camera),
            comparable(4, 12500, camera, model="EOS 200D"), comparable(5, 2100, camera, title="Case for Canon EOS 2000D body"),
        ]
        value = self.evaluate(rows, camera)
        self.assertEqual(value["resale_pence"], 22450)
        self.assertIsNone(value["profit_pence"])
        self.assertEqual(len(value["evidence"]), 2)
        records = {"id": "1", "seller_id": "a", "title": "Artist Album 1995 vinyl", "category": "Records",
                   "condition": "Good", "format": "Vinyl", "edition": "1995 first pressing"}
        value = self.evaluate([comparable(2, 1250, records), comparable(3, 1699, records),
                               comparable(4, 499, records, edition="2023 reissue")], records)
        self.assertEqual(value["resale_pence"], 1200)

    def test_stale_cached_listing_replaced_by_fresher_inactive_row(self):
        rows = [comparable(2, 2250, observed_at=(NOW-timedelta(hours=2)).isoformat()),
                comparable(2, 2250, status="reserved"), comparable(3, 2399)]
        self.assertIsNone(self.evaluate(rows)["resale_pence"])

    def test_advertised_price_outlier_requires_review(self):
        value = self.evaluate([comparable(2, 400), comparable(3, 2800), comparable(4, 2950)])
        self.assertIsNone(value["resale_pence"])
        self.assertEqual(len(value["evidence"]), 3)
        self.assertIn("half", value["explanation"])

    def test_ai_can_remove_matches_but_cannot_set_prices_or_invent_comps(self):
        rows = [comparable(2, 1850), comparable(3, 2250), comparable(4, 2300)]
        value = self.evaluate(rows, review=lambda _target, _rows: [1, 2])
        self.assertEqual((value["resale_pence"], value["review_status"]), (2200, "ai_reviewed"))
        value = self.evaluate(rows, review=lambda _target, _rows: [400, 500])
        self.assertEqual((value["resale_pence"], value["review_status"]), (1800, "ai_unavailable"))
        value = self.evaluate(rows, review=lambda _target, _rows: [])
        self.assertIsNone(value["resale_pence"])
