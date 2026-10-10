"""Shared-alert form rendering and browser initialization, without live services."""

import json
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

from flask import Flask, render_template

ROOT = Path(__file__).resolve().parents[1]


class Fields(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.fields = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("input", "textarea", "select") and attrs.get("name"):
            self.fields.setdefault(attrs["name"], []).append(attrs)


class SharedFormRendering(unittest.TestCase):
    def setUp(self):
        self.app = Flask(
            __name__, template_folder=str(ROOT / "web_ui_plugin/templates")
        )
        self.app.secret_key = "offline-render-test"
        self.app.jinja_env.globals.update(
            money=lambda value: "" if value is None else f"{value / 100:.2f}",
            loads=json.loads,
        )

    def row(self, version=1, mode="both"):
        return {
            "id": 5,
            "query_name": "Fur jackets",
            "query": "https://www.vinted.co.uk/catalog?brand_ids[]=1",
            "reminder": "Resell £50–£65. Check <label> & cuffs.",
            "exclusions": ["teddy", "faux <fur>"],
            "revision": 0,
            "reference_id": "photo-test",
            "shared_alert_version": version,
            "shared_keywords": ["fur", "fur hood"],
            "vinted_keywords": ["legacy"],
            "vinted_variants": [],
            "vinted_max_total": 2000,
            "max_buy": 1500,
            "must_have": "Legacy detail",
            "folder_id": None,
            "folder_name": None,
            "platform_mode": mode,
            "vinted_enabled": mode != "ebay",
            "ebay_enabled": mode != "vinted",
            "ebay": {
                "search_url": "https://www.ebay.co.uk/sch/i.html?_sacat=57988",
                "filter_mode": "url",
                "keywords": "legacy ebay",
                "min_price": None,
                "max_price": 1500,
                "include_shipping": False,
                "buying": "fixed",
                "condition": "used",
                "category": "57988",
                "uk_only": True,
            },
            "ebay_url": "https://www.ebay.co.uk/sch/i.html?_sacat=57988",
            "ebay_health": {},
            "status": "Checking",
            "ago": "Just now",
            "ebay_status": "Standby",
            "ebay_ago": "No check yet",
            "paused": False,
            "archived": False,
        }

    def render(self, template, row):
        with self.app.test_request_context("/search/5"):
            return render_template(
                template,
                row=row,
                rows=[dict(row, exclusions=json.dumps(row["exclusions"]))],
                prices={
                    "vinted_max_total": "20.00",
                    "vinted_postage_estimate": "2.20",
                    "max_buy": "15.00",
                    "resale_low": "50.00",
                    "resale_high": "65.00",
                },
                imported_summary=[],
                folders=[],
                reference_photos=[{"position": 0, "media_id": "photo-test"}],
                ebay_connection={"missing": [], "active": 0, "interval": 192},
                csrf="offline-csrf",
                archived=False,
                active=1,
                photos=1,
                interval=1,
                folder="",
            )

    def test_new_form_has_one_shared_rule_set_and_keeps_photos(self):
        html = self.render("msj_edit.html", self.row())
        fields = Fields(html).fields
        for name in (
            "query",
            "ebay_search_url",
            "shared_keywords",
            "exclusions",
            "vinted_max_total",
            "reminder",
            "photo",
            "remove_reference",
        ):
            self.assertEqual(len(fields[name]), 1, name)
        for name in (
            "vinted_keywords",
            "vinted_postage_estimate",
            "max_buy",
            "resale_low",
            "resale_high",
            "must_have",
            "ebay_keywords",
            "ebay_min_price",
            "ebay_max_price",
            "ebay_include_shipping",
        ):
            self.assertNotIn(name, fields)
        self.assertIn("required", fields["vinted_max_total"][0])
        self.assertEqual(fields["vinted_max_total"][0]["min"], "1")
        self.assertEqual(fields["vinted_max_total"][0]["max"], "1000")
        self.assertIn("5% + £2.20", html)
        self.assertIn("actual complete checkout total", html)
        self.assertIn("Check &lt;label&gt; &amp; cuffs.", html)
        self.assertIn("faux &lt;fur&gt;", html)
        self.assertIn("Listing photos", html)
        self.assertIn("Full notes", html)

    def test_urls_are_required_only_for_enabled_platforms_without_javascript(self):
        for mode in ("vinted", "ebay", "both"):
            with self.subTest(mode=mode):
                fields = Fields(
                    self.render("msj_edit.html", self.row(mode=mode))
                ).fields
                self.assertEqual("required" in fields["query"][0], mode != "ebay")
                self.assertEqual(
                    "required" in fields["ebay_search_url"][0], mode != "vinted"
                )

    def test_legacy_form_keeps_old_fields_and_platform_tabs(self):
        html = self.render("msj_edit.html", self.row(version=0))
        fields = Fields(html).fields
        for name in (
            "vinted_keywords",
            "vinted_postage_estimate",
            "max_buy",
            "resale_low",
            "resale_high",
            "must_have",
            "ebay_keywords",
        ):
            self.assertIn(name, fields)
        self.assertNotIn("shared_keywords", fields)
        self.assertNotIn("data-shared-alert", html)
        self.assertIn('role="tablist"', html)
        self.assertIn("Copy keywords &amp; prices", html)

    def test_shared_card_shows_budget_even_for_ebay_only_and_legacy_stays_separate(
        self,
    ):
        shared = self.render("msj_dashboard.html", self.row(mode="ebay"))
        self.assertIn("Maximum including fees &amp; postage", shared)
        self.assertIn("Shared keywords: fur · fur hood", shared)
        self.assertNotIn("Item-price guide", shared)
        legacy = self.render("msj_dashboard.html", self.row(version=0))
        self.assertIn("Vinted total budget", legacy)
        self.assertIn("1 keyword searches: legacy", legacy)
        self.assertNotIn("Shared keywords:", legacy)


class SharedBrowserInitialization(unittest.TestCase):
    @unittest.skipUnless(
        shutil.which("node"), "Node is required for browser-script checks"
    )
    def test_new_form_initializes_without_removed_legacy_elements(self):
        harness = r"""
const fs = require('fs');
const assert = require('assert');
function field(value='') {
  return {value, required:false, hidden:false, textContent:'', validity:'', events:{},
    addEventListener(type, handler) { this.events[type]=handler; },
    setCustomValidity(value) { this.validity=value; }};
}
const mode = field('both'), query = field(), ebay = field(), keywords = field('fur, fur hood, FUR');
const summary = field(), vintedOff = field(), ebayOff = field();
const shared = {querySelector(selector) {
  return selector.includes('vinted') ? vintedOff : ebayOff;
}};
const elements = {'#platform_mode':mode, '[data-shared-alert]':shared, '#query':query,
  '#ebay_search_url':ebay, '#shared_keywords':keywords, '#shared-keyword-summary':summary};
const document = {querySelector(selector) { return elements[selector] || null; },
  querySelectorAll() { return []; }};
new Function('document', fs.readFileSync(process.argv[1], 'utf8'))(document);
assert(query.required && ebay.required);
assert(vintedOff.hidden && ebayOff.hidden);
assert(summary.textContent.startsWith('2 keyword alternatives:'));
mode.value='ebay'; mode.events.change();
assert(!query.required && ebay.required && !vintedOff.hidden && ebayOff.hidden);
mode.value='vinted'; mode.events.change();
assert(query.required && !ebay.required && vintedOff.hidden && !ebayOff.hidden);
ebay.events.input();
keywords.value=Array.from({length:21}, (_,i)=>'word'+i).join(','); keywords.events.input();
assert(keywords.validity.includes('20'));
keywords.value=''; keywords.events.input(); assert.equal(keywords.validity, '');
"""
        result = subprocess.run(
            ["node", "-e", harness, str(ROOT / "web_ui_plugin/static/msj.js")],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
