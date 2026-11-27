import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from test_resale_pilot import alert

import photo_cards
import resale_pilot as pilot
import resale_pricing as pricing
import resale_sampler as sampler
import resale_telegram as delivery
import telegram_shared_buy as shared_buy


class PriceCoverage(unittest.IsolatedAsyncioTestCase):
    async def test_one_model_review_prices_a_cold_item_without_seeing_the_cheap_ask(
        self,
    ):
        seen = []

        def model(request):
            payload = json.loads(request.content)["messages"][1]["content"][0]["text"]
            seen.append(json.loads(payload))
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": '{"matches":[],"approximate":[],"rough_gbp":34}'
                            }
                        }
                    ]
                },
            )

        with tempfile.TemporaryDirectory() as tmp, pilot.market_cache(
            Path(tmp) / "cache"
        ) as conn:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(model)
            ) as client:
                for cost, decision in [(1899, "Yes"), (1900, "No")]:
                    _, review, _ = await pilot.evaluate_one(
                        alert(cost=cost), [], conn, client, "test"
                    )
                    self.assertEqual(review["profit_pence"], 3400 - cost)
                    text = pricing.valuation_text(review)
                    self.assertIn("Buy: " + decision + "\nConf: ⬇️ Low · AI only", text)
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0]["alternatives"], [])
            self.assertNotIn("total_pence", seen[0]["garment"])

    async def test_provider_limit_does_not_substitute_a_category_price_or_repeat_calls(
        self,
    ):
        calls = []

        def limited(req):
            calls.append(req)
            return httpx.Response(402)

        with tempfile.TemporaryDirectory() as tmp, pilot.market_cache(
            Path(tmp) / "cache"
        ) as conn:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(limited)
            ) as client:
                for identity in ("1", "2"):
                    _, review, _ = await pilot.evaluate_one(
                        alert(identity), [], conn, client, "test"
                    )
                    self.assertEqual(
                        (review["resale_pence"], review["basis"]),
                        (None, "insufficient"),
                    )
                _, empty, _ = await pilot.evaluate_one(
                    dict(alert("3"), title="Item"), [], conn, client, "test"
                )
                self.assertIn("Est. resell: Unknown", pricing.valuation_text(empty))
            self.assertEqual(len(calls), 1)

    async def test_continuing_past_the_trial_is_idempotent_and_keeps_the_trial_report(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp, pilot.market_cache(
            Path(tmp) / "cache"
        ) as conn:
            sampler.sample_table(conn)
            receipts = asyncio.Queue()
            for identity in ("1", "2", "2"):
                receipts.put_nowait((identity, None, time.monotonic()))

            def export(db, limit, identity):
                return [
                    dict(
                        alert(identity), source="search" if identity == "2" else "home"
                    )
                ]

            with patch.object(sampler, "LIMIT", 1), patch.object(
                sampler, "export_alerts", side_effect=export
            ):
                worker = asyncio.create_task(
                    sampler.sample_loop(
                        receipts,
                        "",
                        conn,
                        None,
                        "",
                        continuous=True,
                    )
                )
                try:
                    await asyncio.wait_for(receipts.join(), 3)
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM resale_sample").fetchone()[0], 2
            )
            (report,) = conn.execute(
                "SELECT summary FROM resale_trial_report"
            ).fetchone()
            self.assertEqual(json.loads(report)["unique_alerts"], 1)


class AlertLayout(unittest.IsolatedAsyncioTestCase):
    def test_footer_and_progress_order_on_home_and_saved_search_and_group(self):
        row = {
            "item_id": "123",
            "query_id": 1,
            "search_name": "Mesh",
            "title": "Mesh top",
            "price": "4",
            "currency": "GBP",
            "url": "https://example.test",
        }
        details = {
            "brand": "A & B",
            "budget": {"total": 800},
            "name": "Mesh",
            "guide": "Prefer a zip fastening",
            "reminder": "Check seams",
            "buying_advice": "Fixture budget £7.",
            "buy_feedback": {"state": "in_progress", "message": "Checking pickup"},
        }
        with patch.dict(
            os.environ, {"FINDER_RESALE_PILOT": "1", "FINDER_RESALE_SHADOW_ONLY": "0"}
        ), patch.object(delivery, "reserving", return_value=True):
            for saved in (True, False):
                r, d = dict(row), dict(details)
                if not saved:
                    r["query_id"] = None
                    d.update(
                        home_discovery={
                            "reason": "recommendation",
                            "source_role": "seller",
                        },
                        guide="",
                        reminder="",
                        buying_advice="",
                    )
                base = photo_cards.captions(r, d)[0]
                labels = [
                    "Title: <b>Mesh top</b>",
                    "Brand: A &amp; B",
                    "Total: <b>£8.00</b>",
                    "Est. profit: <b>Pending</b>",
                    "Est. resell: Checking this item…",
                    "Buy: <b>Pending</b>",
                    "Conf: Pending",
                    "<b>Autobuy progress</b>",
                ]
                positions = [
                    base.index(label, base.index("Title: <b>Mesh top</b>"))
                    for label in labels
                ]
                self.assertEqual(positions, sorted(positions))
                self.assertEqual(
                    delivery.initial_price(SimpleNamespace(caption_html=base)),
                    "pending",
                )
                for hidden in (
                    "Fixture budget",
                    "Check seams",
                    "Prefer a zip fastening",
                    "Listing age verified",
                    "personalised Vinted Home feed",
                ):
                    self.assertNotIn(hidden, base)
                changed = pricing.valuation_text(
                    pricing.rough_estimate({"total_pence": 800}, 3000, "ai")
                )
                edited = delivery.append_line(base, changed)
                self.assertEqual(edited.count("Est. resell:"), 1)
                self.assertIn("Est. profit: <b>£22.00</b>", edited)
                self.assertLess(edited.index("Conf:"), edited.index("Autobuy progress"))
                before, _, status = base.rpartition("\n\n")
                self.assertEqual(shared_buy.insert_status(before, status), base)

    def test_long_shared_caption_makes_space_for_progress_beneath_valuation(self):
        footer = "Title: <b>Jacket</b>\nBrand: Sample\nTotal: £6.00\nEst. profit: £2.00\nEst. resell: £8.00\nBuy: No\nConf: ⬇️ Low"
        body = (
            "Heading\n\n📄 <b>Seller description</b>\n"
            + "&lt;Blue&gt; 🧵 " * 110
            + "\n\n"
            + footer
        )
        status = "<b>Autobuy progress</b>\nChecking pickup"
        revised = shared_buy.insert_status(body, status, 1024)
        self.assertLessEqual(photo_cards.units(photo_cards.plain(revised)), 1024)
        self.assertTrue(revised.endswith(footer + "\n\n" + status))
        self.assertIn("&lt;Blue&gt;", revised)

    def test_long_saved_guide_never_reappears_in_full_description_or_buttons(self):
        row = {
            "item_id": "1",
            "query_id": 1,
            "search_name": "Mesh",
            "title": "Mesh top",
            "price": "4",
            "currency": "GBP",
            "url": "https://example.test",
        }
        details = {
            "brand": "Maker",
            "description": "A long description. " * 140,
            "buying_advice": "Fixture budget £7.",
            "guide": "Stop for missing details.",
            "reminder": "Check the blue stitching.",
        }
        caption, pages = photo_cards.captions(row, details)
        self.assertTrue(pages)
        views = [
            caption,
            *(photo_cards.notes_caption(details, i, pages) for i in range(len(pages))),
        ]
        for view in views:
            self.assertNotIn("Fixture budget", view)
            self.assertNotIn("Stop for missing", view)
            self.assertNotIn("blue stitching", view)
            self.assertNotIn("guide and reminder", view)
        labels = [
            b.text
            for line in photo_cards.markup(row, details).inline_keyboard
            for b in line
        ]
        self.assertIn("Full description", labels)
        self.assertFalse(
            any(
                "guide" in name.lower() or "reminder" in name.lower() for name in labels
            )
        )
