"""A garment family's evidence must stay consistent across the photo, baseline and every market."""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from trial_cross_market import guide
from trial_market_hour import evaluate
from trial_market_search import _match
from trial_price_rules import estimate_resale, product_mismatch


def listing(identity, title, **extra):
    return dict({"id":identity, "url":f"https://www.vinted.co.uk/items/{identity}", "seller_id":"seller"+identity,
                 "title":title, "brand":"Morgan", "size":"S", "condition":"Very good",
                 "currency":"GBP", "country":"UK", "active":True, "ask_pence":1000,
                 "source":"vinted_uk", "observed_at":time.time()}, **extra)


class FamilyGate(unittest.TestCase):
    def test_morgan_skirt_cannot_use_jackets_dress_suit_or_generic_brand_across_lanes(self):
        target = listing("10994172333", "Morgan de toi skirt")
        # The owner reported two jackets; these are regression fixture titles,
        # not claims about the exact marketplace descriptions for the recorded IDs.
        other_types = [
            listing("9049695543", "Morgan de toi jacket"), listing("8629621605", "Morgan de toi black jacket"),
            listing("51", "Morgan de toi black dress"), listing("52", "Morgan de toi skirt suit"),
            listing("53", "Morgan de toi matching skirt and jacket"), listing("54", "Morgan de toi black"),
        ]
        for row in other_types:
            with self.subTest(title=row["title"]):
                for ranked in (False, True):
                    self.assertTrue(product_mismatch(target,row,rank_style=ranked))
                    self.assertTrue(product_mismatch(dict(target,model="Signature"),dict(row,model="Signature"),
                                                     rank_style=ranked))
                self.assertFalse(_match(target,row))
        for ranked in (False,True):
            picks=Mock(side_effect=AssertionError("a model cannot re-admit wrong-type offers"))
            wrong=estimate_resale(target,other_types,review=picks,rank_style=ranked)
            self.assertEqual(wrong["evidence"], [])
            self.assertIsNone(wrong["resale_pence"])
            picks.assert_not_called()
        external = [dict(r, url=f"https://www.ebay.co.uk/itm/{100000000000+i}",source="ebay_uk_active")
                    for i,r in enumerate(other_types)]
        self.assertEqual(guide(target,{"ebay_uk_active":{"status":"checked","rows":external}},[],set(),time.time())["evidence"],[])

    def test_true_corroborated_skirts_survive_while_conflicting_leaf_and_photo_do_not(self):
        target=listing("10994172333","Morgan de toi skirt",leaf_title="Pleated skirts",visible_traits=["pleated"])
        correct=[listing("301","Morgan de toi pleated skirt",leaf_title="Pleated skirts",ask_pence=1500),
                 listing("302","Black Morgan pleated skirt",leaf_title="Pleated skirts",ask_pence=1700)]
        mistaken=listing("303","Morgan de toi pleated skirt",leaf_title="Denim jackets")
        self.assertFalse(_match(target,mistaken))
        self.assertTrue(product_mismatch(target,mistaken))
        priced=estimate_resale(target,[mistaken,*correct],rank_style=True)
        self.assertEqual([e["id"] for e in priced["evidence"]],["301","302"])
        self.assertEqual(priced["resale_pence"],1450)
        for wrong in [dict(correct[0],photo_family="jacket"), dict(correct[0],leaf_title="Dresses")]:
            self.assertTrue(product_mismatch(target,wrong))

    def test_photo_contradiction_prevents_price_publication_before_market_and_feedback(self):
        async def run():
            target=listing("10994172333","Morgan de toi skirt")
            counters=Mock()
            for calculated in (True,False):
                data={"photo_family":"jacket","photo_status":"estimated" if calculated else "unidentifiable",
                      "model_calls":1}
                if calculated:
                    data.update(low_pence=800,quick_pence=1000,high_pence=1300)
                with patch("trial_market_hour._live_rows",return_value=(target,[])), \
                     patch("trial_photo_price.rough_estimate",new=AsyncMock(return_value=data)), \
                     patch("trial_market_search.search_marketplaces",new=AsyncMock()) as search:
                    out=await evaluate(target["id"],"unused",None,counters)
                self.assertEqual(out["market_status"],"conflicting_product_type")
                self.assertIsNone(out["rough"]["low_pence"])
                counters.rough.assert_not_called()
                search.assert_not_called()
        asyncio.run(run())
