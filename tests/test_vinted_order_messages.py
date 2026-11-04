"""Order messaging uses offline account fixtures; never send live test messages."""

import unittest
from unittest.mock import Mock, patch

from test_search_controls import DatabaseFixture
import vinted_order_messages as messages


class OrderMessageTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        messages.ensure_schema()
        self.client = Mock()
        self.client._verified_session = ("111", b"offline", {})
        self.client.request.return_value = {"my_orders": [], "pagination": {"total_pages": 1}}
        self.factory = Mock(return_value=self.client)
        self.worker = messages.OrderMessageWorker(self.factory, clock=lambda: 1000)

    def configure(self, mode):
        with patch.object(messages.buyer, "settings", return_value={"connected": True, "user_id": "111"}):
            messages.configure(mode, messages.DEFAULTS)

    def test_paused_worker_performs_no_account_access(self):
        self.worker.tick()
        self.factory.assert_not_called()

    def test_preview_only_reads_and_closes_account_session(self):
        self.configure("preview")
        self.worker.tick()
        self.assertEqual(len(self.client.request.call_args_list), 2)
        self.assertTrue(all(c.args[0] == "GET" for c in self.client.request.call_args_list))
        self.assertEqual(messages.state()["last_scan"], 1000)
        self.client.session.close.assert_called_once()

    def test_wrong_account_cannot_read_orders(self):
        self.configure("preview")
        self.client._verified_session = ("222", b"other", {})
        self.worker.tick()
        self.assertIn("account changed", messages.state()["error"])
        self.client.request.assert_not_called()

    def test_evidence_retains_status_without_chat_or_personal_details(self):
        result = messages.evidence({"is_paid": True, "body": "private chat", "address": {"street": "private street"}, "delivered_at": 1000})
        self.assertTrue(result["is_paid"])
        self.assertEqual(result["delivered_at"], 1000)
        self.assertNotIn("private", str(result))

    def test_message_gateway_limits_paths_and_keeps_personal_data_check(self):
        from test_vinted_network import response
        client = messages.buyer.Client()
        path = "/messaging/main/conversations/01a1a36a-3a23-7d6b-902b-bc85860d0ebf"
        body = {"content": "Approved test text", "is_personal_data_sharing_check_skipped": False, "photo_temp_uuids": None}
        try:
            with patch.object(client.session, "request", return_value=response(data={"id": "offline"})) as request:
                client.request("GET", path)
                self.assertEqual(request.call_args.args, ("GET", "https://api.vinted.co.uk" + path))
                client.request("POST", path + "/replies", body)
                self.assertEqual(request.call_args.kwargs["json"], body)
                self.assertFalse(request.call_args.kwargs["allow_redirects"])
                count = request.call_count
                for method, suffix, payload in [("DELETE", "", None), ("POST", "", body), ("POST", "/replies", dict(body, is_personal_data_sharing_check_skipped=True)), ("GET", "/replies", None)]:
                    with self.assertRaises(messages.buyer.BuyerError):
                        client.request(method, path + suffix, payload)
                self.assertEqual(request.call_count, count)
        finally:
            client.session.close()

from contextlib import closing
from datetime import datetime, timezone
import copy
import vinted_order_message_store as queue
from search_settings import connection

CID = "01234567-89ab-cdef-0123-456789abcdef"
NOW = 1793809000.0


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class AccountFixture:
    def __init__(self, clock):
        self.clock = clock
        self._verified_session = ("111", b"offline", {})
        self.session = Mock()
        self.rows = {"purchased": [], "sold": []}
        self.transactions = {}
        self.conversations = {}
        self.posts = []
        self.lose_receipt = False

    def request(self, method, path, body=None):
        if "/my_orders?" in path:
            kind = "purchased" if "type=purchased" in path else "sold"
            return {"my_orders": copy.deepcopy(self.rows[kind]), "pagination": {"total_pages": 1}}
        if path.startswith("/api/v2/transactions/"):
            return {"transaction": copy.deepcopy(self.transactions[path.rsplit('/', 1)[1]])}
        if method == "GET":
            return copy.deepcopy(self.conversations[path.rsplit('/', 1)[1]])
        self.posts.append((path, copy.deepcopy(body)))
        self.conversations[CID]["messages"].append({"id": "receipt-1", "sender_id": "111", "created_at": iso(self.clock()), "data": {"entity_type": "message", "entity": {"body": body["content"]}}})
        if self.lose_receipt:
            raise messages.buyer.BuyerError("offline timeout", reason="network")
        return {"id": "receipt-1"}


class DurableMessageTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        messages.ensure_schema()
        self.now = NOW
        self.account = AccountFixture(lambda: self.now)
        self.worker = messages.OrderMessageWorker(lambda: self.account, clock=lambda: self.now)
        self.set_mode("preview")
        self.worker.tick()
        self.set_mode("live")

    def set_mode(self, mode):
        with patch.object(messages.buyer, "settings", return_value={"connected": True, "user_id": "111"}):
            messages.configure(mode, messages.DEFAULTS, now=self.now)

    def order(self, side="seller", paid_at=None, delivered_at=None, status="Shipping label sent to seller."):
        paid_at = self.now if paid_at is None else paid_at
        kind = "sold" if side == "seller" else "purchased"
        row = {"transaction_id": 123, "conversation_id": CID, "date": iso(paid_at), "status": status, "transaction_user_status": "waiting"}
        self.account.rows[kind] = [row]
        self.account.transactions["123"] = {
            "id": 123, "user_side": side, "buyer_id": 222 if side == "seller" else 111,
            "seller_id": 111 if side == "seller" else 222,
            "debit_processed_at": iso(paid_at), "status": 230,
            "status_title": status, "suspending_complaint_id": None,
            "shipment": {"status": 400 if delivered_at else 230,
                         "status_updated_at": iso(delivered_at or paid_at)},
        }
        self.account.conversations[CID] = {"id": CID, "allow_reply": True,
            "opposite_users": [{"data": {"id": "222"}}], "messages": []}
        if delivered_at:
            self.account.conversations[CID]["messages"].append({"created_at": iso(delivered_at), "data": {"entity_type": "status_message", "event_type": "delivery_completed"}})
        return row

    def jobs(self):
        return messages.state()["jobs"]

    def test_purchase_and_sale_wait_full_three_minutes_and_survive_restart(self):
        for side, kind in [("seller", "sale"), ("buyer", "purchase")]:
            with self.subTest(side=side):
                # Separate transaction IDs would represent separate orders;
                # use a fresh database test iteration for the other role.
                if side == "buyer":
                    with closing(connection()) as conn, conn:
                        for table in ("vinted_message_jobs", "vinted_message_orders", "vinted_message_discovery"):
                            conn.execute("DELETE FROM " + table)
                    self.account.rows["sold"] = []
                self.order(side=side)
                self.worker.scan(self.account, messages.state())
                self.worker.tick()
                self.assertEqual(self.jobs()[0]["due_at"], self.now + 180)
                self.assertEqual(self.jobs()[0]["kind"], kind)
                self.now += 179
                self.worker.tick()
                before = len(self.account.posts)
                self.assertEqual(before, 0 if side == "seller" else 1)
                self.now += 1
                restarted = messages.OrderMessageWorker(lambda: self.account, clock=lambda: self.now)
                restarted.tick()
                restarted.tick()
                self.assertEqual(len(self.account.posts), before + 1)
                self.assertEqual(self.account.posts[-1][1]["content"], messages.DEFAULTS[kind])
                self.assertEqual(self.jobs()[0]["status"], "sent")

    def test_review_uses_delivery_not_purchase_dispatch_or_completion(self):
        self.now += 10
        self.order(paid_at=NOW - 86400, delivered_at=self.now, status="Package delivered.")
        self.worker.tick()
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["kind"], "review")
        self.assertEqual(jobs[0]["due_at"], self.now + 172800)
        self.now += 172799
        self.worker.tick()
        self.assertFalse(self.account.posts)
        self.now += 1
        self.worker.tick()
        self.assertEqual(len(self.account.posts), 1)
        self.assertEqual(self.account.posts[0][1]["content"], messages.DEFAULTS["review"])

    def test_existing_old_orders_do_not_receive_backdated_messages(self):
        self.order(paid_at=NOW - 90000, delivered_at=NOW - 1000, status="Order completed!")
        self.worker.tick()
        self.assertFalse(self.jobs())
        self.assertFalse(self.account.posts)

    def test_unpaid_and_dispatched_are_not_delivery_events(self):
        self.order(status="Order sent and on its way!")
        self.account.transactions["123"]["debit_processed_at"] = None
        self.worker.tick()
        self.assertFalse(self.jobs())
        self.assertFalse(self.account.posts)

    def test_cancellation_is_rechecked_immediately_before_send(self):
        row = self.order()
        self.worker.tick()
        row["status"] = "Cancelled. Refund processed."
        row["transaction_user_status"] = "failed"
        self.now += 180
        self.worker.tick()
        self.assertFalse(self.account.posts)
        self.assertEqual(self.jobs()[0]["status"], "skipped")

    def test_wrong_conversation_recipient_never_queues_or_sends(self):
        self.order()
        self.account.conversations[CID]["opposite_users"][0]["data"]["id"] = "999"
        self.worker.tick()
        self.assertFalse(self.jobs())
        self.assertIn("recipient", messages.state()["check_errors"][0]["last_error"])

    def test_pause_prevents_due_post_and_resume_keeps_original_timer(self):
        self.order()
        self.worker.tick()
        due = self.jobs()[0]["due_at"]
        self.set_mode("paused")
        self.now += 180
        self.worker.tick()
        self.assertFalse(self.account.posts)
        self.set_mode("live")
        self.worker.tick()
        self.assertEqual(self.jobs()[0]["due_at"], due)
        self.assertEqual(len(self.account.posts), 1)

    def test_lost_post_receipt_is_reconciled_without_resending(self):
        self.order()
        self.worker.tick()
        self.account.lose_receipt = True
        self.now += 180
        self.worker.tick()
        self.assertEqual(self.jobs()[0]["status"], "uncertain")
        restarted = messages.OrderMessageWorker(lambda: self.account, clock=lambda: self.now)
        restarted.tick()
        self.assertEqual(len(self.account.posts), 1)
        self.assertEqual(self.jobs()[0]["status"], "sent")

    def test_committed_send_survives_crash_without_automatic_replay(self):
        self.order()
        self.worker.tick()
        self.now += 180
        job = queue.due("111", self.now)
        self.assertTrue(queue.begin_send(job, self.now))
        self.assertFalse(queue.begin_send(job, self.now))
        restarted = messages.OrderMessageWorker(lambda: self.account, clock=lambda: self.now)
        restarted.tick()
        self.assertFalse(self.account.posts)
        self.assertEqual(self.jobs()[0]["status"], "uncertain")

    def test_duplicate_observation_does_not_shift_due_or_duplicate_queue(self):
        self.order()
        for i in range(5):
            self.worker.scan(self.account, messages.state())
            self.worker.tick()
            self.now += 20
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.jobs()[0]["due_at"], NOW + 180)

    def test_return_in_progress_never_requests_review(self):
        self.order(paid_at=NOW - 1000, delivered_at=NOW, status="Return started")
        self.worker.tick()
        self.assertFalse(self.jobs())

    def test_api_unknown_state_does_not_send(self):
        self.order(status="Unrecognised order state")
        self.worker.tick()
        self.assertFalse(self.jobs())

    def test_deployment_activation_is_once_only_and_preserves_later_pause(self):
        self.set_mode("preview")
        with patch.object(messages.buyer, "settings", return_value={"connected": True, "user_id": "111"}):
            self.assertTrue(messages.apply_requested_activation("111:approved-v1", now=self.now))
        self.assertEqual(messages.state()["mode"], "live")
        self.set_mode("paused")
        with patch.object(messages.buyer, "settings", return_value={"connected": True, "user_id": "111"}):
            self.assertFalse(messages.apply_requested_activation("111:approved-v1", now=self.now + 1))
        self.assertEqual(messages.state()["mode"], "paused")

    def test_deployment_activation_rejects_wrong_account_or_changed_text(self):
        self.set_mode("preview")
        with patch.object(messages.buyer, "settings", return_value={"connected": True, "user_id": "111"}):
            with self.assertRaises(ValueError):
                messages.apply_requested_activation("222:approved-v1", now=self.now)
            changed = dict(messages.DEFAULTS, review="Different text")
            messages.configure("preview", changed, now=self.now)
            with self.assertRaises(ValueError):
                messages.apply_requested_activation("111:approved-v1", now=self.now)
        self.assertEqual(messages.state()["mode"], "preview")
