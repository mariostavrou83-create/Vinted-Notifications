import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import httpx

import trial_market_hour as pricing
from resale_pilot import market_cache


def listing(identity, seller, ask=6, *, condition="Very good"):
    return SimpleNamespace(id=str(identity), title="Hollister argyle cropped hoodie",
                           brand_title="Hollister", size_title="S", price=ask, currency="GBP",
                           url=f"https://www.vinted.co.uk/items/{identity}-hoodie",
                           raw_data={"user": {"id": seller}, "item_box": {"second_line": "S · " + condition}})


class Caps:
    def __init__(self):
        self.path = Path("/unused-fixture-path")
        self.calls = []
    def reserve(self, key, limit):
        self.calls.append((key, limit))
        return self.calls.count((key, limit)) <= limit


class SharedMarketTest(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("trial_market_search.search_marketplaces", {"sources": {}, "fresh_requests": 0}),
            ("trial_photo_price.rough_estimate", {"photo_status": "no_photo", "model_calls": 0}),
        ):
            mocked = patch(name, new_callable=AsyncMock)
            mocked.start().return_value = value
            self.addCleanup(mocked.stop)

    def test_projection_keeps_only_fields_and_uses_printed_condition(self):
        item = listing(100, 15)
        item.raw_data["user"]["login"] = "private"
        item.raw_data["description"] = "private words"
        result = pricing.listing_fields(item)
        assert result["seller_id"] == "15" and result["condition"] == "Very good"
        assert result["ask_pence"] == 600 and result["url"] == "https://www.vinted.co.uk/items/100"
        assert "private" not in json.dumps(result)
        item.raw_data["user"] = {"login": "private"}
        item.raw_data["item_box"]["second_line"] = "S"
        assert pricing.listing_fields(item)["condition"] == ""
        assert pricing.listing_fields(item)["seller_id"] == ""


    def test_uses_observed_sellers_and_only_submits_eligible_model_comparables(self):
        async def replay():
            with tempfile.TemporaryDirectory() as temp:
                database = Path(temp) / "original.db"
                conn = sqlite3.connect(database)
                conn.executescript("CREATE TABLE alert_outbox(item_id TEXT PRIMARY KEY,title,url,found_at,platform,status,currency);"
                                   "CREATE TABLE vinted_alert_details(item_id TEXT PRIMARY KEY,payload);")
                for identity, seller, ask in [(100, 200, 6), (101, 201, 4), (102, 202, 6), (103, 200, 2)]:
                    row = pricing.listing_fields(listing(identity, seller, ask))
                    conn.execute("INSERT INTO alert_outbox VALUES(?,?,?,?,?,?,?)",
                                 (str(identity), row["title"], row["url"], time.time(), "vinted", "sent", "GBP"))
                    conn.execute("INSERT INTO vinted_alert_details VALUES(?,?)",
                                 (str(identity), json.dumps({"trial_listing": row})))
                conn.commit()
                conn.close()
                def model(request):
                    assert request.url.host == "api.mistral.ai"
                    messages = json.loads(request.content)["messages"]
                    payload = json.loads(messages[1]["content"])
                    assert len(payload["candidates"]) == 2
                    for target in payload["candidates"] + [payload["target"]]:
                        assert not ({"seller_id", "url", "ask_pence", "id", "photos"} & target.keys())
                    return httpx.Response(200, json={"choices":[{"message":{"content":'{"matches":[0]}'}}]})
                caps = Caps()
                with patch.dict(os.environ, {"MISTRAL_API_KEY":"fixture"}, clear=True), \
                     patch("trial_public_pool.candidates", return_value=[]), \
                     patch("trial_vision_budget.reserve", return_value=42) as reserve, \
                     patch("trial_vision_budget.record_usage"):

                    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as client:
                        got = await pricing.evaluate("100", database, client, caps)
                assert got["baseline_pence"] == 350
                assert got["resale_pence"] is None and got["model_status"] == "ai_reviewed"
                assert got["market_calls"] == 0 and got["model_calls"] == 1 and reserve.call_count == 1
        asyncio.run(replay())


    def test_original_exhausted_allowance_never_triggers_market_call(self):
        async def replay():
            with tempfile.TemporaryDirectory() as temp:
                cache = Path(temp) / "resale-pilot.sqlite3"
                with market_cache(cache) as conn, conn:
                    conn.execute("INSERT INTO pilot_search_budget VALUES(1,50)")
                caps = Caps()
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: 1 / 0)) as client:
                    target = pricing.listing_fields(listing(100, 200))
                    with patch.dict(os.environ, {"SCAVIO_API_KEY":"fixture", "FINDER_RESALE_CACHE":str(cache)}, clear=True):
                        offers, status, calls = await pricing._get_comps(cache.with_name("original.db"), target, [], client, caps)
                assert not offers and not calls and not caps.calls and status == "original_allowance_unavailable"
        asyncio.run(replay())


    def test_legacy_cache_cannot_fabricate_active_status_and_new_rows_cannot_cross_markets(self):
        raw = {"id": 101, "title": "Hollister argyle cropped hoodie", "brand": "Hollister", "size": "S",
               "seller": {"id": 201}, "price": 4, "condition": "Very good"}
        assert pricing._checked_market(dict(raw, can_be_bought=False), "GBP") is None
        assert pricing._checked_market(dict(raw, status="Withdrawn"), "GBP") is None
        assert pricing._checked_market(dict(raw, url="https://www.vinted.fr/items/101"), "GBP") is None
        checked = pricing._checked_market(raw, "GBP")
        legacy = dict(checked, id="102")
        del legacy["checked_active_uk_url"]
        target = pricing.listing_fields(listing(100, 200))
        with tempfile.TemporaryDirectory() as tmp, market_cache(Path(tmp) / "cache") as conn:
            conn.execute("INSERT INTO market VALUES(?,?,?)",
                         (json.dumps(["hollister", "hoodie"]), time.time(), json.dumps([legacy, checked])))
            got = pricing._market_rows(conn, target, time.time())
        assert [r["id"] for r in got] == ["101"]

    def test_missing_seller_gives_no_estimate(self):
        async def replay():
            target = pricing.listing_fields(listing(100, 200))
            target["seller_id"] = ""
            matches = [pricing.listing_fields(listing(101, 201, 4)), pricing.listing_fields(listing(102, 202, 6))]
            with patch.object(pricing, "_live_rows", return_value=(target, matches)):
                got = await pricing.evaluate("100", "original.db", None, Caps())
            assert got["resale_pence"] is None and not got["had_target_seller"]
            assert got["market_calls"] == got["model_calls"] == 0
        asyncio.run(replay())


    def test_latest_withdrawal_overrides_older_independent_asking_price(self):
        async def replay():
            target = pricing.listing_fields(listing(100, 200))
            first = pricing.listing_fields(listing(101, 201, 4))
            second = pricing.listing_fields(listing(102, 202, 6))
            with patch.dict(os.environ, {}, clear=True), \
                 patch.object(pricing, "_live_rows", return_value=(target, [first, second])), \
                 patch("trial_public_pool.candidates", return_value=[]), \
                 patch("trial_public_pool.latest_unavailable", return_value={"101": time.time()}):
                got = await pricing.evaluate("100", "/missing/original.db", None, Caps())
            assert got["resale_pence"] is None
            assert all(c["id"] != "101" for c in got["evidence"])
        asyncio.run(replay())
