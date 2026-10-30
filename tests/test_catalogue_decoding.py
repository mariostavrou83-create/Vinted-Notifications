"""Catalogue optimisation must preserve listing data and reject invalid input."""

import json
import unittest
from unittest.mock import Mock, patch

from requests import Response
from test_search_controls import DatabaseFixture


class CatalogueDecodingTests(DatabaseFixture, unittest.TestCase):
    def client(self, content):
        from pyVintedVN.items.items import Items

        response = Response()
        response.status_code = 200
        response._content = content
        response.encoding = "utf-8"
        response.json = Mock(
            side_effect=AssertionError("Do not decode a second Unicode copy")
        )
        client = Mock()
        client.get.return_value = response
        return Items(client=client)

    def test_unicode_prices_and_ids_are_unchanged(self):
        data = {
            "items": [
                {
                    "id": 10506000001,
                    "title": "Faux fur – £18 💚",
                    "price": {"amount": "18.00", "currency_code": "GBP"},
                    "url": "/items/10506000001-fur",
                    "item_box": {"first_line": "Hollister", "second_line": "S · Good"},
                }
            ]
        }
        catalogue = self.client(json.dumps(data, ensure_ascii=False).encode())
        self.assertEqual(
            catalogue.search("https://www.vinted.co.uk/catalog", json=True),
            data["items"],
        )
        item = catalogue.search("https://www.vinted.co.uk/catalog")[0]
        self.assertEqual(
            (item.id, item.title, item.price, item.currency),
            (10506000001, "Faux fur – £18 💚", "18.00", "GBP"),
        )

    def test_malformed_or_unreadable_catalogue_is_not_treated_as_empty_success(self):
        for content in (b"{bad", b'{"items":null}', b'{"items":[{}]}'):
            with self.subTest(content=content), self.assertRaises(
                (ValueError, TypeError)
            ):
                self.client(content).search("https://www.vinted.co.uk/catalog")

    def test_committed_entries_skip_construction_but_keep_the_complete_readable_id_page(self):
        from pyVintedVN.items import items as module

        fixture = {
            "id": 100, "title": "Faux fur – £18 💚", "url": "/items/100-fur",
            "price": {"amount": "18.00", "currency_code": "GBP"},
            "item_box": {"first_line": "Hollister", "second_line": "S · Good"},
        }
        rows = [dict(fixture, id=n) for n in range(96, 0, -1)]
        with patch.object(module, "Item", wraps=module.Item) as construct:
            page = module._parsed_items(rows, "www.vinted.co.uk", after_id=95)
        self.assertEqual(construct.call_count, 1)
        self.assertEqual([i.id for i in page], [96])
        self.assertEqual(page.catalogue_ids, tuple(str(n) for n in range(96, 0, -1)))
        full = page.complete()
        self.assertEqual([i.id for i in full], list(range(96, 0, -1)))
        self.assertEqual((full[-1].title, full[-1].size_title, full[-1].price),
                         ("Faux fur – £18 💚", "S", "18.00"))

    def test_corrupt_committed_entry_is_not_an_empty_success_and_real_timestamps_keep_parser(self):
        from pyVintedVN.items import items as module

        with self.assertRaises(ValueError):
            module._parsed_items([{"id": 10, "title": "broken"}], "www.vinted.co.uk", after_id=100)
        row = {
            "id": 10, "title": "jacket", "url": "/items/10",
            "price": {"amount": "18.00", "currency_code": "GBP"},
            "photo": {"high_resolution": {"timestamp": 1000}},
        }
        parsed = module._parsed_items([row], "www.vinted.co.uk", after_id=100)
        self.assertEqual(parsed[0].raw_timestamp, 1000)
        self.assertTrue(parsed[0].has_real_timestamp)
        row["photo"]["high_resolution"]["timestamp"] = "not a timestamp"
        with self.assertRaises(ValueError):
            module._parsed_items([row], "www.vinted.co.uk", after_id=100)


if __name__ == "__main__":
    unittest.main()
