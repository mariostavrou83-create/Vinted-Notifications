"""Public-filter handoff opens an authenticated unsaved form only."""

import unittest
from contextlib import closing
from html.parser import HTMLParser
from unittest.mock import patch
from urllib.parse import unquote, urlencode

from test_ebay_monitor import EbayFixture

import search_settings
from web_ui_plugin.web_ui import create_app

SEARCH = "https://www.ebay.co.uk/sch/i.html?_sacat=63862&Brand=Patagonia&Size=S%7CM&LH_ItemCondition=3000"


class Links(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.links = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs).get("href", ""))


class HelperRoutes(EbayFixture, unittest.TestCase):
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

    def test_helper_and_prefill_require_owner_authentication(self):
        for path in (
            "/ebay/link-helper",
            "/search/new?" + urlencode({"ebay_import": SEARCH}),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login", response.location)

    def test_helper_contains_fixed_local_script_and_both_setup_methods(self):
        self.login()
        response = self.client.get("/ebay/link-helper")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        links = [link for link in Links(html).links if link.startswith("javascript:")]
        self.assertEqual(len(links), 1)
        code = unquote(links[0].removeprefix("javascript:"))
        self.assertIn(
            "https://msj-vinted-fixed-production.up.railway.app/search/new", code
        )
        self.assertIn("'?ebay_import='", code)
        self.assertIn("On your computer", html)
        self.assertIn("On your phone", html)
        self.assertIn("some mobile browsers restrict", html)

    def test_prefill_is_offline_unsaved_and_preserves_all_existing_rows(self):
        self.login()
        with closing(search_settings.connection()) as conn:
            before = conn.execute("SELECT * FROM queries ORDER BY id").fetchall()
        original = search_settings.get_search(1)
        with patch(
            "ebay_browse_link.fetch_page", side_effect=AssertionError("no fetch")
        ):
            response = self.client.get(
                "/search/new?" + urlencode({"ebay_import": SEARCH})
            )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("eBay filters copied into this new form", html)
        self.assertIn("Brand: Patagonia", html)
        self.assertIn("Size: M or S", html)
        self.assertIn('value="both" selected', html)
        with closing(search_settings.connection()) as conn:
            after = conn.execute("SELECT * FROM queries ORDER BY id").fetchall()
        self.assertEqual(before, after)
        self.assertEqual(original, search_settings.get_search(1))

    def test_invalid_prefill_never_fetches_or_becomes_an_accepted_link(self):
        self.login()
        for link in (
            "https://evil.example/",
            "javascript:alert(1)",
            "https://www.ebay.co.uk/b/bn_611014",
            SEARCH + "&UnknownConstraint=yes",
        ):
            with self.subTest(link=link), patch(
                "ebay_browse_link.fetch_page", side_effect=AssertionError("no fetch")
            ):
                response = self.client.get(
                    "/search/new?" + urlencode({"ebay_import": link})
                )
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn("The copied eBay filters were not accepted", html)
                self.assertNotIn("eBay filters copied into this new form", html)

    def test_existing_alert_editor_ignores_prefill_argument(self):
        self.login()
        before = search_settings.get_search(1)
        response = self.client.get("/search/1?" + urlencode({"ebay_import": SEARCH}))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            "eBay filters copied into this new form", response.get_data(as_text=True)
        )
        self.assertEqual(before, search_settings.get_search(1))


if __name__ == "__main__":
    unittest.main()
