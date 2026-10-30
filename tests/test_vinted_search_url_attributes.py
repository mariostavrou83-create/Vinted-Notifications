"""Offline equivalence for legacy and direct catalogue attribute filters."""

import ast
import unittest
from functools import lru_cache
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qsl, urlencode, urlparse

import dashboard_store
import vinted_keywords


def parser():
    tree = ast.parse(
        (Path(__file__).resolve().parents[1] / "pyVintedVN/items/items.py").read_text()
    )
    items = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Items"
    )
    items.body = [
        node for node in items.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"parse_url", "_query_parameters"}
    ]
    namespace = {"parse_qsl": parse_qsl, "urlparse": urlparse, "lru_cache": lru_cache}
    exec(  # noqa: S102
        compile(ast.Module(body=[items], type_ignores=[]), "items.py", "exec"),
        namespace,
    )
    return namespace["Items"]()


def parse(url):
    return parser().parse_url(url, 96)


class VintedSearchUrlAttributes(unittest.TestCase):
    mapping = (
        ("brand_ids[]", "brand"),
        ("catalog[]", "catalog"),
        ("size_ids[]", "size"),
        ("status_ids[]", "status"),
        ("color_ids[]", "color"),
        ("material_ids[]", "material"),
        ("video_game_platform_ids[]", "video_game_platform"),
    )

    def url(self, fields):
        return "https://www.vinted.co.uk/catalog?" + urlencode(fields)

    def test_all_direct_aliases_equal_existing_legacy_translation(self):
        legacy, direct = [], []
        for number, (old, name) in enumerate(self.mapping, 1):
            legacy.extend([(old, str(number)), (old, str(number + 10))])
            direct.extend(
                [
                    (f"attribute_ids[{name}]", str(number)),
                    (f"attribute_ids[{name}]", str(number + 10)),
                ]
            )
        common = [
            ("currency", "GBP"),
            ("price_to", "20.00"),
            ("order", "newest_first"),
            ("search_text", "fur hood"),
        ]
        expected = parse(self.url(legacy + common))
        self.assertEqual(parse(self.url(direct + common)), expected)
        for number, (_, name) in enumerate(self.mapping, 1):
            self.assertEqual(
                expected[f"attribute_ids[{name}]"], f"{number},{number + 10}"
            )
        self.assertEqual(expected["search_text"], "fur hood")
        self.assertEqual(expected["per_page"], 96)
        self.assertEqual(expected["page"], 1)

    def test_mixed_repeated_and_comma_separated_aliases_keep_every_value(self):
        for old, name in self.mapping:
            with self.subTest(attribute=name):
                params = parse(
                    self.url(
                        [
                            (old, "11"),
                            (old, "12"),
                            (f"attribute_ids[{name}]", "12,13"),
                            (f"attribute_ids[{name}]", "14, 13"),
                        ]
                    )
                )
                self.assertEqual(params[f"attribute_ids[{name}]"], "11,12,13,14")
        self.assertEqual(
            parse(self.url([("brand_ids[]", "11"), ("brand_ids[]", "11")]))[
                "attribute_ids[brand]"
            ],
            "11,11",
        )

    def test_normalization_and_keyword_replacement_preserve_direct_filters(self):
        normalized = dashboard_store.normalize_url(
            self.url(
                [(f"attribute_ids[{name}]", "11,12") for _, name in self.mapping]
                + [("search_text", "old"), ("order", "relevance"), ("page", "8")]
            )
        )
        replaced = vinted_keywords.with_keyword(normalized, "fur hood")
        params = parse(replaced)
        for _, name in self.mapping:
            self.assertEqual(params[f"attribute_ids[{name}]"], "11,12")
        self.assertEqual(params["search_text"], "fur hood")
        self.assertEqual(params["order"], "newest_first")
        self.assertEqual(params["page"], 1)

    def test_blank_aliases_do_not_add_empty_api_filters(self):
        params = parse(
            self.url([(f"attribute_ids[{name}]", "") for _, name in self.mapping])
        )
        self.assertEqual(params, {"page": 1, "per_page": 96})

    def test_repeated_queries_reuse_translation_without_sharing_mutable_results(self):
        client = parser()
        translate = client._query_parameters.__wrapped__
        namespace = translate.__globals__
        namespace["parse_qsl"] = Mock(wraps=parse_qsl)
        url = self.url([("brand_ids[]", "11"), ("search_text", "fur hood")])
        for _ in range(100):
            filters = client.parse_url(url, 96)
            self.assertEqual(filters["attribute_ids[brand]"], "11")
            self.assertEqual(filters["search_text"], "fur hood")
            filters["search_text"] = "caller changed copy"
            filters["unexpected"] = True
        self.assertEqual(namespace["parse_qsl"].call_count, 1)
        self.assertNotIn("unexpected", client.parse_url(url, 96))

    def test_changed_filters_pagination_and_timestamp_are_effective_immediately(self):
        client = parser()
        old_url = self.url([("brand_ids[]", "11"), ("search_text", "fur")])
        new_url = self.url([("brand_ids[]", "12"), ("search_text", "leather")])
        client.parse_url(old_url, 96)
        new = client.parse_url(new_url, 20, 2, 123)
        self.assertEqual(new, {
            "attribute_ids[brand]": "12", "search_text": "leather",
            "per_page": 20, "page": 2, "time": 123,
        })
        self.assertEqual(client.parse_url(new_url, 96)["per_page"], 96)
        self.assertNotIn("time", client.parse_url(new_url, 96))

    def test_translation_cache_has_bounded_memory_after_many_distinct_queries(self):
        client = parser()
        for number in range(2050):
            client.parse_url(self.url([("brand_ids[]", str(number))]))
        self.assertEqual(client._query_parameters.cache_info().currsize, 2048)
