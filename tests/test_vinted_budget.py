"""Budget filtering, price provenance and search persistence without live APIs."""

import copy
import unittest
from contextlib import closing
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import test_dashboard
from test_search_controls import DatabaseFixture

import dashboard_store
import db
import photo_cards
import search_settings
import vinted_alerts
import vinted_budget as budget
import vinted_keywords


def item(price="15.00", total="16.45", currency="GBP"):
    return SimpleNamespace(
        price=price,
        currency=currency,
        raw_data={"total_item_price": {"amount": total, "currency_code": currency}},
    )


class Estimates(unittest.TestCase):
    def test_vinted_supplied_protection_plus_postage_and_exact_boundary(self):
        search = {"vinted_max_total": 1995, "vinted_postage_estimate": 350}
        result = budget.estimate(item(), search)
        self.assertEqual(result["total"], 1995)
        self.assertEqual(result["buyer_protection"], 145)
        self.assertFalse(result["buyer_protection_estimated"])
        self.assertTrue(result["within_budget"])
        self.assertFalse(
            budget.estimate(item(), dict(search, vinted_max_total=1994))[
                "within_budget"
            ]
        )

    def test_missing_invalid_fee_is_explicit_estimate_never_zero(self):
        for total in (None, "NaN", "1.00", "16.451"):
            result = budget.estimate(item(total=total), {"vinted_max_total": 2000})
            self.assertEqual(result["total"], 1865)
            self.assertTrue(result["buyer_protection_estimated"])
            self.assertIn("Est. total", budget.alert_lines(result))
            self.assertNotIn("\n", budget.alert_lines(result))
        raw = item()
        raw.raw_data["total_item_price"]["currency_code"] = "EUR"
        self.assertTrue(
            budget.estimate(raw, {"vinted_max_total": 2000})[
                "buyer_protection_estimated"
            ]
        )

    def test_invalid_item_price_or_non_gbp_does_not_pass(self):
        for obj in (
            item(price="NaN"),
            item(price="-1"),
            item(price="1.001"),
            item(currency="EUR"),
        ):
            self.assertFalse(
                budget.estimate(obj, {"vinted_max_total": 2000})["within_budget"]
            )
        self.assertIsNone(budget.estimate(item(), {}))

    def test_configured_postage_is_used_including_explicit_free_postage(self):
        for postage, expected in ((0, 1645), (500, 2145)):
            result = budget.estimate(
                item(), {"vinted_max_total": 2000, "vinted_postage_estimate": postage}
            )
            self.assertEqual(result["total"], expected)
            self.assertEqual(result["within_budget"], expected <= 2000)

    def test_default_postage_is_220_and_unbudgeted_alerts_can_show_a_total(self):
        result = budget.estimate(item(price="9", total="10"), {}, display=True)
        self.assertEqual(result["total"], 1220)
        self.assertEqual(result["postage_estimate"], 220)
        self.assertIsNone(result["max_total"])
        self.assertIsNone(budget.estimate(item(price="9", total="10"), {}))
        self.assertEqual(budget.parse_form({"vinted_max_total": "15"}, {}), (1500, 220))


class BudgetIntegration(DatabaseFixture, unittest.TestCase):
    def save(self, **changes):
        old = search_settings.get_search(1)
        form = {
            "query_name": "Cardigans",
            "query": old["query"],
            "revision": str(old["revision"]),
            "vinted_max_total": "20.00",
            "vinted_postage_estimate": "3.50",
        }
        form.update(changes)
        return dashboard_store.save_search(1, form)

    def test_budget_replaces_url_prices_and_preserves_all_other_filters_for_keywords(
        self,
    ):
        self.save(
            query="https://www.vinted.co.uk/catalog?catalog[]=10&size_ids[]=1&size_ids[]=2&brand_ids[]=30&status_ids[]=2&price_from=5&price_to=9",
            vinted_keywords="fur, sherpa",
        )
        saved = search_settings.get_search(1)
        self.assertEqual(saved["vinted_max_total"], 2000)
        for variant in vinted_keywords.rows(1):
            params = parse_qs(urlsplit(variant["url"]).query)
            self.assertNotIn("price_from", params)
            self.assertEqual(params["price_to"], ["20.00"])
            self.assertEqual(params["currency"], ["GBP"])
            self.assertEqual(params["size_ids[]"], ["1", "2"])
            self.assertEqual(params["catalog[]"], ["10"])
            self.assertEqual(params["status_ids[]"], ["2"])
            self.assertEqual(params["brand_ids[]"], ["30"])
        self.save(vinted_max_total="")
        self.assertNotIn(
            "price_to", parse_qs(urlsplit(search_settings.get_search(1)["query"]).query)
        )

    def test_bad_values_rejected_atomically_and_old_forms_keep_budget(self):
        for changes in (
            {"vinted_max_total": "NaN"},
            {"vinted_max_total": "1000.01"},
            {"vinted_max_total": "0"},
            {"vinted_postage_estimate": "-1"},
            {"vinted_postage_estimate": "20"},
        ):
            with self.assertRaises(ValueError):
                self.save(**changes)
            self.assertIsNone(search_settings.get_search(1)["vinted_max_total"])
        self.save()
        old = search_settings.get_search(1)
        dashboard_store.save_search(
            1,
            {
                "query_name": "Renamed",
                "query": old["query"],
                "revision": str(old["revision"]),
            },
        )
        self.assertEqual(search_settings.get_search(1)["vinted_max_total"], 2000)

    def test_filtered_item_can_match_an_overlapping_higher_budget_search(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.executemany(
                "INSERT INTO vinted_search_budgets VALUES (?,?,350)",
                [(1, 1900), (2, 2000)],
            )
        self.assertEqual(self.batch(1, [101]), [])
        with closing(search_settings.connection()) as conn:
            self.assertIsNone(
                conn.execute("SELECT 1 FROM items WHERE item='101'").fetchone()
            )
        self.assertEqual(len(self.batch(2, [101])), 1)
        self.assertEqual(self.batch(1, [101]), [])

    def test_migration_preserves_searches_and_disables_old_payment_opt_in_once(self):
        before = db.get_queries()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DROP TABLE vinted_search_budgets")
            conn.execute(
                "UPDATE vinted_buyer SET enabled=1,max_total=2500,max_extra=500"
            )
            conn.execute(
                "UPDATE parameters SET value='15' WHERE key='msj_search_schema'"
            )
        search_settings.ensure_schema()
        self.assertEqual(db.get_queries(), before)
        with closing(search_settings.connection()) as conn, conn:
            self.assertEqual(
                conn.execute("SELECT enabled FROM vinted_buyer").fetchone()[0], 0
            )
            conn.execute("UPDATE vinted_buyer SET enabled=1")
            budget.migrate(conn)
            self.assertEqual(
                conn.execute("SELECT enabled FROM vinted_buyer").fetchone()[0], 1
            )

    def test_estimate_stays_in_photo_caption_and_ebay_is_unchanged(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("INSERT INTO vinted_search_budgets VALUES (1,2000,350)")
        self.batch(1, [101])
        with closing(search_settings.connection()) as conn:
            row = dict(
                conn.execute(
                    "SELECT * FROM alert_outbox WHERE item_id='101'"
                ).fetchone()
            )
        details = vinted_alerts.get_details(row)
        details.update(guide="Guide " * 400, reminder="Reminder " * 400)
        caption, pages = photo_cards.captions(row, details)
        self.assertIn(
            "Item: <b>£15</b> · Est. total: <b>£19.95</b> (fees + delivery)", caption
        )
        self.assertNotIn("Search budget:", caption)
        self.assertLessEqual(photo_cards.units(photo_cards.plain(caption)), 1024)
        self.assertTrue(pages)
        ebay_details = dict(copy.deepcopy(details), platform="ebay", shipping=350)
        self.assertNotIn("Est. total", vinted_alerts.sections(row, ebay_details)[2])


class BudgetDashboard(DatabaseFixture, unittest.TestCase):
    owner = test_dashboard.DashboardTests.owner

    def test_owner_can_save_budget_and_invalid_form_retains_entered_prices(self):
        from web_ui_plugin.web_ui import create_app

        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()
        self.owner()
        self.assertIn(
            b"Maximum total including fees &amp; postage",
            self.client.get("/search/new").data,
        )
        form = {
            "csrf": "offline-csrf",
            "query_name": "Cardigans",
            "query": db.get_queries()[0][1],
            "revision": "0",
            "vinted_max_total": "20.00",
            "vinted_postage_estimate": "3.50",
        }
        self.assertEqual(self.client.post("/search/1", data=form).status_code, 302)
        self.assertEqual(search_settings.get_search(1)["vinted_max_total"], 2000)
        self.assertIn(b"Vinted total budget", self.client.get("/").data)
        bad = dict(form, revision="1", vinted_postage_estimate="21.00")
        response = self.client.post("/search/1", data=bad)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'value="21.00"', response.data)
        self.assertEqual(search_settings.get_search(1)["vinted_postage_estimate"], 350)
