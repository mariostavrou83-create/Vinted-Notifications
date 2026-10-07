"""Gallery scoping, bounded fetches, and durable caching without live requests."""

import unittest
from contextlib import closing
from unittest.mock import MagicMock, patch

from test_finds_delivery import outbox
from test_search_controls import DatabaseFixture

import vinted_alerts
import vinted_buyer as buyer
import vinted_gallery as gallery
from search_settings import connection


def photo(token, size="f800"):
    return f"https://images1.vinted.net/t/{token}/{size}/image.webp?s=public"


class ParserTests(unittest.TestCase):
    def test_description_void_elements_cannot_leak_recommendation_text(self):
        html = (
            '<div data-testid="item-description">Soft<wbr>cotton'
            '<source src="unused"><embed src="unused"><br>Small mark</div>'
            "<p>Wrong recommended item notes</p>"
        )
        self.assertEqual(
            gallery.parse_listing(html, "https://www.vinted.co.uk/items/123")[
                "description"
            ],
            "Softcotton\nSmall mark",
        )

    def test_explicit_description_text_excludes_controls_and_hidden_content(self):
        html = (
            '<div data-testid="item-description"><h2>Description</h2>'
            '<div data-testid="item-description-text">Actual seller notes'
            '<style>.private {content: "Wrong"}</style><script>"Wrong"</script>'
            '<span hidden>Hidden duplicate</span><span aria-hidden="true">Wrong</span>'
            "<button>Read more</button><br>Smoke free home</div>"
            "<button>Read more</button></div><p>Unrelated description</p>"
        )
        self.assertEqual(
            gallery.parse_listing(html, "https://www.vinted.co.uk/items/123")[
                "description"
            ],
            "Actual seller notes\nSmoke free home",
        )

    def test_unclosed_dom_description_falls_back_to_identity_scoped_json(self):
        import json

        html = (
            '<script id="__NEXT_DATA__">'
            + json.dumps(
                {"id": 123, "title": "Coat", "description": "Correct seller notes"}
            )
            + '</script><div data-testid="item-description">Broken DOM description'
        )
        self.assertEqual(
            gallery.parse_listing(html, "https://www.vinted.co.uk/items/123")[
                "description"
            ],
            "Correct seller notes",
        )

    def test_server_payload_is_used_for_matching_listing_with_dom_photo_first(self):
        import json

        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(
                {
                    "item": {
                        "id": 123,
                        "seller_id": 2,
                        "description": "Seller cuff notes",
                        "photos": [{"url": photo("b")}],
                    },
                    "recommendations": [
                        {
                            "id": 999,
                            "description": "Wrong item",
                            "photos": [{"url": photo("wrong")}],
                        }
                    ],
                }
            )
            + "</script>"
        )
        html += f'<img data-testid="item-photo-1--img" src="{photo("a")}">'
        result = gallery.parse_listing(html, "https://www.vinted.co.uk/items/123")
        self.assertEqual(
            result,
            {"photos": [photo("a"), photo("b")], "description": "Seller cuff notes"},
        )

    def test_description_stays_inside_listing_and_structured_data_matches_id(self):
        html = '<div data-testid="item-description"><span>Soft cotton</span><br>Small mark &amp; wear</div><p>Unrelated recommendation</p>'
        result = gallery.parse_listing(html, "https://www.vinted.co.uk/items/123")
        self.assertEqual(result["description"], "Soft cotton\nSmall mark & wear")
        html = """<script type="application/ld+json">[{"@type":"Product","url":"https://www.vinted.co.uk/items/999","description":"Wrong item"},{"@type":"Product","url":"https://www.vinted.co.uk/items/123-coat","description":"<p>Right coat</p>"}]</script>"""
        self.assertEqual(
            gallery.parse_listing(html, "https://www.vinted.co.uk/items/123")[
                "description"
            ],
            "Right coat",
        )

    def test_gallery_uses_only_numbered_listing_images_not_recommendations(self):
        html = f'<img src="{photo("seller-avatar")}">'
        for index in (3, 1, 2, 4, 5):
            html += (
                f'<img data-testid="item-photo-{index}--img" src="{photo(str(index))}">'
            )
        html += f'<img data-testid="product-item-id-999--image" src="{photo("recommendation")}">'
        self.assertEqual(
            gallery.parse_gallery(html), [photo(str(i)) for i in range(1, 5)]
        )
        self.assertEqual(
            gallery.distinct_photos([photo("a"), photo("a", "f310"), photo("b")]),
            [photo("a"), photo("b")],
        )

    def test_urls_reject_credentials_redirect_hosts_and_non_listing_paths(self):
        for url in (
            "http://www.vinted.co.uk/items/1",
            "https://vinted.co.uk.evil.test/items/1",
            "https://token@www.vinted.co.uk/items/1",
            "https://www.vinted.co.uk/catalog",
            "https://www.vinted.co.uk:444/items/1",
            "https://[www.vinted.co.uk/items/1",
            "https://www.vinted.co.uk/items/" + "1" * 21,
            "https://www.vinted.co.uk/items/١٢٣",
            "https://www.vinted.co.uk/\nitems/1",
            "https://www.vinted.co.uk/items/123-wrong%2Fpath",
        ):
            self.assertIsNone(gallery.listing_url(url))
        self.assertEqual(
            gallery.listing_url("https://vinted.co.uk/items/123-green-coat?ref=search"),
            "https://www.vinted.co.uk/items/123-green-coat",
        )
        self.assertEqual(
            gallery.listing_url("https://www.vinted.co.uk/items/123-caf%C3%A9"),
            "https://www.vinted.co.uk/items/123-caf%C3%A9",
        )

    def test_invalid_parser_urls_return_empty_data_without_throwing(self):
        for url in (None, "https://[bad", "https://www.vinted.co.uk/catalog"):
            self.assertEqual(
                gallery.parse_listing(
                    '<div data-testid="item-description">Wrong</div>', url
                ),
                {"photos": [], "description": ""},
            )


class FetchTests(DatabaseFixture, unittest.TestCase):
    def response(self, body, status=200):
        response = MagicMock(status_code=status)
        response.__enter__.return_value = response
        response.iter_content.return_value = [body.encode()]
        response.headers = {}
        return response

    def test_same_item_canonical_redirect_is_followed_once_without_automatic_redirects(
        self,
    ):
        redirect = self.response("", 308)
        redirect.headers["Location"] = "/items/123-current-slug"
        ready = self.response('<div data-testid="item-description">Seller notes</div>')
        with patch.object(
            gallery.requests.Session, "get", side_effect=[redirect, ready]
        ) as get:
            result = gallery.fetch_listing(
                "https://www.vinted.co.uk/items/123-old-slug"
            )
        self.assertEqual(result["description"], "Seller notes")
        self.assertEqual(result["state"], "ready")
        self.assertEqual(get.call_count, 2)
        self.assertEqual(
            get.call_args_list[1].args[0],
            "https://www.vinted.co.uk/items/123-current-slug",
        )
        self.assertTrue(
            all(not call.kwargs["allow_redirects"] for call in get.call_args_list)
        )

    def test_auth_foreign_cross_item_and_query_redirects_are_never_followed(self):
        for location in (
            "/web/api/auth/refresh",
            "/items/999-another-item",
            "https://evil.test/items/123-coat",
            "https://secret@www.vinted.co.uk/items/123-coat",
            "https://www.vinted.co.uk:444/items/123-coat",
            "/items/123-coat?verification=secret",
            "/items/123-coat#challenge",
            "https://[invalid",
        ):
            redirect = self.response("", 307)
            redirect.headers["Location"] = location
            with patch.object(
                gallery.requests.Session, "get", return_value=redirect
            ) as get:
                result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
            self.assertEqual(result["state"], "http_307")
            get.assert_called_once()

    def test_second_redirect_is_terminal_and_has_no_third_request(self):
        first = self.response("", 301)
        first.headers["Location"] = "/items/123-first"
        second = self.response("", 302)
        second.headers["Location"] = "/items/123-second"
        with patch.object(
            gallery.requests.Session, "get", side_effect=[first, second]
        ) as get:
            result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
        self.assertEqual(result["state"], "http_302")
        self.assertEqual(get.call_count, 2)

    def test_real_gallery_markup_and_no_redirects(self):
        html = "".join(
            f'<img src="{photo(str(i))}" data-testid="item-photo-{i}--img">'
            for i in range(1, 5)
        )
        with patch.object(
            gallery.requests.Session, "get", return_value=self.response(html)
        ) as get:
            photos, state = gallery.fetch_gallery(
                "https://www.vinted.co.uk/items/123-coat"
            )
        self.assertEqual(state, "ready")
        self.assertEqual(len(photos), 4)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertEqual(get.call_args.kwargs["timeout"], (2, 4))

    def test_public_listing_read_never_uses_saved_buyer_credentials(self):
        saved = {"cookies": {"access_token_web": "expired-private-token-0123456789"}}
        with closing(connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=? WHERE id=1", (buyer.encrypt(saved),)
            )
        response = self.response(
            '<div data-testid="item-description">Seller text</div>'
        )
        with patch.object(buyer, "Client") as construct, patch.object(
            gallery.requests.Session, "get", return_value=response
        ) as get:
            result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
        construct.assert_not_called()
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertEqual(result["description"], "Seller text")

    def test_public_canonical_read_reuses_only_its_own_cookies(self):
        first = self.response("", 301)
        first.headers["Location"] = "/items/123-correct-slug"
        captured = []

        def request(session, url, **kwargs):
            captured.append((session, dict(session.cookies)))
            if len(captured) == 1:
                session.cookies.set(
                    "anon_id", "ordinary-public-id", domain="www.vinted.co.uk"
                )
                return first
            return self.response(
                '<div data-testid="item-description">Seller text</div>'
            )

        with patch.object(
            gallery.requests.Session, "get", autospec=True, side_effect=request
        ), patch.object(gallery.requests.Session, "close") as close:
            result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
        self.assertIs(captured[0][0], captured[1][0])
        self.assertEqual(captured[0][1], {})
        self.assertEqual(captured[1][1], {"anon_id": "ordinary-public-id"})
        close.assert_called_once()
        self.assertEqual(result["description"], "Seller text")

    def test_server_retry_after_survives_in_shared_cooldown_and_result(self):
        from email.utils import formatdate

        for header in ("900", formatdate(1900, usegmt=True)):
            with closing(connection()) as conn, conn:
                conn.execute("DELETE FROM delivery_runtime")
            limited = self.response("", 429)
            limited.headers["Retry-After"] = header
            with patch.object(gallery.time, "time", return_value=1000), patch.object(
                gallery.requests.Session, "get", return_value=limited
            ) as get:
                result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
                deferred = gallery.fetch_listing("https://www.vinted.co.uk/items/456")
            self.assertEqual(result["retry_after"], 1900)
            self.assertEqual(deferred["retry_after"], 1900)
            self.assertEqual(deferred["state"], "cooldown")
            get.assert_called_once()

    def test_invalid_short_or_negative_retry_after_keeps_minimum_pause(self):
        for header in (None, "-10", "10", "invalid date", "x" * 101):
            response = self.response("", 429)
            response.headers["Retry-After"] = header
            self.assertEqual(gallery.retry_delay(response), 300)

    def test_challenge_and_rate_limit_back_off_without_looping(self):
        for status, body, expected in (
            (200, "<title>Client Challenge</title>", "challenge"),
            (403, "<html>Verify you are human</html>", "challenge"),
            (429, "", "access_limited"),
        ):
            with patch.object(
                gallery.requests.Session,
                "get",
                return_value=self.response(body, status),
            ) as get, patch.object(gallery, "_pause") as pause:
                self.assertEqual(
                    gallery.fetch_gallery("https://www.vinted.co.uk/items/123")[1],
                    expected,
                )
                get.assert_called_once()
                pause.assert_called_once_with(300)

    def test_access_denial_body_is_bounded_and_http_status_is_logged(self):
        response = self.response("")
        response.status_code = 403
        response.iter_content.return_value = iter([b"x" * 8192] * 8 + [b"unused"])
        with patch.object(
            gallery.requests.Session, "get", return_value=response
        ), patch.object(gallery, "_pause"), self.assertLogs(
            "vinted_gallery", level="INFO"
        ) as logs:
            result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
        self.assertEqual(result["state"], "access_limited")
        self.assertEqual(next(response.iter_content.return_value), b"unused")
        self.assertIn("http=403", " ".join(logs.output))


class ResolveTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    async def test_transient_failure_retries_after_cooldown_then_caches_success(self):
        self.batch(1, [110])
        row = outbox(110)
        details = vinted_alerts.get_details(row)
        with patch.object(gallery.time, "time", return_value=1000), patch.object(
            gallery,
            "fetch_listing",
            return_value={"photos": [], "description": "", "state": "access_limited"},
        ) as fetch:
            await gallery.resolve(row, details, include_description=True)
            await gallery.resolve(row, details, include_description=True)
            fetch.assert_called_once()
            self.assertTrue(gallery.retry_pending(details))
        details = vinted_alerts.get_details(row)
        with patch.object(gallery.time, "time", return_value=1301), patch.object(
            gallery,
            "fetch_listing",
            return_value={
                "photos": [],
                "description": "Real seller text",
                "state": "ready",
            },
        ) as fetch:
            await gallery.resolve(row, details, include_description=True)
            await gallery.resolve(row, details, include_description=True)
            fetch.assert_called_once()
            self.assertFalse(gallery.retry_pending(details))
        self.assertEqual(details["description"], "Real seller text")
        self.assertEqual(outbox(110)["content"], row["content"])

    async def test_retry_requests_are_bounded_and_challenges_are_terminal(self):
        self.batch(1, [110])
        row = outbox(110)
        for state in ("network_error", "challenge", "http_404"):
            details = {"photos": []}
            with patch.object(
                gallery,
                "fetch_listing",
                return_value={"photos": [], "description": "", "state": state},
            ) as fetch:
                for now in (1000, 1301, 1602, 1903):
                    with patch.object(gallery.time, "time", return_value=now):
                        await gallery.resolve(
                            row, details, persist=False, include_description=True
                        )
                self.assertEqual(fetch.call_count, 3 if state == "network_error" else 1)

    async def test_description_is_fetched_once_and_saved_with_gallery(self):
        self.batch(1, [110])
        row = outbox(110)
        details = vinted_alerts.get_details(row)
        with patch.object(
            gallery,
            "fetch_listing",
            return_value={
                "photos": [],
                "description": "Sleeve has a small mark",
                "state": "ready",
            },
        ) as fetch:
            await gallery.resolve(row, details, include_description=True)
            await gallery.resolve(
                row, vinted_alerts.get_details(row), include_description=True
            )
        fetch.assert_called_once()
        self.assertEqual(details["description"], "Sleeve has a small mark")

    async def test_full_gallery_is_cached_and_fast_alert_content_unchanged(self):
        self.batch(1, [110])
        row = outbox(110)
        details = vinted_alerts.get_details(row)
        with patch.object(
            gallery,
            "fetch_gallery",
            return_value=([photo(str(i)) for i in range(4)], "ready"),
        ) as fetch:
            result = await gallery.resolve(row, details)
            self.assertEqual(len(result), 4)
            self.assertEqual(
                await gallery.resolve(row, vinted_alerts.get_details(row)), result
            )
            fetch.assert_called_once()
        self.assertEqual(outbox(110)["content"], row["content"])
        self.assertEqual(outbox(110)["status"], "pending")
