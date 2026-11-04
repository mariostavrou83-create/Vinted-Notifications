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
        self.client.request.return_value = {"orders": [], "pagination": {"total_pages": 1}}
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
        with self.assertRaises(ValueError):
            self.worker.tick()
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
