"""Catalogue optimisation must preserve listing data and reject invalid input."""

import json
import unittest
from unittest.mock import Mock

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


if __name__ == "__main__":
    unittest.main()
