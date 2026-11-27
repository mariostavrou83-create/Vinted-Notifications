import asyncio
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import trial_market_hour as market
from trial_cross_market import guide
from trial_price_rules import estimate_resale
from trial_style_evidence import reported_wear


def offer(i, ask, *, title="Next beaded cami top", condition="Very good", source="vinted_uk", sold=False):
    ids = str(100000000000+i)
    url = ("https://www.vinted.co.uk/items/" + ids if source == "vinted_uk" else
           "https://www.depop.com/products/holder-"+ids if source == "depop_uk" else "https://www.ebay.co.uk/itm/" + ids)
    return {"id":ids,"seller_id":str(1000+i),"brand":"Next","size":"XS","title":title,"condition":condition,
            "url":url,"currency":"GBP","active":not sold,"country":"UK","observed_at":time.time(),
            "price_kind":"sold" if sold else "ask","sold_pence" if sold else "ask_pence":ask,
            "sold_amount_confirmed":sold,"sold_at":time.time()-86400 if sold else None}


class RankedMarket(unittest.TestCase):
    def test_rough_price_persisted_before_resume_of_market_without_comparables(self):
        async def test():
            order = []
            rough = {"photo_status":"estimated", "low_pence":1200, "high_pence":2200, "quick_pence":1600,
                     "model_calls":1,"provider_ms":80,"visible_traits":[]}
            target = offer(1,500)
            counters = Mock()
            counters.rough.side_effect = lambda i,v:order.append("visible")
            async def lookup(*args):
                order.append("market")
                return {"sources":{},"fresh_requests":0}
            with patch.object(market,"_live_rows",return_value=(target, [])), \
                 patch("trial_photo_price.rough_estimate",new=AsyncMock(return_value=rough)), \
                 patch("trial_public_pool.candidates",return_value=[]), \
                 patch("trial_public_pool.latest_unavailable",return_value={}), \
                 patch.object(market,"_get_comps",new=AsyncMock(return_value=([], "empty", 0))), \
                 patch("trial_market_search.search_marketplaces",new=lookup):
                got = await market.evaluate(target["id"],"unused",None,counters)
            self.assertEqual(order,["visible","market"])
            self.assertEqual(got["rough"]["quick_pence"],1600)
            self.assertEqual(got["model_calls"],1)
            self.assertIsNone(got["resale_pence"])
        asyncio.run(test())

    def test_closest_style_sets_price_and_weaker_offers_are_labelled(self):
        target=offer(1,900,title="Next beaded cami top")
        similar=[offer(2,1700),offer(3,1800)]
        plain=offer(4,800,title="Next cami top")
        worn=dict(offer(5,900),reported_wear=["peeling"])
        # The plain and intact-style peers span more than 2x after the worn £9 listing is excluded.
        self.assertIsNone(estimate_resale(target,[plain,*similar,worn])["resale_pence"])
        result=estimate_resale(target,[plain,*similar,worn],rank_style=True)
        self.assertEqual(result["resale_pence"],1650)
        self.assertEqual(len(result["evidence"]),2)
        self.assertEqual(len(result["context_evidence"]),2)
        self.assertIsNone(estimate_resale(target,[plain,worn],rank_style=True)["resale_pence"])
        self.assertEqual(reported_wear("No stains or peeling. Slight peeling along trim."), ["peeling"])

    def test_cross_listing_and_unknown_best_offer_do_not_create_two_independent_observations(self):
        target=offer(1,900)
        sold=offer(2,1400,source="ebay_uk_sold",sold=True)
        duplicate=offer(3,2500,source="depop_uk")
        unrevealed=dict(offer(4,2800,source="ebay_uk_sold",sold=True),sold_amount_confirmed=False)
        sources={"ebay_uk_sold":{"status":"checked","rows":[sold,unrevealed]},
                 "depop_uk":{"status":"checked","rows":[duplicate]}}
        result=guide(target,sources,[],set(),time.time())
        self.assertEqual(result["status"],"insufficient")
        self.assertEqual(len(result["evidence"]),1)
        extra=offer(5,1600,source="ebay_uk_sold",sold=True)
        sources["ebay_uk_sold"]["rows"].append(extra)
        result=guide(target,sources,[],set(),time.time())
        self.assertEqual((result["low_pence"],result["high_pence"],result["sold_count"]),(1400,1600,2))

    def test_two_sold_results_can_lower_higher_vinted_ask_with_basis_label(self):
        async def test():
            target=offer(1,600)
            vinted=[offer(2,1700),offer(3,1800)]
            sources={"ebay_uk_sold":{"status":"checked","url":"https://www.ebay.co.uk","rows":[
                offer(4,1200,source="ebay_uk_sold",sold=True),offer(5,1300,source="ebay_uk_sold",sold=True)]}}
            with patch.object(market,"_live_rows",return_value=(target,vinted)), \
                 patch("trial_photo_price.rough_estimate",new=AsyncMock(return_value={"photo_status":"hour_limit","model_calls":0})), \
                 patch("trial_public_pool.candidates",return_value=[]),patch("trial_public_pool.latest_unavailable",return_value={}), \
                 patch.object(market,"_get_comps",new=AsyncMock(return_value=(vinted,"cached",0))), \
                 patch("trial_market_search.search_marketplaces",new=AsyncMock(return_value={"sources":sources,"fresh_requests":2})):
                got=await market.evaluate(target["id"],"unused",None,Mock())
            self.assertEqual((got["baseline_pence"],got["resale_pence"]),(1650,1300))
            self.assertIn("confirmed UK eBay sales",got["explanation"])
            self.assertIsNone(got["profit_pence"])
        asyncio.run(test())

    def test_sparse_padded_jacket_is_a_closest_comparison_when_ancillary_traits_are_unknown(self):
        target=dict(offer(1,800,title="Jane Norman fur collared coat"),brand="Jane Norman",
                    leaf_title="Puffer jackets", visible_traits=["puffer","fur collar","cropped","zip front","fitted"])
        dated=dict(offer(2,1000,title="Fur collar jacket"),brand="Jane Norman", leaf_title="Padded jackets")
        other=dict(dated, id="100000000003",seller_id="1003", url="https://www.vinted.co.uk/items/100000000003",
                   ask_pence=1200)
        self.assertIsNone(estimate_resale(target,[dated,other])["resale_pence"])
        result=estimate_resale(target,[dated,other],rank_style=True)
        self.assertEqual(result["resale_pence"],950)
        self.assertIn("Unconfirmed on comparison",result["evidence"][0]["reason"])
        self.assertIn("puffer",result["evidence"][0]["reason"])
        for conflict in (
                dict(dated, title="Faux Fur Coat", leaf_title="Faux Fur Coats"),
                dict(dated, title="Oversized fur collar jacket"),
                dict(dated, title="Fitted black jacket",leaf_title="Jackets"),
                dict(dated, size="S"), dict(dated, brand="Unbranded")):
            self.assertFalse(estimate_resale(target,[conflict],rank_style=True)["evidence"])
        poorer=dict(dated,condition="Good")
        z=estimate_resale(target,[poorer],rank_style=True)
        self.assertFalse(z["evidence"])
        self.assertIn("lower",z["context_evidence"][0]["reason"])

    def test_fully_described_peers_take_priority_over_a_cheaper_partial_match(self):
        target=offer(1,800,title="Next fitted cropped beaded cami top")
        fully=[offer(2,1700,title=target["title"]),offer(3,1900,title=target["title"])]
        partial=offer(4,1000,title="Next beaded cami top")
        self.assertEqual(estimate_resale(target,[*fully,partial],rank_style=True)["resale_pence"],1650)
