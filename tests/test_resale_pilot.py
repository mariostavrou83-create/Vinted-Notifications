import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import resale_pilot as pilot
import resale_sampler as sampler
from resale_pricing import (
    MAX_MARKET_AGE,
    alert_line,
    estimate,
    market_key,
    market_listing,
    shortlist,
)


def alert(identity="101", cost=1000):
    return {
        "id": identity,
        "title": "Waxed fitted field jacket",
        "brand": "Example maker",
        "size": "UK 8",
        "condition": "Very good",
        "total_pence": cost,
        "source": "home",
    }


def market(identity, price=2500, **changes):
    return dict(
        alert(identity),
        **{"price": price, "seller": identity, "listed": None, **changes}
    )


class ResaleRules(unittest.TestCase):
    def test_strict_profit_boundary_and_shipping(self):
        rows = [market(str(i), 3000) for i in range(4)]
        candidates = shortlist(alert(), rows, time.time())
        for cost, gain, verdict in [
            (1349, 1501, "Yes"),
            (1350, 1500, "No"),
            (1900, 950, "No"),
            (2900, -50, "No"),
        ]:
            with self.subTest(cost=cost):
                result = estimate(alert(cost=cost), candidates, verified=[0, 1, 2, 3])
                self.assertEqual(
                    (result["profit_pence"], result["verdict"]), (gain, verdict)
                )
                self.assertIn("£28.50", alert_line(result))
        self.assertIn(
            "est. profit £9.50",
            alert_line(estimate(alert(cost=1900), candidates, verified=[0, 1, 2])),
        )

    def test_rough_and_unknown_are_different(self):
        candidates = shortlist(alert(), [market(str(i)) for i in range(5)], time.time())
        rough = estimate(alert(), candidates, verified=[], approximate=[0, 1, 2, 3])
        self.assertEqual(rough["verdict"], "Not confident")
        self.assertIn("likely No", alert_line(rough))
        self.assertIsNotNone(rough["resale_pence"])
        unknown = estimate(alert(), candidates, verified=[], approximate=[])
        self.assertIsNone(unknown["profit_pence"])
        self.assertIn("Not sure", alert_line(unknown))
        self.assertNotIn("£", alert_line(unknown))
        no_total = estimate(alert(cost=None), candidates, verified=[0, 1, 2])
        self.assertEqual(no_total["verdict"], "Not confident")
        self.assertIn("all-in cost unknown", alert_line(no_total))

    def test_disagreement_or_single_seller_cannot_be_confident(self):
        unalike = shortlist(
            alert(),
            [market(str(i), price) for i, price in enumerate([1000, 5000, 11000])],
            time.time(),
        )
        self.assertEqual(
            estimate(alert(), unalike, verified=[0, 1, 2])["verdict"], "Not confident"
        )
        one_seller = shortlist(
            alert(), [market(str(i), seller="one") for i in range(5)], time.time()
        )
        self.assertEqual(
            estimate(alert(), one_seller, verified=[0, 1, 2])["comparisons"], 1
        )

    def test_market_data_excludes_self_and_stale_or_unrelated_goods(self):
        now = time.time()
        target = dict(alert(), title="Genuine leather fitted jacket")
        rows = [
            market("101"),
            market("2", title="Faux leather biker jacket"),
            market("3", title="Kids leather jacket"),
            market("4", listed=now - 31 * 86400),
            market("5", brand="Other"),
            market("6", title="Leather trousers"),
            market("7", title="Genuine leather jacket"),
        ]
        self.assertEqual([c["id"] for c in shortlist(target, rows, now)], ["7"])

    def test_uses_asking_price_only(self):
        raw = {
            "id": 4,
            "title": "Jacket",
            "brand": {"title": "Example maker"},
            "price": "40.00",
            "price_with_discount": "38.00",
            "total_price": 46,
            "currency": "GBP",
            "seller": {"id": 2},
        }
        self.assertEqual(market_listing(raw, "GBP")["price"], 3800)
        self.assertIsNone(market_listing(dict(raw, is_sold=True), "GBP"))
        self.assertIsNone(market_listing(dict(raw, currency="EUR"), "GBP"))

    def test_stale_asks_can_anchor_a_rough_price_without_supporting_a_confident_yes(
        self,
    ):
        now = time.time()
        rows = [market(str(i), 4000, first_seen=now - 17 * 86400) for i in range(3)]
        candidates = shortlist(alert(), rows, now)
        self.assertIn(
            "at least 17 days", pilot.public_fields(candidates[0])["listing_age"]
        )
        review = estimate(alert(), candidates, verified=[0, 1, 2])
        self.assertEqual(
            (review["verdict"], review["profit_pence"]), ("Not confident", 2800)
        )

    def test_export_is_read_only_and_keeps_only_public_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "alerts.sqlite"
            with sqlite3.connect(path) as db:
                db.executescript(
                    "CREATE TABLE alert_outbox(item_id,title,sent_at,platform,status,currency);"
                    "CREATE TABLE vinted_alert_details(item_id,payload);"
                )
                for i, role in enumerate(["seller", "buyer", None]):
                    db.execute(
                        "INSERT INTO alert_outbox VALUES(?,?,?,?,?,?)",
                        (str(i), "Jacket", 10 + i, "vinted", "sent", "GBP"),
                    )
                    payload = {
                        "brand": "Maker",
                        "budget": {"total": 1800},
                        "ignored_field": "private",
                    }
                    if role:
                        payload["home_discovery"] = {
                            "source_role": role,
                            "size_title": "UK 8",
                        }
                    db.execute(
                        "INSERT INTO vinted_alert_details VALUES(?,?)",
                        (str(i), json.dumps(payload)),
                    )
            items = pilot.export_alerts(path, 100)
            self.assertEqual([r["source"] for r in items], ["search", "home"])
            self.assertNotIn("private", json.dumps(items))
            with self.assertRaises(sqlite3.OperationalError):
                pilot.export_alerts(Path(tmp) / "absent.sqlite", 100)


class ReviewFlow(unittest.IsolatedAsyncioTestCase):
    async def test_shadow_uses_owner_receipt_only_and_group_does_not_consume_another_trial_slot(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp, pilot.market_cache(
            Path(tmp) / "trial"
        ) as cache:
            live = Path(tmp) / "finder"
            with sqlite3.connect(live) as source:
                source.executescript(
                    "CREATE TABLE alert_outbox(item_id,title,sent_at,platform,status,currency);"
                    "CREATE TABLE vinted_alert_details(item_id,payload);"
                )
                source.execute(
                    "INSERT INTO alert_outbox VALUES('101','Waxed fitted field jacket',1,'vinted','sent','GBP')"
                )
                payload = {
                    "brand": "Example maker",
                    "budget": {"total": 1900},
                    "home_discovery": {"source_role": "seller"},
                }
                source.execute(
                    "INSERT INTO vinted_alert_details VALUES('101',?)",
                    (json.dumps(payload),),
                )
            sampler.sample_table(cache)
            cache.execute(
                "INSERT INTO market VALUES(?,?,?)",
                (
                    pilot.encoded(market_key(alert())),
                    time.time(),
                    pilot.encoded(
                        [
                            market("101", 200),
                            market("2", 3000),
                            market("3", 3000),
                            market("4", 3000),
                        ]
                    ),
                ),
            )
            cache.commit()

            def model(request):
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "content": '{"matches":[0,1,2],"approximate":[]}'
                                }
                            }
                        ]
                    },
                )

            receipts = asyncio.Queue(2)
            receipts.put_nowait(("101", None, time.monotonic()))
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(model)
            ) as client:
                with patch.object(sampler, "LIMIT", 1):
                    await sampler.sample_loop(
                        receipts, live, cache, client, "test", [], asyncio.Event()
                    )
                    self.assertFalse(sampler.reserve_item(cache, "101", "home"))
            summary = sampler.sample_summary(cache)
            self.assertEqual(
                (summary["ai_reviewed"], summary["unique_alerts"], summary["home"]),
                (1, 1, 1),
            )
            self.assertEqual(summary["verdicts"], {"No": 1})
            self.assertIn(
                "£9.50",
                cache.execute("SELECT display FROM resale_sample").fetchone()[0],
            )

    async def test_no_pilot_without_both_provider_settings(self):
        with patch.dict(
            "os.environ",
            {"FINDER_RESALE_PILOT": "1", "SCAVIO_API_KEY": "", "MISTRAL_API_KEY": ""},
        ):
            await sampler.run("/a/missing/database")
            self.assertFalse(sampler.active())

    async def test_paid_review_is_shared_filters_supplied_indices_and_hides_prices(
        self,
    ):
        calls = []

        def transport(request):
            calls.append(request)
            body = json.loads(request.content)
            self.assertNotIn("price", body["messages"][1]["content"][0]["text"])
            self.assertNotIn("seller", body["messages"][1]["content"][0]["text"])
            self.assertEqual(request.url.host, "api.mistral.ai")
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": '{"matches":[],"approximate":[0]}'}}
                    ]
                },
            )

        with tempfile.TemporaryDirectory() as tmp, pilot.market_cache(
            Path(tmp) / "cache"
        ) as db:
            rows = shortlist(alert(), [market("2"), market("3")], time.time())
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(transport)
            ) as client:
                first = await pilot.evaluate_one(
                    alert(), rows, db, client, "test", None
                )
                second = await pilot.evaluate_one(
                    alert(), rows, db, client, "test", None
                )
            self.assertEqual(first[1], second[1])
            self.assertEqual(
                (first[2], second[2], len(calls)), ("model", "cached_model", 1)
            )
            self.assertEqual(first[1]["verdict"], "Not confident")

    async def test_timeout_and_invalid_model_output_never_publish_a_guessed_price(self):
        async def slow(request):
            await asyncio.sleep(0.1)

        for response in (
            slow,
            lambda req: httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": '{"matches":[99],"approximate":[]}'}}
                    ]
                },
            ),
        ):
            with tempfile.TemporaryDirectory() as tmp, pilot.market_cache(
                Path(tmp) / "cache"
            ) as db, patch.object(pilot, "MODEL_SECONDS", 0.02):
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(response)
                ) as client:
                    _, reviewed, source = await pilot.evaluate_one(
                        alert(),
                        shortlist(alert(), [market("2")], time.time()),
                        db,
                        client,
                        "test",
                        None,
                    )
                    self.assertEqual(source, "model_unavailable")
                    self.assertIsNone(reviewed["resale_pence"])
                    self.assertIn("AI review unavailable", alert_line(reviewed))

    async def test_warm_keeps_unbiased_results_and_conserves_searches(self):
        calls = []

        def transport(request):
            calls.append(request)
            self.assertEqual(request.url.host, "api.scavio.dev")
            self.assertNotIn("price_to", json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "data": {
                        "country": "uk",
                        "currency": "GBP",
                        "items": [
                            {
                                "id": "1",
                                "title": "Waxed jacket",
                                "brand": "Example maker",
                                "price": 26,
                            }
                        ],
                    }
                },
            )

        with tempfile.TemporaryDirectory() as tmp, pilot.market_cache(
            Path(tmp) / "cache"
        ) as db:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(transport)
            ) as client:
                await pilot.warm([alert(), alert("103")], db, client, "test", 1)
                await pilot.warm([alert("104")], db, client, "test", 1)
                self.assertEqual(len(calls), 1)
                self.assertEqual(
                    pilot.cached_listings(db, market_key(alert()), time.time())[0][
                        "price"
                    ],
                    2600,
                )
                self.assertIsNone(
                    pilot.cached_listings(
                        db, market_key(alert()), time.time() + MAX_MARKET_AGE + 1
                    )
                )
                db.executemany(
                    "INSERT INTO searches VALUES(?,?)",
                    (("other", time.time()) for _ in range(9999)),
                )
                db.commit()
                self.assertFalse(
                    pilot.reserve_search(db, ("another", "jacket"), time.time())
                )

    async def test_market_outage_and_empty_market_have_different_meanings(self):
        with tempfile.TemporaryDirectory() as tmp, pilot.market_cache(
            Path(tmp) / "cache"
        ) as db:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(503))
            ) as client:
                self.assertEqual(
                    (await pilot.warm([alert()], db, client, "test", 1, pilot_limit=1))[
                        "market_groups_refreshed"
                    ],
                    0,
                )
                self.assertFalse(
                    pilot.reserve_search(
                        db, ("other", "top"), time.time() + 32 * 86400, pilot_limit=1
                    )
                )
                output = Path(tmp) / "cold.jsonl"
                await pilot.evaluate([alert()], db, client, "test", None, output)
                self.assertIn(
                    "Comparison prices unavailable",
                    json.loads(output.read_text())["display"],
                )
                db.execute(
                    "INSERT INTO market VALUES(?,?,?)",
                    (pilot.encoded(market_key(alert())), time.time(), "[]"),
                )
                output = Path(tmp) / "empty.jsonl"
                await pilot.evaluate([alert()], db, client, "test", None, output)
                self.assertEqual(
                    json.loads(output.read_text())["display"],
                    "Not sure · no reliable resale estimate",
                )

    async def test_100_alert_replay_reports_actual_coverage_and_excludes_other_alerts(
        self,
    ):
        alerts = [
            dict(
                alert(str(i), cost=1900),
                source=("home" if i % 2 else "search"),
                realised_resale_pence=2800,
            )
            for i in range(1000, 1100)
        ]
        market_rows = [market(str(i), 3000) for i in range(1, 5)] + [
            market("1001", 100)
        ]

        def response(request):
            supplied = json.loads(
                json.loads(request.content)["messages"][1]["content"][0]["text"]
            )
            self.assertEqual(len(supplied["alternatives"]), 4)
            payload = {
                "choices": [
                    {"message": {"content": '{"matches":[0,1,2,3],"approximate":[]}'}}
                ]
            }
            return httpx.Response(200, json=payload)

        with tempfile.TemporaryDirectory() as tmp, pilot.market_cache(
            Path(tmp) / "cache"
        ) as db:
            db.execute(
                "INSERT INTO market VALUES(?,?,?)",
                (
                    pilot.encoded(market_key(alert())),
                    time.time(),
                    pilot.encoded(market_rows),
                ),
            )
            db.commit()
            out = Path(tmp) / "report.jsonl"
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(response)
            ) as client:
                summary = await pilot.evaluate(alerts, db, client, "test", None, out)
            self.assertEqual(summary["unique_alerts"], 100)
            self.assertEqual(summary["sources"], {"home": 50, "search": 50})
            self.assertEqual(summary["verdicts"], {"No": 100})
            self.assertEqual(summary["median_absolute_error_pence"], 50)
            self.assertEqual(summary["false_yes"], 0)
            self.assertEqual(len(out.read_text().splitlines()), 100)


if __name__ == "__main__":
    unittest.main()
