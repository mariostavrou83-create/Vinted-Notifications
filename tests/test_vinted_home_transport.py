import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

import requests

import vinted_home_transport as transport
from vinted_accounts import account_scope
from vinted_buyer import BuyerError


class HomeTransportTests(unittest.TestCase):
    def client(self, body=b"<html>Home</html>", status=200):
        client = Mock(account_role="buyer")
        client._verified_session = ("synthetic", b"sealed", {})
        client.accepted_response_cookies.return_value = nullcontext()
        response = requests.Response()
        response.status_code = status
        response.url = "https://www.vinted.co.uk/"
        response.headers["Date"] = "Fri, 20 Nov 2026 22:00:00 GMT"
        response._content = body
        response._content_consumed = True
        client.session.get.return_value = response
        return client

    def read(self, client, url="https://www.vinted.co.uk/"):
        with patch.object(transport, "yield_to_purchase"), patch.object(
            transport.time, "time", return_value=1795212002
        ):
            return transport.read_html(client, url)

    def test_bounded_one_get_preserves_existing_session(self):
        client = self.client()
        html, observed = self.read(client)
        self.assertIn("Home", html)
        self.assertEqual(observed, 1795212000)
        client.session.get.assert_called_once()
        self.assertFalse(client.session.get.call_args.kwargs["allow_redirects"])
        self.assertTrue(client.session.get.call_args.kwargs["stream"])
        client.persist_session.assert_called_once()
        client.solve_challenge.assert_not_called()
        client.request.assert_not_called()

    def test_home_reader_reuses_only_recent_verified_seller_connections(self):
        clock = Mock(return_value=1000)
        first = self.client()
        first.account_role = "seller"
        replacement = self.client()
        replacement.account_role = "seller"
        reader = transport.HomeReader(clock)
        url = "https://www.vinted.co.uk/"
        with patch.object(transport.seller, "exclusive", side_effect=lambda: account_scope("seller")), patch.object(
            transport.seller, "connected_client", side_effect=[first, replacement]
        ) as connect, patch.object(reader, "current_session", return_value=True) as saved, patch.object(
            transport, "yield_to_purchase"
        ), patch.object(transport, "read_html", return_value=("page", 1000)):
            self.assertEqual(reader.fetch("seller", url), ("page", 1000))
            clock.return_value += 1
            reader.fetch("seller", url)
            connect.assert_called_once_with(solve_challenges=False, allow_refresh=False)
            clock.return_value += transport.VERIFY_SECONDS
            reader.fetch("seller", url)
            self.assertEqual(connect.call_count, 2)
            first.session.close.assert_called_once()
            saved.side_effect = [True, False]
            with self.assertRaisesRegex(BuyerError, "connection changed"):
                reader.fetch("seller", url)
            replacement.session.close.assert_called_once()
            self.assertIsNone(reader.client)

    def test_foreign_urls_and_unverified_sessions_cannot_read(self):
        for url in (
            "https://example.com/",
            "https://www.vinted.co.uk/inbox",
            "https://www.vinted.co.uk/?token=x",
            "https://www.vinted.co.uk@evil.test/",
        ):
            client = self.client()
            with self.assertRaises(BuyerError):
                self.read(client, url)
            client.session.get.assert_not_called()
        client = self.client()
        client._verified_session = None
        with self.assertRaises(BuyerError):
            self.read(client)

    def test_refusal_redirect_size_and_missing_date_never_replay(self):
        cases = [
            self.client(status=403),
            self.client(status=302),
            self.client(body=b"x" * (transport.MAX_HTML_BYTES + 1)),
        ]
        for client in cases:
            with self.assertRaises(BuyerError):
                self.read(client)
            client.session.get.assert_called_once()
            client.persist_session.assert_not_called()
            client.solve_challenge.assert_not_called()

    def test_missing_date_keeps_accepted_rotation_but_cannot_alert(self):
        client = self.client()
        client.session.get.return_value.headers.clear()
        with self.assertRaises(BuyerError):
            self.read(client)
        client.update_tokens.assert_called_once()
        client.persist_session.assert_called_once()

    def test_only_a_plain_listing_get_failure_has_listing_scope(self):
        item_url = "https://www.vinted.co.uk/items/500-top"
        for url, expected in ((item_url, "listing_page"), ("https://www.vinted.co.uk/", "request")):
            client = self.client()
            client.session.get.side_effect = requests.ConnectTimeout()
            with self.assertRaises(BuyerError) as caught:
                self.read(client, url)
            self.assertEqual((caught.exception.reason, caught.exception.stage), ("network", expected))
            client.session.get.assert_called_once()
        client = self.client(status=503)
        with self.assertRaises(BuyerError) as caught:
            self.read(client, item_url)
        self.assertEqual((caught.exception.status, caught.exception.stage), (503, "listing_page"))

    def test_provider_wait_survives_unreadable_error_bodies(self):
        item_url = "https://www.vinted.co.uk/items/500-top"
        for status, body in ((503, b"unavailable"), (503, b"\xff"), (429, b"\xff"), (307, b"redirect")):
            client = self.client(body=body, status=status)
            client.session.get.return_value.headers["Retry-After"] = "480"
            client.session.get.return_value.headers["Location"] = "/items/500-new-title"
            if status == 429:
                client.session.get.return_value.iter_content = Mock(side_effect=AssertionError("must not wait for body"))
            with self.assertRaises(BuyerError) as caught:
                self.read(client, item_url)
            self.assertEqual((caught.exception.status, caught.exception.retry_after, caught.exception.stage),
                             (status, 480, "request"))
            client.session.get.assert_called_once()

    def test_only_plain_same_item_redirect_can_be_skipped_without_cooldown(self):
        item_url = "https://www.vinted.co.uk/items/500-old-title"
        for location in (
            "/items/500-new-title",
            "https://www.vinted.co.uk/items/500-new-title/",
        ):
            client = self.client(status=307)
            client.session.get.return_value.headers["Location"] = location
            with self.assertRaises(BuyerError) as caught:
                self.read(client, item_url)
            self.assertEqual(caught.exception.reason, "listing_redirect")
            client.session.get.assert_called_once()
            client.persist_session.assert_not_called()

        for location in (
            "/items/501-another-item",
            "/items/500-new-title?challenge=private",
            "/items/500-new-title#private",
            "/member/login",
            "/",
            "https://other.test/items/500-new-title",
            "http://www.vinted.co.uk/items/500-new-title",
            "https://name:secret@www.vinted.co.uk/items/500-new-title",
        ):
            client = self.client(status=307)
            client.session.get.return_value.headers["Location"] = location
            with self.assertRaises(BuyerError) as caught:
                self.read(client, item_url)
            self.assertNotEqual(caught.exception.reason, "listing_redirect")
            client.session.get.assert_called_once()

    def test_challenge_body_precedes_same_item_redirect_classification(self):
        client = self.client(body=b"<html>Verify you are human</html>", status=307)
        client.session.get.return_value.headers["Location"] = "/items/500-new-title"
        with self.assertRaises(BuyerError) as caught:
            self.read(client, "https://www.vinted.co.uk/items/500-old-title")
        self.assertEqual(caught.exception.reason, "security_challenge")
        client.session.get.assert_called_once()
        client.solve_challenge.assert_not_called()

    def test_cache_age_uses_the_older_of_the_two_independent_estimates(self):
        headers = {"Date": "Fri, 20 Nov 2026 22:00:00 GMT", "Age": "120"}
        origin = 1795212000
        self.assertEqual(transport.response_time(headers, origin + 2), origin + 2 - 120)
        self.assertEqual(transport.response_time(headers, origin + 120), origin)
        self.assertEqual(transport.response_time(headers, origin + 240), origin)
        self.assertEqual(transport.response_time({"Date": headers["Date"]}, origin + 240), origin)
        self.assertEqual(transport.response_time({"Date": headers["Date"]}, origin - 2), origin - 2)
        for age in ("bad", "-1", "NaN"):
            with self.assertRaises(BuyerError):
                transport.response_time({**headers, "Age": age}, 1795212002)
