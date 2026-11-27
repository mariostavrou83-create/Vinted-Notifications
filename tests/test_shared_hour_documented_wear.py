import time
import unittest

from trial_cross_market import guide
from trial_price_rules import estimate_resale
from trial_style_evidence import assess, reported_wear

MANGO_DISCLOSURE = "It has been worn a few times and there is a slight rip that is showing the photos"


def jacket(identity, ask, *, description="", age=0, seller=None):
    return {"id":identity, "seller_id":seller or identity, "url":f"https://www.vinted.co.uk/items/{identity}",
            "brand":"Mango","title":"Mango chocolate brown leather jacket","size":"S / UK 8-10",
            "condition":"Very good", "country":"UK","currency":"GBP","active":True, "ask_pence":ask,
            "observed_at":time.time()-age,"reported_wear":reported_wear(description)}


class DocumentedWear(unittest.TestCase):
    def test_seller_disclosure_and_local_negations(self):
        for text in (MANGO_DISCLOSURE, "A small tear in the sleeve.", "The lining is torn at the cuff.",
                     "There is no staining, but there is one tiny rip.", "No flaws except a slight tear.",
                     "No rips, and there is a small tear under the arm."):
            with self.subTest(text=text):
                self.assertIn("rip or tear",reported_wear(text))
        self.assertEqual(reported_wear("No stains but one small hole on the sleeve."), ["hole"])
        self.assertEqual(reported_wear("No rips or tears. There are no stains. No flaws."), [])
        self.assertEqual(reported_wear("A slight rip as shown, no other flaws."), ["rip or tear"])

    def test_usual_fashion_finish_is_not_seller_reported_damage(self):
        for text in ("Distressed denim, ripped jeans.", "Distressed jeans, fashionably ripped at the knees",
                     "Intentionally distressed with a small hole on each knee by design",
                     "A rip-resistant shell.", "Rip Curl swimwear.", "Normal wear and tear",
                     "Thumb holes at cuffs.", "No tears in the lining or holes under the arms."):
            self.assertEqual(reported_wear(text),[],text)

    def test_damaged_mango_925_cannot_anchor_the_price_even_with_a_newer_sparse_tile(self):
        target=jacket("10994339595",2000)
        damaged=jacket("10990938106",925,description=MANGO_DISCLOSURE,age=16)
        sparse=dict(damaged,reported_wear=[],observed_at=time.time())
        peers=[jacket("201",1950),jacket("202",2200)]
        for style in (False, True):
            for rows in ((damaged,sparse,*peers),(sparse,damaged,*peers)):
                with self.subTest(style=style):
                    result=estimate_resale(target,rows,rank_style=style)
                    self.assertEqual(result["resale_pence"],1900)
                    self.assertEqual([e["id"] for e in result["evidence"]],["201","202"])
                    if style:
                        ctx=result["context_evidence"]
                        self.assertEqual(len(ctx),1)
                        self.assertIn("Comparison seller discloses rip or tear", ctx[0]["reason"])
                        self.assertNotIn("intact",ctx[0]["reason"].lower())
        self.assertEqual(assess(target,damaged,3,3)[0],"context")

    def test_damaged_external_comp_cannot_establish_cross_market_range(self):
        target=jacket("10994339595",2000)
        damaged=jacket("10990938106",925,description=MANGO_DISCLOSURE)
        ebay=dict(damaged,source="ebay_uk_active",url="https://www.ebay.co.uk/itm/123456789101",price_kind="ask")
        good=dict(ebay,seller_id="independent",url="https://www.ebay.co.uk/itm/123456789103",reported_wear=[])
        result=guide(target,{"ebay_uk_active":{"status":"checked","rows":[ebay,good]}},[],set(),time.time())
        self.assertEqual(result["status"],"insufficient")
        self.assertEqual([e["url"] for e in result["evidence"]],[good["url"]])
