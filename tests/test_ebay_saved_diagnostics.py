"""Saved shared-filter diagnostics: one mocked read, real routes and SQLite."""

import shutil
import subprocess
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from test_ebay_monitor import EbayFixture, item

import dashboard_store
import ebay_monitor
import ebay_store
import search_settings
from web_ui_plugin.web_ui import create_app

ROOT = Path(__file__).resolve().parents[1]
NOW = time.time()
LINK = "https://www.ebay.co.uk/sch/11554/i.html?_nkw=&Brand=Zara&Size=8%257C10&LH_BIN=1&LH_PrefLoc=1"


def jeans(number, **changes):
    fields = {
        "created": NOW - 20,
        "title": "Zara skinny jeans",
        "categories": [{"categoryId": "11554", "categoryName": "Jeans"}],
    }
    fields.update(changes)
    return item(number, **fields)


class SavedDiagnosticTests(EbayFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE dashboard_auth SET password_hash='offline-owner'")
        self.login()
        self.form = {
            "shared_alert_version": "1",
            "query_name": "NEW SHARED TEST · Zara skinny jeans",
            "platform_mode": "ebay",
            "query": "",
            "ebay_search_url": LINK,
            "shared_keywords": "skinny",
            "exclusions": "ripped",
            "vinted_max_total": "25.00",
            "reminder": "Resell £50. Check <label> & stitching.",
        }
        self.identity = dashboard_store.save_search(None, self.form, photos=[])
        ebay_store.save_live_selection([self.identity])
        self.search = search_settings.get_search(self.identity)
        self.snapshot(self.search, [], NOW - 300)
        self.snapshot(self.search, [jeans(999)], NOW - 10)
        self.config = dict(
            ebay_store.configuration(),
            source="browse",
            client_id="private-app-test",
            client_secret="private-cert-test",
        )

    def login(self):
        with self.client.session_transaction() as session:
            session["owner"] = True
            session["csrf"] = "test-csrf"

    def saved_state(self):
        with closing(search_settings.connection()) as conn:
            return {
                table: [tuple(row) for row in conn.execute("SELECT * FROM " + table)]
                for table in (
                    "queries",
                    "search_platforms",
                    "search_dashboard",
                    "search_preferences",
                    "vinted_buyer",
                    "vinted_buy_attempts",
                    "vinted_keyword_variants",
                    "ebay_state",
                    "ebay_seen",
                    "alert_outbox",
                    "telegram_photo_cards",
                )
            }

    def check(self, items, *, wait=0, error=None):
        fake = Mock()
        fake.search.return_value = (items, "")
        if error:
            fake.search.side_effect = error
        with patch("ebay_connections.configuration", return_value=self.config), patch(
            "ebay_connections.reserve_call", return_value=wait
        ) as reservation, patch(
            "ebay_connections.BrowseClient", return_value=fake
        ) as constructor, patch(
            "requests.post", side_effect=AssertionError("No Telegram or other writes")
        ):
            response = self.client.post(
                f"/search/{self.identity}/check-ebay", data={"csrf": "test-csrf"}
            )
        return response, fake, reservation, constructor

    def test_exact_shared_zara_skinny_and_ripped_use_saved_filters_one_read(self):
        before = self.saved_state()
        response, fake, reservation, constructor = self.check(
            [jeans(101), jeans(102, title="ZARA ripped skinny jeans")]
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        data = response.json
        self.assertEqual(
            (data["matched"], data["eligible"], data["rejected"]), (1, 1, 1)
        )
        accepted, rejected = data["samples"][0], data["rejected_samples"][0]
        self.assertEqual(accepted["item_id"], "ebay:101")
        self.assertEqual(accepted["url"], "https://www.ebay.co.uk/itm/101")
        self.assertEqual(accepted["price"], "£15.00")
        self.assertEqual(accepted["shipping"], "£3.00")
        self.assertEqual(accepted["buyer_fee_estimate"], "£1.15")
        self.assertEqual(accepted["estimated_total"], "£19.15")
        self.assertEqual(rejected["item_id"], "ebay:102")
        self.assertEqual(rejected["reasons"], ["Excluded title phrase: ripped."])
        params = data["criteria"]["parameters"]
        self.assertEqual(params["q"], "skinny")
        self.assertEqual(params["category_ids"], "11554")
        self.assertEqual(
            params["aspect_filter"], "categoryId:11554,Brand:{Zara},Size:{10|8}"
        )
        self.assertIn("buyingOptions:{FIXED_PRICE}", params["filter"])
        self.assertIn("itemLocationCountry:GB", params["filter"])
        self.assertEqual(data["criteria"]["exclusions"], ["ripped"])
        self.assertEqual(data["criteria"]["maximum_total"], "£25.00")
        self.assertIn("existing catalogue samples", data["message"])
        self.assertIn("not live alerts", data["message"])
        self.assertIn("not eBay checkout quotes", data["message"])
        fake.search.assert_called_once_with(
            search_settings.get_search(self.identity)["ebay"]
        )
        fake.session.close.assert_called_once()
        constructor.assert_called_once_with(self.config)
        reservation.assert_called_once()
        self.assertEqual(reservation.call_args.args[0], self.config)
        self.assertGreaterEqual(reservation.call_args.args[1], NOW)
        self.assertEqual(reservation.call_args.kwargs, {"diagnostic": True})
        self.assertEqual(self.saved_state(), before)
        for secret in (self.config["client_id"], self.config["client_secret"]):
            self.assertNotIn(secret, response.text)

    def test_current_rules_report_wrong_category_unknown_postage_and_full_budget(self):
        response, _, _, _ = self.check(
            [
                jeans(101),
                jeans(
                    102, categories=[{"categoryId": "57988", "categoryName": "Coats"}]
                ),
                jeans(103, shippingOptions=[]),
                jeans(104, price="23.00"),
                jeans(105, itemWebUrl="https://www.ebay.co.uk/itm/999999"),
            ]
        )
        data = response.json
        self.assertEqual(data["matched"], 1)
        self.assertEqual(data["rejected"], 4)
        rejected = {row["item_id"]: row for row in data["rejected_samples"]}
        self.assertIn(
            "Outside the saved eBay category.", rejected["ebay:102"]["reasons"]
        )
        unknown = rejected["ebay:103"]
        self.assertIsNone(unknown["shipping"])
        self.assertIsNone(unknown["estimated_total"])
        self.assertIsNone(unknown["buyer_fee_estimate"])
        self.assertIn("cannot be treated as free", unknown["reasons"][0])
        over = rejected["ebay:104"]
        self.assertEqual(over["estimated_total"], "£27.62")
        self.assertIn("£27.62 exceeds the saved maximum £25.00", over["reasons"][0])
        unbound = rejected[None]
        for field in ("url", "price", "shipping", "estimated_total"):
            self.assertIsNone(unbound[field])
        self.assertIn("link, item ID", unbound["reasons"][0])

    def test_exact_full_total_boundary_uses_existing_parser_and_rounding(self):
        search = search_settings.get_search(self.identity)
        dashboard_store.save_search(
            self.identity,
            dict(self.form, revision=str(search["revision"]), vinted_max_total="19.15"),
        )
        response, _, _, _ = self.check([jeans(101), jeans(102, price="15.01")])
        self.assertEqual(response.json["samples"][0]["estimated_total"], "£19.15")
        self.assertEqual(
            response.json["rejected_samples"][0]["estimated_total"], "£19.17"
        )
        self.assertEqual(response.json["criteria"]["maximum_total"], "£19.15")

    def test_samples_are_bounded_and_later_actual_exclusion_is_not_hidden(self):
        results = [jeans(n, price="30.00") for n in range(100, 110)]
        results += [jeans(110, title="Zara ripped skinny jeans")]
        results += [jeans(n) for n in range(111, 123)]
        response, fake, _, _ = self.check(results)
        data = response.json
        self.assertEqual((data["matched"], data["rejected"]), (12, 11))
        self.assertEqual(len(data["samples"]), 5)
        self.assertEqual(len(data["rejected_samples"]), 5)
        self.assertIn("ebay:110", {row["item_id"] for row in data["rejected_samples"]})
        fake.search.assert_called_once()

    def test_older_matching_sample_is_never_a_new_notification(self):
        before = self.saved_state()
        raw = jeans(101)
        raw["itemCreationDate"] = raw["itemOriginDate"] = "1970-01-01T00:00:01+00:00"
        response, _, _, _ = self.check([raw])
        self.assertEqual(response.json["matched"], 1)
        self.assertEqual(response.json["eligible"], 0)
        self.assertFalse(response.json["samples"][0]["recent"])
        self.assertEqual(response.json["delivery"], {"pending": 1})
        self.assertEqual(self.saved_state(), before)

    def test_unreadable_response_is_rejected_without_unbound_links_or_cost(self):
        response, _, _, _ = self.check(
            [None, jeans(101, itemOriginDate=None, itemCreationDate=None)]
        )
        self.assertEqual(response.json["rejected"], 2)
        self.assertEqual(response.json["samples"], [])
        for row in response.json["rejected_samples"]:
            for field in ("item_id", "url", "price", "estimated_total"):
                self.assertIsNone(row[field])

    def test_saved_shared_editor_exposes_only_valid_existing_ebay_diagnostic(self):
        html = self.client.get(f"/search/{self.identity}").text
        self.assertEqual(html.count('id="check-saved-ebay"'), 1)
        self.assertIn(f'data-url="/search/{self.identity}/check-ebay"', html)
        self.assertIn("not unsaved changes", html)
        self.assertNotIn('id="check-saved-ebay"', self.client.get("/search/new").text)
        other = dashboard_store.save_search(
            None,
            dict(
                self.form,
                platform_mode="vinted",
                query="https://www.vinted.co.uk/catalog?catalog[]=183",
            ),
        )
        self.assertNotIn(
            'id="check-saved-ebay"', self.client.get(f"/search/{other}").text
        )

    def test_owner_csrf_and_explicit_post_are_required_before_network(self):
        url = f"/search/{self.identity}/check-ebay"
        with patch("ebay_connections.BrowseClient") as api:
            self.assertEqual(self.client.get(url).status_code, 405)
            self.assertEqual(self.client.post(url, data={}).status_code, 400)
            with self.client.session_transaction() as session:
                session.pop("owner")
            self.assertEqual(
                self.client.post(url, data={"csrf": "test-csrf"}).status_code, 302
            )
            api.assert_not_called()

    def test_quota_wait_has_no_api_request_or_automatic_retry(self):
        response, fake, reservation, constructor = self.check([], wait=NOW + 20)
        self.assertEqual(response.status_code, 400)
        self.assertIn("next shared API request slot", response.json["error"])
        reservation.assert_called_once()
        constructor.assert_not_called()
        fake.search.assert_not_called()

    def test_shared_reservation_charges_one_call_and_blocks_next_manual_check(self):
        fake = Mock()
        fake.search.return_value = ([jeans(101)], "")
        before = self.saved_state()
        with patch("ebay_connections.configuration", return_value=self.config), patch(
            "ebay_connections.BrowseClient", return_value=fake
        ):
            url = f"/search/{self.identity}/check-ebay"
            first = self.client.post(url, data={"csrf": "test-csrf"})
            second = self.client.post(url, data={"csrf": "test-csrf"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)
        fake.search.assert_called_once()
        with closing(search_settings.connection()) as conn:
            self.assertEqual(
                conn.execute("SELECT SUM(calls) FROM ebay_call_buckets").fetchone()[0],
                1,
            )
        self.assertEqual(self.saved_state(), before)

    def test_api_failure_closes_client_and_only_existing_cooldown_changes(self):
        before = self.saved_state()
        response, fake, _, _ = self.check(
            [],
            error=ebay_monitor.EbayError(
                "eBay cooldown", global_cooldown=True, retry_after=60
            ),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"], "eBay cooldown")
        fake.search.assert_called_once()
        fake.session.close.assert_called_once()
        self.assertEqual(self.saved_state(), before)
        with closing(search_settings.connection()) as conn:
            cooldown = conn.execute(
                "SELECT value FROM delivery_runtime WHERE key='ebay_api_cooldown'"
            ).fetchone()[0]
            self.assertGreaterEqual(cooldown, NOW + 60)
            self.assertLessEqual(cooldown, time.time() + 61)


class DiagnosticBrowserTests(unittest.TestCase):
    @unittest.skipUnless(
        shutil.which("node"), "Node is required for browser-script checks"
    )
    def test_saved_check_ui_keeps_evidence_literal_and_links_only_bound_samples(self):
        harness = r"""
const fs = require('fs'), assert = require('assert');
class Element {
  constructor(tag) { this.tag=tag; this.children=[]; this.events={}; this.dataset={}; this.textContent=''; }
  append(...children) { this.children.push(...children); }
  replaceChildren() { this.children=[]; this.textContent=''; }
  addEventListener(type, handler) { this.events[type]=handler; }
  set innerHTML(value) { throw Error('Unsafe HTML insertion'); }
}
const output = new Element('div'), button = new Element('button');
button.dataset.url='/search/141/check-ebay';
const elements={'#check-saved-ebay':button, '#ebay-check-result':output, 'input[name="csrf"]':{value:'csrf'}};
const document={querySelector(s) { return elements[s] || null; }, querySelectorAll() { return []; }, createElement(tag) { return new Element(tag); }};
const response={message:'Existing catalogue samples, not live alerts',active:true,baseline:'test baseline',results:3,dated:3,eligible:1,matched:1,rejected:2,delivery:{sent:1},
  criteria:{parameters:{q:'skinny',category_ids:'11554',aspect_filter:'categoryId:11554,Brand:{Zara}'},exclusions:['ripped'],maximum_total:'£25.00'},
  samples:[{item_id:'ebay:101',title:'Zara <script>skinny</script> jeans',price:'£15.00',url:'https://www.ebay.co.uk/itm/101',shipping:'£3.00',buyer_fee_estimate:'£1.15',estimated_total:'£19.15',categories:['Jeans'],category_ids:['11554'],reasons:[],recent:false}],
  rejected_samples:[{item_id:'ebay:102',title:'Zara ripped skinny jeans',price:'£15.00',url:'https://www.ebay.co.uk/itm/102',shipping:'£3.00',estimated_total:'£19.15',reasons:['Excluded title phrase: ripped.']},{item_id:null,title:'Unsafe response',url:null,reasons:['Listing response failed current link, item ID, date, price or listing-type checks.']}]};
let calls=0;
async function fetch(url, options) { calls++; assert.equal(url,button.dataset.url); assert.equal(options.method,'POST'); assert.equal(options.credentials,'same-origin'); assert.equal(options.body.get('csrf'),'csrf'); return {redirected:false,ok:true,headers:{get() {return 'application/json';}},json:async()=>response}; }
new Function('document','fetch',fs.readFileSync(process.argv[1],'utf8'))(document,fetch);
function all(node) { return [node,...node.children.flatMap(all)]; }
(async()=>{
  assert.equal(calls,0);
  await button.events.click();
  assert.equal(calls,1); assert.equal(button.disabled,false);
  const nodes=all(output), text=nodes.map(n=>n.textContent).join('\n');
  for (const phrase of ['q=skinny','Brand:{Zara}','ripped','£19.15','buyer-fee allowance: £1.15','older catalogue sample','not live alerts','Unverified listing data','category IDs: 11554']) assert(text.includes(phrase),phrase);
  assert(text.includes('<script>skinny</script>'));
  assert.equal(nodes.filter(n=>n.tag==='a').length,2);
  assert(nodes.filter(n=>n.tag==='a').every(n=>n.href.startsWith('https://www.ebay.co.uk/itm/')));
  assert.equal(calls,1); // No request until clicked, no automatic retry/polling.
})().catch(error=>{process.stderr.write(error.stack);process.exitCode=1;});
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
