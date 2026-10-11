"""Real eBay discovery writes obey literal shared positive-keyword evidence."""

import asyncio
import json
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_ebay_monitor import EbayFixture, item

import dashboard_store
import ebay_alerts
import ebay_store
import keyword_candidates as candidates
import search_settings


class SharedPositiveEbay(EbayFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.identity = dashboard_store.save_search(
            None,
            {
                "shared_alert_version": "1",
                "query_name": "Shared Zara skinny jeans",
                "platform_mode": "ebay",
                "query": "",
                "ebay_search_url": "https://www.ebay.co.uk/sch/11554/i.html?_nkw=&Brand=Zara&LH_BIN=1&LH_PrefLoc=1",
                "shared_keywords": "skinny",
                "vinted_max_total": "25.00",
                "exclusions": "ripped",
                "reminder": "Check shape. Resell £45.",
            },
        )
        ebay_store.save_live_selection([self.identity])
        self.search = search_settings.get_search(self.identity)
        self.assertEqual(self.snapshot(self.search, [], 1000), 0)

    def jeans(self, number, *, title="Zara skinny jeans", **changes):
        return item(
            number,
            title=title,
            categories=[{"categoryId": "11554", "categoryName": "Jeans"}],
            seller={"username": "offline-zara-seller"},
            **changes,
        )

    def rows(self, table):
        with closing(search_settings.connection()) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM " + table)]

    def buyer_state(self):
        return {
            table: self.rows(table)
            for table in ("vinted_buyer", "vinted_buy_attempts", "items")
        }

    def discover(self, items, now=1020):
        before = self.buyer_state()
        with patch(
            "requests.Session.request", side_effect=AssertionError("No HTTP in poll")
        ), patch(
            "ebay_images.fetch_details", side_effect=AssertionError("No detail read")
        ), patch.object(
            candidates, "stage", wraps=candidates.stage
        ) as stage:
            result = self.snapshot(self.search, items, now)
        self.assertEqual(self.buyer_state(), before)
        return result, stage

    def test_title_positive_goes_straight_to_normal_outbox_without_detail_work(self):
        result, stage = self.discover([self.jeans(101)])
        self.assertEqual(result, 1)
        stage.assert_not_called()
        self.assertEqual(self.rows("keyword_candidates"), [])
        row = self.outbox()[0]
        self.assertEqual(row["item_id"], "ebay:101")
        self.assertEqual(row["status"], "pending")
        self.assertIn("£19.15", row["content"])
        self.assertEqual(self.rows("ebay_alert_timing")[0]["listed_at"], 1010)

    def test_supplied_seller_description_positive_goes_straight_to_outbox(self):
        result, stage = self.discover(
            [
                self.jeans(
                    101, title="Zara straight jeans", description="<p>A skinny fit.</p>"
                )
            ]
        )
        self.assertEqual(result, 1)
        stage.assert_not_called()
        details = ebay_alerts.get_details(self.outbox()[0])
        self.assertEqual(details["description"], "A skinny fit.")
        self.assertEqual(details["keyword_evidence"]["field"], "description")
        self.assertTrue(details["description_complete"])
        self.assertEqual(self.rows("keyword_candidates"), [])

    def test_complete_description_without_alternative_never_enters_outbox_or_candidates(
        self,
    ):
        result, stage = self.discover(
            [
                self.jeans(
                    101,
                    title="Zara wide-leg jeans",
                    description="<p>Loose straight cut.</p>",
                )
            ]
        )
        self.assertEqual(result, 0)
        stage.assert_not_called()
        self.assertEqual(self.outbox(), [])
        self.assertEqual(self.rows("keyword_candidates"), [])
        self.assertEqual(self.snapshot(self.search, [self.jeans(101)], 1030), 0)

    def test_unknown_description_is_retained_before_global_outbox_insertion(self):
        result, stage = self.discover(
            [
                self.jeans(
                    101, title="Zara wide-leg jeans", shortDescription="Nearly new."
                )
            ]
        )
        self.assertEqual(result, 0)
        self.assertEqual(self.outbox(), [])
        stage.assert_called_once()
        platform, parsed, saved, alert = stage.call_args.args
        self.assertEqual(platform, "ebay")
        self.assertEqual(parsed["item_id"], "ebay:101")
        self.assertEqual(saved["id"], self.identity)
        self.assertEqual(alert["url"], "https://www.ebay.co.uk/itm/101")
        self.assertIn("ebay_details", alert)
        self.assertIn("conn", stage.call_args.kwargs)
        staged = self.rows("keyword_candidates")
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0]["status"], "pending")
        self.assertEqual(
            json.loads(staged[0]["payload"])["item"]["item_id"], "ebay:101"
        )
        # Discovery dedupes this raw result; the durable retained candidate
        # remains available after subsequent ordinary polls skip it.
        self.assertEqual(self.snapshot(self.search, [self.jeans(101)], 1030), 0)
        self.assertEqual(len(self.rows("keyword_candidates")), 1)

    def test_exclusions_budget_bad_links_and_quiet_baseline_never_stage(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DELETE FROM ebay_state WHERE query_id=?", (self.identity,))
        result, stage = self.discover([self.jeans(100, title="Zara jeans")], now=1000)
        self.assertEqual(result, 0)
        stage.assert_not_called()
        result, stage = self.discover(
            [
                self.jeans(101, title="Zara ripped jeans"),
                self.jeans(102, title="Zara jeans", price="30.00"),
                self.jeans(
                    103, title="Zara jeans", itemWebUrl="https://www.ebay.co.uk/itm/999"
                ),
                self.jeans(104, title="Zara jeans", shippingOptions=[]),
            ]
        )
        self.assertEqual(result, 0)
        stage.assert_not_called()
        self.assertEqual(self.outbox(), [])
        self.assertEqual(self.rows("keyword_candidates"), [])

    def test_discovery_staged_candidate_recovers_from_one_bound_detail_read(self):
        self.discover([self.jeans(101, title="Zara straight jeans")])
        before = self.buyer_state()
        with patch.object(
            candidates,
            "fetch_candidate_details",
            return_value={
                "description": "The seller describes a skinny fit.",
                "keyword_description": "The seller describes a skinny fit.",
                "description_complete": True,
                "available": True,
                "photos": [],
            },
        ) as fetch:
            self.assertTrue(candidates.process_one("ebay", now=1021))
            self.assertFalse(candidates.process_one("ebay", now=1052))
        fetch.assert_called_once()
        self.assertEqual(self.buyer_state(), before)
        self.assertEqual(self.outbox()[0]["item_id"], "ebay:101")
        self.assertEqual(self.rows("keyword_candidates")[0]["status"], "accepted")
        details = ebay_alerts.get_details(self.outbox()[0])
        self.assertTrue(details["keyword_verified"])
        self.assertTrue(details["description_complete"])
        self.assertEqual(details["keyword_evidence"]["state"], "matched")
        self.assertEqual(details["keyword_evidence"]["field"], "description")

    def test_description_positive_beyond_caption_limit_retains_delivery_proof(self):
        description = "Blue denim " * 600 + "skinny fit"
        result, stage = self.discover(
            [self.jeans(101, title="Zara jeans", description=description)]
        )
        self.assertEqual(result, 1)
        stage.assert_not_called()
        details = ebay_alerts.get_details(self.outbox()[0])
        self.assertTrue(details["description_complete"])
        self.assertEqual(details["keyword_description"], description)
        self.assertIn("skinny", details["keyword_description"])
        self.assertEqual(details["keyword_evidence"]["field"], "description")

    def test_explicit_shared_preview_handles_missing_details_and_current_notes_refresh(
        self,
    ):
        self.discover([self.jeans(101)])
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DELETE FROM ebay_alert_details WHERE item_id='ebay:101'")
        search_settings.update_search(
            self.identity, "reminder", "Current notes: check <label>; resell £60."
        )
        before = self.buyer_state()
        bot = SimpleNamespace()
        manager = AsyncMock()
        manager.__aenter__.return_value = bot
        manager.__aexit__.return_value = False
        with patch("telegram.Bot", return_value=manager) as factory, patch(
            "ebay_store.configuration",
            return_value={"telegram_token": "123456:offline-preview", "chat_id": "123"},
        ), patch(
            "vinted_alerts.send_text",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(message_id=77),
        ) as send, patch(
            "vinted_alerts.enrich", new_callable=AsyncMock, return_value=True
        ) as enrich, patch(
            "ebay_alerts.asyncio.sleep", new_callable=AsyncMock
        ), patch(
            "ebay_alerts.snapshot", wraps=ebay_alerts.snapshot
        ) as snapshot, patch(
            "requests.Session.request", side_effect=AssertionError("No preview HTTP")
        ):
            result = asyncio.run(ebay_alerts.preview(self.identity))
        self.assertIn("eBay layout preview sent", result)
        factory.assert_called_once_with("123456:offline-preview")
        send.assert_awaited_once()
        enrich.assert_awaited_once()
        self.assertEqual(snapshot.call_count, 2)
        # The first snapshot rebuilds missing listing details; the second is
        # intentionally guide-only and must not assume item title/price keys.
        self.assertEqual(
            snapshot.call_args_list[0].args[0]["title"], "Zara skinny jeans"
        )
        self.assertNotIn("title", snapshot.call_args_list[1].args[0])
        sent_bot, chat, row, details = send.call_args.args
        self.assertIs(sent_bot, bot)
        self.assertEqual(chat, "123")
        self.assertTrue(details["name"].startswith("LAYOUT PREVIEW · "))
        self.assertEqual(details["keyword_evidence"]["state"], "matched")
        self.assertEqual(details["keyword_evidence"]["field"], "title")
        self.assertIn(
            "Current notes: check &lt;label&gt;; resell £60.", details["reminder"]
        )
        self.assertEqual(enrich.call_args.kwargs, {"persist": False})
        self.assertEqual(row["item_id"], "ebay:101")
        self.assertEqual(self.outbox()[0]["status"], "pending")
        self.assertEqual(self.buyer_state(), before)
        self.assertEqual(
            self.rows("ebay_preview_messages"),
            [{"message_id": 77, "item_id": "ebay:101"}],
        )
