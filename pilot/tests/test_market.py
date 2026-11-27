import json
import sqlite3
import unittest
from unittest.mock import patch

import requests
from flask import Flask

from pilot import market
from pilot.pricing import estimate_resale


class MarketEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(market.SCHEMA)
        self.now = 1795800000
        self.opts = {"PILOT_SCAVIO_ENABLED": True, "PILOT_SCAVIO_API_KEY": "test",
                     "PILOT_SCAVIO_MAX_SEARCHES": 1}
        self.target = {"id":"91", "seller_id":"101", "brand":"Example", "title":"Example Bedale wax jacket",
                       "condition":"Good","size":"M", "all_in_pence": 2450, "currency":"GBP"}
        # Sanitised provider fields follow the same schema as tests/test_resale_pilot.py.
        self.offer = {"id":1011,"url":"https://www.vinted.co.uk/items/1011-jacket",
                      "seller":{"id":3}, "currency":"GBP", "price":42.00, "price_with_discount":"40.00",
                      "total_price":46.00, "condition":"Good", "size":"M", "brand":{"title":"Example"},
                      "title":"Example Bedale wax jacket"}

    def test_lookup_price_matches_distinct_sellers_and_cache_is_shared(self):
        with patch.object(market,"_search",return_value=[
            self.offer, dict(self.offer,id=1012,url=None,seller={"id":4}),dict(self.offer,id=1013,url=None,seller={})
        ]) as provider:
            comps=market.comparisons_for(self.conn,self.target,config=self.opts,now=self.now)
            self.assertEqual(len(comps),2)
            self.assertEqual(comps[0]["ask_pence"],4000)
            self.assertEqual(comps[0]["source"],"Scavio UK search")
            self.assertIsNotNone(estimate_resale(self.target,comps,now=self.now)["resale_pence"])
            another=market.comparisons_for(self.conn,dict(self.target,id="999"),config=self.opts,now=self.now+10)
            self.assertEqual(another,comps)
            self.assertEqual(provider.call_count,1)
            self.assertEqual(self.conn.execute("select used from market_budget").fetchone()[0],1)

    def test_default_no_spend_and_no_spend_on_unpriceable_item(self):
        with patch.object(market,"_search") as search:
            for cfg in ({},dict(self.opts,PILOT_SCAVIO_MAX_SEARCHES=0)):
                self.assertEqual(market.comparisons_for(self.conn,self.target,config=cfg,now=self.now),[])
            market.comparisons_for(self.conn,dict(self.target,seller_id=None),config=self.opts,now=self.now)
            search.assert_not_called()

    def test_bad_links_inactive_wrong_currency_and_sell_v_buy_prices(self):
        def norm(item): return market._normalize(item,self.now,"Scavio UK search")
        for changes in (
            {"url":"https://www.vinted.co.uk/items/1012-mismatch"},
            {"url":"https://www.vinted.co.uk.example.com/items/1011"},
            {"url":"https://www.vinted.co.uk:444/items/1011"},
            {"currency":"EUR"},{"price":None},{"is_reserved":True},{"active":False},{"status":"sold"},
            {"condition":"Good","seller":{}}
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(norm(dict(self.offer,**changes)))

    def test_failed_calls_cost_budget_but_never_disclose_credentials_or_double_spend(self):
        with patch.object(market,"_search",side_effect=requests.Timeout) as search:
            market.comparisons_for(self.conn,self.target,config=self.opts,now=self.now)
            market.comparisons_for(self.conn,dict(self.target,title="Another product"),config=self.opts,now=self.now+1000)
            self.assertEqual(search.call_count,1)
            self.assertEqual(self.conn.execute("select used from market_budget").fetchone()[0],1)

    def test_db_leases_apply_across_cold_queries_and_failed_queries(self):
        self.assertTrue(market._reserve(self.conn,"q1",self.now,3))
        self.assertFalse(market._reserve(self.conn,"q2",self.now,3))
        self.assertFalse(market._reserve(self.conn,"q1",self.now+31,3))
        self.assertTrue(market._reserve(self.conn,"q2",self.now+31,3))

    def test_verified_import_is_atomic_and_overwrites_only_newer_observations(self):
        app=Flask(__name__)
        app.cli.add_command(market.import_command)
        entry={"id":"1011","seller_id":"77","title":"Bedale wax jacket","condition":"Good",
               "url":self.offer["url"],"ask_pence":3900,"active":True,"country":"UK","currency":"GBP",
               "observed_at":self.now}
        with patch.object(market,"get_db",return_value=self.conn),patch.object(market.time,"time",return_value=self.now):
            def run(*rows):
                return app.test_cli_runner().invoke(args=["market-import","-"],input="\n".join(json.dumps(r) for r in rows))
            self.assertNotEqual(run(entry,dict(entry,id="21",url="https://bad")).exit_code,0)
            self.assertEqual(self.conn.execute("select count(*) from market_offers").fetchone()[0],0)
            self.assertEqual(run(entry).exit_code,0)
            self.assertEqual(run(dict(entry,ask_pence=1700,observed_at=self.now-20)).exit_code,0)
            self.assertEqual(market._cached(self.conn,self.target,self.now)[0]["ask_pence"],3900)
            self.assertEqual(market._cached(self.conn,self.target,self.now+market.MAX_AGE+1),[])


if __name__ == "__main__":
    unittest.main()
