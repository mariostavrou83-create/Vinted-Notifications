"""Real SQLite and Flask; all marketplace reads/writes use an offline fake."""

import unittest
from contextlib import closing
from itertools import pairwise
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from test_dashboard import DashboardTests
from test_search_controls import DatabaseFixture

import search_settings
import vinted_buyer as buyer
import vinted_offers as offers

NOW = 1792594800.0


def notification(item=1001, recipient=222, at=NOW, **extra):
    return {
        "id": str(item * 1000 + recipient),
        "link": f"https://www.vinted.co.uk/items/{item}/want_it/new?offering_id={recipient}",
        "created_at": at,
        **extra,
    }


class FakeClient:
    def __init__(self):
        self._verified_session = ("111", b"offline", {})
        self.session = Mock()
        self.calls = []
        self.notifications = []
        self.items = {}
        self.price = "23.00"
        self.messages = []
        self.conversation_changes = {}
        self.offer_result = {"offer": {"id": 9001}}
        self.fail = None
        self.before_offer = None
        self.wardrobe = []

    def listing_page(self, url, item_id):
        item = dict(self.request("GET", f"/api/v2/items/{item_id}")["item"])
        item["user_id"] = item.get("user", {}).get("id")
        return {"item": item}

    def request(self, method, path, body=None, *, params=None):
        self.calls.append((method, path, body))
        if self.fail:
            self.fail(method, path, body)
        if path == buyer.NOTIFICATIONS_PATH:
            page = params["page"]
            return {"notifications": self.notifications[(page - 1) * 20 : page * 20]}
        if path.startswith("/api/v2/wardrobe/"):
            page = int(parse_qs(urlsplit(path).query)["page"][0])
            return {"items": self.wardrobe[(page - 1) * 96 : page * 96]}
        if path.startswith("/api/v2/items/"):
            item_id = path.rsplit("/", 1)[1]
            return {
                "item": self.items.get(
                    item_id,
                    {
                        "id": item_id,
                        "user": {"id": 111},
                        "title": "Offline test jacket",
                        "price": {"amount": self.price, "currency_code": "GBP"},
                        "is_closed": False,
                        "is_reserved": False,
                        "is_hidden": False,
                    },
                )
            }
        if path == "/api/v2/conversations":
            self.item_id, self.recipient_id = body["item_id"], body["opposite_user_id"]
            return {"conversation": {"id": 7001}}
        if path == "/api/v2/conversations/7001":
            result = {
                "id": 7001,
                "transaction": {
                    "id": 8001,
                    "seller_id": 111,
                    "user_side": "seller",
                    "item_id": self.item_id,
                },
                "opposite_user": {"id": self.recipient_id},
                "messages": self.messages,
            }
            result.update(self.conversation_changes)
            return {"conversation": result}
        if path == "/api/v2/transactions/8001/offers":
            if self.before_offer:
                self.before_offer()
            return self.offer_result
        raise AssertionError((method, path, body))

    def writes(self):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/offers")]


class OfferTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.now = NOW
        self.client = FakeClient()
        self.factory = Mock(return_value=self.client)
        self.worker = offers.OfferWorker(
            client_factory=self.factory,
            clock=lambda: self.now,
            item_reader=self.item_read,
        )
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET user_id='111',username='offline-seller',verified_at=?,session=? WHERE id=1",
                (NOW, b"offline-sealed"),
            )

    def item_read(self, client, job):
        return client.request("GET", f"/api/v2/items/{job['item_id']}")

    def enable(self, mode="live", rounding="half_pound", history="today"):
        offers.configure(mode, rounding, history, now=self.now)

    def add(self, item=1001, recipient=222, at=NOW):
        with closing(search_settings.connection()) as conn, conn:
            return conn.execute(
                """INSERT INTO vinted_offer_jobs
                (account_id,item_id,recipient_id,notification_id,liked_at,created)
                VALUES ('111',?,?,?,?,?)""",
                (str(item), str(recipient), f"{item}-{recipient}", at, self.now),
            ).lastrowid

    def jobs(self):
        with closing(search_settings.connection()) as conn:
            return [
                dict(r)
                for r in conn.execute("SELECT * FROM vinted_offer_jobs ORDER BY id")
            ]

    def test_price_rules_and_no_float_drift(self):
        self.assertEqual(offers.offer_amount(2300, "half_pound"), 2150)
        self.assertEqual(offers.offer_amount(2300, "penny"), 2185)
        self.assertEqual(offers.offer_amount(1999, "penny"), 1899)
        self.assertEqual(offers.offer_amount(2000, "half_pound"), 1900)
        for price in (0, -10, 100, 100.0, True, 10**12):
            with self.assertRaises(offers.OfferCheck):
                offers.offer_amount(price, "penny")
        for price in ("NaN", "Infinity", "-10", "2.001", None, True):
            with self.assertRaises(offers.OfferCheck):
                offers.price_pennies(price)

    def test_favourites_only_explicit_same_site_ids_and_timezone(self):
        n = notification()
        self.assertEqual(offers.favourite(n)["recipient_id"], "222")
        self.assertEqual(
            offers.favourite(
                dict(n, link="/inbox/want_it?item_id=1001&offering_id=222")
            )["item_id"],
            "1001",
        )
        for changes in (
            {"link": "https://evil.invalid/items/1001/want_it/new?offering_id=222"},
            {
                "link": "https://www.vinted.co.uk@evil.invalid/items/1001/want_it/new?offering_id=222"
            },
            {"link": "/items/1001/want_it/new?offering_id=222&offering_id=333"},
            {"link": "/items/1001?offering_id=222"},
            {"link": "/items/1001/want_it/new?offering_id=-1"},
            {"created_at": "yesterday"},
            {"created_at": "2026-10-21T11:00:00"},
            {"created_at": None},
        ):
            self.assertIsNone(offers.favourite(dict(n, **changes)), changes)
        self.assertIsNotNone(
            offers.favourite(dict(n, created_at="2026-10-21T11:00:00Z"))
        )

    def test_gateway_notification_updated_date_is_supported(self):
        row = notification(created_at=None, updated_at="2026-10-21T11:00:00Z")
        parsed = offers.favourite(row)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["item_id"], "1001")

    def test_current_item_favourite_uses_explicit_subject_and_liker(self):
        row = {
            "id": "notification-uuid",
            "entry_type": 20,
            "subject_id": 1001,
            "user_id": 222,
            "updated_at": "2026-10-21T11:00:00Z",
            "link": "vinted://messaging?item_id=1001&portal=uk",
        }
        parsed = offers.favourite(row)
        self.assertEqual(parsed["item_id"], "1001")
        self.assertEqual(parsed["recipient_id"], "222")
        for entry_type in (30, 50, 110, 120, 121, 300, 750, "20", True):
            self.assertIsNone(offers.favourite(dict(row, entry_type=entry_type)))
        for field in ("subject_id", "user_id", "updated_at"):
            invalid = dict(row)
            invalid.pop(field)
            self.assertIsNone(offers.favourite(invalid))

    def test_current_gateway_favourites_queue_without_messaging_other_notices(self):
        self.enable("preview", history="all")
        self.client.notifications = [
            {
                "entry_type": kind,
                "subject_id": 1000 + index,
                "user_id": 222,
                "updated_at": self.now,
                "link": "/inbox",
            }
            for index, kind in enumerate((20, 50, 110, 121, 300))
        ]
        self.worker.tick()
        self.assertEqual([j["item_id"] for j in self.jobs()], ["1000"])
        self.assertFalse(self.client.writes())

    def test_typed_favourite_accepts_current_web_action_without_user_field(self):
        row = {
            "entry_type": 20,
            "subject_id": 1001,
            "updated_at": NOW,
            "link": "vinted://messaging?item_id=1001&offering_id=222&portal=uk",
        }
        parsed = offers.favourite(row)
        self.assertEqual(parsed["item_id"], "1001")
        self.assertEqual(parsed["recipient_id"], "222")
        self.assertIsNotNone(
            offers.favourite(
                dict(row, link="/inbox/want_it?item_id=1001&offering_id=222")
            )
        )
        for changes in (
            {"user_id": 333},
            {"subject_id": 1002},
            {"entry_type": 110},
            {"link": "https://untrusted.invalid/?offering_id=222"},
            {"link": "vinted://messaging?offering_id=222&offering_id=333"},
            {"link": "vinted://messaging?offering_id=0"},
        ):
            self.assertIsNone(offers.favourite(dict(row, **changes)), changes)

    def test_notification_diagnostics_never_log_bodies_links_or_ids(self):
        row = notification(
            item=98765432101,
            recipient=98765432102,
            body="Private buyer text",
            entry_type=20,
            subject_id=98765432101,
            user_id=98765432102,
            updated_at="2026-10-21T11:00:00Z",
        )
        with self.assertLogs("vinted_offers", level="INFO") as logs:
            offers.notification_shape([row], 1)
        rendered = " ".join(logs.output)
        self.assertNotIn("Private buyer", rendered)
        self.assertNotIn("98765432101", rendered)
        self.assertNotIn("98765432102", rendered)
        self.assertNotIn("https://", rendered)
        self.assertIn("updated_dates=1", rendered)
        self.assertIn("supported_favourites=1", rendered)

    def test_gateway_pagination_keeps_scanning_when_server_returns_short_page(self):
        self.enable("preview", history="all")
        rows = [notification(item=1000 + i) for i in range(5)]
        client = Mock()
        client.request.return_value = {
            "notifications": rows,
            "pagination": {"current_page": 1, "total_pages": 3, "per_page": 5},
        }
        self.worker.scan(client, offers.state())
        self.assertEqual(offers.state()["scan_page"], 2)
        client.request.assert_called_once_with(
            "GET", buyer.NOTIFICATIONS_PATH, params={"page": 1, "per_page": 20}
        )

    def test_paused_worker_makes_no_marketplace_requests(self):
        self.worker.tick()
        self.factory.assert_not_called()
        self.assertEqual(self.client.calls, [])

    def test_preview_reads_prices_without_creating_conversations_or_offers(self):
        self.enable("preview")
        self.client.notifications = [notification()]
        self.worker.tick()
        self.now += 5
        self.worker.tick()
        self.assertEqual(self.jobs()[0]["status"], "ready")
        self.assertEqual(self.jobs()[0]["offer_price"], 2150)
        self.assertFalse(any(c[0] != "GET" for c in self.client.calls))
        count = self.factory.call_count
        self.now += 5
        self.worker.tick()
        self.assertEqual(self.factory.call_count, count)

    def test_more_than_two_notification_pages_and_dedup_survive_restart(self):
        self.enable("preview", history="all")
        self.client.notifications = [
            notification(item=1000 + i, recipient=300 + i) for i in range(65)
        ]
        for _ in range(4):
            self.worker.tick()
            self.now += 5
        self.assertEqual(len(self.jobs()), 65)
        self.assertEqual(offers.state()["scan_page"], 1)
        self.enable("preview", history="all")
        restarted = offers.OfferWorker(
            client_factory=self.factory,
            clock=lambda: self.now,
            item_reader=self.item_read,
        )
        for _ in range(4):
            restarted.tick()
            self.now += 5
        self.assertEqual(len(self.jobs()), 65)

    def test_history_scope_and_likes_from_self_are_excluded(self):
        self.enable("preview", history="new")
        self.client.notifications = [
            notification(at=NOW - 1),
            notification(item=1002, at=NOW + 1),
            notification(item=1003, recipient=111),
        ]
        self.now += 5
        self.worker.tick()
        self.assertEqual([j["item_id"] for j in self.jobs()], ["1002"])
        since = offers.state()["since"]
        self.now += 500
        self.enable("paused", history="new")
        self.enable("preview", history="new")
        self.assertEqual(offers.state()["since"], since)

    def test_send_uses_current_price_and_seller_endpoint_exactly_once(self):
        self.enable(rounding="penny")
        self.add()
        self.client.price = "30.00"
        self.worker.tick()
        self.assertEqual(
            self.client.writes(),
            [
                (
                    "POST",
                    "/api/v2/transactions/8001/offers",
                    {"offer": {"price": "28.50", "currency": "GBP"}},
                )
            ],
        )
        self.assertEqual(self.jobs()[0]["status"], "sent")
        self.assertEqual(self.jobs()[0]["price"], 3000)
        self.assertTrue(
            all(
                "payment" not in c[1]
                and "checkout" not in c[1]
                and "replies" not in c[1]
                for c in self.client.calls
            )
        )
        self.client.notifications = [notification()]
        self.now += 300
        self.worker.tick()
        self.worker.tick()
        self.assertEqual(len(self.client.writes()), 1)

    def test_three_minute_global_spacing_persists_across_workers_and_restart(self):
        self.enable()
        self.add()
        self.add(item=1002, recipient=223)
        self.worker.tick()
        for offset in (1, 5, 60, 179):
            self.now = NOW + offset
            offers.OfferWorker(
                client_factory=self.factory,
                clock=lambda: self.now,
                item_reader=self.item_read,
            ).tick()
            self.assertEqual(len(self.client.writes()), 1)
        self.now = NOW + 180
        self.worker.tick()
        self.assertEqual(len(self.client.writes()), 2)

    def test_same_buyer_can_receive_offers_on_different_items_without_daily_cap(self):
        self.enable()
        self.add()
        self.add(item=1002)
        self.worker.tick()
        self.now += 180
        self.worker.tick()
        self.assertEqual([j["status"] for j in self.jobs()], ["sent", "sent"])

    def test_250_offers_fit_thirteen_hours_at_configured_spacing(self):
        self.enable()
        for i in range(250):
            self.add(item=1000 + i, recipient=1000 + i)
        for _ in range(250):
            self.worker.tick()
            self.now += 180
        rows = self.jobs()
        self.assertEqual(sum(j["status"] == "sent" for j in rows), 250)
        self.assertLess(rows[-1]["sent_at"] - NOW, 13 * 3600)
        self.assertTrue(
            all(b["sent_at"] - a["sent_at"] >= 180 for a, b in pairwise(rows))
        )

    def test_sold_reserved_hidden_foreign_and_bad_prices_are_not_sent(self):
        self.enable()
        cases = [
            {"is_closed": True},
            {"is_reserved": True},
            {"is_hidden": True},
            {"user": {"id": 999}},
            {"price": {"amount": "23", "currency_code": "EUR"}},
            {"price": {"amount": "NaN", "currency_code": "GBP"}},
        ]
        for i, change in enumerate(cases):
            item = str(1000 + i)
            self.add(item=int(item))
            base = self.client.request("GET", f"/api/v2/items/{item}")["item"]
            self.client.items[item] = dict(base, **change)
            self.worker.tick()
        self.assertFalse(self.client.writes())
        self.assertTrue(all(j["status"] == "skipped" for j in self.jobs()))

    def test_recipient_mismatch_pauses_before_offer(self):
        self.enable()
        self.add()
        self.client.conversation_changes = {"opposite_user": {"id": 999}}
        self.worker.tick()
        self.assertEqual(offers.state()["mode"], "paused")
        self.assertEqual(self.jobs()[0]["reason"], "recipient")
        self.assertFalse(self.client.writes())

    def test_existing_offer_is_not_overwritten_or_repeated(self):
        self.enable()
        self.add()
        self.client.messages = [
            {"entity_type": "offer_request_message", "entity": {"price": "20"}}
        ]
        self.worker.tick()
        self.assertEqual(self.jobs()[0]["reason"], "existing_offer")
        self.assertFalse(self.client.writes())

    def test_pause_during_checks_prevents_next_write(self):
        self.enable()
        self.add()

        def pause_in_read(method, path, body):
            if path == "/api/v2/conversations/7001":
                offers.pause("")

        self.client.fail = pause_in_read
        self.worker.tick()
        self.assertEqual(self.jobs()[0]["status"], "queued")
        self.assertFalse(self.client.writes())

    def test_rate_limit_and_security_failures_pause_without_solver_or_replay(self):
        for status, reason in (
            (429, "rate_limited"),
            (403, "security_challenge"),
            (401, "credentials"),
        ):
            self.now += 1000
            self.enable()

            def refused(method, path, body, status=status, reason=reason):
                raise buyer.BuyerError(
                    "private body must never be shown", status, reason=reason
                )

            self.client.fail = refused
            self.worker.tick()
            self.assertEqual(offers.state()["mode"], "paused")
            count = len(self.client.calls)
            self.now += 200
            self.worker.tick()
            self.assertEqual(len(self.client.calls), count)
            self.assertFalse(self.client.writes())

    def test_network_failure_after_offer_is_uncertain_and_never_replayed(self):
        self.enable()
        self.add()

        def failed(method, path, body):
            if path.endswith("/offers"):
                raise buyer.BuyerError("network", reason="network")

        self.client.fail = failed
        self.worker.tick()
        self.assertEqual(self.jobs()[0]["status"], "uncertain")
        self.assertEqual(offers.state()["mode"], "paused")
        self.client.fail = None
        self.now += 400
        self.enable()
        self.worker.tick()
        self.assertEqual(len(self.client.writes()), 1)

    def test_unreadable_success_is_uncertain_not_sent(self):
        self.enable()
        self.add()
        self.client.offer_result = {"code": 0}
        self.worker.tick()
        self.assertEqual(self.jobs()[0]["status"], "uncertain")
        self.assertIsNone(offers.state()["last_sent"])

    def test_interrupted_send_is_not_reclaimed_or_replayed(self):
        self.enable()
        job_id = self.add()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_offer_jobs SET status='sending',lease_until=? WHERE id=?",
                (NOW - 1, job_id),
            )
        self.worker.tick()
        self.assertEqual(self.jobs()[0]["status"], "uncertain")
        self.assertEqual(offers.state()["mode"], "paused")
        self.factory.assert_not_called()

    def test_expired_check_restarts_safely_and_skip_is_persistent(self):
        self.enable("preview")
        first = self.add()
        second = self.add(item=1002)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_offer_jobs SET status='checking',lease_until=? WHERE id=?",
                (NOW - 1, first),
            )
        self.assertTrue(offers.cancel(second))
        self.worker.tick()
        self.assertEqual([j["status"] for j in self.jobs()], ["ready", "skipped"])
        self.assertTrue(offers.cancel(first))

    def test_account_swap_stops_all_work_before_marketplace_writes(self):
        self.enable()
        self.add()
        self.client._verified_session = ("999", b"offline", {})
        self.worker.tick()
        self.assertEqual(offers.state()["pause_reason"], "account_changed")
        self.assertFalse(self.client.calls)

    def test_busy_buyer_lock_does_not_wait_or_issue_requests(self):
        self.enable()
        with patch.object(
            buyer, "exclusive", side_effect=buyer.BuyerError("busy", reason="busy")
        ):
            self.worker.tick()
        self.factory.assert_not_called()
        self.assertEqual(offers.state()["mode"], "live")

    def test_preview_does_not_require_autobuy_permission(self):
        self.enable("preview")
        self.add()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET enabled=0 WHERE id=1")
        self.worker.tick()
        self.assertEqual(self.jobs()[0]["status"], "ready")

    def test_current_wardrobe_api_and_listing_page_recheck_price(self):
        self.client.wardrobe = [
            self.client.request("GET", "/api/v2/items/1001")["item"]
        ]
        self.client.price = "30.00"
        reader = offers.SellerItems(lambda: self.now)
        data = reader.read(self.client, {"account_id": "111", "item_id": "1001"})
        self.assertEqual(
            offers.checked_item(
                data, {"account_id": "111", "item_id": "1001"}, "penny"
            )["offer_price"],
            2850,
        )
        self.assertTrue(
            any(
                c[1] == "/api/v2/wardrobe/111/items?page=1&per_page=96&order=relevance"
                for c in self.client.calls
            )
        )

    def test_default_worker_runs_notification_wardrobe_page_conversation_offer_flow(
        self,
    ):
        self.enable()
        self.client.notifications = [notification()]
        self.client.wardrobe = [
            self.client.request("GET", "/api/v2/items/1001")["item"]
        ]
        worker = offers.OfferWorker(client_factory=self.factory, clock=lambda: self.now)
        worker.tick()
        self.now += 5
        worker.tick()
        self.assertEqual(self.jobs()[0]["status"], "sent")
        self.assertEqual(self.jobs()[0]["offer_price"], 2150)
        self.assertEqual(len(self.client.writes()), 1)
        self.assertTrue(
            any("/wardrobe/111/items?" in path for _, path, _ in self.client.calls)
        )

    def test_default_worker_keeps_older_wardrobe_item_queued_between_pages(self):
        self.enable()
        self.add(item=1099)
        self.client.wardrobe = [
            self.client.request("GET", f"/api/v2/items/{1000+i}")["item"]
            for i in range(100)
        ]
        worker = offers.OfferWorker(client_factory=self.factory, clock=lambda: self.now)
        worker.tick()
        self.assertEqual(self.jobs()[0]["status"], "queued")
        self.assertEqual(self.jobs()[0]["reason"], "inventory")
        self.now += 5
        worker.tick()
        self.assertEqual(self.jobs()[0]["status"], "sent")

    def test_wardrobe_pagination_yields_between_pages_and_keeps_old_items(self):
        self.client.wardrobe = [
            self.client.request("GET", f"/api/v2/items/{1000+i}")["item"]
            for i in range(100)
        ]
        job = {"account_id": "111", "item_id": "1099"}
        reader = offers.SellerItems(lambda: self.now)
        with self.assertRaises(offers.InventoryPending):
            reader.read(self.client, job)
        self.now += 5
        data = reader.read(self.client, job)
        self.assertEqual(data["item"]["id"], "1099")
        pages = [c for c in self.client.calls if c[1].startswith("/api/v2/wardrobe/")]
        self.assertEqual(len(pages), 2)

    def test_missing_or_closed_wardrobe_item_cannot_be_offered(self):
        job = {"account_id": "111", "item_id": "1001"}
        with self.assertRaises(offers.OfferCheck) as absent:
            offers.SellerItems(lambda: self.now).read(self.client, job)
        self.assertEqual(absent.exception.reason, "unavailable")
        self.client.wardrobe = [
            dict(
                self.client.request("GET", "/api/v2/items/1001")["item"], is_closed=True
            )
        ]
        with self.assertRaises(offers.OfferCheck):
            offers.SellerItems(lambda: self.now).read(self.client, job)
        self.assertFalse(self.client.writes())

    def test_cooldown_cannot_be_cleared_by_resume(self):
        self.enable()
        self.add()
        self.client.fail = lambda *args: (_ for _ in ()).throw(
            buyer.BuyerError("wait", 429, reason="rate_limited", retry_after=3600)
        )
        self.worker.tick()
        self.assertEqual(offers.state()["cooldown_until"], NOW + 3600)
        self.client.fail = None
        self.now += 900
        self.enable()
        count = len(self.client.calls)
        self.worker.tick()
        self.assertEqual(len(self.client.calls), count)
        self.now = NOW + 3601
        self.worker.tick()
        self.assertEqual(len(self.client.writes()), 1)

    def test_retry_after_headers_are_carried_without_echoing_response(self):
        response = Mock(
            status_code=429, headers={"Retry-After": "1200"}, text="private body"
        )
        error = buyer.response_error(response, {"message": "private body"}, "request")
        self.assertEqual(error.retry_after, 1200)
        self.assertNotIn("private body", str(error))

    def test_unknown_item_availability_pauses_before_contacting_anyone(self):
        self.enable()
        self.add()
        item = self.client.request("GET", "/api/v2/items/1001")["item"]
        item.pop("is_closed")
        self.client.items["1001"] = item
        self.worker.tick()
        self.assertEqual(offers.state()["mode"], "paused")
        self.assertFalse(any(c[0] == "POST" for c in self.client.calls))


class OfferGatewayTests(DatabaseFixture, unittest.TestCase):
    def test_only_exact_read_only_gateway_has_web_headers(self):
        from test_vinted_network import response

        client = buyer.Client()
        client.solver_attempted = True
        try:
            with patch.object(
                client.session,
                "request",
                return_value=response(data={"notifications": []}),
            ) as request:
                result = client.request(
                    "GET", buyer.NOTIFICATIONS_PATH, params={"page": 2, "per_page": 20}
                )
            self.assertEqual(result, {"notifications": []})
            self.assertEqual(
                request.call_args.args,
                (
                    "GET",
                    "https://api.vinted.co.uk/inbox-notifications/v1/notifications",
                ),
            )
            self.assertEqual(
                request.call_args.kwargs["params"], {"page": 2, "per_page": 20}
            )
            self.assertEqual(
                request.call_args.kwargs["headers"],
                {
                    "Platform": "web",
                    "X-Next-App": "marketplace-web",
                    "Sec-Fetch-Site": "same-site",
                },
            )
            self.assertFalse(request.call_args.kwargs["allow_redirects"])
            self.assertNotIn("Authorization", client.session.headers)
        finally:
            client.session.close()

    def test_gateway_rejects_mutations_or_unbounded_parameters_before_network(self):
        client = buyer.Client()
        try:
            with patch.object(client.session, "request") as request:
                for method, body, params in (
                    ("POST", None, {"page": 1, "per_page": 20}),
                    ("GET", {"anything": "not allowed"}, {"page": 1, "per_page": 20}),
                    ("GET", None, {"page": True, "per_page": 20}),
                    ("GET", None, {"page": 0, "per_page": 20}),
                    ("GET", None, {"page": 1, "per_page": 101}),
                    (
                        "GET",
                        None,
                        {"page": 1, "per_page": 20, "url": "https://untrusted.invalid"},
                    ),
                    ("GET", None, None),
                ):
                    with self.assertRaises(buyer.BuyerError):
                        client.request(
                            method, buyer.NOTIFICATIONS_PATH, body, params=params
                        )
                request.assert_not_called()
        finally:
            client.session.close()

    def test_gateway_refusal_never_replays_an_old_route(self):
        from test_vinted_network import response

        client = buyer.Client()
        client.solver_attempted = True
        try:
            with patch.object(
                client.session,
                "request",
                return_value=response(status=404, text="not found"),
            ) as request, self.assertRaises(buyer.BuyerError):
                client.request(
                    "GET",
                    buyer.NOTIFICATIONS_PATH,
                    params={"page": 1, "per_page": 20},
                )
            self.assertEqual(request.call_count, 1)
        finally:
            client.session.close()


class OfferRouteTests(DashboardTests):
    def test_offers_auth_csrf_render_and_pause(self):
        self.assertEqual(self.client.get("/offers").location, "/login")
        self.owner()
        self.assertEqual(self.client.get("/offers").status_code, 200)
        self.assertIn(b"Offers to favourites", self.client.get("/offers").data)
        self.assertEqual(self.client.post("/offers/pause").status_code, 400)
        self.assertEqual(
            self.client.post(
                "/offers/settings",
                data={
                    "csrf": "offline-csrf",
                    "mode": "live",
                    "rounding": "penny",
                    "history": "today",
                },
            ).status_code,
            302,
        )
        self.assertEqual(offers.state()["mode"], "paused")  # No connected account.
        self.assertEqual(
            self.client.post(
                "/offers/pause", data={"csrf": "offline-csrf"}
            ).status_code,
            302,
        )

    def test_queue_text_is_escaped_and_reads_never_send_offers(self):
        self.owner()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_offer_settings SET account_id='111'")
            conn.execute(
                """INSERT INTO vinted_offer_jobs
                (account_id,item_id,recipient_id,notification_id,liked_at,created,title)
                VALUES ('111','1001','222','1',?,?,'<script>unsafe</script>')""",
                (NOW, NOW),
            )
        with patch.object(buyer, "connected_client") as factory:
            data = self.client.get("/offers").data
        self.assertNotIn(b"<script>unsafe</script>", data)
        self.assertIn(b"&lt;script&gt;", data)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
