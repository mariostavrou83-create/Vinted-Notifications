"""An owner review must never export credentials or mutate operational state."""

import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from test_search_controls import DatabaseFixture
import db
import search_review_export as review
from web_ui_plugin.web_ui import create_app


class ReviewExportTests(DatabaseFixture, unittest.TestCase):
    def test_read_only_projection_and_explicit_coverage(self):
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("CREATE TABLE private_credentials(token TEXT)")
            conn.execute("INSERT INTO private_credentials VALUES('never-export-this')")
            conn.execute("INSERT INTO search_platforms(query_id,ebay_config) VALUES(1,?)",
                         (json.dumps({"keywords": "jacket", "client_secret": "never-export-this"}),))
            before = list(conn.iterdump())
        data = review.collect(Path(db.DB_PATH))
        self.assertEqual(data["live"]["queries"]["count"], 44)
        self.assertFalse(data["live"]["queries"]["truncated"])
        self.assertNotIn("never-export-this", json.dumps(data))
        self.assertEqual(data["live"]["search_platforms"]["rows"][0]["ebay_config"], {"keywords": "jacket"})
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            self.assertEqual(list(conn.iterdump()), before)
            limited = review.read_table(conn, "queries", "id query password", limit=0)
            self.assertTrue(limited["truncated"])
            self.assertEqual(limited["count"], 44)
        self.assertNotIn("dashboard_auth", data["live"])

    def test_route_does_not_read_data_before_owner_authentication(self):
        app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        with patch.object(review, "collect") as collect:
            response = app.test_client().get("/search-review.json")
            self.assertEqual(response.status_code, 302)
            collect.assert_not_called()

    def test_missing_historical_tables_are_not_reported_as_zero(self):
        with closing(sqlite3.connect(":memory:")) as conn:
            self.assertEqual(review.read_table(conn, "queries", "id query"), {"available": False})
        projected = review.project({"item": {"title": "mesh top", "seller": {"email": "private"}},
                                    "alert": {"content": "private bot message", "vinted_details": {
                                        "keyword_description": "mesh stretch", "description_complete": True}}})
        self.assertEqual(projected["item"], {"title": "mesh top"})
        self.assertNotIn("content", projected["alert"])
