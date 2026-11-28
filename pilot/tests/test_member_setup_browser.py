"""Exercise a new member's actual setup forms and the separate administrator login in Chromium."""
import os
import re
import secrets
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from pilot import flow
from pilot.app import create_app, digest
from pilot.db import get_db, settings_for


@unittest.skipUnless(os.environ.get("FINDER_TEST_CHROME"), "set FINDER_TEST_CHROME to run buyer setup in a browser")
class MemberSetupBrowserTests(unittest.TestCase):
    def test_admin_can_verify_bot_from_the_https_dashboard(self):
        from cryptography.fernet import Fernet
        from playwright.sync_api import expect, sync_playwright
        from werkzeug.security import generate_password_hash
        from werkzeug.serving import make_server

        password = secrets.token_urlsafe(24)
        bot_name = "FinderFixtureBot"
        bot_token = "12345:" + secrets.token_urlsafe(32)
        with tempfile.TemporaryDirectory() as folder:
            app = create_app({"TESTING": True, "MULTIPLE_ACCOUNTS": True,
                              "DATABASE": str(Path(folder) / "test.db"),
                              "FINDER_PILOT_CONFIG_KEY": Fernet.generate_key().decode()})
            with app.app_context():
                get_db().execute("INSERT INTO finder_admin VALUES(1,?,?,?)",
                                 ("finder-admin", generate_password_hash(password), secrets.token_hex(32)))
            with make_server("127.0.0.1", 0, app, ssl_context="adhoc", threaded=True) as server:
                url = f"https://127.0.0.1:{server.server_port}"
                app.config["FINDER_PUBLIC_URL"] = url
                threading.Thread(target=server.serve_forever, daemon=True).start()
                try:
                    with sync_playwright() as pw:
                        browser = pw.chromium.launch(executable_path=os.environ["FINDER_TEST_CHROME"],
                            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process"])
                        page = browser.new_page(ignore_https_errors=True)
                        page.goto(url + "/admin")
                        page.get_by_label("Admin username").fill("finder-admin")
                        page.get_by_label("Admin password").fill(password)
                        page.get_by_role("button", name="Sign in as admin").click()
                        page.get_by_label("Bot username").fill(bot_name)
                        page.get_by_label("BotFather token").fill("incomplete")
                        page.get_by_label("This bot is dedicated").check()
                        submit = page.get_by_role("button", name="Verify and connect bot")
                        with page.expect_response(lambda r: r.url == url + "/team/telegram") as result:
                            submit.click()
                        self.assertEqual(result.value.status, 400)
                        expect(page.get_by_label("BotFather token")).to_have_value("")
                        page.get_by_label("Bot username").fill(bot_name)
                        page.get_by_label("BotFather token").fill(bot_token)
                        page.get_by_label("This bot is dedicated").check()
                        me = {"id": 12345, "username": bot_name, "is_bot": True}
                        with patch("pilot.flow_worker.telegram", side_effect=[me, {"url": ""}]) as telegram:
                            submit.click()
                            expect(page.get_by_text("Connected to @" + bot_name, exact=False)).to_be_visible()
                        self.assertEqual([call.args[0] for call in telegram.call_args_list], ["getMe", "getWebhookInfo"])
                        expect(page.get_by_label("BotFather token")).to_have_value("")
                        browser.close()
                finally:
                    server.shutdown()

    def test_phone_setup_delivery_and_admin(self):
        from playwright.sync_api import expect, sync_playwright
        from waitress import create_server

        code, owner_password, admin_password = (secrets.token_urlsafe(24) for _ in range(3))
        with tempfile.TemporaryDirectory() as folder:
            app = create_app({"TESTING":True, "MULTIPLE_ACCOUNTS":True, "FINDER_SUPPORT_EMAIL":"support@example.test",
                              "DATABASE":str(Path(folder) / "test.db"),
                              "FINDER_PILOT_OWNER_INVITE_HASH":digest(code)})
            server = create_server(app, host="127.0.0.1", port=0, threads=3)
            self.addCleanup(server.close)
            threading.Thread(target=server.run,daemon=True).start()
            url = f"http://127.0.0.1:{server.effective_port}"
            app.config["FINDER_PUBLIC_URL"] = url

            def directory(method,path,payload):
                self.assertEqual(path,"/v1/delivery/destinations")
                choices = [{"id":"nearby","label":"Nearby locker","couriers":["InPost"]}]
                if payload.get("query")=="W9":
                    choices.append({"id":"verified","label":"Four-courier shop","couriers":["InPost","Royal Mail","Relay","Evri"]})
                return {"account_ref":"account-1","destinations":choices}

            with sync_playwright() as pw, patch("pilot.destinations.partner_request", side_effect=directory):
                browser = pw.chromium.launch(executable_path=os.environ["FINDER_TEST_CHROME"],
                    args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu","--single-process"])
                page = browser.new_page(viewport={"width":390,"height":844})
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.goto(url+"/signup")
                page.get_by_label("Invite code").fill(code)
                page.locator("[name=username]").fill("finder-owner")
                page.locator("[name=password]").fill(owner_password)
                page.get_by_role("button", name="Create account").click()
                page.wait_for_url("**/setup")
                expect(page.get_by_role("heading",name="Set up Finder")).to_be_visible()
                expect(page.get_by_text("0 of 4 steps complete")).to_be_visible()
                self.assertEqual(page.locator(".setup-steps h2").all_text_contents()[:2],
                                 ["Link your Telegram","Connect your Vinted buying account"])
                expect(page.get_by_text("Awaiting Telegram",exact=True)).to_be_visible()
                expect(page.get_by_role("button",name="Link Telegram",exact=True)).to_have_count(0)
                page.get_by_role("link",name="Choose filters").click()
                page.locator('[data-filter="categories"] summary').click()
                for name in ("Women / Dresses","Women / Skirts"):
                    page.get_by_role("checkbox",name=name,exact=True).check()
                page.locator('[data-filter="sizes"] summary').click()
                expect(page.get_by_role("checkbox",name="UK shoe 7",exact=True)).to_have_count(0)
                for name in ("UK 8","UK 10"):
                    page.get_by_role("checkbox",name=name,exact=True).check()
                page.keyboard.press("Escape")
                page.get_by_label("Quiet hours start").select_option("23:00")
                page.get_by_label("Quiet hours end").select_option("07:00")
                page.get_by_role("button",name="Save Home filters").click()
                page.wait_for_url("**/setup")
                expect(page.get_by_text("1 of 4 steps complete")).to_be_visible()
                with app.app_context():
                    self.assertEqual(settings_for(get_db(),1)["sizes"],["UK 8","UK 10"])
                page.get_by_role("link",name="Delivery settings").click()
                expect(page.get_by_role("button",name="Find pickup points")).to_be_disabled()
                page.get_by_label("Delivery method").select_option("home")
                expect(page.get_by_role("button",name="Choose a saved address")).to_be_visible()
                expect(page.get_by_text("Only shops or lockers",exact=False)).to_be_hidden()

                app.config.update(FINDER_PARTNER_URL="https://fixture.example",FINDER_PARTNER_TOKEN="test")
                with app.app_context():
                    get_db().execute("INSERT INTO pilot_buyer_link VALUES(1,'account-1','vinted-member',0)")
                page.goto(url+"/setup")
                page.get_by_role("link",name="Delivery settings").click()
                postcode=page.get_by_label("Find a shop or locker")
                postcode.fill("W2")
                page.get_by_role("button",name="Find pickup points").click()
                expect(page.get_by_text("No point in these results",exact=False)).to_be_visible()
                postcode.fill("W9")
                page.get_by_role("button",name="Find pickup points").click()
                page.get_by_role("button",name="Four-courier shop",exact=False).click()
                page.get_by_label("Preferred courier").select_option("Relay")
                page.get_by_role("button",name="Save preferences").click()
                page.wait_for_url("**/setup")
                expect(page.get_by_text("3 of 4 steps complete")).to_be_visible()
                page.get_by_role("link",name="Delivery settings").click()
                expect(page.get_by_label("Preferred courier")).to_have_value("Relay")
                app.config.update(PILOT_TELEGRAM_MODE="polling", PILOT_TELEGRAM_BOT_USERNAME="ExampleBot",
                                  PILOT_TELEGRAM_BOT_TOKEN="1234:fixture")
                page.goto(url + "/setup")
                page.get_by_role("button", name="Link Telegram").click()
                page.wait_for_url("**/setup/telegram")
                app_link = page.get_by_role("link", name="Open Telegram app")
                code = parse_qs(urlsplit(app_link.get_attribute("href")).query)["start"][0]
                web_url = urlsplit(page.get_by_role("link", name="Open Telegram Web").get_attribute("href"))
                self.assertEqual(web_url.netloc, "web.telegram.org")
                deep_link = parse_qs(web_url.fragment.removeprefix("?"))["tgaddr"][0]
                self.assertEqual(parse_qs(urlsplit(deep_link).query), {"domain":["ExampleBot"], "start":[code]})
                page.reload()
                expect(app_link).to_have_attribute("href", "https://t.me/ExampleBot?start=" + code)
                page.get_by_text("Chat didn’t open or no reply?").click()
                expect(page.get_by_label("One-time message")).to_have_value("/start " + code)
                page.get_by_role("button", name="Copy message").click()
                expect(page.locator("#link-copy-status")).to_have_text(re.compile("Copied|selected"))
                page.get_by_role("link", name="Back to setup").click()
                page.get_by_role("link", name="Connected accounts").click()
                expect(page.get_by_role("heading", name="Connected accounts")).to_be_visible()
                page.locator("#account").get_by_role("button", name="Link Telegram").click()
                expect(app_link).to_have_attribute("href", "https://t.me/ExampleBot?start=" + code)
                with app.app_context():
                    msg = {"chat":{"type":"private","id":12345},"from":{"id":12345},"text":"/start " + code}
                    self.assertIn("connected", flow.telegram_update(get_db(), {"message":msg})["text"])
                expect(page.get_by_role("heading", name="Telegram linked")).to_be_visible(timeout=10000)
                page.get_by_role("link", name="Continue setup").click()
                expect(page.get_by_text("4 of 4 steps complete")).to_be_visible()
                page.goto(url+"/admin")
                page.get_by_role("link",name="Create admin login").click()
                page.get_by_label("Existing Finder owner username").fill("finder-owner")
                page.get_by_label("Existing owner password").fill(owner_password)
                page.get_by_label("Admin username").fill("finder-admin")
                page.get_by_label("New admin password").fill(admin_password)
                page.get_by_role("button",name="Create admin login").click()
                page.wait_for_url("**/admin")
                expect(page.get_by_role("heading",name="Admin dashboard")).to_be_visible()
                expect(page.get_by_text("1 of 10 buyer places used",exact=False)).to_be_visible()
                expect(page.get_by_role("rowheader",name="finder-owner")).to_be_visible()
                expect(page.get_by_text("4 of 4 complete")).to_be_visible()
                for width in (390,1280):
                    page.set_viewport_size({"width":width,"height":844})
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"),width)
                page.get_by_role("button",name="Pause access").click()
                expect(page.get_by_text("Access paused for finder-owner.")).to_be_visible()
                page.get_by_role("link",name="Delete account").click()
                page.get_by_role("link",name="Cancel").click()
                expect(page.get_by_role("button",name="Resume access")).to_be_visible()
                page.get_by_role("button",name="Sign out of admin").click()
                page.get_by_role("link",name="Buyer sign-in").click()
                page.locator("[name=username]").fill("finder-owner")
                page.locator("[name=password]").fill(owner_password)
                page.get_by_role("button",name="Sign in",exact=True).click()
                expect(page.get_by_role("heading",name="Your Finder access is paused")).to_be_visible()
                expect(page.get_by_role("link",name="support@example.test")).to_have_attribute("href","mailto:support@example.test")
                page.goto(url+"/admin/login")
                page.get_by_label("Admin username").fill("finder-admin")
                page.get_by_label("Admin password").fill(admin_password)
                page.get_by_role("button",name="Sign in as admin").click()
                page.get_by_role("button",name="Resume access").click()
                expect(page.get_by_text("Access resumed for finder-owner.")).to_be_visible()
                page.get_by_role("link",name="Delete account").click()
                page.locator("[name=confirm_name]").fill("finder-owner")
                page.get_by_role("button",name="Permanently delete Finder account").click()
                expect(page.get_by_text("0 of 10 buyer places used",exact=False)).to_be_visible()
                self.assertEqual(errors,[])
                browser.close()
