"""New shared alerts opt in without changing old searches or payment limits."""

import unittest
from contextlib import closing
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from test_search_controls import DatabaseFixture

import dashboard_store as store
import search_settings
import vinted_budget
import vinted_keywords


class SharedAlertStore(DatabaseFixture, unittest.TestCase):
    def form(self, **changes):
        form = {
            "shared_alert_version": "1",
            "query_name": "Shared jacket",
            "platform_mode": "both",
            "query": "https://www.vinted.co.uk/catalog?brand_ids[]=111&catalog[]=2050&size_ids[]=4&search_text=old&price_from=99&price_to=100",
            "ebay_search_url": "https://www.ebay.co.uk/sch/i.html?_sacat=57988&LH_BIN=1&_nkw=old&_udlo=99&_udhi=100",
            "shared_keywords": "fur, sherpa, fur hood, FUR",
            "vinted_max_total": "20.00",
            "exclusions": "teddy",
            "reminder": "Resale aim £60; check <fur> trim & cuffs",
            "revision": "0",
        }
        return dict(form, **changes)

    def table_rows(self, table):
        with closing(search_settings.connection()) as conn:
            return [tuple(row) for row in conn.execute("SELECT * FROM " + table)]

    def test_shared_creation_keeps_existing_definitions_and_buyer_state(self):
        old = [search_settings.get_search(i) for i in range(1, 45)]
        buyer = self.table_rows("vinted_buyer")
        attempts = self.table_rows("vinted_buy_attempts")
        identity = store.save_search(None, self.form(), photos=[])
        self.assertEqual(old, [search_settings.get_search(i) for i in range(1, 45)])
        self.assertEqual(buyer, self.table_rows("vinted_buyer"))
        self.assertEqual(attempts, self.table_rows("vinted_buy_attempts"))
        search = search_settings.get_search(identity)
        self.assertEqual(search["shared_alert_version"], 1)
        self.assertEqual(search["shared_keywords"], ["fur", "sherpa", "fur hood"])
        self.assertEqual(search["exclusions"], ["teddy"])
        self.assertIn("Resale aim", search["reminder"])
        self.assertIsNone(search["max_buy"])
        self.assertIsNone(search["resale_low"])
        self.assertIsNone(search["resale_high"])
        self.assertEqual(search["must_have"], "")
        params = parse_qs(urlsplit(search["query"]).query)
        self.assertEqual(params["brand_ids[]"], ["111"])
        self.assertEqual(params["size_ids[]"], ["4"])
        self.assertNotIn("search_text", params)
        self.assertNotIn("price_from", params)
        self.assertEqual(params["price_to"], ["20.00"])
        self.assertEqual(search["vinted_postage_estimate"], 220)
        self.assertEqual(
            vinted_budget.purchase_limits({"query_id": identity}).total_maximum, 2000
        )
        self.assertEqual(
            next(r for r in store.list_searches() if r["id"] == identity)[
                "shared_keywords"
            ],
            search["shared_keywords"],
        )

    def test_each_platform_mode_and_ebay_only_keywords_persist(self):
        for mode in ("vinted", "ebay", "both"):
            form = self.form(
                platform_mode=mode,
                query_name=mode,
                vinted_max_total=str(20 + len(mode)),
            )
            identity = store.save_search(None, form)
            search = search_settings.get_search(identity)
            self.assertEqual(search["platform_mode"], mode)
            self.assertEqual(search["shared_keywords"], ["fur", "sherpa", "fur hood"])
            self.assertEqual(len(search["vinted_variants"]), 0 if mode == "ebay" else 3)
            self.assertEqual(bool(search["query"]), mode != "ebay")

    def test_marker_is_persistent_and_cannot_convert_legacy(self):
        store.save_search(
            1,
            self.form(
                query_name="Legacy",
                query="https://www.vinted.co.uk/catalog?search_text=test1",
                platform_mode="vinted",
                vinted_keywords="",
                vinted_postage_estimate="3.50",
            ),
        )
        legacy = search_settings.get_search(1)
        self.assertNotEqual(legacy.get("shared_alert_version"), 1)
        self.assertEqual(legacy["vinted_postage_estimate"], 350)
        identity = store.save_search(None, self.form(platform_mode="vinted"))
        search = search_settings.get_search(identity)
        form = self.form(
            platform_mode="vinted",
            query=search["query"],
            revision=str(search["revision"]),
            vinted_postage_estimate="99",
            max_buy="100",
            must_have="discarded legacy field",
        )
        form.pop("shared_alert_version")
        store.save_search(identity, form)
        updated = search_settings.get_search(identity)
        self.assertEqual(updated["shared_alert_version"], 1)
        self.assertEqual(updated["vinted_postage_estimate"], 220)
        self.assertIsNone(updated["max_buy"])
        self.assertEqual(updated["must_have"], "")

    def test_notes_only_edit_preserves_keyword_baselines(self):
        identity = store.save_search(None, self.form())
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_keyword_variants SET primed=1,max_item_id=123,last_item=123 WHERE query_id=?",
                (identity,),
            )
        before = vinted_keywords.rows(identity)
        search = search_settings.get_search(identity)
        store.save_search(
            identity,
            self.form(
                query=search["query"],
                ebay_search_url=search["ebay"]["search_url"],
                revision=str(search["revision"]),
                reminder="New notes only",
            ),
        )
        self.assertEqual(before, vinted_keywords.rows(identity))
        self.assertEqual(
            search["ebay_generation"],
            search_settings.get_search(identity)["ebay_generation"],
        )

    def test_invalid_shared_save_is_atomic(self):
        tables = (
            "queries",
            "search_preferences",
            "search_platforms",
            "vinted_search_budgets",
            "vinted_keyword_variants",
        )
        before = {t: self.table_rows(t) for t in tables}
        for changes in (
            {"vinted_max_total": ""},
            {"vinted_max_total": "NaN"},
            {"ebay_search_url": "https://example.com/sch/i.html"},
            {"exclusions": "!!!"},
        ):
            with self.assertRaises(ValueError):
                store.save_search(None, self.form(**changes), photos=[])
            self.assertEqual(before, {t: self.table_rows(t) for t in tables})


class SharedVintedEstimate(unittest.TestCase):
    def test_new_fixed_estimate_legacy_formula_and_boundary(self):
        item = SimpleNamespace(
            price="15.00",
            currency="GBP",
            raw_data={"total_item_price": {"amount": "19.00", "currency_code": "GBP"}},
        )
        search = {
            "shared_alert_version": 1,
            "vinted_max_total": 1795,
            "vinted_postage_estimate": 999,
        }
        result = vinted_budget.estimate(item, search)
        self.assertEqual(result["total"], 1795)
        self.assertEqual(result["buyer_protection"], 75)
        self.assertEqual(result["postage_estimate"], 220)
        self.assertTrue(result["within_budget"])
        self.assertFalse(
            vinted_budget.estimate(item, dict(search, vinted_max_total=1794))[
                "within_budget"
            ]
        )
        self.assertEqual(
            vinted_budget.estimate(item, {"vinted_max_total": 3000})["total"], 2120
        )
        item.raw_data = {}
        self.assertEqual(
            vinted_budget.estimate(item, {"vinted_max_total": 3000})["total"], 1865
        )

    def test_rounding_is_half_up(self):
        item = SimpleNamespace(price="15.10", currency="GBP", raw_data={})
        self.assertEqual(
            vinted_budget.estimate(
                item, {"shared_alert_version": 1, "vinted_max_total": 2000}
            )["total"],
            1806,
        )
