"""Fresh alert evidence without contents, purchases, messages or layout changes."""

import json
import os
import time
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

from test_dashboard import photo_bytes
from test_search_controls import DatabaseFixture

import alert_delivery
import dashboard_store
import photo_cards
import search_settings
import vinted_alert_check as check
import vinted_alerts

DESCRIPTION = "Fictional private seller description; do not export this text."


class AlertCheckTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        photo_cards.enable()
        dashboard_store.save_search(
            1,
            {
                "query_name": "Test search",
                "query": search_settings.get_search(1)["query"],
                "revision": "0",
                "max_buy": "15",
                "reminder": "Private reminder",
            },
            photo_bytes(),
        )
        self.batch(1, [110])
        self.row = alert_delivery.claim(now=time.time())
        details = vinted_alerts.get_details(self.row)
        details["description"] = DESCRIPTION
        photo_cards.record(
            self.row,
            details,
            SimpleNamespace(
                message_id=42, photo=[SimpleNamespace(file_id="private-file-id")]
            ),
        )
        self.listing = {
            "state": "ready",
            "description": DESCRIPTION,
            "photos": ["https://images1.vinted.net/fake.jpg"],
        }

    def records(self):
        with closing(search_settings.connection()) as conn:
            return {
                table: [tuple(row) for row in conn.execute("SELECT * FROM " + table)]
                for table in (
                    "queries",
                    "search_preferences",
                    "search_dashboard",
                    "dashboard_media",
                    "vinted_search_budgets",
                    "vinted_buyer",
                    "vinted_buy_attempts",
                    "vinted_alert_details",
                    "alert_outbox",
                    "telegram_photo_cards",
                    "parameters",
                )
            }

    def test_matching_description_and_controls_return_only_safe_evidence(self):
        before = self.records()
        with patch(
            "vinted_gallery.fetch_listing", return_value=self.listing
        ) as fetch, self.assertLogs(check.logger, level="INFO") as logs:
            result = check.check_latest_alert()
        self.assertEqual(result["outcome"], "matched")
        self.assertTrue(result["description_matches"])
        self.assertTrue(result["example_image_readable"])
        self.assertTrue(result["listing_and_example_controls_present"])
        self.assertTrue(result["autobuy_callback_present"])
        self.assertFalse(result["photo_buttons_exercised"])
        self.assertEqual(result["listing_photo_count"], 1)
        self.assertEqual(self.records(), before)
        fetch.assert_called_once_with(self.row["url"])
        text = json.dumps(result) + " ".join(logs.output) + check.summary(result)
        for private in (
            DESCRIPTION,
            "Private reminder",
            "private-file-id",
            self.row["url"],
        ):
            self.assertNotIn(private, text)
        self.assertFalse(result["checkout_created"])
        self.assertFalse(result["payment_submitted"])
        self.assertEqual(result["messages_sent"], 0)
        self.assertEqual(result["messages_edited"], 0)

    def test_mismatch_is_reported_without_replacing_description_or_photo_view(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE telegram_photo_cards SET view='examples'")
        before = self.records()
        with patch(
            "vinted_gallery.fetch_listing",
            return_value={
                **self.listing,
                "description": "Different listing description",
            },
        ):
            result = check.check_latest_alert()
        self.assertEqual(result["outcome"], "mismatch")
        self.assertEqual(result["selected_photo_view"], "examples")
        self.assertEqual(self.records(), before)

    def test_old_or_missing_alert_does_not_make_a_listing_request(self):
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE alert_outbox SET sent_at=?", (time.time() - 7200,))
        with patch("vinted_gallery.fetch_listing") as fetch:
            self.assertEqual(check.check_latest_alert()["stage"], "fresh_alert")
            fetch.assert_not_called()

    def test_blocked_listing_and_secret_exception_remain_unverified(self):
        for candidate in (
            {"state": "cooldown"},
            RuntimeError("private-token-never-export"),
        ):
            with self.subTest(candidate=type(candidate).__name__):
                args = (
                    {"side_effect": candidate}
                    if isinstance(candidate, Exception)
                    else {"return_value": candidate}
                )
                with patch("vinted_gallery.fetch_listing", **args), self.assertLogs(
                    check.logger, level="INFO"
                ) as logs:
                    result = check.check_latest_alert()
                self.assertEqual(result["outcome"], "unverified")
                self.assertNotIn("private-token-never-export", str(logs.output))

    def test_missing_corrupt_or_oversized_reference_is_not_reported_readable(self):
        self.assertFalse(check.readable_reference("missing"))
        for image in (b"not-an-image", b"x" * 100):
            with closing(search_settings.connection()) as conn, conn:
                conn.execute("UPDATE dashboard_media SET image=?", (image,))
            with patch.object(check, "MAX_IMAGE_BYTES", 50):
                self.assertFalse(check.readable_reference(self.row["reference_id"]))

    def test_startup_is_opt_in_and_reserved_before_a_single_request(self):
        with patch.dict(
            os.environ, {"MSJ_ALERT_CHECK_ON_START": "offline-alert-release"}
        ), patch.object(
            check, "check_latest_alert", return_value={"outcome": "unverified"}
        ) as execute:
            self.assertEqual(check.run_once(), {"outcome": "unverified"})
            self.assertIsNone(check.run_once())
            execute.assert_called_once_with()
