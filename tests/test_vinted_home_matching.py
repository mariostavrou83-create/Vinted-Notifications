import copy
import unittest

from vinted_home_matching import match_candidate


def search(**changes):
    return {
        "id": 43,
        "query": "https://www.vinted.co.uk/catalog?brand_ids[]=88&catalog[]=1037&size_ids[]=3&price_to=10&search_text=zip+up",
        "vinted_enabled": True,
        "paused": 0,
        "archived": 0,
        "exclusions": ["damaged", "teddy"],
        **changes,
    }


def item(**changes):
    return {
        "id": "1001",
        "title": "Full zip jacket",
        "description": "Ralph Lauren jacket. Very good condition.",
        "description_complete": True,
        "brand_id": "88",
        "category_ids": ["1904", "1037", "1097"],
        "size_id": "3",
        "price": {"amount": "7.50", "currency_code": "GBP"},
        **changes,
    }


class HomeMatchingTests(unittest.TestCase):
    def test_equivalent_style_with_same_saved_filters(self):
        candidate, targets = item(), [search()]
        before = copy.deepcopy((candidate, targets))
        result = match_candidate(candidate, targets)
        self.assertEqual(result["query_id"], 43)
        self.assertEqual(result["style_evidence"], "equivalent_words")
        self.assertEqual((candidate, targets), before)

    def test_missing_style_is_not_inferred_from_brand_or_price(self):
        self.assertIsNone(match_candidate(item(title="Nice jacket"), [search()]))
        self.assertIsNone(
            match_candidate(item(title="Not full zip jacket"), [search()])
        )

    def test_each_missing_or_wrong_structural_attribute_rejects(self):
        for field in ("brand_id", "category_ids", "size_id"):
            for value in (None, "999", True):
                with self.subTest(field=field, value=value):
                    self.assertIsNone(
                        match_candidate(item(**{field: value}), [search()])
                    )

    def test_blocked_description_and_incomplete_description_reject(self):
        self.assertIsNone(
            match_candidate(item(description="Damaged lining"), [search()])
        )
        self.assertIsNone(match_candidate(item(description_complete=False), [search()]))

    def test_price_constraints_remain_separate(self):
        self.assertIsNone(
            match_candidate(
                item(price={"amount": "10.01", "currency_code": "GBP"}), [search()]
            )
        )
        self.assertIsNone(match_candidate(item(), [search(max_buy=700)]))
        self.assertIsNone(match_candidate(item(), [search(vinted_max_total=1000)]))
        self.assertIsNotNone(match_candidate(item(), [search(vinted_max_total=1100)]))

    def test_unknown_and_malformed_filters_fail_closed(self):
        for suffix in (
            "&country=UK",
            "&price_to=20",
            "&currency=EUR",
            "&size_ids[]=",
            "&disposal[]=1",
        ):
            with self.subTest(suffix=suffix):
                self.assertIsNone(
                    match_candidate(item(), [search(query=search()["query"] + suffix)])
                )

    def test_explicit_attribute_filter_aliases_and_path_category(self):
        query = "https://www.vinted.co.uk/catalog/1037-outerwear?attribute_ids[brand]=12,88&attribute_ids[size]=2,3&price_to=10&search_text=zip+up"
        self.assertIsNotNone(match_candidate(item(), [search(query=query)]))

    def test_no_keyword_requires_structural_scope_and_finite_cap(self):
        query = "https://www.vinted.co.uk/catalog?brand_ids[]=88&price_to=10"
        self.assertIsNotNone(
            match_candidate(item(title="Jacket"), [search(query=query)])
        )
        self.assertIsNone(
            match_candidate(
                item(), [search(query="https://www.vinted.co.uk/catalog?price_to=10")]
            )
        )
        self.assertIsNone(
            match_candidate(
                item(),
                [search(query="https://www.vinted.co.uk/catalog?brand_ids[]=88")],
            )
        )

    def test_inactive_and_unsupported_targets_reject(self):
        for patch in (
            {"paused": 1},
            {"archived": 1},
            {"vinted_enabled": False},
            {"must_have": "real fur"},
        ):
            self.assertIsNone(match_candidate(item(), [search(**patch)]))

    def test_matching_is_deterministic_and_no_cross_field_phrase(self):
        result = match_candidate(item(), [search(id=60), search(id=43)])
        self.assertEqual(result["query_id"], 43)
        self.assertIsNone(
            match_candidate(item(title="Full", description="zip jacket"), [search()])
        )

    def test_shared_alternatives_are_preserved(self):
        target = search(shared_alert_version=1, shared_keywords=["fur trim", "zip up"])
        self.assertIsNotNone(match_candidate(item(), [target]))
        self.assertIsNone(match_candidate(item(title="Fur coat"), [target]))

    def test_minimum_price_condition_and_currency(self):
        base = search()["query"]
        self.assertIsNone(
            match_candidate(item(), [search(query=base + "&price_from=8")])
        )
        self.assertIsNone(
            match_candidate(item(), [search(query=base + "&status_ids[]=2")])
        )
        self.assertIsNotNone(
            match_candidate(
                item(condition_id="2"), [search(query=base + "&status_ids[]=2")]
            )
        )
        for amount in ("NaN", "Infinity", "7.501", "-1", "0"):
            self.assertIsNone(
                match_candidate(
                    item(price={"amount": amount, "currency_code": "GBP"}), [search()]
                )
            )
        self.assertIsNone(
            match_candidate(
                item(price={"amount": "7.50", "currency_code": "EUR"}), [search()]
            )
        )


if __name__ == "__main__":
    unittest.main()
