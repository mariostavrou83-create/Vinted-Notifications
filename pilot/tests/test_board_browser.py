"""Opt-in browser integration. Set FINDER_TEST_CHROME to a Chromium executable."""
import os
import secrets
import tempfile
import threading
import time
import unittest
from pathlib import Path

from pilot import flow
from pilot.app import create_app
from pilot.db import get_db


@unittest.skipUnless(os.environ.get("FINDER_TEST_CHROME"), "set FINDER_TEST_CHROME to run browser integration")
class LiveBoardBrowserTests(unittest.TestCase):
    def test_new_listings_reach_active_board_and_retain_unchanged_card(self):
        from playwright.sync_api import sync_playwright
        from waitress import create_server

        with tempfile.TemporaryDirectory() as directory:
            app = create_app({"TESTING": True, "DATABASE": str(Path(directory) / "finder.sqlite")})
            code = app.test_cli_runner().invoke(args=["invite"]).output.strip()
            server = create_server(app, host="127.0.0.1", port=0, threads=3)
            self.addCleanup(server.close)
            threading.Thread(target=server.run, daemon=True).start()
            address = f"http://127.0.0.1:{server.effective_port}"

            def record(idx, home):
                now = int(time.time())
                item = {"id": idx, "title": f"Browser test listing {idx}", "currency": "GBP", "price_pence": 500,
                        "listing_url": f"https://www.vinted.co.uk/items/{idx}-test", "listed_at": now}
                with app.app_context():
                    record_item = flow.record_manual_home_listing if home else flow.record_public_listing
                    self.assertTrue(record_item(get_db(), 1, item, {}, now))

            with sync_playwright() as driver:
                browser = driver.chromium.launch(
                    executable_path=os.environ["FINDER_TEST_CHROME"],
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process"],
                )
                page = browser.new_page()
                page.goto(address + "/signup")
                page.locator("[name=invite]").fill(code)
                page.locator("[name=username]").fill("board-test")
                page.locator("[name=password]").fill(secrets.token_urlsafe(22))
                page.get_by_role("button", name="Create account").click()
                page.get_by_role("heading", name="No listings yet").wait_for()

                def version(response):
                    return response.url.endswith("/api/board-version") and response.status == 200

                # Insert immediately after the last poll to test near-worst-case latency.
                with page.expect_response(version):
                    page.get_by_role("button", name="Home", exact=True).click()
                record(101, True)
                start = time.monotonic()
                page.get_by_role("heading", name="Browser test listing 101").wait_for(timeout=2200)
                self.assertLess(time.monotonic() - start, 2.2)
                first = page.locator('[data-alert-id]').first
                first.locator("summary").click()
                first.evaluate("node => window.originalBoardCard = node")

                with page.expect_response(version):
                    page.get_by_role("button", name="Saved searches", exact=True).click()
                record(102, False)
                start = time.monotonic()
                page.get_by_role("heading", name="Browser test listing 102").wait_for(timeout=6500)
                self.assertLess(time.monotonic() - start, 6.5)
                self.assertTrue(page.evaluate("window.originalBoardCard.isConnected && window.originalBoardCard.querySelector('details').open"))
                self.assertEqual(page.locator(".feed-card:visible").count(), 1)
                page.get_by_role("button", name="All listings").click()
                self.assertEqual(page.locator(".feed-card:visible").count(), 2)
                browser.close()
