"""Local Chrome integration for the Home sharing controls and both extension downloads."""
import json
import os
import secrets
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

from pilot import flow, home_groups
from pilot.app import create_app
from pilot.db import get_db


@unittest.skipUnless(os.environ.get("FINDER_TEST_CHROME"), "set FINDER_TEST_CHROME to run browser integration")
class HomeSharingBrowserTests(unittest.TestCase):
    def test_select_a_home_and_manage_its_destinations(self):
        from playwright.sync_api import expect, sync_playwright
        from waitress import create_server

        with tempfile.TemporaryDirectory() as directory:
            app = create_app({"TESTING": True, "DATABASE": str(Path(directory) / "pilot.db"),
                              "PILOT_TELEGRAM_BOT_TOKEN": "9876543:example",
                              "PILOT_TELEGRAM_BOT_USERNAME": "PilotUnitBot", "PILOT_TELEGRAM_MODE": "polling"})
            invite = app.test_cli_runner().invoke(args=["invite"]).output.strip()
            server = create_server(app, host="127.0.0.1", port=0, threads=5)
            self.addCleanup(server.close)
            threading.Thread(target=server.run, daemon=True).start()
            address = f"http://127.0.0.1:{server.effective_port}"
            with sync_playwright() as pw:
                chrome = pw.chromium.launch(executable_path=os.environ["FINDER_TEST_CHROME"],
                                            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
                self.addCleanup(server.task_dispatcher.shutdown, cancel_pending=True)
                page = chrome.new_page(viewport={"width": 390, "height": 844})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(address + "/signup")
                page.locator("[name=invite]").fill(invite)
                page.locator("[name=username]").fill("source-user")
                page.locator("[name=password]").fill(secrets.token_urlsafe(22))
                page.get_by_role("button", name="Create account").click()
                page.get_by_role("heading", name="No listings yet").wait_for()
                with app.app_context():
                    conn = get_db()
                    conn.execute("INSERT INTO users VALUES(2,'receiver','test-only')")
                    conn.executemany("INSERT INTO pilot_chats VALUES(?,?)", [(1,101),(2,102)])
                client = app.test_client()
                with client.session_transaction() as session:
                    session["user_id"], session["csrf_token"] = 2, "test-only"
                recipient = client.post("/api/home-routes/invite", json={},
                                        headers={"X-CSRF-Token":"test-only"}).json["code"]

                page.goto(address + "/#home-sharing")
                expect(page.get_by_role("heading", name="Home sharing", exact=True)).to_be_visible()
                page.get_by_role("button", name="Create recipient code").click()
                expect(page.locator("#home-code")).not_to_have_text("")
                for path, expected_worker in [("/extension/download", "native-worker.js"),
                                               ("/extension/home-companion.zip", "worker.js")]:
                    with page.expect_download() as download:
                        page.evaluate("path => location.assign(path)", path)
                    with ZipFile(download.value.path()) as package:
                        manifest = json.loads(package.read("manifest.json"))
                        self.assertEqual(manifest["background"]["service_worker"], expected_worker)
                        self.assertIn(expected_worker, package.namelist())

                label = "Weekend Home"
                page.get_by_label("Vinted profile label").fill(label)
                page.get_by_role("button", name="Get Home pairing code").click()
                code_display = page.locator("#home-extra-code")
                expect(code_display).not_to_have_text("")
                first_code = code_display.text_content()
                self.assertEqual(client.post("/api/home/companion/pair",
                                             json={"code": first_code,"account_id":"221"}).status_code, 200)
                page.reload()
                page.get_by_label("Home to share").select_option(label=label)
                page.get_by_label("Recipient code", exact=True).fill(recipient)
                page.get_by_role("button", name="Share Home alerts").click()
                expect(page.locator("#home-outgoing")).to_contain_text("receiver")
                self.assertEqual(client.get("/api/home-routes").json["incoming"][0]["home"], label)

                page.get_by_role("button", name="Reconnect " + label).click()
                expect(code_display).not_to_have_text("")
                renewed = code_display.text_content()
                self.assertNotEqual(renewed, first_code)
                self.assertEqual(client.post("/api/home/companion/pair",
                                             json={"code": renewed,"account_id":"222"}).status_code, 403)
                self.assertEqual(client.post("/api/home/companion/pair",
                                             json={"code": renewed,"account_id":"221"}).status_code, 200)

                page.get_by_label("Group’s Home feed").select_option(label=label)
                page.get_by_role("button", name="Connect a Telegram group").click()
                group_url = page.get_by_role("link", name="Choose group in Telegram")
                expect(group_url).to_be_visible()
                uri = urlsplit(group_url.get_attribute("href"))
                self.assertEqual(uri.netloc, "t.me")
                group_code = parse_qs(uri.query)["startgroup"][0]
                with app.app_context(), patch("pilot.flow_worker.telegram", return_value=[
                        {"status": "administrator", "user": {"id": 101}}]):
                    conn = get_db()
                    flow.telegram_update(conn, {"update_id": 1, "message": {"message_id":9,
                        "chat": {"id": -199, "type": "group", "title":"Team alerts"},
                        "from": {"id":101,"is_bot":False}, "text":"/start@PilotUnitBot " + group_code}})
                    self.assertTrue(home_groups.verify(conn, time.time()))
                page.reload()
                expect(page.locator("#home-groups")).to_contain_text("Team alerts")
                page.get_by_role("button", name="Stop sharing · Team alerts").click()
                expect(page.locator("#home-groups li")).to_have_count(0)
                page.locator("#home-outgoing button").click()
                expect(page.locator("#home-outgoing li")).to_have_count(0)
                self.assertEqual(client.get("/api/home-routes").json["incoming"], [])
                page.get_by_role("button", name="Remove profile", exact=False).click()
                expect(page.locator("#home-extras li")).to_have_count(0)
                self.assertEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
                self.assertEqual(errors, [])
                chrome.close()
