import io
import json
import os
import shutil
import subprocess
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask
from home_companion_download import ASSETS, home_companion_zip_response

ROOT = Path(__file__).resolve().parents[1]


class HomeCompanionPackageTests(unittest.TestCase):
    def test_exact_dashboard_host_no_broad_permissions_or_secrets(self):
        app = Flask(__name__)
        app.add_url_rule("/connections/home-companion.zip", view_func=home_companion_zip_response)
        reply = app.test_client().get("/connections/home-companion.zip", base_url="https://finder.example.test")
        self.assertEqual(reply.status_code, 200)
        self.assertIn("private", reply.headers["Cache-Control"])
        with zipfile.ZipFile(io.BytesIO(reply.data)) as bundle:
            self.assertEqual(set(bundle.namelist()), {"manifest.json", "dashboard.json", *ASSETS})
            self.assertIsNone(bundle.testzip())
            self.assertEqual(json.loads(bundle.read("dashboard.json")), {"origin":"https://finder.example.test"})
            manifest = json.loads(bundle.read("manifest.json"))
            self.assertEqual(manifest["permissions"], ["activeTab", "scripting", "storage"])
            self.assertEqual(manifest["host_permissions"], ["https://finder.example.test/*"])
            self.assertEqual(set(manifest["optional_host_permissions"]), {"https://vinted.co.uk/*","https://www.vinted.co.uk/*"})

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_pair_observe_revoke_lifecycle(self):
        subprocess.run([shutil.which("node"),str(ROOT/"tests/companion/worker.cjs")],check=True)


class ChromeHomeDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("Playwright unavailable")
        binary = os.getenv("CHROME_BIN") or shutil.which("chromium") or shutil.which("google-chrome")
        if not binary:
            raise unittest.SkipTest("Local Chromium unavailable")
        cls.api = sync_playwright().start()
        cls.browser = cls.api.chromium.launch(headless=True, executable_path=binary, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.api.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        def card(item_id, stamp, **kwargs):
            badge = kwargs.get("badge","")
            testid = kwargs.get("testid","item-uploaded")
            date = f'<time data-testid="{testid}" datetime="{stamp}">Uploaded now</time>' if stamp else '<span>Uploaded just now</span>'
            return f'''<article data-testid="feed-item"><a href="/items/{item_id}-skirt" title="Printed skirt">View item</a>
                <span data-testid="feed-item--price">£7.50</span><span data-testid="feed-item--total-price">£8.58</span>
                <span data-testid="item-size">S / UK 8–10</span><span data-testid="item-department">Women</span>
                {date}<div>{badge}</div></article>'''
        now = datetime.now(timezone.utc)
        fresh = (now-timedelta(seconds=40)).isoformat()
        stale = (now-timedelta(minutes=5)).isoformat()
        html = ('''<!doctype html><meta charset="utf-8"><header><nav><a href="/member/42-local_member">My profile</a></nav></header>
            <main><a href="/member/887-seller">My profile</a><section><h2>Recommended for you</h2>'''
            +card(101,fresh)+card(102,stale)+card(103,None)+card(104,fresh,badge="Sold")+
            '<h2>Popular brands</h2>'+card(105,fresh)+'</section></main>')
        self.page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
        self.page.goto("https://www.vinted.co.uk/")
        self.page.add_script_tag(path=str(ROOT/"home_companion/home.js"))

    def tearDown(self):
        self.page.close()

    def test_own_profile_and_actual_item_price_and_time(self):
        self.assertEqual(self.page.evaluate("finderHomeCompanion.identity()"),{"id":"42","name":"local_member"})
        self.assertFalse(self.page.evaluate("finderHomeCompanion.matchesAccount({id:'887',name:'seller'})"))
        self.page.locator("header").evaluate("el=>el.remove()")
        self.assertIsNone(self.page.evaluate("finderHomeCompanion.identity()"))
        result = self.page.evaluate("finderHomeCompanion.capture()")
        self.assertEqual([x["id"] for x in result["items"]],["101"])
        item = result["items"][0]
        self.assertEqual(item["price_gbp"],7.5)
        self.assertEqual(item["size"],"S / UK 8–10")
        self.assertEqual(item["category"],"Women")
        self.assertEqual(result["noTime"],2)
        self.assertEqual(item["listed_at"],self.page.evaluate("Math.floor(Date.parse(document.querySelector('time').dateTime)/1000)"))

    def test_account_switch_and_nonhome_rejected(self):
        self.page.evaluate("history.pushState({},'', '/checkout')")
        self.assertIsNone(self.page.evaluate("finderHomeCompanion.identity()"))
        self.assertFalse(self.page.evaluate("finderHomeCompanion.start({id:'42',name:'local_member'},'demo')"))
        self.page.evaluate("history.pushState({},'', '/')")
        self.page.locator('header a').evaluate("a=>a.href='/member/99-other'")
        self.assertFalse(self.page.evaluate("finderHomeCompanion.matchesAccount({id:'42',name:'local_member'})"))


if __name__=="__main__":
    unittest.main()
