"""Observed overlaps retain actual listing identity and one durable alert."""

import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

import test_dashboard as dashboard_fixtures
import test_keyword_candidates as candidate_fixtures
from test_search_controls import DatabaseFixture

import db
import keyword_candidates
import search_match_audit as audit
import search_settings


class MatchAudit(DatabaseFixture, unittest.TestCase):
    def enable_audit(self, start=0):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT INTO search_audit_runs VALUES ('test',?,?,'sha','{}')",
                (start, start),
            )
            conn.executemany(
                "INSERT INTO search_audit_members VALUES ('test',?,?)",
                [(1, "old"), (2, "new"), (3, "new")],
            )

    def rows(self, table):
        with closing(search_settings.connection()) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM " + table)]

    def test_two_cohorts_and_multiple_searches_make_one_alert(self):
        self.enable_audit()
        for identity in (1, 2, 3, 2, 1):
            self.batch(identity, [201])
        self.assertEqual(len(self.rows("alert_outbox")), 1)
        self.assertEqual(
            {r["query_id"] for r in self.rows("search_match_log")}, {1, 2, 3}
        )
        self.assertEqual(sum(r["canonical"] for r in self.rows("search_match_log")), 1)
        self.assertEqual(audit.summary()["overlap"], 1)
        self.assertEqual(audit.summary()["counts"]["suppressed"], 2)
        self.assertEqual(self.rows("alert_outbox")[0]["item_id"], "201")
        self.assertEqual(len(self.rows("vinted_buy_attempts")), 0)

    def test_history_is_not_replayed_or_relabelled_as_a_new_match(self):
        self.batch(1, [201])
        self.enable_audit(start=time.time() + 10)
        self.batch(2, [201])
        self.assertEqual(self.rows("search_match_log"), [])
        self.assertEqual(len(self.rows("alert_outbox")), 1)

    def test_new_variant_baseline_remains_quiet_for_an_already_alerted_item(self):
        self.enable_audit()
        self.batch(1, [201])
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE queries SET last_item=NULL WHERE id=2")
        self.batch(2, [201])
        self.assertEqual(len(self.rows("search_match_log")), 1)
        self.assertEqual(len(self.rows("alert_outbox")), 1)

    def test_blocked_and_overbudget_searches_are_not_logged_as_matches(self):
        self.enable_audit()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("INSERT INTO search_preferences VALUES (2,'','[\"fur\"]')")
            conn.execute("INSERT INTO vinted_search_budgets VALUES (3,1000,220)")
        for identity in (1, 2, 3):
            self.batch(identity, [201])
        self.assertEqual([r["query_id"] for r in self.rows("search_match_log")], [1])

    def test_description_proof_logs_suppressed_match_without_another_outbox(self):
        self.enable_audit()
        self.batch(1, [9001], title="Zara jeans")
        # Use the real durable candidate worker, not a second alert path.
        search = candidate_fixtures.CandidateTests.enable(
            self, query_id=2, keywords=["skinny"]
        )
        item = candidate_fixtures.CandidateTests.item(
            self, title="Zara jeans", number=9001
        )
        alert = candidate_fixtures.CandidateTests.alert(self)
        keyword_candidates.stage("vinted", item, search, alert, now=1020)
        with patch.object(
            keyword_candidates,
            "fetch_candidate_details",
            return_value={
                "description": "skinny fit",
                "keyword_description": "skinny fit",
                "description_complete": True,
                "available": True,
            },
        ):
            self.assertTrue(keyword_candidates.process_one("vinted", now=1021))
        self.assertEqual(len(self.rows("alert_outbox")), 1)
        matches = self.rows("search_match_log")
        self.assertEqual(len(matches), 2)
        saved = json.loads(matches[-1]["evidence"])
        self.assertEqual(saved["keyword_evidence"]["field"], "description")

    def test_supplied_fee_survives_description_staging(self):
        search = candidate_fixtures.CandidateTests.enable(
            self, query_id=2, keywords=["skinny"]
        )
        search["vinted_max_total"] = 1100
        item = {
            "price": "7.00",
            "currency": "GBP",
            "raw_data": {
                "total_item_price": {"amount": "9.00", "currency_code": "GBP"}
            },
        }
        # Actual supplied protection + 2.20 exceeds 11.00, while fallback 10.25 would fit.
        self.assertFalse(keyword_candidates._budget(item, search, "vinted"))

    def test_stale_revision_does_not_record_rules_that_were_not_checked(self):
        self.enable_audit()
        search = search_settings.get_search(1)
        snap = audit.capture(search, {"keyword_evidence": {"state": "unrestricted"}})
        self.batch(1, [201])
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO search_dashboard(query_id,revision) VALUES (2,2)"
            )
            audit.record(conn, "201", 2, dict(snap, rules=dict(snap["rules"], id=2)))
        self.assertEqual(len(self.rows("search_match_log")), 1)

    def test_concurrent_direct_matches_share_one_listing_and_outbox(self):
        self.enable_audit()

        def record(identity):
            search = search_settings.get_search(identity)
            alert = {
                "search_name": search["query_name"],
                "content": "Test",
                "url": "https://www.vinted.co.uk/items/202",
                "match_audit": audit.capture(
                    search, {"keyword_evidence": {"state": "unrestricted"}}
                ),
            }
            return db.add_item_to_db(
                202, "Test", identity, "10", 200, None, "GBP", alert
            )

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(record, (1, 2, 3)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(len(self.rows("alert_outbox")), 1)
        self.assertEqual(
            len(self.rows("items")), 2
        )  # original fixture + one real listing
        self.assertEqual(len(self.rows("search_match_log")), 3)

    def test_audit_export_is_owner_only_and_contains_delivery_attribution(self):
        from web_ui_plugin.web_ui import create_app

        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()
        self.assertEqual(self.client.get("/search-match-audit.json").status_code, 302)
        dashboard_fixtures.DashboardTests.owner(self)
        self.enable_audit()
        self.batch(1, [201])
        self.batch(2, [201])
        response = self.client.get("/search-match-audit.json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json["summary"]["overlap"], 1)
        self.assertEqual(len(response.json["matches"]), 2)
        self.assertEqual(
            {r["notified_query_id"] for r in response.json["matches"]}, {1}
        )
