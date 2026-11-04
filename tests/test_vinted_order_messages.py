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
