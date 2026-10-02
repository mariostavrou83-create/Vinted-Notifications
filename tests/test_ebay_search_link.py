"""Offline search-link fidelity, persistence, API and owner-route regression tests."""

import json
import time
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

from test_ebay_monitor import ClientTests, EbayFixture, item

import dashboard_store
import ebay_monitor
import ebay_search_link as links
import search_settings
from web_ui_plugin.web_ui import create_app

LINK = "https://www.ebay.co.uk/sch/11554/i.html?_nkw=jeans&Brand=7%2520For%2520All%2520Mankind&Size=8%257C10%257C12&_udhi=15&LH_BIN=1&LH_PrefLoc=1&_sop=15&_trksid=tracking"


class LinkParserTests(unittest.TestCase):
    def test_brand_multi_size_price_location_and_category_reach_api(self):
        config = links.parse_link(LINK)
        params = ebay_monitor.search_params(config)
        self.assertEqual(config["max_price"], 1500)
        self.assertEqual(params["q"], "jeans")
        self.assertEqual(params["category_ids"], "11554")
        self.assertEqual(
            params["aspect_filter"],
            "categoryId:11554,Brand:{7 For All Mankind},Size:{10|12|8}",
        )
        self.assertIn("itemLocationCountry:GB", params["filter"])
        self.assertEqual(params["sort"], "newlyListed")
        self.assertNotIn("tracking", config["search_url"])
        self.assertEqual(links.parse_link(config["search_url"]), config)
        self.assertNotIn(
            "price", params["filter"]
        )  # Local prices must not make old price drops new arrivals.

    def test_condition_ids_free_postage_and_category_only_search(self):
        config = links.parse_link(
            "https://www.ebay.co.uk/sch/i.html?_sacat=11554&LH_ItemCondition=1000%7C1500&LH_FS=1"
        )
        params = ebay_monitor.search_params(config)
        self.assertNotIn("q", params)
        self.assertIn("conditionIds:{1000|1500}", params["filter"])
        self.assertIn("maxDeliveryCost:0", params["filter"])
        self.assertEqual(config["buying"], "both")
        self.assertFalse(config["uk_only"])

    def test_dcat_category_with_zero_sacat_and_aspect_encoding(self):
        config = links.parse_link(
            "https://www.ebay.co.uk/sch/i.html?_sacat=0&_dcat=11554&Brand=Marks%2520%2526%2520Spencer&Size%2520Type=Petite"
        )
        self.assertIn("Brand:{Marks & Spencer}", links.aspect_filter(config))
        self.assertIn("Size Type:{Petite}", links.aspect_filter(config))

    def test_missing_brand_and_size_are_explicit(self):
        summary = links.describe(
            links.parse_link("https://www.ebay.co.uk/sch/i.html?_nkw=jeans")
        )
        self.assertIn("Brand: no separate brand filter", summary)
        self.assertIn("Size: no size filter", summary)

    def test_unsafe_unsupported_and_ambiguous_links_fail_closed(self):
        urls = [
            "https://evil.example/sch/i.html?_nkw=jeans",
            "https://www.ebay.co.uk.evil.example/sch/i.html?_nkw=jeans",
            "https://secret@www.ebay.co.uk/sch/i.html?_nkw=jeans",
            "https://www.ebay.co.uk:444/sch/i.html?_nkw=jeans",
            "https://ebay.us/example",
            "http://www.ebay.co.uk/sch/i.html?_nkw=jeans",
            "https://www.ebay.co.uk/itm/123",
            "https://www.ebay.co.uk/b/Jeans/11554/bn_123",
            "https://www.ebay.co.uk/sch/i.html?_nkw=jeans#Brand=Example",
            "https://www.ebay.co.uk/sch/i.html?_nkw=jeans&Brand=Example",
            "https://www.ebay.co.uk/sch/11554/i.html?_sacat=123",
        ]
        for suffix in [
            "LH_Sold=1",
            "LH_Complete=1",
            "LH_Distance=10",
            "_ssn=seller",
            "_stpos=W21UA",
            "SomethingNew=1",
            "_sacurrency=USD",
            "LH_PrefLoc=2",
            "LH_BIN=2",
            "LH_BIN=1&LH_Auction=1",
            "LH_ItemCondition=9999",
            "_udlo=20&_udhi=15",
            "_nkw=coat",
            "_sacat=11554&Brand=bad%7D%2CSize%3A%7B8",
            "_sacat=11554&Size=",
        ]:
            urls.append("https://www.ebay.co.uk/sch/i.html?_nkw=jeans&" + suffix)
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                links.parse_link(url)

    def test_aspect_filters_prevent_incorrect_request_grouping(self):
        one = links.parse_link(LINK)
        two = links.parse_link(LINK.replace("Size=8%257C10%257C12", "Size=16"))
        self.assertEqual(
            len(ebay_monitor.grouped_searches([{"ebay": one}, {"ebay": two}])), 2
        )

    def test_api_warnings_never_process_potentially_unfiltered_results(self):
        client, _ = ClientTests().client(
            SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "itemSummaries": [item(1)],
                    "warnings": [{"message": "Ignored aspect"}],
                },
            )
        )
        with self.assertRaisesRegex(ebay_monitor.EbayError, "No results processed"):
            client.search(links.parse_link(LINK))

    def test_public_mode_does_not_drop_api_aspects(self):
        import ebay_public

        with self.assertRaisesRegex(ebay_monitor.EbayError, "Browse API"):
            ebay_public.search_url(links.parse_link(LINK))


class LinkDashboardTests(EbayFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()
        from werkzeug.security import generate_password_hash

        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE dashboard_auth SET password_hash=?",
                (generate_password_hash("test-password-only"),),
            )

    def login(self):
        with self.client.session_transaction() as session:
            session["owner"] = True
            session["csrf"] = "csrf"

    def form(self):
        search = search_settings.get_search(1)
        return {
            "csrf": "csrf",
            "revision": str(search["revision"]),
            "query_name": search["query_name"],
            "query": search["query"],
            "platform_mode": "both",
            "ebay_filter_mode": "url",
            "ebay_search_url": LINK,
            "ebay_keywords": "WRONG hidden manual keywords",
        }

    def test_owner_csrf_and_preview_without_network_or_save(self):
        self.assertEqual(
            self.client.post("/ebay/import-search", data={}).status_code, 400
        )
        with self.client.session_transaction() as session:
            session["csrf"] = "csrf"
        self.assertEqual(
            self.client.post("/ebay/import-search", data={"csrf": "csrf"}).status_code,
            302,
        )
        self.login()
        before = search_settings.get_search(1)
        with patch(
            "requests.Session.get", side_effect=AssertionError("must not fetch URL")
        ):
            response = self.client.post(
                "/ebay/import-search", data={"csrf": "csrf", "search_url": LINK}
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Brand: 7 For All Mankind", response.json["summary"])
        self.assertEqual(search_settings.get_search(1), before)

    def test_save_reload_and_invalid_link_preserve_previous_filters(self):
        self.login()
        self.assertEqual(
            self.client.post("/search/1", data=self.form()).status_code, 302
        )
        saved = search_settings.get_search(1)
        self.assertEqual(saved["ebay"]["keywords"], "jeans")
        self.assertEqual(saved["ebay"]["aspects"]["Size"], ["10", "12", "8"])
        self.assertIn("Brand: 7 For All Mankind", self.client.get("/search/1").text)
        form = self.form()
        form["ebay_search_url"] += "&LH_Sold=1"
        response = self.client.post("/search/1", data=form)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Cannot import these filters safely", response.text)
        self.assertIn("LH_Sold=1", response.text)
        self.assertEqual(search_settings.get_search(1), saved)

    def test_imported_filter_edit_resets_only_ebay_and_rebaselines_silently(self):
        search = self.enable(ebay_filter_mode="url", ebay_search_url=LINK)
        self.snapshot(search, [item(123)], 1000)
        before = search_settings.get_search(1)
        form = self.form()
        form["ebay_search_url"] = LINK.replace("Size=8%257C10%257C12", "Size=16")
        dashboard_store.save_search(1, form)
        after = search_settings.get_search(1)
        self.assertGreater(after["ebay_generation"], before["ebay_generation"])
        self.assertEqual(after["query"], before["query"])
        self.assertIsNone(after["ebay_health"].get("baseline_at"))
        self.assertEqual(self.snapshot(after, [item(124)], 1020), 0)

    def test_legacy_manual_metadata_edit_does_not_reset_baseline(self):
        search = self.enable()
        self.snapshot(search, [], 1000)
        with closing(search_settings.connection()) as conn, conn:
            config = dict(search["ebay"])
            for key in (
                "filter_mode",
                "search_url",
                "aspects",
                "condition_ids",
                "free_shipping",
            ):
                config.pop(key)
            conn.execute(
                "UPDATE search_platforms SET ebay_config=? WHERE query_id=1",
                (json.dumps(config),),
            )
        self.enable(reminder="Updated reminder")
        self.assertEqual(
            search_settings.get_search(1)["ebay_health"]["baseline_at"], 1000
        )

    def test_saved_check_uses_saved_filters_and_does_not_create_alerts(self):
        self.login()
        self.enable(ebay_filter_mode="url", ebay_search_url=LINK)
        with patch(
            "ebay_connections.configuration",
            return_value={
                "source": "browse",
                "client_id": "test",
                "client_secret": "test",
            },
        ), patch("ebay_connections.reserve_call", return_value=0), patch(
            "ebay_connections.BrowseClient"
        ) as client:
            client.return_value.search.return_value = (
                [item(123, created=time.time() - 5)],
                "",
            )
            response = self.client.post("/search/1/check-ebay", data={"csrf": "csrf"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["eligible"], 1)
        self.assertEqual(response.json["dated"], 1)
        self.assertEqual(
            client.return_value.search.call_args.args[0]["aspects"]["Brand"],
            ["7 For All Mankind"],
        )
        self.assertEqual(self.outbox(), [])
        self.assertFalse(search_settings.get_search(1)["ebay_health"])

    def test_filter_check_shows_old_matching_jeans_without_alerting(self):
        self.login()
        self.enable(ebay_filter_mode="url", ebay_search_url=LINK)
        with patch(
            "ebay_connections.configuration",
            return_value={
                "source": "browse",
                "client_id": "test",
                "client_secret": "test",
            },
        ), patch("ebay_connections.reserve_call", return_value=0), patch(
            "ebay_connections.BrowseClient"
        ) as client:
            client.return_value.search.return_value = (
                [
                    item(
                        123,
                        created=time.time() - 86400,
                        title="7 For All Mankind Jeans",
                        categories=[{"categoryId": "11554", "categoryName": "Jeans"}],
                    )
                ],
                "",
            )
            response = self.client.post("/search/1/check-ebay", data={"csrf": "csrf"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["eligible"], 0)
        self.assertEqual(response.json["samples"][0]["categories"], ["Jeans"])
        self.assertEqual(self.outbox(), [])
