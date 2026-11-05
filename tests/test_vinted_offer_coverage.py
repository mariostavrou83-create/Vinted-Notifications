"""Coverage regressions for daily new stock and availability recovery."""

import unittest
from contextlib import closing

from test_search_controls import DatabaseFixture
from test_vinted_offers import FakeClient, NOW
import search_settings
import vinted_offers as offers


class CoverageTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.now = NOW
        self.client = FakeClient()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET user_id='111',verified_at=?,session=? WHERE id=1",
                         (NOW, b"offline"))
        offers.configure("live", "half_pound", "three_months", now=NOW)

    def item(self, item_id, **changes):
        return {**self.client.request("GET", f"/api/v2/items/{item_id}")["item"], **changes}

    def job(self, item_id=1001, recipient=222, status="skipped", reason="unavailable", **fields):
        values = {"account_id": "111", "item_id": str(item_id),
                  "recipient_id": str(recipient), "notification_id": str(recipient),
                  "liked_at": NOW - 600, "created": NOW - 590,
                  "status": status, "reason": reason, **fields}
        with closing(search_settings.connection()) as conn, conn:
            return conn.execute(
                "INSERT INTO vinted_offer_jobs (" + ",".join(values) + ") VALUES (" +
                ",".join("?" for _ in values) + ")", tuple(values.values())
            ).lastrowid

    def status(self, job_id):
        with closing(search_settings.connection()) as conn:
            return conn.execute("SELECT status FROM vinted_offer_jobs WHERE id=?", (job_id,)).fetchone()[0]

    def test_expired_partial_wardrobe_refreshes_page_one_for_new_listings(self):
        self.client.wardrobe = [self.item(1000 + i) for i in range(100)]
        reader = offers.SellerItems(lambda: self.now)
        reader.read(self.client, {"account_id": "111", "item_id": "1001"})
        self.assertFalse(reader.complete)
        self.now += 3600
        new_items = [self.item(5000 + i) for i in range(8)]
        self.client.wardrobe = new_items + self.client.wardrobe
        for item in new_items:
            result = reader.read(self.client, {"account_id": "111", "item_id": str(item["id"])})
            self.assertEqual(result["item"]["id"], item["id"])
        pages = [path for _, path, _ in self.client.calls if "/wardrobe/" in path]
        self.assertEqual(len(pages), 2)
        self.assertTrue(all("page=1&" in path for path in pages))

    def test_server_page_size_cap_does_not_hide_later_active_listings(self):
        base = [self.item(1000 + i) for i in range(40)]
        original = self.client.request

        def capped(method, path, *args, **kwargs):
            if "/wardrobe/" in path:
                from urllib.parse import parse_qs, urlsplit
                page = int(parse_qs(urlsplit(path).query)["page"][0])
                return {"items": base[(page-1)*20:page*20],
                        "pagination": {"total_pages": 2, "per_page": 20}}
            return original(method, path, *args, **kwargs)

        self.client.request = capped
        reader = offers.SellerItems(lambda: self.now)
        with self.assertRaises(offers.InventoryPending):
            reader.read(self.client, {"account_id": "111", "item_id": "1039"})
        self.now += 5
        self.assertEqual(reader.read(self.client, {"account_id": "111", "item_id": "1039"})["item"]["id"], "1039")

    def test_recovery_requires_active_owned_item_and_no_previous_send_attempt(self):
        eligible = self.job()
        missing = self.job(recipient=223, reason="inventory_missing")
        excluded = [self.job(recipient=224, reason="existing_offer"),
                    self.job(recipient=225, status="uncertain", reason="crash"),
                    self.job(recipient=226, status="sent", reason="", offer_id="999", sent_at=NOW),
                    self.job(recipient=227, reason="cancelled"),
                    self.job(recipient=228, reason="closed_conversation"),
                    self.job(recipient=229, liked_at=NOW - 120*86400),
                    self.job(item_id=1002, recipient=230),
                    self.job(item_id=1003, recipient=231)]
        attempted = self.job(recipient=232)
        with closing(search_settings.connection()) as conn, conn:
            offers.event(conn, "sending", job_id=attempted, now=NOW-60)
        rows = [self.item(1001), self.item(1002, is_closed=True),
                self.item(1003, user={"id": 999})]
        active, recovered = offers.recover_available_items(rows, offers.state(), NOW)
        self.assertEqual(active, ["1001"])
        self.assertEqual({r["id"] for r in recovered}, {eligible, missing})
        self.assertEqual(self.status(eligible), "queued")
        self.assertEqual(self.status(missing), "queued")
        self.assertTrue(all(self.status(i) != "queued" for i in excluded + [attempted]))
        self.assertFalse(self.client.writes())
        offers._finish(eligible, "skipped", "unavailable")
        self.assertEqual(offers.recover_available_items(rows, offers.state(), NOW+60)[1], [])

    def test_full_wardrobe_is_checked_in_pages_without_sending(self):
        self.client.wardrobe = [self.item(1000 + i) for i in range(100)]
        missed = self.job(item_id=1099)
        coverage = offers.WardrobeCoverage()
        coverage.scan(self.client, offers.state(), NOW)
        self.assertEqual(len(coverage.active), 96)
        self.assertEqual(self.status(missed), "skipped")
        coverage.scan(self.client, offers.state(), NOW+5)
        self.assertEqual(len(coverage.active), 100)
        self.assertEqual(self.status(missed), "queued")
        self.assertEqual(coverage.next_scan, NOW+605)
        self.assertFalse(self.client.writes())

    def test_recovered_like_passes_normal_checks_and_sends_only_once(self):
        self.client.wardrobe = [self.item(1001)]
        missed = self.job()
        offers.recover_available_items(self.client.wardrobe, offers.state(), NOW)
        worker = offers.OfferWorker(client_factory=lambda: self.client, clock=lambda: self.now)
        worker.tick()
        self.assertEqual(self.status(missed), "sent")
        self.now += 700
        worker.tick()
        self.assertEqual(len(self.client.writes()), 1)

    def test_existing_conversation_offer_still_prevents_recovered_send(self):
        self.client.wardrobe = [self.item(1001)]
        self.client.messages = [{"entity_type": "offer_message"}]
        missed = self.job()
        offers.recover_available_items(self.client.wardrobe, offers.state(), NOW)
        worker = offers.OfferWorker(client_factory=lambda: self.client, clock=lambda: self.now)
        worker.tick()
        self.assertEqual(self.status(missed), "skipped")
        self.assertFalse(self.client.writes())

    def test_manual_pause_is_preserved_by_coverage_recovery(self):
        missed = self.job()
        config = offers.state()
        offers.pause("")
        self.assertEqual(offers.recover_available_items([self.item(1001)], config, NOW)[1], [])
        self.assertEqual(self.status(missed), "skipped")

    def test_new_likes_keep_priority_over_recovered_history(self):
        self.client.wardrobe = [self.item(1001), self.item(1002)]
        old = self.job(liked_at=NOW-1200)
        fresh = self.job(item_id=1002, status="queued", reason="", liked_at=NOW-300)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_offer_settings SET priority_since=? WHERE id=1", (NOW-86400,))
        offers.recover_available_items(self.client.wardrobe, offers.state(), NOW)
        worker = offers.OfferWorker(client_factory=lambda: self.client, clock=lambda: self.now)
        worker.tick()
        self.assertEqual(self.status(fresh), "sent")
        self.assertEqual(self.status(old), "queued")

    def test_recovered_history_leaves_space_for_like_due_soon(self):
        self.client.wardrobe = [self.item(1001), self.item(1002)]
        old = self.job(liked_at=NOW-1200)
        fresh = self.job(item_id=1002, status="queued", reason="", liked_at=NOW-240)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_offer_settings SET priority_since=? WHERE id=1", (NOW-86400,))
        offers.recover_available_items(self.client.wardrobe, offers.state(), NOW)
        worker = offers.OfferWorker(client_factory=lambda: self.client, clock=lambda: self.now)
        worker.tick()
        self.assertFalse(self.client.writes())
        self.now += 60
        worker.tick()
        self.assertEqual(self.status(fresh), "sent")
        self.assertEqual(self.status(old), "queued")
