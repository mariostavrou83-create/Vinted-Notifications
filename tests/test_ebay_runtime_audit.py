"""Full quota boundaries and local rejection/delivery visibility."""

import json
import unittest
from collections import Counter
from contextlib import closing
from unittest.mock import patch

from test_ebay_monitor import EbayFixture, item
import ebay_monitor
import ebay_runtime_audit as audit
import ebay_store
import search_settings


class AuditTests(EbayFixture, unittest.TestCase):
    def test_all_5000_calls_are_available_but_the_5001st_is_blocked(self):
        config = {"daily_budget": 5000}
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("INSERT INTO ebay_call_buckets VALUES (10,4999)")
        self.assertIsNone(ebay_store.reserve_call(config, 1000))
        self.assertIsNotNone(ebay_store.reserve_call(config, 1100))
        self.assertIsNotNone(ebay_store.reserve_call(config, 1100, diagnostic=True))
        self.assertIsNotNone(ebay_store.reserve_call(config, 1100, media=True))
        with closing(search_settings.connection()) as conn:
            self.assertEqual(conn.execute("SELECT SUM(calls) FROM ebay_call_buckets").fetchone()[0], 5000)
        self.assertAlmostEqual(ebay_store.call_spacing(config) * 10, 172.8)

    def test_filter_report_explains_new_items_without_replaying_baseline(self):
        search = self.enable()
        self.snapshot(search, [item(1, created=900)], 1000)
        self.snapshot(search, [item(1, created=900), item(2, price="90"), item(3)], 1020)
        result = audit.runtime_summary(ebay_store.configuration(), ebay_monitor.grouped_searches(ebay_store.active_searches()), 1030)
        counts = result["filter_counts"][str(search["id"])]
        self.assertEqual(counts["initial_baseline"], 1)
        self.assertEqual(counts["already_seen"], 1)
        self.assertEqual(counts["new_unseen"], 2)
        self.assertEqual(counts["above_maximum_budget"], 1)
        self.assertEqual(counts["queued"], 1)
        self.assertEqual(result["outbox_24h"][0]["count"], 1)
        self.assertEqual(len(self.outbox()), 1)

    def test_report_counts_usage_and_sent_history_without_account_secrets(self):
        search = self.enable()
        self.snapshot(search, [], 1000)
        self.snapshot(search, [item(1)], 1020)
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("INSERT INTO ebay_call_buckets VALUES(10,123)")
            conn.execute("UPDATE alert_outbox SET status='sent',sent_at=1025")
            audit.record_flow(conn, 1, Counter(queued=4), -50 * 3600)
        config = dict(ebay_store.configuration(), client_id="private-id", client_secret="private-secret", telegram_token="private-token", chat_id="private-chat")
        result = audit.runtime_summary(config, ebay_monitor.grouped_searches(ebay_store.active_searches()), 1030)
        self.assertEqual(result["local_reserved_calls_rolling_24h"], 123)
        self.assertEqual(result["sent_24h"], 1)
        self.assertEqual(result["filter_counts"]["1"]["queued"], 1)
        for secret in ("private-id", "private-secret", "private-token", "private-chat"):
            self.assertNotIn(secret, json.dumps(result))
        with closing(search_settings.connection()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM parameters WHERE key GLOB 'ebay_flow:*'").fetchone()[0], 1)

    def test_native_allowance_check_never_runs_a_search_or_sends_a_message(self):
        ebay_store.save_configuration({"source": "browse", "client_id": "test-id", "client_secret": "private-secret"})
        with patch("ebay_monitor.BrowseClient") as client, patch("ebay_quota.check_allowance") as check:
            audit.refresh_native_allowance()
            client.return_value.authenticate.assert_called_once()
            client.return_value.search.assert_not_called()
            check.assert_called_once()
