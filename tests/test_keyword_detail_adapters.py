"""Public detail proof is item-bound, budgeted and separate from buying."""

import json
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_ebay_monitor import EbayFixture

import ebay_images
import ebay_monitor
import ebay_store
import search_settings
import vinted_gallery
from shared_matching import keyword_evidence


class KeywordAdapterTests(EbayFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.row = {"item_id": "ebay:123", "url": "https://www.ebay.co.uk/itm/123"}
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "INSERT INTO keyword_candidates VALUES ('ebay','ebay:123',1,0,0,'{}',1000,1900,'pending','')"
            )
            conn.execute(
                "INSERT INTO ebay_item_owners VALUES ('v1|123|0','ebay:123','')"
            )
        self.config = dict(
            ebay_store.configuration(),
            client_id="offline",
            client_secret="offline",
            source="browse",
        )
        self.search = {"shared_alert_version": 1, "shared_keywords": ["skinny"]}

    def ebay_read(self, data):
        response = SimpleNamespace(status_code=200, json=lambda: data)
        client = Mock(
            token="offline",
            session=SimpleNamespace(get=Mock(return_value=response), close=Mock()),
        )
        with patch("ebay_store.configuration", return_value=self.config), patch(
            "ebay_monitor.BrowseClient", return_value=client
        ):
            result = ebay_images.fetch_candidate_details(self.row)
        return result, client

    def test_full_ebay_description_matches_after_display_cutoff(self):
        data, client = self.ebay_read(
            {
                "legacyItemId": "123",
                "description": "<p>" + "x" * 7000 + " skinny fit</p>",
            }
        )
        self.assertTrue(data["available"])
        self.assertTrue(data["description_complete"])
        self.assertIn("Description shortened", data["description"])
        self.assertEqual(
            keyword_evidence(
                self.search,
                "Zara jeans",
                data["keyword_description"],
                description_complete=True,
            )["state"],
            "matched",
        )
        client.session.get.assert_called_once()
        client.session.close.assert_called_once()

    def test_short_description_is_positive_evidence_but_not_complete_negative(self):
        data, _ = self.ebay_read(
            {"legacyItemId": "123", "shortDescription": "<p>wide leg</p>"}
        )
        self.assertFalse(data["description_complete"])
        self.assertEqual(
            keyword_evidence(
                self.search,
                "Zara jeans",
                data["keyword_description"],
                description_complete=data["description_complete"],
            )["state"],
            "unverified",
        )

    def test_wrong_identity_cannot_supply_keyword_proof(self):
        data, _ = self.ebay_read({"legacyItemId": "999", "description": "skinny"})
        self.assertEqual(data, {})

    def test_budget_deferred_candidate_never_calls_api_or_claims_empty_description(
        self,
    ):
        with patch("ebay_store.configuration", return_value=self.config), patch(
            "ebay_store.reserve_call", return_value=9999999999
        ), patch("ebay_monitor.BrowseClient") as client:
            data = ebay_images.fetch_candidate_details(self.row)
        self.assertFalse(data["available"])
        self.assertEqual(data["retry_after"], 9999999999)
        self.assertNotIn("description_complete", data)
        client.assert_not_called()

    def test_nonexistent_candidate_never_calls_api(self):
        with patch("ebay_store.configuration", return_value=self.config), patch(
            "ebay_monitor.BrowseClient"
        ) as client:
            data = ebay_images.fetch_candidate_details(
                dict(self.row, item_id="ebay:456")
            )
        self.assertEqual(data, {})
        client.assert_not_called()

    def test_hidden_seller_html_cannot_prove_keyword(self):
        data, _ = self.ebay_read(
            {
                "legacyItemId": "123",
                "description": "<script>skinny</script><p>wide leg jeans</p>",
            }
        )
        self.assertEqual(
            keyword_evidence(
                self.search,
                "Zara jeans",
                data["keyword_description"],
                description_complete=data["description_complete"],
            )["state"],
            "absent",
        )

    def test_shared_search_enables_description_filter_without_changing_legacy(self):
        config = dict(
            ebay_store.DEFAULTS,
            keywords="skinny",
            shared_alert_version=1,
            shared_keywords=["skinny"],
        )
        self.assertIn(
            "searchInDescription:true", ebay_monitor.search_params(config)["filter"]
        )
        self.assertNotIn(
            "searchInDescription",
            ebay_monitor.search_params(dict(config, shared_alert_version=0))["filter"],
        )

    def test_one_letter_ebay_keyword_rejected_before_save(self):
        with self.assertRaisesRegex(ValueError, "at least two characters"):
            ebay_store.parse_shared_form(
                {
                    "platform_mode": "ebay",
                    "ebay_search_url": "https://www.ebay.co.uk/sch/i.html?_sacat=11554",
                    "shared_keywords": "a",
                },
                maximum=2500,
            )

    def vinted_read(self, value, identity=123):
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(
                {
                    "item": {
                        "id": identity,
                        "title": "Zara jeans",
                        "seller_id": 2,
                        "description": value,
                    }
                }
            )
            + "</script>"
        )

        def read(url, *, parser):
            return dict(parser(html, url), state="ready")

        with patch.object(vinted_gallery, "fetch_listing", side_effect=read):
            return vinted_gallery.fetch_candidate_details(
                {"item_id": "123", "url": "https://www.vinted.co.uk/items/123-jeans"}
            )

    def test_bound_vinted_description_matches_after_display_cutoff(self):
        data = self.vinted_read("x" * 7000 + " skinny fit")
        self.assertTrue(data["description_complete"])
        self.assertIn("Description shortened", data["description"])
        self.assertEqual(
            keyword_evidence(
                self.search,
                "Zara jeans",
                data["keyword_description"],
                description_complete=True,
            )["state"],
            "matched",
        )

    def test_unrelated_vinted_record_never_proves_absence_or_match(self):
        data = self.vinted_read("skinny", identity=999)
        self.assertEqual(data["keyword_description"], "")
        self.assertFalse(data["description_complete"])

    def test_overlong_vinted_text_cannot_prove_negative(self):
        data = self.vinted_read("x" * 100001)
        self.assertFalse(data["description_complete"])

    def test_wrong_vinted_url_identity_never_reads_page(self):
        with patch.object(vinted_gallery, "fetch_listing") as read:
            self.assertEqual(
                vinted_gallery.fetch_candidate_details(
                    {
                        "item_id": "123",
                        "url": "https://www.vinted.co.uk/items/999-jeans",
                    }
                ),
                {},
            )
        read.assert_not_called()
