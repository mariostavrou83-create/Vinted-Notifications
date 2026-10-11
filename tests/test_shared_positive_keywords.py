"""Shared-only literal title/description positives, independent of provider q."""

import unittest
from unittest.mock import patch

import shared_matching as matching


def shared(*keywords):
    return {"shared_alert_version": 1, "shared_keywords": list(keywords)}


class PositiveKeywordTests(unittest.TestCase):
    def test_one_alternative_must_occur_in_title_or_description(self):
        rules = shared("skinny", "bootcut")
        for title, description, word, field in [
            ("Zara SKINNY jeans", "Wide-leg blue denim", "skinny", "title"),
            ("Zara blue jeans", "These have a skinny fit.", "skinny", "description"),
            ("Zara bootcut jeans", "Skinny fit", "bootcut", "title"),
        ]:
            with self.subTest(title=title, description=description):
                self.assertEqual(
                    matching.keyword_evidence(rules, title, description),
                    {"state": "matched", "keyword": word, "field": field},
                )
                self.assertTrue(matching.matches_keywords(rules, title, description))

    def test_same_zara_wide_leg_result_is_unverified_until_description_read(self):
        rules = shared("skinny")
        title = "Zara wide-leg jeans"
        self.assertEqual(matching.keyword_evidence(rules, title)["state"], "unverified")
        self.assertFalse(matching.matches_keywords(rules, title))
        self.assertEqual(
            matching.keyword_evidence(
                rules, title, "Straight fit blue denim", description_complete=True
            )["state"],
            "absent",
        )
        self.assertTrue(
            matching.matches_keywords(rules, title, "Seller describes them as skinny")
        )

    def test_multiword_phrases_are_contiguous_whole_words_in_one_field(self):
        rules = shared("fur hood", "low rise")
        for title, description in [
            ("Fur jacket", "Includes a hood"),
            ("Low waist jeans", "Comfortable rise"),
            ("Furry hoodie", "Lowest risers"),
            ("Fur lined hood", "Low stretch rise"),
        ]:
            with self.subTest(title=title, description=description):
                self.assertFalse(matching.matches_keywords(rules, title, description))
        self.assertTrue(matching.matches_keywords(rules, "LOW-RISE jeans"))
        self.assertTrue(matching.matches_keywords(rules, "Coat", "Has a fur\nhood"))

    def test_casefold_unicode_and_punctuation_match_existing_word_rules(self):
        self.assertTrue(
            matching.matches_keywords(shared("skinny"), "ＳＫＩＮＮＹ jeans")
        )
        self.assertTrue(matching.matches_keywords(shared("strasse"), "Straße jacket"))
        self.assertTrue(matching.matches_keywords(shared("2XL"), "Jacket, size (2xl)."))
        self.assertFalse(matching.matches_keywords(shared("skinny"), "skinnyleg jeans"))

    def test_html_seller_description_uses_sanitized_text_not_script_style_or_frames(
        self,
    ):
        rules = shared("skinny")
        hidden = (
            "<script>skinny</script><style>.skinny{}</style>"
            "<iframe>skinny</iframe><template>skinny</template>"
            "<p>Wide-leg jeans</p>"
        )
        self.assertEqual(
            matching.keyword_evidence(
                rules,
                "Zara jeans",
                hidden,
                description_html=True,
                description_complete=True,
            )["state"],
            "absent",
        )
        self.assertTrue(
            matching.matches_keywords(
                rules,
                "Zara jeans",
                "<p>Skinny <strong>fit</strong></p>",
                description_html=True,
            )
        )

    def test_description_matching_precedes_display_truncation_when_full_text_supplied(
        self,
    ):
        text = "Blue denim " * 600 + "skinny fit"
        self.assertTrue(matching.matches_keywords(shared("skinny"), "Zara jeans", text))

    def test_shortened_or_oversized_description_never_proves_absence(self):
        rules = shared("skinny")
        for text in [
            "Straight jeans" + matching.SHORTENED,
            "Blue denim " * 12000,
        ]:
            with self.subTest(length=len(text)):
                self.assertEqual(
                    matching.keyword_evidence(
                        rules, "Zara jeans", text, description_complete=True
                    )["state"],
                    "unverified",
                )
        self.assertTrue(
            matching.matches_keywords(
                rules, "Zara jeans", "Skinny fit" + matching.SHORTENED
            )
        )
        self.assertFalse(
            matching.matches_keywords(
                shared("listing"), "Zara jeans", matching.SHORTENED
            )
        )

    def test_missing_text_does_not_become_a_match_or_complete_description(self):
        for description in [None, {}, 123]:
            with self.subTest(description=description):
                self.assertEqual(
                    matching.keyword_evidence(
                        shared("skinny"), None, description, description_complete=True
                    )["state"],
                    "unverified",
                )
        self.assertEqual(
            matching.keyword_evidence(
                shared("skinny"), "Wide-leg", "", description_complete=True
            )["state"],
            "absent",
        )

    def test_blank_alternatives_and_legacy_alerts_preserve_current_matching(self):
        for rules in [
            {},
            {"shared_alert_version": 0, "shared_keywords": ["skinny"]},
            {"vinted_keywords": ["skinny"], "ebay": {"keywords": "skinny"}},
            shared(),
        ]:
            with self.subTest(rules=rules):
                self.assertEqual(
                    matching.keyword_evidence(rules, "Wide-leg jeans")["state"],
                    "unrestricted",
                )
                self.assertTrue(matching.matches_keywords(rules, "Wide-leg jeans"))

    def test_actual_saved_search_and_platform_config_shapes_have_same_rules(self):
        config = shared("skinny")
        self.assertEqual(matching.required_keywords(config), ["skinny"])
        self.assertEqual(matching.required_keywords({"ebay": config}), ["skinny"])
        self.assertTrue(matching.matches_keywords({"ebay": config}, "Skinny jeans"))

    def test_matching_performs_no_network_database_or_buyer_operations(self):
        with patch(
            "requests.Session.request", side_effect=AssertionError("Network")
        ), patch("search_settings.connection", side_effect=AssertionError("Database")):
            self.assertTrue(matching.matches_keywords(shared("skinny"), "Skinny jeans"))
            self.assertFalse(
                matching.matches_keywords(
                    shared("skinny"),
                    "Wide-leg jeans",
                    "Straight cut",
                    description_complete=True,
                )
            )
