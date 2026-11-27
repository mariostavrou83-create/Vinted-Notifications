import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from lxml import html

import trial_market_search as market


class MarketSearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "finder.db")
        self.target = {
            "id": "101",
            "seller_id": "40",
            "brand": "Jane Norman",
            "title": "Jane Norman fur collared coat",
            "active": True,
            "country": "UK",
            "currency": "GBP",
            "size": "S / UK 8-10",
            "condition": "Very good",
        }
        self.counter = []
        self.date = datetime.now(UTC).strftime("%d %b %Y")

    def link(self, source):
        if source == "vinted_uk":
            return "https://www.vinted.co.uk/items/902-jane-norman-fur-collared-coat"
        if source == "depop_uk":
            return "https://www.depop.com/products/user-jane-norman-fur-collared-coat/"
        return "https://www.ebay.co.uk/itm/" + (
            "123459123459" if source.endswith("sold") else "123458123458"
        )

    def product(self, source):
        link = self.link(source)
        brand = self.target["brand"]
        profile = {
            "vinted_uk": "https://www.vinted.co.uk/member/900-publicseller",
            "depop_uk": "https://www.depop.com/publicseller/",
        }.get(source, "https://www.ebay.co.uk/usr/publicseller")
        return {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": self.target["title"],
            "url": link,
            "brand": {"name": brand},
            "size": "S / UK 8-10",
            "additionalProperty": [{"name": "condition", "value": "Very good"}],
            "offers": {
                "@type": "Offer",
                "price": "10.00",
                "priceCurrency": "GBP",
                "availability": "https://schema.org/"
                + ("SoldOut" if source.endswith("sold") else "InStock"),
                "seller": {
                    "name": "publicseller",
                    "url": profile,
                    "address": {"addressCountry": "GB"},
                },
            },
        }

    def page(self, product):
        return '<script type="application/ld+json">' + json.dumps(product) + "</script>"

    def response(self, request):
        self.counter.append(request)
        path = request.url.path
        if "vinted" in request.url.host:
            src = "vinted_uk"
        elif "depop" in request.url.host:
            src = "depop_uk"
        else:
            src = (
                "ebay_uk_sold"
                if "LH_Sold" in request.url.params or path.endswith("123459")
                else "ebay_uk_active"
            )
        if path in {"/catalog", "/gb/search/", "/sch/i.html"}:
            text = f"Sold {self.date} · Sold for £7.50" if src == "ebay_uk_sold" else ""
            body = f'<ul><li><a href="{self.link(src)}">{self.target["title"]}</a>{text}</li></ul>'
        else:
            body = self.page(self.product(src))
        return httpx.Response(200, text=body, headers={"Content-Type": "text/html"})

    async def test_normal_listing_links_in_all_four_and_cache_reused(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(self.response),
            headers={"Authorization": "should-not-leak"},
            cookies={"secret": "no"},
        ) as client:
            data = await market.search_marketplaces(self.target, self.db, client)
            self.assertEqual(data["fresh_requests"], 8)
            self.assertTrue(
                all(
                    "authorization" not in req.headers and "cookie" not in req.headers
                    for req in self.counter
                )
            )
            self.assertEqual(set(data["sources"]), set(market.SITES))
            for src, entry in data["sources"].items():
                self.assertEqual(entry["status"], "checked", (src, entry))
                row = entry["rows"][0]
                self.assertEqual(row["public_seller_handle"], "publicseller")
                if src.endswith("sold"):
                    self.assertEqual(
                        (
                            row["sold_pence"],
                            row["sold_amount_confirmed"],
                            row["active"],
                        ),
                        (750, True, False),
                    )
                    self.assertNotIn("ask_pence", row)
                else:
                    self.assertEqual(row["ask_pence"], 1000)
                self.assertEqual(row["url"], self.link(src))
            other = await market.search_marketplaces(
                dict(self.target, condition="Good"), self.db, client
            )
            self.assertEqual(other["fresh_requests"], 0)
            self.assertFalse(any(s["rows"] for s in other["sources"].values()))

    async def test_host_block_cooldown_and_limited_http_budget(self):
        def forbidden(request):
            return httpx.Response(429)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(forbidden)
        ) as client:
            data = await market.search_marketplaces(self.target, self.db, client)
            self.assertTrue(
                all(s["status"] == "blocked" for s in data["sources"].values())
            )
            again = await market.search_marketplaces(
                dict(self.target, brand="Other"), self.db, client
            )
            self.assertEqual(again["fresh_requests"], 0)
        with TemporaryDirectory() as tmp, patch.object(market, "MAX_REQUESTS", 1):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(forbidden)
            ) as client:
                data = await market.search_marketplaces(
                    self.target, str(Path(tmp) / "app"), client
                )
                self.assertEqual(data["fresh_requests"], 1)

    async def test_incomplete_target_has_links_without_network_spend(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(self.response)
        ) as client:
            data = await market.search_marketplaces(
                dict(self.target, condition=""), self.db, client
            )
        self.assertEqual(data["fresh_requests"], 0)
        self.assertTrue(
            all(
                s["status"] == "insufficient_target" and s["url"].startswith("https:")
                for s in data["sources"].values()
            )
        )

    def test_invalid_prices_seller_inactive_or_country_are_excluded(self):
        src = "depop_uk"

        def read(p):
            return market._detail(
                html.fromstring(self.page(p)),
                src,
                market._canonical(src, self.link(src), self.link(src)),
                None,
            )

        for change in (
            {"priceCurrency": "USD"},
            {"price": "1.123"},
            {"availability": "https://schema.org/OutOfStock"},
            {"seller": {"name": "publicseller", "address": {"addressCountry": "US"}}},
            {"seller": {"name": "publicseller"}},
        ):
            p = self.product(src)
            p["offers"].update(change)
            self.assertIsNone(read(p))
        p = self.product(src)
        p["url"] = "https://www.depop.com/products/other-cardigan/"
        self.assertIsNone(read(p))
        p = self.product(src)
        p["additionalProperty"][0]["value"] = "used"
        self.assertIsNone(read(p))

    def test_sold_pending_best_offer_and_different_shapes_are_excluded(self):
        examples = [
            f"Sold {self.date} · £9",
            f"Sold {self.date} · Sold for £9 · Best offer accepted",
            f"Sold {self.date} · sold for £9 or best offer",
            "Sold 01 Jan 2020 · Winning bid £9",
            "3 sold; £9",
        ]
        self.assertTrue(all(market._sold_label(e) is None for e in examples))
        self.assertEqual(
            market._sold_label(f"Sold {self.date} · Winning bid £9.40")[0], 940
        )
        row = dict(
            self.target, source="vinted_uk", id="202", seller_id="99", ask_pence=800
        )
        for change in (
            {"title": "Jane Norman fur hooded coat"},
            {"size": "M"},
            {"seller_id": "40"},
            {"condition": "Good"},
            {"brand": "Other"},
            {"id": "101"},
        ):
            self.assertFalse(market._match(self.target, dict(row, **change)))
        self.assertTrue(market._match(self.target, row))

    def test_query_uses_photo_construction_without_internal_size_or_third_trait(self):
        target = dict(
            self.target, visible_traits=["cropped", "puffer", "arbitrary URL"]
        )
        self.assertEqual(market._query(target), "jane norman coat puffer fur collar S")
        self.assertIn("/gb/search/?", market.search_links(target)["depop_uk"])
        self.assertTrue(market._query(dict(target, size="UK 10")).endswith("UK 10"))
        self.assertNotIn("8 10", market._query(target))

    def test_unclassified_product_search_keeps_real_title_and_specific_variant(self):
        target = {
            "brand": "Sony",
            "title": "Sony Cyber-shot DSC RX100 IV compact camera",
            "size": "medium",
        }
        self.assertEqual(
            market._query(target), "sony cyber shot dsc rx100 iv compact camera"
        )

    def test_only_ordinary_exact_hosts(self):
        base = self.link("depop_uk")
        for href in (
            "https://example.com/products/x-wrong",
            "https://www.depop.com.evil.co/products/x-wrong",
            "https://www.depop.com:444/products/x-wrong",
            "/products/../../login",
            "http://www.depop.com/products/xx/",
        ):
            self.assertIsNone(market._canonical("depop_uk", href, base))
        self.assertNotIn("?", market._canonical("depop_uk", base + "?track=1", base)[1])
