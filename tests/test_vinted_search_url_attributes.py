"""Offline equivalence for legacy and direct catalogue attribute filters."""

import ast
import unittest
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

import dashboard_store
import vinted_keywords


def parse(url):
    tree = ast.parse(
        (Path(__file__).resolve().parents[1] / "pyVintedVN/items/items.py").read_text()
    )
    items = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Items"
    )
    method = next(
        node
        for node in items.body
        if isinstance(node, ast.FunctionDef) and node.name == "parse_url"
    )
    namespace = {"parse_qsl": parse_qsl, "urlparse": urlparse}
    exec(  # noqa: S102
        compile(ast.Module(body=[method], type_ignores=[]), "items.py", "exec"),
        namespace,
    )
    return namespace["parse_url"](None, url, 96)


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
