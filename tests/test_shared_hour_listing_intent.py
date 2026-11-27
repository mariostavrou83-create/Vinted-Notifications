import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from trial_listing_intent import not_for_sale
from trial_market_hour import evaluate
from trial_price_rules import estimate_resale, product_mismatch


class ExplicitNonSale(unittest.TestCase):
    def test_notices_and_wanted_searches(self):
        for title in ("Jeans (don’t buy listing)", "DO NOT BUY THIS LISTING", "Jeans - don't buy",
                      "Dress NOT FOR SALE", "This listing is not for sale", "ISO: vintage Diesel belt",
                      "Wanted — vintage jacket", "Wanted jeans UK 8", "In search of Bambi necklace",
                      "Denim jacket (ISO)", "Looking to buy this dress"):
            with self.subTest(title=title):
                self.assertTrue(not_for_sale({"title":title}))

    def test_real_products_do_not_become_wanted_notices(self):
        for title,brand in (("Most Wanted jeans","Levi's"),("Wanted Jeans","Wanted"),("Wanted by Night perfume","Azzaro"),
                            ("The Wanted tour T-shirt",""),("Not for Sale graphic T-shirt",""),
                            ("Don't Buy This Book",""),("ISO 100 camera film","Kodak"),
                            ("Jacket – please message before buying",""),("Top – no offers","")):
            with self.subTest(title=title):
                self.assertFalse(not_for_sale({"title":title,"brand":brand}))

    def test_full_evaluator_never_prices_or_spends_on_placeholder_or_withdrawn_target(self):
        async def run():
            for target in ({"id":"1","title":"Jeans (don’t buy listing)","active":True},
                           {"id":"2","title":"Jeans","active":False}):
                with patch("trial_market_hour._live_rows",return_value=(target,[])), \
                     patch("trial_market_hour._first_price",new=AsyncMock()) as rough, \
                     patch("trial_market_search.search_marketplaces",new=AsyncMock()) as search, \
                     patch("trial_market_hour._get_comps",new=AsyncMock()) as fresh:
                    counters=Mock()
                    got=await evaluate(target["id"],"unused",None,counters)
                    self.assertEqual((got["model_calls"],got["market_calls"],got["market_status"]),(0,0,"not_for_sale"))
                    self.assertIsNone(got["resale_pence"])
                    rough.assert_not_called()
                    search.assert_not_called()
                    fresh.assert_not_called()
                    counters.reserve.assert_not_called()
        asyncio.run(run())

    def test_placeholder_cannot_be_an_asking_price_comparable(self):
        real={"title":"Diesel flared jeans","brand":"Diesel"}
        fake={"title":"Diesel flared jeans (not for sale)","brand":"Diesel"}
        self.assertEqual(product_mismatch(real,fake,rank_style=True),"Not offered for sale")
        self.assertIsNone(estimate_resale(fake,[],review=lambda *args:1/0)["resale_pence"])
