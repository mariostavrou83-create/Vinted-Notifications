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
        ):
            self.assertIsNone(gallery.listing_url(url))
        self.assertEqual(
            gallery.listing_url("https://vinted.co.uk/items/123-green-coat?ref=search"),
            "https://www.vinted.co.uk/items/123-green-coat",
        )


class FetchTests(DatabaseFixture, unittest.TestCase):
    def response(self, body, status=200):
        response = MagicMock(status_code=status)
        response.headers = {}
        response.__enter__.return_value = response
        response.iter_content.return_value = [body.encode()]
        return response

    def test_real_gallery_markup_and_no_redirects(self):
        html = "".join(
            f'<img src="{photo(str(i))}" data-testid="item-photo-{i}--img">'
            for i in range(1, 5)
        )
        with patch.object(
            buyer.BrowserSession, "get", return_value=self.response(html)
        ) as get:
            photos, state = gallery.fetch_gallery(
                "https://www.vinted.co.uk/items/123-coat"
            )
        self.assertEqual(state, "ready")
        self.assertEqual(len(photos), 4)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertEqual(get.call_args.kwargs["timeout"], (2, 4))

    def test_saved_buyer_session_is_used_for_read_only_html_without_refresh(self):
        saved = {"cookies": {"access_token_web": "test-access-token-0123456789"}}
        with closing(connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=? WHERE id=1", (buyer.encrypt(saved),)
            )
        client = MagicMock()
        client.session.get.return_value = self.response(
            '<div data-testid="item-description">Seller text</div>'
        )
        with patch.object(buyer, "Client", return_value=client) as construct:
            result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
        construct.assert_called_once_with(saved)
        client.request.assert_not_called()
        client.session.close.assert_called_once()
        self.assertFalse(client.session.get.call_args.kwargs["allow_redirects"])
        self.assertEqual(
            client.session.get.call_args.args[0], "https://www.vinted.co.uk/items/123"
        )
        self.assertEqual(result["description"], "Seller text")

    def test_challenge_and_rate_limit_back_off_without_looping(self):
        for status, body, expected in (
            (200, "<title>Client Challenge</title>", "challenge"),
            (403, "<html>Verify you are human</html>", "challenge"),
            (429, "", "access_limited"),
        ):
            with patch.object(
                buyer.BrowserSession, "get", return_value=self.response(body, status)
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
            buyer.BrowserSession, "get", return_value=response
        ), patch.object(gallery, "_pause"), self.assertLogs(
            "vinted_gallery", level="INFO"
        ) as logs:
            result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
        self.assertEqual(result["state"], "access_limited")
        self.assertEqual(next(response.iter_content.return_value), b"unused")
        self.assertIn("http=403", " ".join(logs.output))

    def test_anonymous_reads_use_browser_transport_and_close_the_session(self):
        client = MagicMock()
        client.session.get.return_value = self.response(
            '<div data-testid="item-description">Public seller text</div>'
        )
        with patch.object(buyer, "Client", return_value=client) as construct:
            result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
        construct.assert_called_once_with()
        client.session.close.assert_called_once()
        client.request.assert_not_called()
        headers = client.session.get.call_args.kwargs["headers"]
        self.assertEqual(headers["Sec-Fetch-Mode"], "navigate")
        self.assertIsNone(headers["Origin"])
        self.assertIsNone(headers["Content-Type"])
        self.assertEqual(result["description"], "Public seller text")

    def test_unreadable_saved_session_keeps_configured_browser_fallback(self):
        with closing(connection()) as conn, conn:
            conn.execute("UPDATE vinted_buyer SET session=? WHERE id=1", (b"bad",))
        client = MagicMock()
        client.session.get.return_value = self.response("")
        with patch.object(buyer, "Client", return_value=client) as construct:
            result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
        construct.assert_called_once_with()
        client.session.close.assert_called_once()
        self.assertEqual(result["state"], "gallery_unavailable")

    def test_invalid_private_proxy_fails_without_anonymous_network_fallback(self):
        with closing(connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET network=? WHERE id=1",
                (buyer.encrypt({"proxy": "invalid-proxy"}),),
            )
        with patch.object(buyer.BrowserSession, "get") as get:
            result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
        get.assert_not_called()
        self.assertEqual(result["state"], "configuration_error")

    def test_supported_challenge_retries_once_with_fresh_download_deadline(self):
        clock = [0]
        first = self.response(
            '<html><script src="https://geo.captcha-delivery.com/captcha/?id=test"></script></html>',
            403,
        )
        second = self.response(
            '<div data-testid="item-description">Actual seller notes</div>'
        )
        client = MagicMock()
        client.session.get.side_effect = [first, second]

        def solve(snapshot, data):
            self.assertTrue(first.__exit__.called)
            self.assertEqual(snapshot.status_code, 403)
            self.assertEqual(snapshot.url, "https://www.vinted.co.uk/items/123")
            self.assertLessEqual(len(snapshot.content), 65536)
            clock[0] = 60
            return True

        client.solve_challenge.side_effect = solve
        with patch.object(buyer, "Client", return_value=client), patch.object(
            gallery.time, "monotonic", side_effect=lambda: clock[0]
        ), patch.object(gallery, "_pause") as pause:
            result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
        self.assertEqual(result["description"], "Actual seller notes")
        self.assertEqual(client.session.get.call_count, 2)
        client.solve_challenge.assert_called_once()
        client.session.close.assert_called_once()
        pause.assert_not_called()

    def test_repeated_challenge_stops_after_single_solve_and_two_reads(self):
        client = MagicMock()
        client.session.get.side_effect = [
            self.response("<html>Verify you are human</html>", 403),
            self.response("<html>Verify you are human</html>", 403),
        ]
        client.solve_challenge.return_value = True
        with patch.object(buyer, "Client", return_value=client), patch.object(
            gallery, "_pause"
        ) as pause:
            result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
        self.assertEqual(result["state"], "challenge")
        self.assertEqual(client.session.get.call_count, 2)
        client.solve_challenge.assert_called_once()
        pause.assert_called_once_with(300)
        client.session.close.assert_called_once()

    def test_generic_access_denial_never_starts_paid_solver_task(self):
        client = MagicMock()
        client.session.get.return_value = self.response(
            '{"message":"Access forbidden"}', 403
        )
        with patch.object(buyer, "Client", return_value=client), patch.object(
            gallery, "_pause"
        ) as pause:
            result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
        self.assertEqual(result["state"], "access_limited")
        client.solve_challenge.assert_not_called()
        client.session.get.assert_called_once()
        pause.assert_called_once_with(300)

    def test_json_challenge_uses_only_bounded_bytes_and_closes_stream_before_solve(
        self,
    ):
        body = b'{"url":"https://geo.captcha-delivery.com/captcha/?id=test"}'
        first = self.response(body.decode(), 403)
        first.iter_content.return_value = iter([body])
        client = MagicMock()
        client.session.get.return_value = first
        client.solve_challenge.return_value = False
        with patch.object(buyer, "Client", return_value=client), patch.object(
            gallery, "_pause"
        ):
            result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
        self.assertEqual(result["state"], "challenge")
        snapshot, data = client.solve_challenge.call_args.args
        self.assertEqual(
            data, {"url": "https://geo.captcha-delivery.com/captcha/?id=test"}
        )
        self.assertEqual(snapshot.content, body)
        first.__exit__.assert_called_once()
        client.session.get.assert_called_once()

    def test_oversized_listing_stops_and_closes_without_parsing_or_solving(self):
        response = self.response("")
        response.iter_content.return_value = iter([b"x" * 65536] * 65 + [b"unused"])
        client = MagicMock()
        client.session.get.return_value = response
        with patch.object(buyer, "Client", return_value=client), patch.object(
            gallery, "parse_listing"
        ) as parse:
            result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
        self.assertEqual(result["state"], "download_limit")
        self.assertEqual(next(response.iter_content.return_value), b"unused")
        parse.assert_not_called()
        client.solve_challenge.assert_not_called()
        response.__exit__.assert_called_once()
        client.session.close.assert_called_once()

    def test_one_same_item_canonical_redirect_returns_actual_listing(self):
        first = self.response("", 307)
        first.headers = {"Location": "/items/123-green-coat"}
        second = self.response(
            '<div data-testid="item-description">Canonical seller text</div>'
        )
        client = MagicMock()
        client.session.get.side_effect = [first, second]
        with patch.object(buyer, "Client", return_value=client):
            result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
        self.assertEqual(result["description"], "Canonical seller text")
        self.assertEqual(
            [call.args[0] for call in client.session.get.call_args_list],
            [
                "https://www.vinted.co.uk/items/123",
                "https://www.vinted.co.uk/items/123-green-coat",
            ],
        )
        self.assertTrue(
            all(
                not call.kwargs["allow_redirects"]
                for call in client.session.get.call_args_list
            )
        )
        client.solve_challenge.assert_not_called()
        client.session.close.assert_called_once()

    def test_redirects_to_other_items_accounts_hosts_or_query_strings_stop(self):
        for location in (
            "/items/999-different-item",
            "/web/api/auth/refresh",
            "https://example.test/items/123-coat",
            "https://vinted.co.uk/items/123-coat",
            "https://user:password@www.vinted.co.uk/items/123-coat",
            "/items/123-coat?token=secret",
            "/items/123-coat#fragment",
            "/items/123",
            "https://[invalid/items/123-coat",
        ):
            with self.subTest(location=location):
                response = self.response("", 307)
                response.headers = {"Location": location}
                client = MagicMock()
                client.session.get.return_value = response
                with patch.object(buyer, "Client", return_value=client):
                    result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
                self.assertEqual(result["state"], "http_307")
                client.session.get.assert_called_once()
                client.solve_challenge.assert_not_called()
                client.session.close.assert_called_once()

    def test_canonical_redirect_loop_stops_after_two_reads(self):
        first = self.response("", 307)
        first.headers = {"Location": "/items/123-coat"}
        second = self.response("", 307)
        second.headers = {"Location": "/items/123"}
        client = MagicMock()
        client.session.get.side_effect = [first, second]
        with patch.object(buyer, "Client", return_value=client):
            result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
        self.assertEqual(result["state"], "http_307")
        self.assertEqual(client.session.get.call_count, 2)
        client.solve_challenge.assert_not_called()

    def test_canonical_redirect_and_supported_challenge_use_at_most_three_reads(self):
        first = self.response("", 307)
        first.headers = {"Location": "/items/123-coat"}
        second = self.response("<html>Verify you are human</html>", 403)
        third = self.response(
            '<div data-testid="item-description">Seller text after check</div>'
        )
        client = MagicMock()
        client.session.get.side_effect = [first, second, third]
        client.solve_challenge.return_value = True
        with patch.object(buyer, "Client", return_value=client), patch.object(
            gallery, "_pause"
        ) as pause:
            result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
        self.assertEqual(result["description"], "Seller text after check")
        self.assertEqual(client.session.get.call_count, 3)
        client.solve_challenge.assert_called_once()
        pause.assert_not_called()

    def test_security_redirect_is_solved_without_following_the_challenge_host(self):
        first = self.response("", 307)
        first.headers = {
            "Location": "https://geo.captcha-delivery.com/captcha/?t=fe&id=test"
        }
        second = self.response(
            '<div data-testid="item-description">Text after solver</div>'
        )
        client = MagicMock()
        client.session.get.side_effect = [first, second]
        client.solve_challenge.return_value = True
        with patch.object(buyer, "Client", return_value=client):
            result = gallery.fetch_listing("https://www.vinted.co.uk/items/123")
        self.assertEqual(result["description"], "Text after solver")
        self.assertEqual(
            [call.args[0] for call in client.session.get.call_args_list],
            ["https://www.vinted.co.uk/items/123"] * 2,
        )
        snapshot, data = client.solve_challenge.call_args.args
        self.assertEqual(snapshot.headers["Location"], first.headers["Location"])
        self.assertIsNone(data)
        client.solve_challenge.assert_called_once()

    def test_caller_owned_client_and_custom_parser_return_purchase_data(self):
        html = "<html>Bounded marketplace item payload</html>"
        response = self.response(html)
        client = MagicMock()
        client.session.get.return_value = response
        item = {"id": 123, "seller_id": 456, "price": "12.50", "currency": "GBP"}
        parser = MagicMock(return_value={"item": item})
        with patch.object(buyer, "Client") as construct:
            result = gallery.fetch_listing(
                "https://www.vinted.co.uk/items/123", client=client, parser=parser
            )
        self.assertEqual(
            result,
            {"photos": [], "description": "", "item": item, "state": "ready"},
        )
        parser.assert_called_once_with(html, "https://www.vinted.co.uk/items/123")
        client.session.get.assert_called_once()
        client.session.close.assert_not_called()
        response.__exit__.assert_called_once()
        construct.assert_not_called()
        with closing(connection()) as conn:
            self.assertIsNone(
                conn.execute("SELECT session FROM vinted_buyer WHERE id=1").fetchone()[
                    0
                ]
            )

    def test_custom_parser_receives_only_successful_same_item_canonical_response(self):
        first = self.response("unusable redirect payload", 307)
        first.headers = {"Location": "/items/123-coat"}
        html = "<html>Canonical purchase data</html>"
        second = self.response(html)
        client = MagicMock()
        client.session.get.side_effect = [first, second]
        parser = MagicMock(return_value={"item": {"id": 123}})
        with patch.object(buyer, "Client") as construct:
            result = gallery.fetch_listing(
                "https://www.vinted.co.uk/items/123", client=client, parser=parser
            )
        self.assertEqual(result["state"], "ready")
        parser.assert_called_once_with(html, "https://www.vinted.co.uk/items/123")
        self.assertEqual(client.session.get.call_count, 2)
        client.session.close.assert_not_called()
        construct.assert_not_called()

    def test_custom_parser_is_skipped_on_denials_challenges_and_download_limits(self):
        denied = self.response("Access forbidden", 403)
        missing = self.response("Missing", 404)
        challenge = self.response("<html>Verify you are human</html>", 403)
        oversized = self.response("")
        oversized.iter_content.return_value = [b"x" * (4 * 1024 * 1024 + 1)]
        for response, expected in (
            (denied, "access_limited"),
            (missing, "http_404"),
            (challenge, "challenge"),
            (oversized, "download_limit"),
        ):
            with self.subTest(state=expected):
                client = MagicMock()
                client.session.get.return_value = response
                client.solve_challenge.return_value = False
                parser = MagicMock()
                with patch.object(gallery, "_pause"):
                    result = gallery.fetch_listing(
                        "https://www.vinted.co.uk/items/123",
                        client=client,
                        parser=parser,
                    )
                self.assertEqual(result["state"], expected)
                parser.assert_not_called()
                client.session.close.assert_not_called()
                response.__exit__.assert_called_once()

    def test_shared_cooldown_applies_to_caller_owned_purchase_reads(self):
        gallery._pause(300)
        client = MagicMock()
        parser = MagicMock()
        result = gallery.fetch_listing(
            "https://www.vinted.co.uk/items/123", client=client, parser=parser
        )
        self.assertEqual(result["state"], "cooldown")
        self.assertGreater(result["retry_after"], gallery.time.time())
        client.session.get.assert_not_called()
        client.session.close.assert_not_called()
        parser.assert_not_called()

    def test_listing_response_closes_only_a_client_it_creates(self):
        borrowed = MagicMock()
        borrowed_response = self.response("Borrowed")
        borrowed.session.get.return_value = borrowed_response
        with patch.object(buyer, "Client") as construct, gallery.listing_response(
            "https://www.vinted.co.uk/items/123", borrowed
        ) as response:
            self.assertIs(response, borrowed_response)
        construct.assert_not_called()
        borrowed.session.close.assert_not_called()
        borrowed_response.__exit__.assert_called_once()

        owned = MagicMock()
        owned_response = self.response("Owned")
        owned.session.get.return_value = owned_response
        with patch.object(
            buyer, "Client", return_value=owned
        ), gallery.listing_response("https://www.vinted.co.uk/items/123") as response:
            self.assertIs(response, owned_response)
        owned.session.close.assert_called_once()
        owned_response.__exit__.assert_called_once()

    def test_listing_response_rejects_external_url_before_touching_buyer_client(self):
        client = MagicMock()
        with self.assertRaises(buyer.BuyerError), gallery.listing_response(
            "https://example.test/items/123", client
        ):
            self.fail("An external listing request must not be made")
        client.session.get.assert_not_called()
        client.session.close.assert_not_called()


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
