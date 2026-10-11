"""Positive evidence is decided before Vinted global seen/outbox writes."""

import unittest
from contextlib import closing
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

from test_search_controls import DatabaseFixture

import dashboard_store
import db
import search_settings
import vinted_alerts
import vinted_keywords


class SharedPositiveVinted(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.identity = self.save("skinny")
        self.page(self.identity, 500, title="Zara wide-leg jeans")

    def save(self, word):
        return dashboard_store.save_search(
            None,
            {
                "shared_alert_version": "1",
                "query_name": "Shared Zara jeans",
                "platform_mode": "vinted",
                "query": "https://www.vinted.co.uk/catalog?catalog[]=183",
                "shared_keywords": word,
                "vinted_max_total": "25.00",
                "exclusions": "ripped",
                "reminder": "Check fit. Resell £45.",
            },
        )

    def page(self, identity, number, *, title, raw=None, price="15"):
        variant = vinted_keywords.rows(identity)[0]
        source, output = Queue(), Queue()
        source.put(
            (
                [
                    SimpleNamespace(
                        id=number,
                        title=title,
                        brand_title="Zara",
                        price=price,
                        currency="GBP",
                        photo=None,
                        url=f"https://www.vinted.co.uk/items/{number}",
                        has_real_timestamp=False,
                        raw_timestamp=1000,
                        observed_at=1000,
                        raw_data=raw or {},
                    )
                ],
                identity,
                variant["url"],
                variant["id"],
            )
        )
        with patch.object(self.core, "time", return_value=1000):
            self.core.clear_item_queue(source, output)
        return [output.get_nowait() for _ in range(output.qsize())]

    def test_title_match_keeps_existing_fast_path_without_detail_read(self):
        with patch("keyword_candidates.stage") as stage, patch(
            "requests.Session.request", side_effect=AssertionError("Network")
        ):
            self.assertEqual(
                len(self.page(self.identity, 501, title="Zara skinny jeans")), 1
            )
        stage.assert_not_called()
        self.assertTrue(db.is_item_in_db_by_id(501))
        with closing(search_settings.connection()) as conn:
            row = dict(
                conn.execute(
                    "SELECT * FROM alert_outbox WHERE item_id='501'"
                ).fetchone()
            )
        details = vinted_alerts.get_details(row)
        self.assertEqual(details["keyword_evidence"]["field"], "title")
        self.assertFalse(details["description_complete"])

    def test_supplied_description_match_is_fast_and_retained_for_delivery(self):
        with patch("keyword_candidates.stage") as stage, patch(
            "requests.Session.request", side_effect=AssertionError("Network")
        ):
            result = self.page(
                self.identity,
                501,
                title="Zara blue jeans",
                raw={"description": "These have a SKINNY fit."},
            )
        self.assertEqual(len(result), 1)
        stage.assert_not_called()
        with closing(search_settings.connection()) as conn:
            row = dict(
                conn.execute(
                    "SELECT * FROM alert_outbox WHERE item_id='501'"
                ).fetchone()
            )
        details = vinted_alerts.get_details(row)
        self.assertEqual(details["keyword_evidence"]["field"], "description")
        self.assertEqual(details["keyword_description"], "These have a SKINNY fit.")
        self.assertTrue(details["description_complete"])

    def test_confirmed_absence_is_only_filtered_for_this_search(self):
        other = self.save("bootcut")
        self.page(other, 500, title="Zara jeans")
        with patch("keyword_candidates.stage") as stage:
            result = self.page(
                self.identity,
                501,
                title="Zara bootcut jeans",
                raw={"description": "Straight fit denim."},
            )
        self.assertEqual(result, [])
        stage.assert_not_called()
        self.assertFalse(db.is_item_in_db_by_id(501))
        self.assertIn("501", search_settings.filtered_ids(self.identity))
        self.assertNotIn("501", search_settings.filtered_ids(other))
        self.assertEqual(len(self.page(other, 501, title="Zara bootcut jeans")), 1)

    def test_unknown_description_stages_before_advancing_frontier_or_global_seen(self):
        with patch("keyword_candidates.stage", return_value=True) as stage:
            self.assertEqual(self.page(self.identity, 501, title="Zara jeans"), [])
        stage.assert_called_once()
        platform, item, saved, alert = stage.call_args.args
        self.assertEqual(platform, "vinted")
        self.assertEqual(item["item_id"], "501")
        self.assertEqual(item["timestamp"], 1000)
        self.assertEqual(saved["id"], self.identity)
        self.assertEqual(alert["url"], item["url"])
        self.assertEqual(
            alert["vinted_details"]["keyword_evidence"]["state"], "unverified"
        )
        self.assertFalse(db.is_item_in_db_by_id(501))
        self.assertNotIn("501", search_settings.filtered_ids(self.identity))
        with closing(search_settings.connection()) as conn:
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM alert_outbox WHERE item_id='501'"
                ).fetchone()
            )
        self.assertEqual(vinted_keywords.rows(self.identity)[0]["max_item_id"], 501)

    def test_unknown_first_search_cannot_consume_other_title_positive_search(self):
        other = self.save("bootcut")
        self.page(other, 500, title="Zara jeans")
        with patch("keyword_candidates.stage", return_value=True) as stage:
            self.assertEqual(
                self.page(self.identity, 501, title="Zara bootcut jeans"), []
            )
            self.assertEqual(len(self.page(other, 501, title="Zara bootcut jeans")), 1)
        stage.assert_called_once()

    def test_quiet_baseline_exclusions_and_budget_never_start_detail_work(self):
        quiet = self.save("bootcut")
        with patch("keyword_candidates.stage") as stage, patch(
            "requests.Session.request", side_effect=AssertionError("Network")
        ):
            self.assertEqual(self.page(quiet, 600, title="Zara wide-leg jeans"), [])
            self.assertEqual(
                self.page(self.identity, 501, title="Zara ripped jeans"), []
            )
            self.assertEqual(
                self.page(self.identity, 502, title="Zara jeans", price="30"), []
            )
        stage.assert_not_called()

    def test_description_match_after_caption_limit_is_not_lost(self):
        description = "Blue denim " * 600 + "skinny fit"
        self.assertEqual(
            len(
                self.page(
                    self.identity,
                    501,
                    title="Zara jeans",
                    raw={"description": description},
                )
            ),
            1,
        )
