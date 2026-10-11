"""Browse-link conversion fidelity and save isolation, without live requests."""

import copy
import json
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from requests.exceptions import Timeout
from test_ebay_monitor import EbayFixture, item

import dashboard_store
import ebay_browse_link as browse
import ebay_monitor
import ebay_search_link
import search_settings
from web_ui_plugin.web_ui import create_app

BROWSE = (
    "https://www.ebay.co.uk/b/bn_611014?LH_ItemCondition=3000"
    "&Type=Coat%7CJacket&Size=S%7CM&LH_PrefLoc=1&mag=1"
)
SEARCH = (
    "https://www.ebay.co.uk/sch/i.html?_sacat=63862&Brand=Patagonia"
    "&LH_ItemCondition=3000&Type=Coat%7CJacket&Size=S%7CM&LH_PrefLoc=1"
)


def metadata():
    entity = {
        "id": "611014",
        "type": "BrowseNode",
        "attributes": [{"name": "Brand", "value": ["Patagonia"]}],
        "namespaceAttributes": [],
    }
    state = {
        "category": {"id": "63862"},
        "entity": entity,
        "aspects": {
            "local": [
                {"name": "Brand", "value": ["Patagonia"]},
                {
                    "name": "Type",
                    "value": ["Coat", "Jacket"],
                    "userDefined": True,
                },
                {"name": "Size", "value": ["S", "M"], "userDefined": True},
            ],
            "global": [
                {
                    "name": "LH_ItemCondition",
                    "value": ["3000"],
                    "userDefined": True,
                },
                {
                    "name": "LH_PrefLoc",
                    "value": ["1"],
                    "userDefined": True,
                },
            ],
        },
    }
    return {
        "o": {
            "w": [
                ["fixture", {}, {"context": {"browse": state}}],
                [
                    "independent-page-context",
                    {},
                    {
                        "pageContext": {
                            "categoryId": "63862",
                            "entity": copy.deepcopy(entity),
                        }
                    },
                ],
            ]
        }
    }


def page(data=None, *, prefix="", suffix=""):
    data = metadata() if data is None else data
    return (
        "<!doctype html><html><head><title>Fixture eBay results</title></head><body>"
        + prefix
        + "<script>$brwweb_C=(window.$brwweb_C||[]).concat("
        + json.dumps(data)
        + ");</script>"
        + suffix
        + "</body></html>"
    )


def unfiltered_base_page():
    data = metadata()
    state = data["o"]["w"][0][2]["context"]["browse"]
    state["aspects"]["local"] = state["aspects"]["local"][:1]
    state["aspects"]["global"] = []
    return page(data)


def redirected_metadata(*, include_brand):
    data = metadata()
    state = data["o"]["w"][0][2]["context"]["browse"]
    state["entity"]["id"] = "661673"
    state["entity"]["attributes"] = []
    if include_brand:
        state["aspects"]["local"][0]["value"] = ["Patagonia", "Zara"]
    else:
        state["aspects"]["local"].pop(0)
    data["o"]["w"][1][2]["pageContext"]["entity"] = copy.deepcopy(state["entity"])
    return page(data)


class CacheIsolation:
    def setUp(self):
        cache = getattr(browse, "_cache", None)
        if cache is not None:
            cache.clear()
        super().setUp()


class BrowseMetadataTests(CacheIsolation, unittest.TestCase):
    def test_implicit_brand_category_and_explicit_multiselects_are_preserved(self):
        params = browse.filters_from_html(page(), BROWSE)
        self.assertEqual(params["_sacat"], "63862")
        self.assertEqual(params["Brand"], "Patagonia")
        self.assertEqual(set(params["Type"].split("|")), {"Coat", "Jacket"})
        self.assertEqual(set(params["Size"].split("|")), {"S", "M"})
        self.assertEqual(params["LH_ItemCondition"], "3000")
        self.assertEqual(params["LH_PrefLoc"], "1")

    def test_category_in_path_is_bound_to_the_same_page_metadata(self):
        params = browse.filters_from_html(
            page(),
            "https://www.ebay.co.uk/b/Patagonia-Coats-Jackets/63862/bn_611014",
        )
        self.assertEqual(params["_sacat"], "63862")
        self.assertEqual(params["Brand"], "Patagonia")
        with self.assertRaises(ValueError):
            browse.filters_from_html(
                page(), "https://www.ebay.co.uk/b/Wrong-Category/11554/bn_611014"
            )

    def test_node_identity_must_match_the_response_url(self):
        with self.assertRaises(ValueError):
            browse.filters_from_html(page(), "https://www.ebay.co.uk/b/bn_661673")

    def test_independent_page_category_and_entity_must_agree(self):
        for changes in (
            {"categoryId": "11554"},
            {"entity": {"id": "661673", "type": "BrowseNode", "attributes": []}},
        ):
            with self.subTest(changes=changes):
                data = metadata()
                data["o"]["w"][1][2]["pageContext"].update(changes)
                with self.assertRaises(ValueError):
                    browse.filters_from_html(page(data), BROWSE)

    def test_missing_page_context_or_selected_aspects_fail_closed(self):
        for missing in ("page_context", "category", "entity", "aspects"):
            with self.subTest(missing=missing):
                data = metadata()
                if missing == "page_context":
                    data["o"]["w"].pop()
                else:
                    data["o"]["w"][0][2]["context"]["browse"].pop(missing)
                with self.assertRaises(ValueError):
                    browse.filters_from_html(page(data), BROWSE)

    def test_unknown_selected_aspect_is_not_silently_dropped(self):
        data = metadata()
        data["o"]["w"][0][2]["context"]["browse"]["aspects"]["local"].append(
            {
                "name": "UnsupportedFutureFilter",
                "value": ["Example"],
                "userDefined": True,
            }
        )
        with self.assertRaises(ValueError):
            browse.filters_from_html(page(data), BROWSE)

    def test_unknown_global_filters_and_namespace_constraints_fail_closed(self):
        for kind in ("global", "namespace"):
            data = metadata()
            state = data["o"]["w"][0][2]["context"]["browse"]
            if kind == "global":
                state["aspects"]["global"].append(
                    {"name": "LH_Distance", "value": ["10"], "userDefined": True}
                )
            else:
                state["entity"]["namespaceAttributes"] = [
                    {"name": "UnsupportedNamespace", "value": ["Example"]}
                ]
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                browse.filters_from_html(page(data), BROWSE)

    def test_entity_brand_cannot_disappear_from_selected_aspects(self):
        data = metadata()
        state = data["o"]["w"][0][2]["context"]["browse"]
        state["aspects"]["local"][0]["value"] = ["Zara"]
        with self.assertRaises(ValueError):
            browse.filters_from_html(page(data), BROWSE)

    def test_conflicting_duplicate_browse_states_are_rejected(self):
        data = metadata()
        other = copy.deepcopy(data["o"]["w"][0])
        other[2]["context"]["browse"]["category"]["id"] = "11554"
        data["o"]["w"].append(other)
        with self.assertRaises(ValueError):
            browse.filters_from_html(page(data), BROWSE)

    def test_missing_malformed_and_executable_metadata_is_never_evaluated(self):
        for html in (
            "<html><title>Patagonia Jackets</title></html>",
            "<script>$brwweb_C=(window.$brwweb_C||[]).concat({not JSON});</script>",
            '<script>$brwweb_C=(window.$brwweb_C||[]).concat(JSON.parse("{}"));</script>',
            "<html><title>Verify yourself</title><p>Security check</p></html>",
        ):
            with self.subTest(html=html), self.assertRaises(ValueError):
                browse.filters_from_html(html, BROWSE)

    def test_aspect_operator_injection_is_rejected(self):
        data = metadata()
        state = data["o"]["w"][0][2]["context"]["browse"]
        state["aspects"]["local"][2]["value"] = ["M},Brand:{Anything"]
        with patch.object(
            browse, "fetch_page", return_value=(page(data), BROWSE)
        ), self.assertRaises(ValueError):
            browse.import_link(BROWSE)

    def test_malformed_aspect_containers_are_reported_as_import_errors(self):
        for value in (None, {}, "unexpected"):
            data = metadata()
            data["o"]["w"][0][2]["context"]["browse"]["aspects"]["local"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                browse.filters_from_html(page(data), BROWSE)


class BrowseImportTests(CacheIsolation, unittest.TestCase):
    def test_normal_search_import_is_offline_and_round_trips(self):
        with patch.object(
            browse, "fetch_page", side_effect=AssertionError("no web fetch")
        ):
            config = browse.import_link(SEARCH)
            self.assertEqual(config, ebay_search_link.parse_link(SEARCH))
            self.assertEqual(
                ebay_search_link.parse_link(browse.resolve_link(SEARCH)), config
            )

    def test_browse_conversion_generates_explicit_api_filters(self):
        with patch.object(browse, "fetch_page", return_value=(page(), BROWSE)) as fetch:
            config = browse.import_link(BROWSE)
        fetch.assert_called_once()
        self.assertEqual(config["category"], "63862")
        self.assertEqual(config["aspects"]["Brand"], ["Patagonia"])
        self.assertEqual(config["aspects"]["Size"], ["M", "S"])
        self.assertEqual(config["condition_ids"], ["3000"])
        self.assertTrue(config["uk_only"])
        self.assertEqual(urlsplit(config["search_url"]).path, "/sch/i.html")
        self.assertNotIn("mag", parse_qs(urlsplit(config["search_url"]).query))
        params = ebay_monitor.search_params(config)
        self.assertEqual(params["category_ids"], "63862")
        self.assertIn("Brand:{Patagonia}", params["aspect_filter"])
        self.assertIn("Size:{M|S}", params["aspect_filter"])
        self.assertIn("itemLocationCountry:GB", params["filter"])

    def test_shared_keywords_replace_url_text_and_budget_removes_url_prices(self):
        url = BROWSE + "&_nkw=old&_udlo=99&_udhi=100"
        with patch.object(browse, "fetch_page", return_value=(page(), url)):
            config = browse.import_link(
                url, keywords='(fur,"fur hood")', ignore_prices=True
            )
        self.assertEqual(config["keywords"], '(fur,"fur hood")')
        self.assertIsNone(config["min_price"])
        self.assertIsNone(config["max_price"])
        self.assertNotIn("_udlo", parse_qs(urlsplit(config["search_url"]).query))
        self.assertNotIn("_udhi", parse_qs(urlsplit(config["search_url"]).query))

    def test_unsafe_urls_and_unknown_filters_are_rejected_before_fetch(self):
        urls = [
            "http://www.ebay.co.uk/b/bn_611014",
            "https://www.ebay.co.uk.evil.example/b/bn_611014",
            "https://secret@www.ebay.co.uk/b/bn_611014",
            "https://www.ebay.co.uk:444/b/bn_611014",
            "https://www.ebay.co.uk/b/bn_611014#Brand=Zara",
            "https://www.ebay.co.uk/itm/611014",
            "https://ebay.us/short",
            BROWSE + "&LH_Sold=1",
            BROWSE + "&UnsupportedFutureFilter=1",
            BROWSE + "&Size=L",
        ]
        with patch.object(
            browse, "fetch_page", side_effect=AssertionError("must not fetch")
        ):
            for url in urls:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    browse.import_link(url)

    def test_query_and_resolved_selected_size_conflict_rejects(self):
        url = BROWSE.replace("Size=S%7CM", "Size=L")
        with patch.object(
            browse, "fetch_page", return_value=(page(), url)
        ), self.assertRaises(ValueError):
            browse.import_link(url)

    def test_response_binding_and_host_cannot_broaden_original_search(self):
        for final in (
            "https://evil.example/b/bn_611014",
            "https://www.ebay.co.uk/b/bn_661673",
        ):
            with self.subTest(final=final), patch.object(
                browse, "fetch_page", return_value=(page(), final)
            ), self.assertRaises(ValueError):
                browse.import_link(BROWSE)

    def test_redirect_to_generic_browse_cannot_drop_original_implicit_brand(self):
        final = BROWSE.replace("bn_611014", "bn_661673")
        with patch.object(
            browse,
            "fetch_page",
            side_effect=[
                (redirected_metadata(include_brand=False), final),
                (unfiltered_base_page(), "https://www.ebay.co.uk/b/bn_611014"),
            ],
        ), self.assertRaises(ValueError):
            browse.import_link(BROWSE)

    def test_search_redirect_cannot_drop_original_implicit_brand(self):
        final = SEARCH.replace("&Brand=Patagonia", "")
        with patch.object(
            browse,
            "fetch_page",
            side_effect=[
                ("", final),
                (unfiltered_base_page(), "https://www.ebay.co.uk/b/bn_611014"),
            ],
        ), self.assertRaises(ValueError):
            browse.import_link(BROWSE)

    def test_changed_node_brand_union_preserves_bound_implicit_brand(self):
        original = BROWSE + "&Brand=Zara"
        final = original.replace("bn_611014", "bn_661673")
        with patch.object(
            browse,
            "fetch_page",
            side_effect=[
                (redirected_metadata(include_brand=True), final),
                (unfiltered_base_page(), "https://www.ebay.co.uk/b/bn_611014"),
            ],
        ) as fetch:
            config = browse.import_link(original)
        self.assertEqual(config["aspects"]["Brand"], ["Patagonia", "Zara"])
        self.assertEqual(config["aspects"]["Size"], ["M", "S"])
        self.assertEqual(fetch.call_count, 2)

    def test_search_redirect_keeps_original_implicit_brand_when_base_proves_it(self):
        with patch.object(
            browse,
            "fetch_page",
            side_effect=[
                ("", SEARCH),
                (unfiltered_base_page(), "https://www.ebay.co.uk/b/bn_611014"),
            ],
        ) as fetch:
            config = browse.import_link(BROWSE)
        self.assertEqual(config["aspects"]["Brand"], ["Patagonia"])
        self.assertEqual(fetch.call_count, 2)

    def test_same_node_redirect_cannot_change_category_embedded_in_original_path(self):
        original = "https://www.ebay.co.uk/b/Patagonia/63862/bn_611014"
        final = "https://www.ebay.co.uk/b/Patagonia/11554/bn_611014"
        data = metadata()
        data["o"]["w"][0][2]["context"]["browse"]["category"]["id"] = "11554"
        data["o"]["w"][1][2]["pageContext"]["categoryId"] = "11554"
        with patch.object(
            browse, "fetch_page", return_value=(page(data), final)
        ) as fetch, self.assertRaisesRegex(ValueError, "original category|conflicting"):
            browse.import_link(original)
        fetch.assert_called_once()

    def test_validated_resolution_cache_reuses_identical_url_only(self):
        with patch.object(browse, "fetch_page", return_value=(page(), BROWSE)) as fetch:
            one = browse.resolve_link(BROWSE)
            self.assertEqual(browse.resolve_link(BROWSE), one)
        fetch.assert_called_once()
        other = BROWSE.replace("Size=S%7CM", "Size=L")
        with patch.object(
            browse, "fetch_page", return_value=(page(), other)
        ) as fetch, self.assertRaises(ValueError):
            browse.resolve_link(other)
        fetch.assert_called_once()

    def test_failed_conversion_is_not_cached_as_success(self):
        with patch.object(
            browse, "fetch_page", side_effect=ValueError("challenge")
        ) as fetch:
            for _ in range(2):
                with self.assertRaises(ValueError):
                    browse.resolve_link(BROWSE)
        self.assertEqual(fetch.call_count, 2)


class FakeResponse(SimpleNamespace):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def fake_response(url=BROWSE, *, status=200, headers=None, chunks=None):
    data = page().encode("utf-8")
    return FakeResponse(
        status_code=status,
        headers=(
            {"Content-Type": "text/html; charset=utf-8"} if headers is None else headers
        ),
        url=url,
        encoding="utf-8",
        apparent_encoding="utf-8",
        iter_content=lambda *args, **kwargs: iter([data] if chunks is None else chunks),
        close=lambda: None,
    )


class BrowseTransportTests(CacheIsolation, unittest.TestCase):
    def session(self, *responses):
        factory = patch("requests.Session")
        mocked = factory.start()
        self.addCleanup(factory.stop)
        session = mocked.return_value
        session.__enter__.return_value = session
        session.get.side_effect = list(responses)
        return session

    def test_fetch_uses_bounded_stream_without_env_credentials_or_redirects(self):
        session = self.session(fake_response())
        html, final = browse.fetch_page(BROWSE)
        self.assertIn("$brwweb_C", html)
        self.assertEqual(final, BROWSE)
        self.assertFalse(session.trust_env)
        kwargs = session.get.call_args.kwargs
        self.assertTrue(kwargs["stream"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], (3, 8))

    def test_external_redirect_is_rejected_before_second_request(self):
        session = self.session(
            fake_response(status=302, headers={"Location": "https://127.0.0.1/private"})
        )
        with self.assertRaises(ValueError):
            browse.fetch_page(BROWSE)
        self.assertEqual(session.get.call_count, 1)

    def test_safe_ebay_search_redirect_is_bounded_and_allowed(self):
        session = self.session(
            fake_response(status=302, headers={"Location": SEARCH}),
            fake_response(url=SEARCH),
        )
        _html, final = browse.fetch_page(BROWSE)
        self.assertEqual(final, SEARCH)
        self.assertEqual(session.get.call_count, 2)

    def test_redirect_loop_and_missing_location_are_rejected(self):
        session = self.session(
            *[fake_response(status=302, headers={"Location": BROWSE}) for _ in range(5)]
        )
        with self.assertRaises(ValueError):
            browse.fetch_page(BROWSE)
        self.assertLessEqual(session.get.call_count, 4)
        self.session(fake_response(status=302, headers={}))
        with self.assertRaises(ValueError):
            browse.fetch_page(BROWSE)

    def test_non_html_http_errors_and_oversized_decompressed_body_are_rejected(self):
        for response in (
            fake_response(status=403),
            fake_response(status=500),
            fake_response(headers={"Content-Type": "application/json"}),
            fake_response(chunks=[b"x" * (4 * 1024 * 1024 + 1)]),
        ):
            with self.subTest(response=response.status_code):
                self.session(response)
                with self.assertRaises(ValueError):
                    browse.fetch_page(BROWSE)

    def test_network_timeout_is_an_actionable_bounded_import_error(self):
        self.session(Timeout("provider network detail must remain internal"))
        with self.assertRaises(ValueError) as caught:
            browse.fetch_page(BROWSE)
        self.assertNotIn("provider network detail", str(caught.exception))


class BrowseDashboardTests(CacheIsolation, EbayFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE dashboard_auth SET password_hash='offline-owner-fixture'"
            )

    def login(self):
        with self.client.session_transaction() as session:
            session["owner"] = True
            session["csrf"] = "csrf"

    def form(self, **changes):
        search = search_settings.get_search(1)
        return dict(
            {
                "csrf": "csrf",
                "revision": str(search["revision"]),
                "query_name": search["query_name"],
                "query": search["query"],
                "platform_mode": "both",
                "ebay_filter_mode": "url",
                "ebay_search_url": BROWSE,
            },
            **changes,
        )

    def test_owner_and_csrf_are_checked_before_browse_fetch(self):
        with patch.object(
            browse, "fetch_page", side_effect=AssertionError("must not fetch")
        ):
            self.assertEqual(
                self.client.post("/ebay/import-search", data={}).status_code, 400
            )
            with self.client.session_transaction() as session:
                session["csrf"] = "csrf"
            self.assertEqual(
                self.client.post(
                    "/ebay/import-search", data={"csrf": "csrf", "search_url": BROWSE}
                ).status_code,
                302,
            )
            self.login()
            self.assertEqual(
                self.client.post(
                    "/ebay/import-search", data={"search_url": BROWSE}
                ).status_code,
                400,
            )

    def test_preview_converts_without_saving_or_consuming_browse_api_calls(self):
        self.login()
        before = search_settings.get_search(1)
        with patch.object(browse, "fetch_page", return_value=(page(), BROWSE)), patch(
            "ebay_connections.reserve_call", side_effect=AssertionError("no API call")
        ):
            response = self.client.post(
                "/ebay/import-search", data={"csrf": "csrf", "search_url": BROWSE}
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Brand: Patagonia", response.json["summary"])
        self.assertEqual(search_settings.get_search(1), before)
        self.assertEqual(self.outbox(), [])

    def test_shared_preview_shows_dashboard_budget_and_current_keyword_alternatives(
        self,
    ):
        self.login()
        with patch.object(browse, "fetch_page", return_value=(page(), BROWSE)):
            response = self.client.post(
                "/ebay/import-search",
                data={
                    "csrf": "csrf",
                    "search_url": BROWSE,
                    "shared_alert_version": "1",
                    "shared_keywords": "fur, fur hood",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Price: your dashboard complete budget applies when saved.",
            response.json["summary"],
        )
        self.assertIn('Keywords: (fur,"fur hood")', response.json["summary"])
        self.assertFalse(
            any("postage excluded" in line for line in response.json["summary"])
        )
        self.assertEqual(self.outbox(), [])

    def test_direct_save_does_not_require_a_prior_preview_and_reload_is_offline(self):
        self.login()
        with patch.object(browse, "fetch_page", return_value=(page(), BROWSE)):
            response = self.client.post("/search/1", data=self.form())
        self.assertEqual(response.status_code, 302)
        saved = search_settings.get_search(1)
        self.assertEqual(saved["ebay"]["aspects"]["Brand"], ["Patagonia"])
        self.assertEqual(saved["ebay"]["category"], "63862")
        with patch.object(
            browse, "fetch_page", side_effect=AssertionError("GET must not fetch")
        ):
            response = self.client.get("/search/1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Brand: Patagonia", response.text)
        self.assertEqual(saved["query"], self.form()["query"])

    def test_conversion_failure_preserves_saved_filters_baselines_and_outbox(self):
        search = self.enable(ebay_filter_mode="url", ebay_search_url=SEARCH)
        self.snapshot(search, [], 1000)
        self.snapshot(search, [item(123)], 1020)
        before = search_settings.get_search(1)
        outbox = self.outbox()
        with patch.object(
            browse,
            "fetch_page",
            side_effect=ValueError("Unable to verify browse filters"),
        ), self.assertRaises(ValueError):
            dashboard_store.save_search(1, self.form())
        self.assertEqual(search_settings.get_search(1), before)
        self.assertEqual(self.outbox(), outbox)

    def test_revision_is_rechecked_after_slow_resolution_before_save(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("INSERT OR IGNORE INTO search_dashboard(query_id) VALUES (1)")
        before = search_settings.get_search(1)
        form = self.form()

        def concurrent_edit(_url):
            with closing(search_settings.connection()) as conn, conn:
                conn.execute(
                    "UPDATE search_dashboard SET revision=revision+1 WHERE query_id=1"
                )
            return page(), BROWSE

        with patch.object(
            browse, "fetch_page", side_effect=concurrent_edit
        ), self.assertRaisesRegex(ValueError, "another tab|changed"):
            dashboard_store.save_search(1, form)
        saved = search_settings.get_search(1)
        self.assertEqual(saved["query"], before["query"])
        self.assertEqual(saved["ebay"], before["ebay"])

    def test_new_shared_alert_has_one_keyword_set_exclusions_budget_and_buyer_untouched(
        self,
    ):
        with closing(search_settings.connection()) as conn:
            buyers = [tuple(row) for row in conn.execute("SELECT * FROM vinted_buyer")]
            attempts = [
                tuple(row) for row in conn.execute("SELECT * FROM vinted_buy_attempts")
            ]
        form = self.form(
            shared_alert_version="1",
            query_name="Shared Patagonia fixture",
            query="https://www.vinted.co.uk/catalog?brand_ids[]=111",
            shared_keywords="fur, fur hood",
            exclusions="teddy\nripped",
            vinted_max_total="20.00",
            reminder="Resell £60; check cuffs",
        )
        with patch.object(browse, "fetch_page", return_value=(page(), BROWSE)):
            identity = dashboard_store.save_search(None, form)
        saved = search_settings.get_search(identity)
        self.assertEqual(saved["shared_keywords"], ["fur", "fur hood"])
        self.assertEqual(saved["vinted_keywords"], ["fur", "fur hood"])
        self.assertEqual(saved["ebay"]["keywords"], '(fur,"fur hood")')
        self.assertEqual(saved["exclusions"], ["teddy", "ripped"])
        self.assertEqual(saved["ebay"]["max_price"], 2000)
        self.assertTrue(saved["ebay"]["include_shipping"])
        self.assertEqual(saved["vinted_max_total"], 2000)
        self.assertEqual(saved["ebay"]["aspects"]["Brand"], ["Patagonia"])
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                buyers,
                [tuple(row) for row in conn.execute("SELECT * FROM vinted_buyer")],
            )
            self.assertEqual(
                attempts,
                [
                    tuple(row)
                    for row in conn.execute("SELECT * FROM vinted_buy_attempts")
                ],
            )
