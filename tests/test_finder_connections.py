import time
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lxml import html

import alert_controls
import finder_connections as connections
import finder_group_notices as notices
import search_settings
import telegram_reconnect
import telegram_shared_alerts as shared
from test_search_controls import DatabaseFixture
from vinted_accounts import preserve_seller


class PersonalConnectionsTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET session=?,user_id='seller',username='seller',verified_at=? WHERE id=1",
                         (b"fixture", self.now - 20))
            preserve_seller(conn)
            conn.execute("UPDATE vinted_buyer SET user_id='buyer',username='buyer' WHERE id=1")

    def test_hold_survives_restart_and_requires_verification_of_each_account(self):
        with closing(search_settings.connection()) as conn, conn:
            self.assertTrue(connections.allows_listing(self.now, conn))
            connections.mark_disconnected(conn, "buyer", self.now)
            connections.mark_disconnected(conn, "seller", self.now + 1)
            self.assertFalse(connections.allows_listing(self.now + 2, conn))
            conn.execute("UPDATE vinted_buyer SET verified_at=? WHERE id=1", (self.now + 3,))
            self.assertEqual(connections.gate(conn)[0], ("seller",))
            conn.execute("UPDATE vinted_seller SET session=? WHERE id=1", (b"still-not-identity",))
            connections.migrate(conn)
            self.assertFalse(connections.allows_listing(self.now + 4, conn))
            conn.execute("UPDATE vinted_seller SET verified_at=? WHERE id=1", (self.now + 5,))
            self.assertFalse(connections.allows_listing(self.now + 4, conn))
            self.assertTrue(connections.allows_listing(self.now + 6, conn))
        alert_controls.set_paused("home", True)
        self.assertTrue(alert_controls.state("home").paused)

    def test_upgrade_adopts_current_refusal_and_explicit_disconnect(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("""UPDATE vinted_seller_connection_notice SET buyer_id='seller', reason='credentials',
                stage='identity',http_status=401,identity_rejected=1,checked=? WHERE id=1""", (self.now,))
            connections.migrate(conn)
            self.assertEqual(connections.gate(conn)[0], ("seller",))
            conn.execute("UPDATE vinted_buyer SET session=NULL,verified_at=NULL,user_id=NULL WHERE id=1")
            self.assertEqual(set(connections.gate(conn)[0]), {"buyer", "seller"})
        self.assertFalse(connections.is_disconnect("network", "request", 503))
        self.assertFalse(connections.is_disconnect("forbidden", "payment", 403))
        self.assertFalse(connections.is_disconnect("renewal_failed", "renewal", 400))

    def test_queued_private_and_routed_listings_are_blocked_but_errors_can_send(self):
        import alert_delivery
        import finder_incidents
        import home_routing_delivery
        import home_routing_store
        import vinted_alerts
        group = shared.save_group(-655,123,456)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("""INSERT INTO alert_outbox(item_id,query_id,search_name,content,url,title,price,
                currency,found_at) VALUES('471',1,'Search','Listing','link','title','8','GBP',?)""", (self.now,))
        leased = alert_delivery.claim(now=self.now + 1)
        with closing(search_settings.connection()) as conn, conn:
            connections.mark_disconnected(conn,"seller",self.now+1)
            self.assertIsNone(home_routing_delivery._destination(conn, {
                "owner":home_routing_store.owner_id(conn), "chat":group.chat,"item":"471","expires":self.now+180
            },self.now+2))
            finder_incidents.enqueue(conn,"hold","Listing alerts","Listing alerts paused",self.now+2,
                                     target="seller_reconnect")
            row = conn.execute("SELECT body FROM finder_group_notices WHERE version=?", (group.version,)).fetchone()
            self.assertEqual(row[0], "Listing alerts paused")
        self.assertFalse(vinted_alerts.prepare_listing(leased))
        with closing(search_settings.connection()) as conn:
            row = conn.execute("SELECT status,telegram_message_id FROM alert_outbox WHERE item_id='471'").fetchone()
            self.assertEqual(tuple(row),("cancelled",None))

    def test_three_visible_entry_points_only_and_separate_post_handlers(self):
        from test_dashboard import DashboardTests
        from web_ui_plugin.web_ui import create_app
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()
        DashboardTests.owner(self)
        page = html.fromstring(self.client.get("/connections").data)
        cards = page.xpath('//section[contains(@class,"service-card")]')
        self.assertEqual([c.xpath("string(h2)") for c in cards], [v[0] for v in connections.SERVICES.values()])
        self.assertFalse(page.xpath('//details[@id="other-connection-settings"]/@open'))
        self.assertFalse(page.xpath('//details[not(ancestor::details) and @id!="other-connection-settings"]'))
        for service, (_, role) in connections.SERVICES.items():
            url = "/connections?reconnect=" + service
            self.assertTrue(page.xpath('//a[@href=$url]', url=url))
            form = html.fromstring(self.client.get(url).data)
            self.assertEqual(form.xpath('//main//button[@name="action"]/@value'), [role+"_recheck", role+"_signin", role+"_session_records"])
            wrong = ("buyer" if role == "seller" else "seller") + "_recheck"
            self.assertEqual(self.client.post(url, data={"csrf":"offline-csrf","action":wrong}).status_code, 400)
        self.assertEqual(self.client.get("/connections?reconnect=other").status_code, 404)

    def test_both_opted_in_home_feeds_use_separate_accounts_and_one_delivery_per_item(self):
        import vinted_home_activity as activity
        import vinted_home_controls as controls
        import vinted_home_monitor as monitor
        import vinted_alerts
        from vinted_accounts import account_scope

        self.assertFalse(controls.source_selected("buyer"))
        controls.configure_sources("1")
        controls.configure("1")
        self.now = time.time()
        self.assertEqual(controls.source_roles(), ("seller", "buyer"))
        for role, name in (("seller","vinted_seller"),("buyer","vinted_buyer")):
            client = SimpleNamespace(account_role=role, session=SimpleNamespace(close=lambda: None))
            with patch(name+".exclusive",side_effect=lambda:account_scope(role)), \
                 patch(name+".connected_client",return_value=client) as identity, \
                 patch.object(monitor,"yield_to_purchase"), patch.object(monitor,"read_html",return_value=("page",self.now)):
                self.assertEqual(monitor._page(role, "https://www.vinted.co.uk/"), ("page", self.now))
                identity.assert_called_once_with(solve_challenges=False,allow_refresh=False)
        item={"id":"123","title":"Dress","url":"https://www.vinted.co.uk/items/123-dress",
              "price":{"amount":"2","currency_code":"GBP"},"category_ids":["1904","4","13"],"brand_title":"Test",
              "size_title":"UK 8","photos":[],"description_complete":False}
        age={"verified":True,"checked_at":self.now,"max_age_seconds":10}
        self.assertTrue(monitor._publish(item,None,None,age,"buyer",now=self.now))
        self.assertFalse(monitor._publish(item,None,None,age,"seller",now=self.now))
        details = vinted_alerts.get_details({"item_id":"123"})
        self.assertTrue(vinted_alerts.home_discovery_fresh(details,now=self.now))
        from vinted_budget import home_cap
        self.assertEqual(home_cap(details),details["budget"]["total"])
        self.assertEqual({a["role"] for a in controls.summary()["accounts"]},{"seller","buyer"})
        with closing(search_settings.connection()) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM alert_outbox WHERE "+activity.HOME_ALERT).fetchone()[0],1)
        controls.configure_sources("0")
        self.assertFalse(vinted_alerts.home_discovery_fresh(vinted_alerts.get_details({"item_id":"123"}),now=self.now))

    def test_phone_signin_and_code_are_bound_to_selected_existing_account(self):
        import vinted_buyer as buyer
        from test_dashboard import DashboardTests
        from web_ui_plugin.web_ui import create_app

        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()
        DashboardTests.owner(self)
        for service, role in (("alerts", "seller"), ("autobuy", "buyer")):
            url = "/connections?reconnect=" + service
            with closing(search_settings.connection()) as conn, conn:
                connections.mark_disconnected(conn, role, self.now)
            with patch.object(buyer, "Client") as provider:
                client = provider.return_value
                client.account_role = role
                client.request.return_value = {"challenge_id":"offline"}
                client.exported.return_value = {"cookies":{}, "csrf":"test-csrf"}
                response = self.client.post(url, data={"csrf":"offline-csrf", "action":role+"_signin",
                                            "username":"other_account", "password":"offline-password"})
                self.assertEqual(response.status_code,303)
                self.assertIn(url,response.location)
                self.assertEqual(client.request.call_args.args[2]["username"],role)
                self.assertTrue(client.solver_attempted)
                page = self.client.get(url).data
                self.assertIn(b'autocomplete="one-time-code"',page)
                self.assertNotIn(b"offline-password",page)
                other = "offers" if role == "buyer" else "autobuy"
                self.assertNotIn(b'autocomplete="one-time-code"',self.client.get("/connections?reconnect="+other).data)
                client.request.return_value = {}
                client.identity.return_value = ("unrelated", "unrelated")
                payload = {"csrf":"offline-csrf","action":role+"_code","code":"123456"}
                self.client.post(url, data=payload)
                self.assertIn(role,connections.gate()[0])
                client.identity.return_value = (role,role)
                done = self.client.post(url, data=payload)
                self.assertEqual(done.location, "/connections")
                self.assertNotIn(role,connections.gate()[0])


class GroupAndBotTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    async def test_group_error_is_deduplicated_and_never_sent_to_a_replacement_group(self):
        group = shared.save_group(-654,123,456)
        bot = SimpleNamespace(send_message=AsyncMock())
        now = time.time()
        for _ in range(2):
            notices.queue("incident:one", "Listing alerts paused", now)
        self.assertTrue(await notices.deliver(bot, group))
        self.assertFalse(await notices.deliver(bot, group))
        self.assertEqual(bot.send_message.await_count,1)
        self.assertEqual(bot.send_message.await_args.kwargs["chat_id"], -654)
        notices.queue("incident:two", "Example error", time.time())
        shared.unlink()
        self.assertFalse(await notices.deliver(bot, group))

    async def test_group_photo_rejection_rechecks_connection_before_text_fallback(self):
        from telegram.error import BadRequest
        group = shared.save_group(-654,123,456)
        row = dict(item_id="471",found_at=time.time(),platform="vinted",content="Listing",
                   body="Listing",photo_id="offline",title="Dress",url="https://example.test/dress",attempts=0)
        allowed = True
        async def photo(**kwargs):
            nonlocal allowed
            allowed = False
            raise BadRequest("Offline rejected photo")
        bot = SimpleNamespace(send_photo=AsyncMock(side_effect=photo),send_message=AsyncMock())
        with patch("finder_connections.allows_listing",side_effect=lambda *args: allowed), \
             patch("vinted_alerts.get_details",return_value={}), patch("vinted_buying.result",return_value=None), \
             patch("home_routing_delivery.native_transport",new=AsyncMock(return_value=bot)), \
             patch("telegram_shared_alerts.finish") as finish:
            await shared.send(bot,group,row)
        bot.send_photo.assert_awaited_once()
        bot.send_message.assert_not_awaited()
        finish.assert_called_once_with(row,failed=True)

    async def test_telegram_verification_is_owner_only_and_uses_the_selected_account(self):
        chat = SimpleNamespace(id=123,type="private")
        user = SimpleNamespace(id=123,is_bot=False)
        for service, (_, role) in connections.SERVICES.items():
            query = SimpleNamespace(data="finder:reconnect:"+service, answer=AsyncMock(),
                                    message=SimpleNamespace(reply_text=AsyncMock()))
            update = SimpleNamespace(effective_chat=chat,effective_user=user,callback_query=query)
            with patch("vinted_seller.check_saved_connection") as sell, patch("vinted_buyer.check_saved_connection") as buy:
                await telegram_reconnect.check(update,None)
                self.assertEqual(sell.call_count, int(role=="seller"))
                self.assertEqual(buy.call_count, int(role=="buyer"))
                user.id=456
                await telegram_reconnect.check(update,None)
                self.assertEqual(query.message.reply_text.await_count,1)
                user.id=123
