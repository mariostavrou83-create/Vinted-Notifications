import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

import requests

import vinted_home_transport as transport
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
        with patch.object(transport, "yield_to_purchase"), patch.object(transport.time, "time", return_value=1795212002):
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

    def test_foreign_urls_and_unverified_sessions_cannot_read(self):
        for url in ("https://example.com/", "https://www.vinted.co.uk/inbox", "https://www.vinted.co.uk/?token=x", "https://www.vinted.co.uk@evil.test/"):
            client = self.client()
            with self.assertRaises(BuyerError):
                self.read(client, url)
            client.session.get.assert_not_called()
        client = self.client()
        client._verified_session = None
        with self.assertRaises(BuyerError):
            self.read(client)

    def test_refusal_redirect_size_and_missing_date_never_replay(self):
        cases = [self.client(status=403), self.client(status=302), self.client(body=b"x"*(transport.MAX_HTML_BYTES+1))]
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

    def test_cache_age_is_added_not_erased(self):
        headers = {"Date": "Fri, 20 Nov 2026 22:00:00 GMT", "Age": "120"}
        self.assertEqual(transport.response_time(headers, 1795212002), 1795211880)
        for age in ("bad", "-1", "NaN"):
            with self.assertRaises(BuyerError):
                transport.response_time({**headers, "Age": age}, 1795212002)
