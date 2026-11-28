import json
import os
import time
import unittest
from contextlib import closing
from unittest.mock import patch

import alert_delivery
import db
import photo_cards
import search_settings
import vinted_alerts
import vinted_buying
import vinted_home_activity as activity
import vinted_home_controls as controls
import vinted_home_feed as feed
import vinted_home_monitor as monitor
from test_search_controls import DatabaseFixture
from test_vinted_home_feed import home, card, listing, native


class HomeRecommendationsTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        controls.configure("1")
        self.now = time.time()
        self.item = {"id":"8123", "url":"https://www.vinted.co.uk/items/8123-vintage-find", "title":"Vintage find",
                     "category_ids":["1904","4","13"], "brand_title":"", "size_title":"", "price":{"amount":"35.00","currency_code":"GBP"},
                     "photo":{"url":"https://images1.vinted.net/t/01_test/f800/image.webp"},
                     "description":"", "description_complete":False}
        self.age = {"verified":True, "max_age_seconds":20, "checked_at":self.now}
        self.worker = monitor.Monitor(clock=lambda:self.now)
        self.search = search_settings.get_search(1)

    def row(self):
        with closing(search_settings.connection()) as conn:
            return dict(conn.execute("SELECT * FROM alert_outbox WHERE item_id='8123'").fetchone())

    def new(self):
        self.worker.notify_item(self.item, self.age, "seller", [])

    def test_unguided_home_button_is_limited_to_the_displayed_total(self):
        self.new()
        self.worker.notify_item(self.item, self.age, "buyer", [])
        row = self.row()
        self.assertIsNone(row["query_id"])
        self.assertIn("Vinted Home · recommendation", row["content"])
        self.assertNotIn("saved buying criteria", row["content"])
        self.assertFalse(db.get_seen_item_ids(["8123"]))
        self.assertEqual(activity.alerted_ids(["8123"]), {"8123"})
        details = vinted_alerts.get_details(row)
        buttons = [btn for group in photo_cards.markup(row, details).inline_keyboard for btn in group]
        self.assertTrue(any(btn.url == self.item["url"] for btn in buttons))
        self.assertTrue(any(btn.callback_data == "buy:click" for btn in buttons))
        import vinted_budget
        self.assertEqual(vinted_budget.purchase_limits(row).total_maximum,details["budget"]["total"])
        ready = alert_delivery.claim(now=self.now + 1)
        self.assertTrue(vinted_alerts.prepare_listing(ready))
        controls.configure("0")
        self.assertFalse(vinted_alerts.prepare_listing(ready))

    @patch.dict(os.environ, {"MSJ_AUTOBUY_PRICE_POLICY": "postage_under_5"})
    def test_home_price_does_not_filter_alerts_and_tap_uses_postage_rule(self):
        import vinted_budget

        self.item["price"]["amount"] = "175.00"
        self.new()
        row = self.row()
        details = vinted_alerts.get_details(row)
        self.assertIsNone(row["query_id"])
        self.assertTrue(vinted_alerts.prepare_listing(alert_delivery.claim(now=self.now + 1)))
        limits = vinted_budget.purchase_limits(row)
        self.assertIsNone(limits.total_maximum)
        self.assertEqual(limits.shipping_maximum, 499)
        buttons = photo_cards.markup(row, details).inline_keyboard
        self.assertIn("Autobuy · shipping under £5", [button.text for group in buttons for button in group])

    def test_only_confirmed_womens_clothing_can_alert_regardless_of_the_title(self):
        for categories,labels in [(["2309","3037"],["Entertainment","Books"]),(["1904","19","156"],["Women","Bags"]),
                                  (["5","1859"],["Men","Clothes"]),([],["Kids","Clothes"]),([],[])]:
            row=dict(self.item, title="Vintage women's dress", category_ids=categories, category_labels=labels)
            self.worker.notify_item(row,self.age,"seller",[])
            self.assertEqual(activity.alerted_ids([row["id"]]),set())
        body=home(card()+native(created_at_ts=int(self.now)-15,catalog_id=9999))
        self.worker.fetch=lambda role,url:(body,self.now)
        self.worker.home("seller",self.now)
        self.assertEqual([i["id"] for i in self.worker.pending["seller"]],["123"])
        self.new()
        self.assertIs(vinted_alerts.get_details(self.row())["home_discovery"]["womens_clothing"],True)

    def test_guide_error_cannot_suppress_a_verified_home_listing(self):
        self.item["description_complete"] = True
        with patch.object(monitor, "global_filters_allow", return_value=True), patch.object(monitor, "match_candidate", side_effect=ValueError):
            self.worker.notify_item(self.item, self.age, "seller", [self.search])
        self.assertIsNone(self.row()["query_id"])

    def test_home_excludes_sizes_over_uk_14_from_either_size_label_or_title(self):
        for label in ("UK 14-16", "L / UK 16–18", "EU 44", "18", "XL", "XXL", "2XL", "L"):
            with self.subTest(label=label):
                self.worker.notify_item(dict(self.item, size_title=label), self.age, "seller", [])
                self.assertEqual(activity.alerted_ids([self.item["id"]]), set())
        self.worker.notify_item(dict(self.item, size_title="M", title="Vintage top size UK 16"), self.age, "seller", [])
        self.assertEqual(activity.alerted_ids([self.item["id"]]), set())
        self.worker.notify_item(dict(self.item, size_title="M / UK 12-14"), self.age, "seller", [])
        self.assertEqual(self.row()["status"], "pending")
        self.assertEqual(len(self.batch(1, [8124], title="Vintage top size XL")), 1)

    def test_paused_home_uses_no_requests_and_rejects_a_stale_delivery_lease_after_resume(self):
        from unittest.mock import Mock

        self.new()
        leased = alert_delivery.claim()
        self.worker.fetch = Mock()
        controls.configure("0")
        self.worker.tick()
        self.worker.fetch.assert_not_called()
        controls.configure("1")
        self.assertFalse(vinted_alerts.prepare_listing(leased))

    def test_buying_account_cannot_supply_a_page_or_send_a_pending_home_alert(self):
        self.worker.notify_item(self.item, self.age, "buyer", [])
        with closing(search_settings.connection()) as conn:
            self.assertFalse(conn.execute("SELECT 1 FROM alert_outbox").fetchone())
        with self.assertRaises(monitor.buyer.BuyerError), patch.object(monitor.seller,"exclusive") as lock:
            monitor._page("buyer","https://www.vinted.co.uk/")
        lock.assert_not_called()
        self.new()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_alert_details SET payload=json_set(payload,'$.home_discovery.source_role','buyer')")
            conn.execute("INSERT OR REPLACE INTO vinted_home_pages VALUES('buyer',?,'[]')",(self.now,))
        self.assertEqual(activity.panel(detail=True)["pages"], [])
        self.assertEqual(activity.panel(detail=True)["deals"], [])
        self.assertFalse(vinted_alerts.prepare_listing(alert_delivery.claim(now=self.now + 1)))

    def test_fresh_home_can_replace_an_unsent_cancelled_saved_match(self):
        monitor._publish(self.item,self.search,{"reason":"brand_style"},self.age,"seller",now=self.now)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE alert_outbox SET status='cancelled' WHERE item_id='8123'")
        self.new()
        row = self.row()
        self.assertEqual((row["status"],row["query_id"]), ("pending",None))
        self.assertEqual(vinted_alerts.get_details(row)["home_discovery"]["reason"],"recommendation")

    def test_native_old_unavailable_or_unconfirmed_stock_never_sends_on_card_alone(self):
        for identity, changes in (("192", {"created_at_ts":int(self.now)-300}),
                                  ("193", {"created_at_ts":int(self.now)-20, "is_reserved": True}),
                                  ("195", {"is_reserved": True}),
                                  ("194", {"created_at_ts":int(self.now)-20, "can_buy": None})):
            body=home(card(identity)+native(id=identity,**changes))
            self.worker.fetch=lambda role,url: (body,self.now)
            self.worker.home("seller",self.now)
            self.assertFalse(activity.alerted_ids([identity]))
            self.assertEqual(bool(self.worker.pending["seller"]),identity=="194")

    def test_exact_home_time_and_confirmed_listing_can_queue_until_real_expiry(self):
        self.now = int(self.now)
        created = self.now - 145
        page = home(card() + native(created_at_ts=created, can_buy=None, catalog_id=13))
        self.worker.fetch = lambda role, url: (page, self.now)
        self.worker.home("seller", self.now)
        self.assertFalse(activity.alerted_ids(["123"]))
        self.assertEqual([c["id"] for c in self.worker.pending["seller"]], ["123"])
        self.now += 2
        self.worker.fetch = lambda role, url: (listing("2 min ago"), self.now)
        self.worker.listing("seller", [], self.now)
        with closing(search_settings.connection()) as conn:
            row = dict(conn.execute("SELECT * FROM alert_outbox WHERE item_id='123'").fetchone())
        details = vinted_alerts.get_details(row)
        self.assertEqual(details["home_discovery"]["expires_at"], created + 180)
        self.assertTrue(vinted_alerts.home_discovery_fresh(details, now=created + 179))
        self.assertFalse(vinted_alerts.home_discovery_fresh(details, now=created + 180))

    def test_later_real_saved_match_enriches_sent_home_item_once(self):
        self.new()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE alert_outbox SET status='sent',telegram_message_id=222 WHERE item_id='8123'")
        details = vinted_alerts.get_details(self.row())
        details.pop("home_discovery")
        details.update(name="Named saved guide", buying_advice="Check the seam.")
        self.assertTrue(db.add_item_to_db("8123", "Vintage find", 1, "35.00", self.now,
            self.item["photo"]["url"], currency="GBP", alert={"search_name":"Named saved guide",
                "url":self.item["url"], "content":"saved", "vinted_details":details}))
        row = self.row()
        self.assertEqual((row["query_id"],row["telegram_message_id"],row["status"]),(1,222,"sent"))
        self.assertIn("Also matched your saved search", row["content"])
        self.assertNotIn("Check the seam",row["content"])
        with closing(search_settings.connection()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alert_outbox WHERE item_id='8123'").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT query_id FROM items WHERE item=8123").fetchone()[0],1)

    def test_saved_matches_first_and_latest_page_still_shows_it_in_vinted_order(self):
        monitor._publish(self.item,self.search,{"reason":"brand_style"},self.age,"seller",now=self.now)
        self.assertFalse(monitor._publish(self.item,None,None,self.age,"buyer",now=self.now))
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE alert_outbox SET status='sent',content='saved match' WHERE item_id='8123'")
        extra = dict(self.item,id="8125",url="https://www.vinted.co.uk/items/8125",title="Jeans size XL")
        activity.save_feed("seller",[self.item,extra],self.now)
        cards = activity.panel(True)["pages"][0]["cards"]
        self.assertEqual([c["id"] for c in cards], ["8123","8125"])
        self.assertEqual(cards[0]["sent_via"], "saved search")

    def test_guide_paused_before_delivery_becomes_general_fresh_recommendation(self):
        monitor._publish(self.item,self.search,{"reason":"brand_style"},self.age,"seller",now=self.now)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO search_dashboard(query_id,paused) VALUES(1,1)")
        claimed = alert_delivery.claim(now=self.now + 1)
        self.assertTrue(vinted_alerts.prepare_listing(claimed))
        self.assertIsNone(self.row()["query_id"])
        self.assertIn("Vinted Home · recommendation",self.row()["content"])

    def test_complete_home_card_can_alert_with_zero_listing_reads(self):
        stamp = int(self.now) - 20
        body = home(card() + native(created_at_ts=stamp, catalog_id=13))
        self.worker.fetch = lambda role,url: (body,self.now)
        self.worker.home("seller",self.now)
        self.assertFalse(self.worker.pending["seller"])
        self.assertEqual(activity.alerted_ids(["123"]),{"123"})
        self.assertEqual(self.worker.counts["seller"]["feed_verified"],1)
        for fixture in (home(card()+native(id="999",created_at_ts=stamp)),
                        home(card()+native(created_at_ts=stamp, title="Different title")),
                        home(card()+native(created_at_ts=stamp, price={"amount":"9.00","currency_code":"GBP"})),
                        home(card()+native(created_at_ts=stamp,created_at=stamp-8))):
            parsed = feed.parse_home(fixture)["items"][0]
            self.assertNotIn("home_created_at",parsed)

    def test_cooldown_feedback_offers_buyer_status_in_telegram(self):
        with patch.dict("os.environ",{"DASHBOARD_URL":"https://finder.example"}):
            buttons = vinted_buying.feedback_buttons({"query_id":1},
                {"state":"failed_before_payment","reason":"renewal_cooldown"})
        self.assertTrue(any(btn.callback_data == "finder:reconnect:autobuy" for row in buttons for btn in row))
