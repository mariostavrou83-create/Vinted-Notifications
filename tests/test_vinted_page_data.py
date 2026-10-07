"""Offline payload regressions; these fixtures do not establish live access."""

import json
import unittest

import vinted_page_data as page


def photo(identity, size="f800"):
    return f"https://images1.vinted.net/t/{identity}/{size}/image.webp"


def next_data(value):
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(value)
        + "</script>"
    )


def flight_payload(payload, chunks=3):
    size = max(1, len(payload) // chunks)
    return "".join(
        "<script>self.__next_f.push("
        + json.dumps([1, payload[i : i + size]])
        + ")</script>"
        for i in range(0, len(payload), size)
    )


def flight(rows, chunks=3):
    payload = "".join(f"{key}:{json.dumps(value)}\n" for key, value in rows)
    return flight_payload(payload, chunks)


def purchase_item(**changes):
    return {
        "id": "123",
        "seller_id": "456",
        "price": {"amount": "15.00", "currency_code": "GBP"},
        "can_buy": True,
        "is_reserved": False,
        "is_hidden": False,
        **changes,
    }


class PurchasePageDataTests(unittest.TestCase):
    def test_current_sidebar_resolves_price_and_agrees_with_core_item(self):
        item = purchase_item()
        html = flight(
            [
                ("e1", ["$", "$L2", None, {"value": item}]),
                ("e2", {**item, "price": "$e1:props:value:price", "plugins": "$e3"}),
                (
                    "e3",
                    [
                        {
                            "name": "status",
                            "data": {
                                "item_id": "123",
                                "seller_id": "456",
                                "title": "Sold",
                            },
                        }
                    ],
                ),
                (
                    "e4",
                    purchase_item(
                        id="999",
                        seller_id="888",
                        price={"amount": "1.00", "currency_code": "GBP"},
                    ),
                ),
            ],
            chunks=9,
        )
        self.assertEqual(
            page.parse_purchase_item(html, "123"),
            {
                "id": "123",
                "user_id": "456",
                "price": item["price"],
                "can_buy": True,
                "is_reserved": False,
                "is_hidden": False,
            },
        )

    def test_purchase_data_requires_complete_typed_same_record_metadata(self):
        item = purchase_item()
        for key in item:
            with self.subTest(missing=key):
                incomplete = {k: v for k, v in item.items() if k != key}
                self.assertIsNone(
                    page.parse_purchase_item(next_data(incomplete), "123")
                )
        for changes in (
            {"seller_id": True},
            {"seller_id": "other"},
            {"price": "$missing"},
            {"price": {"amount": "15.00"}},
            {"price": []},
            {"can_buy": "true"},
            {"is_reserved": 0},
            {"is_hidden": None},
            {"item_id": "999"},
            {"url": "https://www.vinted.co.uk/items/999-wrong"},
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(
                    page.parse_purchase_item(next_data(purchase_item(**changes)), "123")
                )

    def test_conflicting_target_records_cannot_authorize_a_purchase(self):
        for changes in (
            {"seller_id": "789"},
            {"price": {"amount": "16.00", "currency_code": "GBP"}},
            {"can_buy": False},
            {"is_reserved": True},
            {"is_hidden": True},
        ):
            with self.subTest(changes=changes):
                html = flight([("1", purchase_item()), ("2", purchase_item(**changes))])
                self.assertIsNone(page.parse_purchase_item(html, "123"))

    def test_public_product_offer_or_neighbor_cannot_replace_buy_eligibility(self):
        product = {
            "@type": "Product",
            "url": "https://www.vinted.co.uk/items/123",
            "offers": {
                "price": "1.00",
                "priceCurrency": "GBP",
                "availability": "https://schema.org/InStock",
            },
        }
        html = next_data(
            {"product": product, "recommendations": [purchase_item(id="999")]}
        )
        self.assertIsNone(page.parse_purchase_item(html, "123"))
        self.assertIsNone(
            page.parse_purchase_item("<script>window.runCheckout()</script>", "123")
        )

    def test_unavailable_item_keeps_explicit_false_flags_for_purchase_gate(self):
        data = page.parse_purchase_item(
            next_data(purchase_item(can_buy=False, is_reserved=True)), "123"
        )
        self.assertFalse(data["can_buy"])
        self.assertTrue(data["is_reserved"])

    def test_traversal_and_payload_bounds_fail_closed_before_conflicting_data(self):
        html = next_data(
            {
                "item": purchase_item(),
                "later": [{"other": i} for i in range(page.MAX_PURCHASE_NODES + 1)]
                + [purchase_item(can_buy=False)],
            }
        )
        self.assertIsNone(page.parse_purchase_item(html, "123"))
        self.assertIsNone(page.parse_purchase_item("x" * (page.MAX_HTML + 1), "123"))
        self.assertIsNone(page.parse_purchase_item(next_data(purchase_item()), "١٢٣"))


class PageDataTests(unittest.TestCase):
    def test_large_bootstrap_does_not_hide_later_listing_flight_record(self):
        html = flight(
            [
                ("0", {"translations": ["irrelevant" for _ in range(11000)]}),
                (
                    "1",
                    [
                        "$",
                        "$L2",
                        None,
                        {
                            "item": {
                                "id": "123",
                                "seller_id": 2,
                                "photos": [{"url": photo("a")}],
                                "plugins": [
                                    {
                                        "name": "description",
                                        "data": {
                                            "item_id": "123",
                                            "description": "Actual seller notes",
                                        },
                                    }
                                ],
                            }
                        },
                    ],
                ),
            ]
        )
        self.assertEqual(
            page.parse_page_data(html, 123),
            {"photos": [photo("a")], "description": "Actual seller notes"},
        )

    def test_next_data_keeps_target_and_excludes_recommendations(self):
        html = next_data(
            {
                "props": {
                    "pageProps": {
                        "recommendations": [
                            {
                                "id": 999,
                                "title": "Wrong",
                                "description": "Wrong seller",
                                "photos": [photo("wrong")],
                            }
                        ],
                        "item": {
                            "id": 123,
                            "title": "Target",
                            "description": "Small mark\nSee last photo",
                            "photos": [photo("a"), photo("a", "f310"), photo("b")],
                            "recommendations": [
                                {"id": 555, "description": "Nested wrong"}
                            ],
                        },
                    }
                }
            }
        )
        self.assertEqual(
            page.parse_page_data(html, 123),
            {
                "description": "Small mark\nSee last photo",
                "photos": [photo("a"), photo("b")],
            },
        )

    def test_flight_separate_core_and_anchored_plugin_rows(self):
        plugins = [
            {"name": "summary", "data": {"item_id": "123"}},
            {
                "name": "description",
                "data": {"description": 'Seller "notes"\nSmoke free home'},
            },
            {"name": "shipping", "data": {"item_id": "123"}},
        ]
        html = flight(
            [
                (
                    "9",
                    [
                        "$",
                        "$L1",
                        None,
                        {
                            "value": {
                                "id": 999,
                                "seller_id": 1,
                                "description": "Wrong",
                                "photos": [photo("wrong")],
                            }
                        },
                    ],
                ),
                (
                    "a",
                    [
                        "$",
                        "$L2",
                        None,
                        {
                            "value": {
                                "id": "123",
                                "seller_id": 2,
                                "photos": [{"url": photo("a")}, {"url": photo("b")}],
                            }
                        },
                    ],
                ),
                ("b", ["$", "$L3", None, {"plugins": plugins}]),
            ],
            chunks=9,
        )
        self.assertEqual(
            page.parse_page_data(html, "123"),
            {
                "photos": [photo("a"), photo("b")],
                "description": 'Seller "notes"\nSmoke free home',
            },
        )

    def test_referenced_plugins_and_data_keep_explicit_item_identity(self):
        html = flight(
            [
                ("a", {"item_id": 123, "description": "Actual seller notes"}),
                ("b", {"name": "description", "data": "$a"}),
                ("c", ["$b"]),
                ("d", [photo("a")]),
                ("e", {"item_id": 123, "plugins": "$c", "photos": "$d"}),
            ]
        )
        self.assertEqual(
            page.parse_page_data(html, 123),
            {"description": "Actual seller notes", "photos": [photo("a")]},
        )

    def test_description_plugin_identity_and_enclosing_id_must_agree(self):
        for item in (
            {
                "plugins": [
                    {"name": "summary", "data": {"item_id": 123}},
                    {
                        "name": "description",
                        "data": {"item_id": 999, "description": "Wrong item"},
                    },
                ]
            },
            {
                "id": 999,
                "seller_id": 2,
                "plugins": [
                    {"name": "summary", "data": {"item_id": 123}},
                    {"name": "description", "data": {"description": "Wrong item"}},
                ],
            },
            {
                "id": 123,
                "item_id": 999,
                "title": "Conflicting item",
                "description": "Wrong item",
                "photos": [photo("wrong")],
            },
        ):
            self.assertEqual(
                page.parse_page_data(next_data(item), 123),
                {"description": "", "photos": []},
            )

    def test_referenced_photo_list_proves_listing_shape_without_seller_record(self):
        html = flight(
            [
                ("a", [{"url": photo("a")}]),
                ("b", {"id": 123, "description": "Seller notes", "photos": "$a"}),
            ]
        )
        self.assertEqual(
            page.parse_page_data(html, 123),
            {"description": "Seller notes", "photos": [photo("a")]},
        )

    def test_encoded_unicode_product_slug_matches_without_encoded_path_escape(self):
        for slug, expected in (("caf%C3%A9", "Seller notes"), ("wrong%2Fpath", "")):
            html = next_data(
                {
                    "@type": "Product",
                    "url": "https://www.vinted.co.uk/items/123-" + slug,
                    "description": "Seller notes",
                }
            )
            self.assertEqual(page.parse_page_data(html, 123)["description"], expected)

    def test_target_sidebar_resolves_explicit_flight_references(self):
        html = flight(
            [
                (
                    "a",
                    [
                        "$",
                        "$L1",
                        None,
                        {
                            "plugins": [
                                {
                                    "name": "description",
                                    "data": {"description": "Referenced notes"},
                                }
                            ]
                        },
                    ],
                ),
                ("b", ["$", "$L2", None, {"value": {"photos": [{"url": photo("a")}]}}]),
                (
                    "c",
                    {
                        "item": {
                            "id": 123,
                            "seller_id": 2,
                            "plugins": "$a:props:plugins",
                            "photos": "$b:props:value:photos",
                        }
                    },
                ),
            ]
        )
        self.assertEqual(
            page.parse_page_data(html, 123),
            {"description": "Referenced notes", "photos": [photo("a")]},
        )

    def test_unanchored_wrong_or_conflicting_plugins_do_not_supply_text(self):
        for anchors in ([], [999], [123, 999], [True], [None]):
            plugins = [
                {"name": "summary", "data": {"item_id": value}} for value in anchors
            ]
            plugins.append(
                {"name": "description", "data": {"description": "Unsafe description"}}
            )
            html = flight([("a", {"plugins": plugins})])
            self.assertEqual(page.parse_page_data(html, 123)["description"], "")
        html = next_data(
            {
                "item": {
                    "id": 123,
                    "seller_id": 2,
                    "plugins": [
                        {"name": "shipping", "data": {"item_id": 999}},
                        {"name": "description", "data": {"description": "Wrong"}},
                    ],
                }
            }
        )
        self.assertEqual(page.parse_page_data(html, 123)["description"], "")

    def test_nested_product_offer_url_proves_target_identity(self):
        html = flight(
            [
                (
                    "a",
                    {
                        "jsonLd": {
                            "@type": "Product",
                            "description": "<p>Right &amp; clean</p>",
                            "image": [photo("a"), "https://evil.test/a"],
                            "offers": {
                                "url": "https://www.vinted.co.uk/items/123-coat"
                            },
                        }
                    },
                )
            ]
        )
        self.assertEqual(
            page.parse_page_data(html, 123),
            {"description": "Right & clean", "photos": [photo("a")]},
        )

    def test_conflicting_missing_or_foreign_product_identity_is_rejected(self):
        for fields in (
            {},
            {"url": "https://evil.test/items/123"},
            {"url": "https://www.vinted.co.uk/items/999"},
            {
                "url": "https://www.vinted.co.uk/items/123",
                "offers": {"url": "https://www.vinted.co.uk/items/999"},
            },
        ):
            html = (
                '<script type="application/ld+json">'
                + json.dumps(
                    {
                        "@type": "Product",
                        "description": "Wrong",
                        "image": photo("a"),
                        **fields,
                    }
                )
                + "</script>"
            )
            self.assertEqual(
                page.parse_page_data(html, 123), {"description": "", "photos": []}
            )

    def test_safe_photo_hosts_and_four_distinct_photos_only(self):
        values = [
            "https://evil.test/a",
            "https://user:secret@images1.vinted.net/a",
            "http://images1.vinted.net/a",
        ] + [photo(str(i)) for i in range(8)]
        result = page.parse_page_data(next_data({"id": 123, "photos": values}), 123)
        self.assertEqual(result["photos"], [photo(str(i)) for i in range(4)])

    def test_no_identity_is_inherited_by_nested_descriptions(self):
        html = next_data(
            {
                "id": 123,
                "seller_id": 2,
                "recommendations": [
                    {"description": "Wrong", "photos": [photo("wrong")]}
                ],
                "user": {"id": 123, "description": "Seller profile"},
            }
        )
        self.assertEqual(
            page.parse_page_data(html, 123), {"description": "", "photos": []}
        )

    def test_javascript_lookalikes_and_unsupported_rows_are_not_executed(self):
        payload = json.dumps(
            [1, 'a:{"id":123,"title":"Target","description":"Wrong"}\n']
        )
        for script in (
            f'window.attack = "self.__next_f.push({payload})"',
            f"run(); self.__next_f.push({payload})",
            "self.__next_f.push([1, process.env.SECRET])",
        ):
            self.assertEqual(
                page.parse_page_data(f"<script>{script}</script>", 123),
                {"description": "", "photos": []},
            )
        html = (
            "<script>self.__next_f.push("
            + json.dumps(
                [1, 'a:T10,{"id":123,"title":"Target","description":"Wrong"}\n']
            )
            + ")</script>"
        )
        self.assertEqual(page.parse_page_data(html, 123)["description"], "")

    def test_malformed_overlong_and_deep_payloads_fail_closed(self):
        for html in (
            "x" * (page.MAX_HTML + 1),
            '<script id="__NEXT_DATA__">'
            + "[" * 10000
            + "0"
            + "]" * 10000
            + "</script>",
            '<script id="__NEXT_DATA__">{"item":</script>',
            '<script>self.__next_f.push([1,"a:{"]) </script>',
        ):
            self.assertEqual(
                page.parse_page_data(html, 123), {"description": "", "photos": []}
            )
        self.assertEqual(
            page.parse_page_data(next_data({"id": True, "photos": [photo("a")]}), True),
            {"description": "", "photos": []},
        )

    def test_duplicate_rows_and_reference_cycles_cannot_supply_data(self):
        html = flight(
            [
                (
                    "a",
                    {
                        "plugins": [
                            {
                                "name": "description",
                                "data": {"description": "Ambiguous"},
                            }
                        ]
                    },
                ),
                (
                    "a",
                    {
                        "plugins": [
                            {"name": "description", "data": {"description": "Other"}}
                        ]
                    },
                ),
                (
                    "b",
                    {
                        "item": {
                            "id": 123,
                            "seller_id": 2,
                            "plugins": "$a:plugins",
                            "photos": "$c:photos",
                        }
                    },
                ),
                ("c", {"photos": "$c:photos"}),
            ]
        )
        self.assertEqual(
            page.parse_page_data(html, 123), {"description": "", "photos": []}
        )

    def test_text_record_contents_cannot_impersonate_json_rows(self):
        text = 'a:Tff,Seller notes\nb:{"id":123,"title":"Target","description":"Injected notes"}\n'
        html = "<script>self.__next_f.push(" + json.dumps([1, text]) + ")</script>"
        self.assertEqual(
            page.parse_page_data(html, 123), {"description": "", "photos": []}
        )
        duplicate = flight(
            [
                ("a", {"id": 123, "title": "Target", "description": "Ambiguous"}),
                ("a", {"id": 123, "title": "Target", "description": "Other"}),
            ]
        )
        self.assertEqual(
            page.parse_page_data(duplicate, 123), {"description": "", "photos": []}
        )

    def test_plain_seller_text_keeps_literal_angle_brackets(self):
        html = next_data(
            {
                "id": 123,
                "title": "Target",
                "description": "Marked <3 cm\n\nStill wearable",
            }
        )
        self.assertEqual(
            page.parse_page_data(html, 123)["description"],
            "Marked <3 cm\n\nStill wearable",
        )

    def test_explicit_description_reference_and_missing_markers(self):
        html = flight(
            [
                ("a", "Long seller notes\nKept on a separate JSON record"),
                ("b", {"id": 123, "title": "Target", "description": "$a"}),
            ]
        )
        self.assertEqual(
            page.parse_page_data(html, 123)["description"],
            "Long seller notes\nKept on a separate JSON record",
        )
        for marker in ("$undefined", "$abc"):
            html = next_data({"id": 123, "title": "Target", "description": marker})
            self.assertEqual(page.parse_page_data(html, 123)["description"], "")

    def test_utf8_length_text_record_and_target_plugin_reference(self):
        description = 'Café 📦\nSmall mark on sleeve\nSeller "notes"'
        plugins = [
            {"name": "summary", "data": {"item_id": 123}},
            {"name": "description", "data": {"description": "$a"}},
        ]
        payload = (
            f"a:T{len(description.encode('utf-8')):x},{description}"
            + "b:"
            + json.dumps({"plugins": plugins})
            + "\n"
            + "c:"
            + json.dumps({"id": 123, "seller_id": 2, "photos": [photo("a")]})
            + "\n"
        )
        result = page.parse_page_data(flight_payload(payload, chunks=11), 123)
        self.assertEqual(result, {"description": description, "photos": [photo("a")]})

    def test_text_with_fake_rows_is_consumed_only_by_byte_length(self):
        text = (
            'Seller text\nb:{"id":123,"title":"Target","description":"Injected","photos":["'
            + photo("wrong")
            + '"]}\n'
        )
        payload = (
            f"a:T{len(text.encode('utf-8')):x},{text}"
            + "c:"
            + json.dumps({"id": 123, "seller_id": 2, "photos": [photo("a")]})
            + "\n"
        )
        self.assertEqual(
            page.parse_page_data(flight_payload(payload, chunks=7), 123),
            {"description": "", "photos": [photo("a")]},
        )

    def test_raw_text_starting_with_reference_marker_remains_literal(self):
        payload = (
            'a:T2,$bc:"Other text"\nd:{"id":123,"title":"Target","description":"$a"}\n'
        )
        self.assertEqual(
            page.parse_page_data(flight_payload(payload), 123)["description"], "$b"
        )

    def test_unrelated_text_binary_and_metadata_do_not_hide_target_record(self):
        payload = '1:I["module"]\n2:HL["/asset.css"]\n3:T5,hello4:A3,abc5:{"id":123,"title":"Target","description":"Right notes"}\n'
        self.assertEqual(
            page.parse_page_data(flight_payload(payload, chunks=5), 123)["description"],
            "Right notes",
        )

    def test_malformed_text_lengths_discard_flight_without_losing_json_root(self):
        for raw in (
            "a:Tgg,text",
            "a:Tffff,text",
            "a:T1,é",
            "a:T1,hellob:{}\n",
            "a:T,text",
        ):
            payload = 'b:{"id":123,"title":"Target","description":"Untrusted"}\n' + raw
            self.assertEqual(
                page.parse_page_data(flight_payload(payload), 123),
                {"description": "", "photos": []},
            )
            html = next_data(
                {
                    "id": 123,
                    "title": "Target",
                    "description": "Trusted independent JSON",
                }
            ) + flight_payload(payload)
            self.assertEqual(
                page.parse_page_data(html, 123)["description"],
                "Trusted independent JSON",
            )
