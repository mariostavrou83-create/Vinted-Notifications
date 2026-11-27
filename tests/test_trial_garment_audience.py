import time
import unittest

from trial_cross_market import guide
from trial_market_search import _match as external_match
from trial_price_rules import estimate_resale, product_mismatch
from trial_style_evidence import audience_fields, same_garment_family

NOW = time.time()


def jacket(identity, title="Mango chocolate brown leather jacket", size="S / UK 8–10", **fields):
    return {"id": str(identity), "url": f"https://www.vinted.co.uk/items/{identity}",
            "seller_id": str(identity), "title": title, "brand": "Mango", "size": size,
            "condition": "Very good", "active": True, "observed_at": NOW, "currency": "GBP",
            "country": "UK", "ask_pence": 1500, "price_kind": "ask", **fields}


class GarmentAudience(unittest.TestCase):
    def test_real_kids_listing_cannot_set_mango_adult_price(self):
        target = jacket(10994339595)
        child = jacket(10924241338, "Kids leather jacket", ask_pence=1000,
                       description="Black leather jacket - Mango Kids size 9-11 years")
        adult = jacket(11, ask_pence=1850)
        self.assertEqual(audience_fields(child)["audience_age"], "conflict")
        self.assertFalse(same_garment_family(target, child))
        for ranked in (False, True):
            with self.subTest(ranked=ranked):
                self.assertIn("age", product_mismatch(target, child, rank_style=ranked))
                result = estimate_resale(target, [child, adult], now=NOW, rank_style=ranked)
                self.assertIsNone(result["resale_pence"])
                self.assertNotIn(child["id"], [x["id"] for x in result["evidence"]])
        e = dict(child, url="https://www.ebay.co.uk/itm/10924241338")
        self.assertFalse(external_match(target, e))
        result = guide(target, {"ebay_uk_active": {"status":"checked","rows":[e]}},
                       [adult], {adult["id"]}, NOW)
        self.assertNotIn(child["title"], [x["title"] for x in result["evidence"]])

    def test_explicit_age_in_description_category_and_size(self):
        target = jacket(1)
        for row in (jacket(2, description="Sold as age 9–11 years"), jacket(2, category_path=["Kids", "Outerwear"]),
                    jacket(2, size="11–12 yrs"), jacket(2, size="12Y"), jacket(2, size="Aged 12"),
                    jacket(2, size="4T"), jacket(2, size="9-11 yo"), jacket(2, category_path="Kidswear"),
                    jacket(2, age_group="teen"), jacket(2, audience={"suggestedMaxAge": 15}),
                    jacket(2, department="Women and children")):
            with self.subTest(row=row):
                self.assertIsNotNone(product_mismatch(target, row))
        # Known UK children's size ranges must agree even when their standard alpha-size tags match.
        self.assertIsNotNone(product_mismatch(jacket(1, "Kids jacket", "S", description="Kids size 6–8 years"),
                                              jacket(2, "Kids jacket", "S", description="Kids size 10–12 years")))
        self.assertEqual(audience_fields({"size":"age 9-11 months"})["audience_age_band"], "9-11")

    def test_positive_adult_and_child_matches_and_ambiguous_size(self):
        self.assertIsNone(product_mismatch(jacket(1), jacket(2, department="women", size="S")))
        child = jacket(11, "Mango kids brown leather jacket", "9–11 years", department="girls")
        self.assertIsNone(product_mismatch(child, dict(child, id="12", seller_id="12")))
        for val in ("XXS","XS","S","8–10"):
            self.assertNotIn("audience_age", audience_fields({"title":"leather jacket","size":val}))
        self.assertNotIn("audience_age", audience_fields({"title":"Boyfriend jeans","size":"XXS"}))
        self.assertNotIn("audience_age", audience_fields({"title":"Fitted baby tee","size":"XS"}))
        self.assertIsNone(product_mismatch(jacket(1, size="XXS"), jacket(2, size="XXS")))

    def test_different_or_contradictory_declared_departments(self):
        target = jacket(1, gender="female")
        self.assertIn("gender", product_mismatch(target,jacket(2, department="Men")))
        self.assertIn("Conflicting",product_mismatch(target,jacket(2, department="Women's",gender="male")))
        self.assertIsNone(product_mismatch(target,jacket(2, gender="unisex")))
        self.assertEqual(audience_fields({"audience":{"suggestedGender":"female","suggestedMinAge":9,
                                                     "suggestedMaxAge":11}}),
                         {"audience_gender":"female","audience_age":"child","audience_age_band":"108-143"})
