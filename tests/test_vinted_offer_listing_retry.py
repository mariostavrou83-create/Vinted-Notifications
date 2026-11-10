"""One bad listing must not block healthy offers or replay an uncertain write."""

import unittest
from contextlib import closing
from unittest.mock import patch

import test_vinted_offers as fixture
from test_vinted_offers import NOW
import search_settings
import vinted_buyer as buyer
import vinted_offers as offers
import vinted_offer_listing_recovery as recovery
import vinted_page_data as page
from test_vinted_page_data import next_data, purchase_item


class ListingRetryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.OfferTests("test_new_like_waits_full_five_minutes_across_worker_restart")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.enable()
        self.job = self.fixture.add()

    def fail_item(self, method, path, body):
        if path == "/api/v2/items/1001":
            raise buyer.BuyerError("Unverified listing", reason="item_unavailable")

    def test_failed_read_waits_while_another_listing_sends_then_recovers(self):
        f = self.fixture
        other = f.add(item=1002, recipient=223)
        f.client.fail = self.fail_item
        f.worker.tick()
        held = f.jobs()[0]
        self.assertEqual((held["status"], held["reason"], held["lease_until"]),
                         ("queued", "listing_check", NOW+60))
        self.assertEqual(offers.state()["mode"], "live")
        f.now += 5
        f.worker.tick()
        self.assertEqual(f.jobs()[1]["id"], other)
        self.assertEqual(f.jobs()[1]["status"], "sent")
        self.assertEqual(f.jobs()[0]["status"], "queued")
        f.client.fail = None
        f.now += 200
        f.worker.tick()
        self.assertTrue(all(j["status"] == "sent" for j in f.jobs()))
        self.assertEqual(len(f.client.writes()), 2)

    def test_listing_rechecks_back_off_without_repeated_account_access(self):
        f = self.fixture
        f.client.fail = self.fail_item
        f.worker.tick()
        calls = len(f.client.calls)
        for offset in (5, 20, 59):
            f.now = NOW+offset
            f.worker.tick()
        self.assertEqual(len(f.client.calls), calls)
        f.now = NOW+60
        f.worker.tick()
        self.assertEqual(f.jobs()[0]["lease_until"], NOW+180)
        self.assertEqual(offers.state()["mode"], "live")
        self.assertFalse(f.client.writes())

    def test_same_error_after_offer_attempt_is_uncertain_and_never_replayed(self):
        f = self.fixture
        def fail_send(method, path, body):
            if path.endswith("/offers"):
                raise buyer.BuyerError("Unknown receipt", reason="item_unavailable")
        f.client.fail = fail_send
        f.worker.tick()
        self.assertEqual(f.jobs()[0]["status"], "uncertain")
        self.assertEqual(offers.state()["mode"], "paused")
        f.now += 3600
        f.client.fail = None
        f.worker.tick()
        self.assertEqual(len(f.client.writes()), 1)


class OwnerPageOfferTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.OfferTests("test_new_like_waits_full_five_minutes_across_worker_restart")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        f = self.fixture
        f.enable()
        f.add()
        f.client.wardrobe = [f.client.request("GET", "/api/v2/items/1001")["item"]]
        self.owner_item = purchase_item(id="1001", seller_id="111", can_buy=False)
        del self.owner_item["price"]
        self.worker = offers.OfferWorker(client_factory=f.factory, clock=lambda: f.now)

    def listing_page(self, url, item_id, *, for_offer=False):
        self.assertTrue(for_offer)
        parsed = page.parse_offer_item(next_data(self.owner_item), item_id)
        if parsed is None:
            raise buyer.BuyerError("Unverified item", reason="item_unavailable")
        return {"item": parsed}

    def test_real_owner_parser_and_wardrobe_price_send_one_correct_offer(self):
        f = self.fixture
        f.client.wardrobe[0]["price"] = {"amount": "30.00", "currency_code": "GBP"}
        with patch.object(f.client, "listing_page", side_effect=self.listing_page):
            self.worker.tick()
        self.assertEqual(f.jobs()[0]["status"], "sent")
        self.assertEqual(f.jobs()[0]["price"], 3000)
        self.assertEqual(f.jobs()[0]["offer_price"], 2850)
        self.assertEqual(len(f.client.writes()), 1)

    def test_owner_restrictions_or_invalid_wardrobe_price_still_block_sending(self):
        f = self.fixture
        for changes in ({"is_reserved": True}, {"is_hidden": True}, {"seller_id": "999"}):
            with self.subTest(changes=changes):
                original = self.owner_item
                self.owner_item = {**original, **changes}
                with patch.object(f.client, "listing_page", side_effect=self.listing_page):
                    with self.assertRaises(offers.OfferCheck):
                        data = offers.SellerItems(lambda: f.now).read(
                            f.client, {"account_id": "111", "item_id": "1001"})
                        offers.checked_item(data, {"account_id": "111", "item_id": "1001"}, "half_pound")
                self.owner_item = original
        f.client.wardrobe[0]["price"] = {"amount": "NaN", "currency_code": "GBP"}
        with patch.object(f.client, "listing_page", side_effect=self.listing_page) as listing:
            self.worker.tick()
            listing.assert_not_called()
        self.assertEqual(f.jobs()[0]["status"], "skipped")
        self.assertFalse(f.client.writes())


class ListingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.OfferTests("test_new_like_waits_full_five_minutes_across_worker_restart")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.enable()
        self.job = self.fixture.add()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_offer_jobs SET reason='shape' WHERE id=?", (self.job,))
            conn.execute("UPDATE vinted_offer_settings SET mode='paused',pause_reason='shape'")
            offers.event(conn, "paused", "shape", now=NOW-100)
            conn.execute("UPDATE vinted_buyer SET verified_at=? WHERE id=1", (NOW-10,))
        self.payload = {"account_id": "111", "window": [NOW-101, NOW-99], "jobs": [self.job]}

    def test_matching_incident_resumes_once_without_changing_or_sending_jobs(self):
        before = self.fixture.jobs()
        self.assertEqual(recovery.run_once(self.payload, NOW), "applied")
        self.assertEqual(offers.state()["mode"], "live")
        self.assertEqual(self.fixture.jobs(), before)
        offers.pause("")
        self.assertEqual(recovery.run_once(self.payload, NOW), "already_applied")
        self.assertEqual(offers.state()["mode"], "paused")
        self.assertFalse(self.fixture.client.writes())

    def test_later_manual_pause_or_wrong_account_cannot_be_reopened(self):
        for mutation in ("UPDATE vinted_offer_settings SET pause_reason=''",
                         "UPDATE vinted_buyer SET user_id='999'"):
            with self.subTest(mutation=mutation):
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute(mutation)
                self.assertEqual(recovery.run_once(self.payload, NOW), "state_changed_or_unverified")
                with closing(search_settings.connection()) as conn, conn:
                    conn.execute("UPDATE vinted_offer_settings SET pause_reason='shape'")
                    conn.execute("UPDATE vinted_buyer SET user_id='111'")

    def test_previous_send_attempt_blocks_recovery(self):
        with closing(search_settings.connection()) as conn, conn:
            offers.event(conn, "sending", job_id=self.job, now=NOW-150)
        self.assertEqual(recovery.run_once(self.payload, NOW), "previous_send_attempt")
        self.assertEqual(offers.state()["mode"], "paused")

    def test_changed_or_uncertain_queue_is_preserved(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_offer_jobs SET status='uncertain' WHERE id=?", (self.job,))
        self.assertEqual(recovery.run_once(self.payload, NOW), "uncertain_offer")
        self.assertEqual(offers.state()["mode"], "paused")

    def test_expired_or_overbroad_request_cannot_resume(self):
        for changes in ({"window": [NOW-200000, NOW-199999]},
                        {"window": [NOW-101, NOW+1]}, {"jobs": []},
                        {"window": [NOW-101, NOW-80]}):
            self.assertEqual(recovery.run_once({**self.payload, **changes}, NOW), "invalid_request")
