"""User-operated public page conversion, with browser APIs mocked in Node."""

import copy
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from test_ebay_browse_link import BROWSE, SEARCH, metadata, page

import ebay_search_link

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "web_ui_plugin/static/ebay_filter_bookmarklet.js"
DASHBOARD = "https://msj-vinted-fixed-production.up.railway.app/search/new"

HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const opens = [], alerts = [], forbidden = [];
function denied(name) { forbidden.push(name); throw Error('forbidden browser API'); }
const document = {scripts: input.scripts.map(text => ({textContent: text}))};
Object.defineProperty(document, 'cookie', {get: () => denied('cookie')});
Object.defineProperty(document, 'forms', {get: () => denied('forms')});
const window = {
  location: {href: input.url},
  open: (...args) => opens.push(args),
  alert: value => alerts.push(value),
};
for (const key of ['localStorage', 'sessionStorage', 'indexedDB']) {
  Object.defineProperty(window, key, {get: () => denied(key)});
}
const context = {
  window, document, URL, URLSearchParams,
  fetch: () => denied('fetch'),
  XMLHttpRequest: function () { denied('XMLHttpRequest'); },
  navigator: {sendBeacon: () => denied('sendBeacon')},
  localStorage: {getItem: () => denied('localStorage')},
  sessionStorage: {getItem: () => denied('sessionStorage')},
};
vm.runInNewContext(fs.readFileSync(input.file, 'utf8'), context, {timeout: 2000});
process.stdout.write(JSON.stringify({opens, alerts, forbidden}));
"""


def script(data=None, *, prefix="", suffix=""):
    html = page(data, prefix=prefix, suffix=suffix)
    return re.search(r"<script>(.*?)</script>", html, re.DOTALL)[1]


@unittest.skipUnless(shutil.which("node"), "Node is needed for browser JS verification")
class BookmarkletTests(unittest.TestCase):
    def run_browser(self, *, url=BROWSE, scripts=None):
        input_data = {
            "file": str(SOURCE),
            "url": url,
            "scripts": [script()] if scripts is None else scripts,
        }
        result = subprocess.run(
            [shutil.which("node"), "-e", HARNESS],
            input=json.dumps(input_data),
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        )
        browser = json.loads(result.stdout)
        self.assertEqual(browser["forbidden"], [])
        return browser

    def canonical(self, browser):
        self.assertEqual(browser["alerts"], [])
        self.assertEqual(len(browser["opens"]), 1)
        target, name, features = browser["opens"][0]
        parsed = urlsplit(target)
        self.assertEqual(parsed.scheme + "://" + parsed.netloc + parsed.path, DASHBOARD)
        self.assertEqual(name, "_blank")
        self.assertEqual(features, "noopener,noreferrer")
        params = parse_qs(parsed.query)
        self.assertEqual(set(params), {"ebay_import"})
        return params["ebay_import"][0]

    def assert_stopped(self, browser):
        self.assertEqual(browser["opens"], [])
        self.assertEqual(len(browser["alerts"]), 1)
        self.assertIn("Nothing has been saved", browser["alerts"][0])
        self.assertNotIn("Unreadable public filters", browser["alerts"][0])

    def test_exact_owner_example_keeps_brand_category_sizes_condition_and_uk(self):
        config = ebay_search_link.parse_link(self.canonical(self.run_browser()))
        self.assertEqual(config["category"], "63862")
        self.assertEqual(config["aspects"]["Brand"], ["Patagonia"])
        self.assertEqual(config["aspects"]["Type"], ["Coat", "Jacket"])
        self.assertEqual(config["aspects"]["Size"], ["M", "S"])
        self.assertEqual(config["condition_ids"], ["3000"])
        self.assertTrue(config["uk_only"])

    def test_normal_search_url_needs_no_script_and_keeps_all_filters(self):
        canonical = self.canonical(self.run_browser(url=SEARCH, scripts=[]))
        self.assertEqual(
            ebay_search_link.parse_link(canonical), ebay_search_link.parse_link(SEARCH)
        )

    def test_numeric_category_search_path_is_preserved(self):
        url = SEARCH.replace("/sch/i.html?_sacat=63862&", "/sch/63862/i.html?")
        config = ebay_search_link.parse_link(
            self.canonical(self.run_browser(url=url, scripts=[]))
        )
        self.assertEqual(config["category"], "63862")
        self.assertEqual(config["aspects"]["Brand"], ["Patagonia"])

    def test_keyword_text_and_explicit_price_limits_are_kept_for_server_review(self):
        url = BROWSE + "&_nkw=fur%20hood&_udlo=5.00&_udhi=25.50"
        config = ebay_search_link.parse_link(self.canonical(self.run_browser(url=url)))
        self.assertEqual(config["keywords"], "fur hood")
        self.assertEqual(config["min_price"], 500)
        self.assertEqual(config["max_price"], 2550)

    def test_bound_category_in_browse_path_is_checked(self):
        valid = "https://www.ebay.co.uk/b/Patagonia-Coats/63862/bn_611014"
        self.assertEqual(
            ebay_search_link.parse_link(self.canonical(self.run_browser(url=valid)))[
                "category"
            ],
            "63862",
        )
        self.assert_stopped(self.run_browser(url=valid.replace("63862", "11554")))

    def test_page_entity_and_independent_category_must_match_current_address(self):
        for change in ("node", "category", "brand"):
            data = metadata()
            if change == "node":
                data["o"]["w"][1][2]["pageContext"]["entity"]["id"] = "661673"
            elif change == "category":
                data["o"]["w"][1][2]["pageContext"]["categoryId"] = "11554"
            else:
                data["o"]["w"][1][2]["pageContext"]["entity"]["attributes"][0][
                    "value"
                ] = ["Zara"]
            with self.subTest(change=change):
                self.assert_stopped(self.run_browser(scripts=[script(data)]))

    def test_unknown_query_aspects_or_hidden_namespace_constraints_stop(self):
        self.assert_stopped(self.run_browser(url=BROWSE + "&LH_Sold=1"))
        for kind in ("local", "global", "namespace"):
            data = metadata()
            state = data["o"]["w"][0][2]["context"]["browse"]
            if kind == "namespace":
                state["entity"]["namespaceAttributes"] = [
                    {"name": "HiddenScope", "value": ["Example"]}
                ]
            else:
                state["aspects"][kind].append(
                    {"name": "UnsupportedFilter", "value": ["Example"]}
                )
            with self.subTest(kind=kind):
                self.assert_stopped(self.run_browser(scripts=[script(data)]))

    def test_missing_json_or_script_expressions_never_execute(self):
        for text in (
            "",
            '$brwweb_C=(window.$brwweb_C||[]).concat((window.open("https://evil.example"),{}));',
            '$brwweb_C=(window.$brwweb_C||[]).concat(JSON.parse("{}"));',
            "$brwweb_C=(window.$brwweb_C||[]).concat({not JSON});",
        ):
            with self.subTest(text=text):
                self.assert_stopped(self.run_browser(scripts=[text]))

    def test_quoted_brackets_and_escaped_quotes_do_not_confuse_json_scanner(self):
        data = metadata()
        data["irrelevant_public_text"] = (
            'A quoted string: } ], \\ " literal brackets [ { end'
        )
        config = ebay_search_link.parse_link(
            self.canonical(self.run_browser(scripts=[script(data)]))
        )
        self.assertEqual(config["aspects"]["Brand"], ["Patagonia"])

    def test_multiple_payload_scripts_and_extra_literal_code_are_not_executed(self):
        one = metadata()
        first = copy.deepcopy(one)
        first["o"]["w"] = [one["o"]["w"][0]]
        second = {"o": {"w": [one["o"]["w"][1]]}}
        browser = self.run_browser(
            scripts=[
                script(first) + 'window.open("https://evil.example");',
                script(second),
            ]
        )
        self.assertEqual(
            ebay_search_link.parse_link(self.canonical(browser))["category"], "63862"
        )

    def test_normal_ebay_non_metadata_payloads_do_not_reject_complete_criteria(self):
        non_metadata = (
            '$brwweb_C=(window.$brwweb_C||[]).concat({"l":1,"w":[["unrelated",1,{}]]});'
        )
        browser = self.run_browser(scripts=[script(), non_metadata])
        self.assertEqual(
            ebay_search_link.parse_link(self.canonical(browser))["aspects"]["Brand"],
            ["Patagonia"],
        )

    def test_unused_provider_fields_are_not_transferred_to_dashboard(self):
        data = metadata()
        data["unused_sensitive_fixture"] = "FICTIONAL_SECRET_DO_NOT_TRANSFER"
        canonical = self.canonical(self.run_browser(scripts=[script(data)]))
        self.assertNotIn("FICTIONAL_SECRET", canonical)
        self.assertNotIn("unused_sensitive_fixture", canonical)

    def test_conflicting_original_filters_or_query_duplicates_stop(self):
        for url in (
            BROWSE.replace("Size=S%7CM", "Size=L"),
            BROWSE + "&Size=L",
            BROWSE + "&_sacat=11554",
            BROWSE + "&mag=2",
        ):
            with self.subTest(url=url):
                self.assert_stopped(self.run_browser(url=url))

    def test_unsafe_host_item_link_and_credentials_stop_before_page_read(self):
        for url in (
            "http://www.ebay.co.uk/b/bn_611014",
            "https://www.ebay.co.uk.evil.example/b/bn_611014",
            "https://secret@www.ebay.co.uk/b/bn_611014",
            "https://www.ebay.co.uk:444/b/bn_611014",
            "https://www.ebay.co.uk/itm/611014",
            BROWSE + "#Brand=Zara",
        ):
            with self.subTest(url=url):
                self.assert_stopped(self.run_browser(url=url))

    def test_invalid_aspect_containers_values_and_api_operators_stop(self):
        for change in ("container", "empty", "object", "operator"):
            data = metadata()
            aspects = data["o"]["w"][0][2]["context"]["browse"]["aspects"]
            if change == "container":
                aspects["local"] = {}
            elif change == "empty":
                aspects["local"][2]["value"] = []
            elif change == "object":
                aspects["local"][2]["value"] = [{}]
            else:
                aspects["local"][2]["value"] = ["M},Brand:{Anything"]
            with self.subTest(change=change):
                self.assert_stopped(self.run_browser(scripts=[script(data)]))

    def test_conflicting_payload_contexts_are_not_combined(self):
        one = metadata()
        two = metadata()
        two["o"]["w"][0][2]["context"]["browse"]["aspects"]["local"][2]["value"] = ["L"]
        self.assert_stopped(self.run_browser(scripts=[script(one), script(two)]))

    def test_script_resource_limit_stops_without_opening_or_saving(self):
        self.assert_stopped(self.run_browser(scripts=["x" * (4 * 1024 * 1024 + 1)]))


class BookmarkletStaticContract(unittest.TestCase):
    def test_filter_allowlists_stay_in_sync_with_the_actual_importer(self):
        source = SOURCE.read_text()
        for name in ("ASPECTS", "FIELDS", "IGNORED"):
            literal = re.search(
                r"const " + name + r" = new Set\((\[[^\n]+\])\);", source
            )[1]
            self.assertEqual(set(json.loads(literal)), getattr(ebay_search_link, name))

    def test_reader_has_no_account_network_storage_or_script_execution_api(self):
        source = SOURCE.read_text()
        for pattern in (
            r"\beval\s*\(",
            r"\bFunction\s*\(",
            r"\bfetch\s*\(",
            r"\bXMLHttpRequest\b",
            r"document\.(?:cookie|forms)",
            r"\b(?:localStorage|sessionStorage|indexedDB|sendBeacon)\b",
            r"\bsubmit\s*\(",
        ):
            with self.subTest(pattern=pattern):
                self.assertNotRegex(source, pattern)
        self.assertIn("JSON.parse", source)
        self.assertIn(DASHBOARD, source)
