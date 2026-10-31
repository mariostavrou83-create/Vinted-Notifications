"""Canonical CSRF metadata cannot be shadowed by quoted listing text."""

import json
import unittest

import vinted_buyer as buyer

TOKEN = "canonical-csrf-token-0123456789"
OTHER = "description-csrf-token-0123456789"


class CSRFBootstrapTests(unittest.TestCase):
    def test_canonical_meta_beats_quoted_token_text_in_item_description(self):
        description = json.dumps({"description": json.dumps({"CSRF_TOKEN": OTHER})})
        html = f'<meta name="csrf-token" content="{TOKEN}"><script type="application/json">{description}</script>'
        self.assertEqual(buyer.csrf_from_html(html), TOKEN)

    def test_meta_attribute_order_case_and_whitespace_are_supported(self):
        for html in (
            f'<META content="{TOKEN}" NAME="csrf-token">',
            f"<meta content='{TOKEN}' data-extra='unrelated' name='CSRF-TOKEN'>",
            f'<head>\n<meta name = "csrf-token" content = "{TOKEN}" />\n</head>',
        ):
            with self.subTest(html=html):
                self.assertEqual(buyer.csrf_from_html(html), TOKEN)

    def test_conflicting_meta_values_and_ambiguous_attributes_fail_closed(self):
        for html in (
            f'<meta name="csrf-token" content="{TOKEN}"><meta name="csrf-token" content="{OTHER}">',
            f'<meta name="csrf-token" content="{TOKEN}" content="{OTHER}">',
            f'<meta name="csrf-token" name="unrelated" content="{TOKEN}">',
            f'<meta name="csrf-token" content="bad"><script id="__NEXT_DATA__">{{"CSRF_TOKEN":"{TOKEN}"}}</script>',
        ):
            self.assertIsNone(buyer.csrf_from_html(html))

    def test_repeated_identical_canonical_meta_is_unambiguous(self):
        tag = f'<meta name="csrf-token" content="{TOKEN}">'
        self.assertEqual(buyer.csrf_from_html(tag + tag), TOKEN)

    def test_meta_lookalikes_in_comments_scripts_text_or_body_are_not_canonical(self):
        for html in (
            f'<!-- <meta name="csrf-token" content="{OTHER}"> -->',
            f'<script>"<meta name=csrf-token content={OTHER}>"</script>',
            f'&lt;meta name="csrf-token" content="{OTHER}"&gt;',
            f'<body><meta name="csrf-token" content="{OTHER}"></body>',
        ):
            with self.subTest(html=html):
                self.assertIsNone(buyer.csrf_from_html(html))

    def test_whole_json_bootstrap_and_two_string_wrappers_remain_supported(self):
        value = json.dumps({"CSRF_TOKEN": TOKEN})
        for _ in range(3):
            self.assertEqual(buyer.csrf_from_html(value), TOKEN)
            value = json.dumps(value)

    def test_named_next_bootstrap_reads_fixed_configuration_containers(self):
        for value in (
            {"CSRF_TOKEN": TOKEN},
            {"config": {"csrf_token": TOKEN}},
            {"props": {"pageProps": {"config": {"csrfToken": TOKEN}}}},
        ):
            html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(value) + '</script>'
            self.assertEqual(buyer.csrf_from_html(html), TOKEN)

    def test_complete_json_documents_do_not_promote_description_text(self):
        for html in (
            json.dumps({"description": {"CSRF_TOKEN": OTHER}}),
            json.dumps({"description": f"<meta name=csrf-token content={OTHER}>"}),
        ):
            with self.subTest(html=html):
                self.assertIsNone(buyer.csrf_from_html(html))

    def test_legacy_quoted_next_bootstrap_remains_compatible_without_meta(self):
        # This keeps the pre-existing compatibility behavior. It does not
        # establish that arbitrary HTML fallback fields are trusted.
        bootstrap = json.dumps(json.dumps({"CSRF_TOKEN": TOKEN}))
        html = '<script>self.__next_f.push([1,' + bootstrap + '])</script>'
        self.assertEqual(buyer.csrf_from_html(html), TOKEN)

    def test_legacy_html_fallback_cannot_choose_between_conflicting_tokens(self):
        html = '<script>' + json.dumps({"CSRF_TOKEN": TOKEN}) + '</script>'
        html += '<script>' + json.dumps({"CSRF_TOKEN": OTHER}) + '</script>'
        self.assertIsNone(buyer.csrf_from_html(html))

    def test_duplicate_bootstrap_fields_and_conflicting_trusted_values_fail_closed(self):
        for html in (
            '{"CSRF_TOKEN":"' + TOKEN + '","CSRF_TOKEN":"' + OTHER + '"}',
            json.dumps({"CSRF_TOKEN": TOKEN, "config": {"CSRF_TOKEN": OTHER}}),
            '<script id="__NEXT_DATA__">' + json.dumps({"CSRF_TOKEN": TOKEN}) + '</script>'
            + '<script id="__NEXT_DATA__">' + json.dumps({"CSRF_TOKEN": OTHER}) + '</script>',
        ):
            self.assertIsNone(buyer.csrf_from_html(html))

    def test_invalid_header_values_and_bounded_input_fail_closed(self):
        for value in ("short", TOKEN + "\n", TOKEN + "\r", "x" * 1025, True, None):
            self.assertIsNone(buyer.csrf_from_html(json.dumps({"CSRF_TOKEN": value})))
        for html in (None, b"not-html", "x" * (4 * 1024 * 1024 + 1), "[" * 2000):
            self.assertIsNone(buyer.csrf_from_html(html))
