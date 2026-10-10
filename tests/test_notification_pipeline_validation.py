"""Shared rule-to-notification and pre-send checks; no live sends or buying."""

import json
import unittest
from contextlib import closing
from queue import Queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_dashboard import photo_bytes
from test_ebay_monitor import item
from test_finds_delivery import outbox
from test_search_controls import DatabaseFixture

import alert_delivery
import alert_images
import dashboard_store
import ebay_alerts
import ebay_monitor
import ebay_store
import photo_cards
import search_settings
import vinted_alerts
import vinted_keywords


def shared_form(**changes):
    return dict(
        {
            "shared_alert_version": "1",
            "query_name": "Shared Hollister jacket",
            "platform_mode": "both",
            "query": "https://www.vinted.co.uk/catalog?brand_ids[]=111&catalog[]=2050&size_ids[]=4",
            "ebay_search_url": "https://www.ebay.co.uk/sch/i.html?_sacat=57988&LH_BIN=1",
            "shared_keywords": "fur, sherpa, fur hood",
            "vinted_max_total": "20.00",
            "exclusions": "teddy",
            "reminder": "Resale aim £60; check <fur> trim & cuffs",
            "revision": "0",
        },
        **changes,
    )


class EbayItemLinkBinding(unittest.TestCase):
    def parse(self, url):
        return ebay_monitor.parse_item(
            item(123, itemWebUrl=url), dict(ebay_store.DEFAULTS), 1020
        )

    def test_direct_slug_and_tracking_links_keep_exact_item_binding(self):
        for url in (
            "https://www.ebay.co.uk/itm/123",
            "https://www.ebay.co.uk/itm/Hollister-fur-jacket/123",
            "https://ebay.co.uk/itm/Fur%20jacket/123/?_trksid=p123&_skw=fur",
            "https://www.ebay.com:443/itm/123?hash=item123",
            "https://www.ebay.co.uk/ws/eBayISAPI.dll?ViewItem&item=123&category=57988",
            "https://www.ebay.com/itm/ws/eBayISAPI.dll?cmd=ViewItem&item=123",
        ):
            with self.subTest(url=url):
                parsed = self.parse(url)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed["item_id"], "ebay:123")
                self.assertEqual(parsed["url"], url)

    def test_wrong_nonlisting_or_unbound_routes_cannot_become_alert_links(self):
        for url in (
            "https://www.ebay.co.uk/itm/456",
            "https://www.ebay.co.uk/itm/Fur/456",
            "https://www.ebay.co.uk/help",
            "https://www.ebay.co.uk/sch/i.html?item=123",
            "https://www.ebay.co.uk/itm/123/456",
            "https://www.ebay.co.uk/itm/title/123/buy",
            "https://www.ebay.co.uk/ws/eBayISAPI.dll?ViewItem&item=456",
            "https://www.ebay.co.uk/ws/eBayISAPI.dll?ViewItem&item=123&item=456",
            "https://www.ebay.co.uk/ws/eBayISAPI.dll?item=123",
            "https://www.ebay.co.uk/ws/eBayISAPI.dll?ViewItem&item=",
        ):
            with self.subTest(url=url):
                self.assertIsNone(self.parse(url))

    def test_unsafe_url_inputs_still_fail_closed(self):
        for url in (
            None,
            b"https://www.ebay.co.uk/itm/123",
            "http://www.ebay.co.uk/itm/123",
            "https://www.ebay.co.uk.evil.example/itm/123",
            "https://secret@www.ebay.co.uk/itm/123",
            "https://www.ebay.co.uk:123/itm/123",
            "https://www.ebay.co.uk:invalid/itm/123",
            "https://www.ebay.co.uk\n/itm/123",
            "https://www.ebay.co.uk/itm/123?x=" + "a" * 4096,
        ):
            with self.subTest(url=url):
                self.assertIsNone(self.parse(url))


class SharedNotificationPipeline(DatabaseFixture, unittest.TestCase):
    def batch_variant(self, search, variant, number, *, title=None, price="15.00"):
        source, output = Queue(), Queue()
        listing = SimpleNamespace(
            id=number,
            title=title or "Hollister fur hood jacket",
            brand_title="Hollister",
            price=price,
            currency="GBP",
            photo="https://images1.vinted.net/example.jpg",
            url=f"https://www.vinted.co.uk/items/{number}",
            has_real_timestamp=False,
            raw_timestamp=1020,
            observed_at=1020,
            raw_data={"description": "Cotton <label> & cuffs"},
            is_new_item=lambda: True,
        )
        source.put(([listing], search["id"], variant["url"], variant["id"]))
        self.core.clear_item_queue(source, output)

    def test_shared_rules_keep_one_matching_alert_per_platform_and_correct_controls(
        self,
    ):
        photo_cards.enable()
        query_id = dashboard_store.save_search(None, shared_form(), photo=photo_bytes())
        search = search_settings.get_search(query_id)
        with patch("core.time", return_value=1020):
            for variant in vinted_keywords.rows(query_id):
                self.batch_variant(search, variant, 1000)
            for variant in vinted_keywords.rows(query_id):
                self.batch_variant(search, variant, 1001)
                self.batch_variant(
                    search, variant, 1002, title="Hollister TEDDY-fur hood jacket"
                )
                self.batch_variant(search, variant, 1003, price="18.00")
        with patch.object(ebay_monitor.time, "time", return_value=1000):
            self.assertEqual(ebay_monitor.record_snapshot(search, [], 1000), 0)
        raw = [
            item(1001, categories=[{"categoryId": "57988"}]),
            item(
                1002,
                title="Hollister TEDDY-fur hood jacket",
                categories=[{"categoryId": "57988"}],
            ),
            item(1003, price="18.00", categories=[{"categoryId": "57988"}]),
        ]
        with patch.object(ebay_monitor.time, "time", return_value=1020):
            self.assertEqual(ebay_monitor.record_snapshot(search, raw, 1020), 1)
            self.assertEqual(ebay_monitor.record_snapshot(search, raw, 1020), 0)
        with closing(search_settings.connection()) as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM alert_outbox")]
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM vinted_buy_attempts").fetchone()[0],
                0,
            )
        self.assertEqual(
            {(row["platform"], row["item_id"]) for row in rows},
            {("vinted", "1001"), ("ebay", "ebay:1001")},
        )
        for row in rows:
            module = ebay_alerts if row["platform"] == "ebay" else vinted_alerts
            details = module.get_details(row)
            caption, _ = photo_cards.captions(row, details)
            buttons = [
                button
                for group in photo_cards.markup(row, details).inline_keyboard
                for button in group
            ]
            with self.subTest(platform=row["platform"]):
                self.assertEqual(row["query_id"], query_id)
                self.assertEqual(row["reference_id"], search["reference_id"])
                self.assertIn(f"#{query_id}", caption)
                self.assertIn("Hollister", caption)
                self.assertIn(
                    "Resale aim £60; check &lt;fur&gt; trim &amp; cuffs", caption
                )
                self.assertTrue(any(b.url == row["url"] for b in buttons))
                self.assertTrue(any(b.text == "Your examples" for b in buttons))
                self.assertEqual(
                    any("Autobuy" in b.text for b in buttons),
                    row["platform"] == "vinted",
                )
                self.assertLessEqual(
                    photo_cards.units(photo_cards.plain(caption)), 1024
                )
                self.assertEqual(
                    details.get("estimated_total") or details["budget"]["total"],
                    1915 if row["platform"] == "ebay" else 1795,
                )


class VintedPendingRules(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        photo_cards.enable()
        self.edit(vinted_max_total="20.00")
        self.batch(1, [109])  # The changed filter link takes its quiet baseline.
        self.batch(1, [110])
        self.bot = SimpleNamespace(
            send_photo=AsyncMock(
                return_value=SimpleNamespace(
                    message_id=42, photo=[SimpleNamespace(file_id="listing-photo")]
                )
            ),
            send_message=AsyncMock(
                return_value=SimpleNamespace(message_id=42, photo=[])
            ),
        )
        self.worker = alert_delivery.VintedDeliveryWorker(self.bot, "123")
        self.worker.send_slot_delay = lambda: 0

    async def asyncTearDown(self):
        await self.worker.close()

    def edit(self, **changes):
        search = search_settings.get_search(1)
        form = {
            "query_name": search["query_name"],
            "query": search["query"],
            "revision": str(search["revision"]),
            "reminder": search["reminder"],
            "exclusions": "\n".join(search["exclusions"]),
            "vinted_max_total": (
                ""
                if search["vinted_max_total"] is None
                else f"{search['vinted_max_total'] / 100:.2f}"
            ),
            "vinted_postage_estimate": f"{search['vinted_postage_estimate'] / 100:.2f}",
        }
        form.update(changes)
        dashboard_store.save_search(1, form)

    async def test_current_exclusion_cancels_queued_item_without_sending(self):
        self.edit(exclusions="fur")
        await self.worker.tick(now=1000)
        self.assertEqual(outbox(110)["status"], "cancelled")
        self.bot.send_photo.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_lower_budget_rechecks_original_estimate_and_keeps_eligible_job(self):
        self.edit(vinted_max_total="19.00")
        with patch.object(
            alert_images, "listing_collage", new=AsyncMock(return_value=None)
        ):
            await self.worker.tick(now=1000)
        self.assertEqual(outbox(110)["status"], "sent")
        self.assertIn("£18.65", self.bot.send_message.call_args.kwargs["text"])

    async def test_lower_budget_blocks_now_overbudget_original_listing(self):
        self.edit(vinted_max_total="18.00")
        await self.worker.tick(now=1000)
        self.assertEqual(outbox(110)["status"], "cancelled")
        self.bot.send_photo.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_rule_change_during_image_download_is_rechecked_before_native_send(
        self,
    ):
        async def downloaded(_urls):
            self.edit(exclusions="fur")
            return photo_bytes()

        with patch.object(alert_images, "listing_collage", side_effect=downloaded):
            await self.worker.tick(now=1000)
        self.assertEqual(outbox(110)["status"], "cancelled")
        self.bot.send_photo.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_paused_or_archived_search_never_sends_pending_listing(self):
        for state in ("pause", "archive"):
            with self.subTest(state=state):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute(
                        "UPDATE search_dashboard SET paused=?,archived=? WHERE query_id=1",
                        (1, state == "archive"),
                    )
                    conn.execute(
                        "UPDATE alert_outbox SET status='pending',lease_token=NULL,leased_until=0 WHERE item_id='110'"
                    )
                await self.worker.tick(now=1000)
                self.assertEqual(outbox(110)["status"], "cancelled")
        self.bot.send_photo.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    def test_stale_lease_cannot_send_or_cancel_reclaimed_job(self):
        old = alert_delivery.claim(now=1000)
        replacement = alert_delivery.claim(now=1121)
        self.assertFalse(vinted_alerts.prepare_listing(old))
        self.assertEqual(outbox(110)["lease_token"], replacement["lease_token"])
        self.assertEqual(outbox(110)["status"], "pending")

    def test_sent_photo_history_bypasses_pending_rule_checks(self):
        row = alert_delivery.claim(now=1000)
        alert_delivery.finish(row, message_id=42, now=1001)
        self.edit(exclusions="fur")
        self.assertTrue(vinted_alerts.prepare_listing(dict(row, kind="photo")))
        self.assertEqual(outbox(110)["status"], "sent")

    def test_legacy_missing_snapshot_uses_existing_estimate(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("DELETE FROM vinted_alert_details WHERE item_id='110'")
        leased = alert_delivery.claim(now=1000)
        self.assertTrue(vinted_alerts.prepare_listing(leased))
        self.assertEqual(outbox(110)["status"], "pending")

    def supplied_fee(self, maximum):
        with closing(search_settings.connection()) as conn, conn:
            payload = json.loads(
                conn.execute(
                    "SELECT payload FROM vinted_alert_details WHERE item_id='110'"
                ).fetchone()[0]
            )
            payload["budget"].update(
                item=1500, buyer_protection=200, buyer_protection_estimated=False
            )
            conn.execute(
                "UPDATE vinted_alert_details SET payload=? WHERE item_id='110'",
                (json.dumps(payload),),
            )
            conn.execute(
                "UPDATE vinted_search_budgets SET max_total=? WHERE query_id=1",
                (maximum,),
            )
        return alert_delivery.claim(now=1000)

    def test_known_legacy_fee_above_fallback_does_not_pass_lowered_budget(self):
        # Supplied £2 protection + £2.20 postage is £19.20, although the
        # documented fallback would estimate only £18.65 for this £15 item.
        leased = self.supplied_fee(1900)
        self.assertFalse(vinted_alerts.prepare_listing(leased))
        self.assertEqual(outbox(110)["status"], "cancelled")

    def test_known_legacy_fee_exact_boundary_remains_eligible_and_in_caption(self):
        leased = self.supplied_fee(1920)
        self.assertTrue(vinted_alerts.prepare_listing(leased))
        details = vinted_alerts.get_details(leased)
        self.assertEqual(details["budget"]["total"], 1920)
        self.assertEqual(details["budget"]["buyer_protection"], 200)
        self.assertFalse(details["budget"]["buyer_protection_estimated"])
        self.assertIn("£19.20", photo_cards.captions(leased, details)[0])


if __name__ == "__main__":
    unittest.main()
