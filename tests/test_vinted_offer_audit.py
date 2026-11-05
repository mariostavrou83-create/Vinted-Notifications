"""The support audit is bounded, account-scoped and read-only."""

import json
import unittest
from contextlib import closing
from unittest.mock import patch

from test_search_controls import DatabaseFixture
import search_settings
import vinted_offer_audit as audit


class AuditTests(DatabaseFixture, unittest.TestCase):
    def test_read_only_matches_include_untitled_jobs_without_recipient_data(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE vinted_offer_settings SET account_id='111' WHERE id=1")
            conn.executemany(
                "INSERT INTO vinted_offer_jobs "
                "(account_id,item_id,recipient_id,notification_id,liked_at,created,title,status,reason) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [("111", "42", "private-recipient-1", "private-notice-1", 10, 10,
                  "Cop.Copine skirt", "sent", ""),
                 ("111", "42", "private-recipient-2", "private-notice-2", 20, 20,
                  "", "skipped", "unavailable"),
                 ("999", "42", "private-recipient-3", "private-notice-3", 30, 30,
                  "Cop.Copine other account", "queued", "")],
            )
            before = list(conn.execute("SELECT * FROM vinted_offer_jobs"))
        traced = []
        connect = search_settings.connection

        def tracking():
            conn = connect()
            conn.set_trace_callback(traced.append)
            return conn

        with patch.object(audit, "connection", tracking):
            data = audit.snapshot("cop")
        self.assertEqual([r["status"] for r in data["matches"]], ["skipped", "sent"])
        self.assertEqual(len(data["recent"]), 2)
        self.assertNotIn("private-", json.dumps(data))
        self.assertNotIn("other account", json.dumps(data))
        self.assertFalse(any(s.lstrip().upper().startswith(
            ("INSERT", "UPDATE", "DELETE", "BEGIN")) for s in traced))
        with closing(connect()) as conn:
            self.assertEqual(before, list(conn.execute("SELECT * FROM vinted_offer_jobs")))

    def test_off_by_default_and_invalid_filters_make_no_database_calls(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(audit, "connection") as conn:
            audit.log_on_start()
            for value in ("", " " * 3, "x" * 81, None):
                self.assertIsNone(audit.snapshot(value))
            conn.assert_not_called()

    def test_diagnostic_failures_are_contained(self):
        with patch.dict("os.environ", {"MSJ_OFFER_AUDIT_TITLE": "cop"}), \
             patch.object(audit, "snapshot", side_effect=ValueError("secret")), \
             self.assertLogs(audit.logger, level="WARNING") as logs:
            audit.log_on_start()
        self.assertNotIn("secret", " ".join(logs.output))
