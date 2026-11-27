import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from test_resale_pilot import alert, market

import resale_pilot as pilot
import resale_pricing as pricing
import resale_sampler as sampler


class ItemReview(unittest.IsolatedAsyncioTestCase):
    def test_style_query_and_shortlist_exclude_unrelated_garments(self):
        now = time.time()
        top = dict(alert(), title="Black strapless top")
        self.assertIn("strapless", pricing.search_key(top))
        choices = [
            market("1", title="Black bandeau top"),
            market("2", title="Black T shirt"),
            market("3", title="Black strapless dress"),
            market("4", title="Plain black top"),
        ]
        self.assertEqual([c["id"] for c in pricing.shortlist(top, choices, now)], ["1"])

        hoodie = dict(alert(), title="Brown knitted zip-up hoodie")
        self.assertEqual(pricing.search_key(hoodie)[2:], ("full zip", "knit"))
        choices = [
            market("1", title="Brown full zip cable knit hooded cardigan"),
            market("2", title="Brown fleece zip through hoodie"),
            market("3", title="Brown knit quarter zip hoodie"),
            market("4", title="Brown knit hooded sweater"),
            market("5", title="Brown cotton crewneck jumper"),
        ]
        viable = pricing.shortlist(hoodie, choices, now)
        self.assertEqual(
            [(c["id"], c["strong"]) for c in viable], [("1", True), ("4", False)]
        )

    def test_recent_asks_and_size_override_stale_asking_prices(self):
        now = time.time()
        a = alert()
        rows = [market("1", 2000), market("2", 2100), market("3", 10500, size="UK 24")]
        rows.extend(
            market(str(i), 9000, first_seen=now - 18 * 86400) for i in range(4, 8)
        )
        candidates = pricing.shortlist(a, rows, now)
        result = pricing.estimate(a, candidates)
        self.assertEqual(result["comparisons"], 2)
        self.assertEqual(result["resale_pence"], 1900)
        self.assertIn("2 close sellers", pricing.valuation_text(result))

    async def test_same_alert_uses_new_market_rows_and_each_item_gets_one_review(self):
        with tempfile.TemporaryDirectory() as tmp, pilot.market_cache(
            Path(tmp) / "cache"
        ) as db:
            finder = Path(tmp) / "finder"
            with sqlite3.connect(finder) as live:
                live.execute("CREATE TABLE alert_outbox(item_id)")
                live.execute("INSERT INTO alert_outbox VALUES('100')")
            calls = {"market": 0, "model": 0}

            async def transport(request):
                body = json.loads(request.content)
                if request.url.host == "api.scavio.dev":
                    calls["market"] += 1
                    self.assertIn("strapless", body["query"])
                    self.assertEqual(body["sort"], "relevance")
                    await asyncio.sleep(0.02)
                    rows = [
                        {
                            "id": i,
                            "title": "Black bandeau top",
                            "brand": "Example maker",
                            "price": "30.00" if i != 100 else "2.00",
                            "seller": {"id": i},
                        }
                        for i in (100, 201, 202, 203)
                    ]
                    return httpx.Response(
                        200,
                        json={
                            "data": {"country": "uk", "currency": "GBP", "items": rows}
                        },
                    )
                calls["model"] += 1
                self.assertEqual(body["response_format"]["type"], "json_schema")
                garment = json.loads(body["messages"][1]["content"][0]["text"])
                self.assertEqual(len(garment["alternatives"]), 3)
                selection = json.dumps(
                    {"matches": [0, 1, 2], "approximate": [], "rough_gbp": None}
                )
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "content": [{"type": "text", "text": selection}]
                                },
                            }
                        ]
                    },
                )

            sampler.sample_table(db)
            receipts = asyncio.Queue()
            for identity in ("100", "101", "101"):
                receipts.put_nowait((identity, None, time.monotonic()))

            def export(_db, _limit, identity):
                return [
                    dict(
                        alert(identity, cost=1000 if identity == "100" else 2000),
                        title="Black strapless top",
                    )
                ]

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(transport)
            ) as client:
                lookups = sampler.MarketLookups(db, client, "test")
                with patch.object(sampler, "export_alerts", side_effect=export):
                    workers = [
                        asyncio.create_task(
                            sampler.sample_loop(
                                receipts,
                                finder,
                                db,
                                client,
                                "test",
                                lookups,
                                continuous=True,
                            )
                        )
                        for _ in range(2)
                    ]
                    try:
                        await asyncio.wait_for(receipts.join(), 3)
                    finally:
                        for task in workers:
                            task.cancel()
                        await asyncio.gather(*workers, return_exceptions=True)
            records = db.execute(
                "SELECT reviewed FROM resale_sample ORDER BY item_id"
            ).fetchall()
            results = [json.loads(r[0]) for r in records]
            self.assertEqual([r["profit_pence"] for r in results], [1850, 850])
            self.assertTrue(all(r["comparisons"] == 3 for r in results))
            self.assertEqual(calls, {"market": 1, "model": 2})

    async def test_model_failure_identifies_truncated_reply_without_logging_the_body(
        self,
    ):
        transport = httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": "PRIVATE TEXT"},
                        }
                    ]
                },
            )
        )
        with tempfile.TemporaryDirectory() as tmp, pilot.market_cache(
            Path(tmp) / "cache"
        ) as db:
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertLogs("resale_pilot") as logs:
                    _, result, path = await pilot.evaluate_one(
                        alert(), [], db, client, "test"
                    )
            self.assertEqual(path, "model_unavailable")
            self.assertIsNone(result["resale_pence"])
            self.assertIn("reason=truncated", logs.output[0])
            self.assertNotIn("PRIVATE TEXT", repr(logs.output))
