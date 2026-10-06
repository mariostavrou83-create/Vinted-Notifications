"""Gallery scoping, bounded fetches, and durable caching without live requests."""

import unittest
from unittest.mock import MagicMock, patch

from test_finds_delivery import outbox
from test_search_controls import DatabaseFixture

import vinted_alerts
import vinted_gallery as gallery


def photo(token, size="f800"):
    return f"https://images1.vinted.net/t/{token}/{size}/image.webp?s=public"


class ParserTests(unittest.TestCase):
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
        response.__enter__.return_value = response
        response.iter_content.return_value = [body.encode()]
        return response

    def test_real_gallery_markup_and_no_redirects(self):
        html = "".join(
            f'<img src="{photo(str(i))}" data-testid="item-photo-{i}--img">'
            for i in range(1, 5)
        )
        with patch.object(
            gallery.requests, "get", return_value=self.response(html)
        ) as get:
            photos, state = gallery.fetch_gallery(
                "https://www.vinted.co.uk/items/123-coat"
            )
        self.assertEqual(state, "ready")
        self.assertEqual(len(photos), 4)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertEqual(get.call_args.kwargs["timeout"], (2, 4))

    def test_challenge_and_rate_limit_back_off_without_looping(self):
        for status, body, expected in (
            (200, "<title>Client Challenge</title>", "challenge"),
            (429, "", "access_limited"),
        ):
            with patch.object(
                gallery.requests, "get", return_value=self.response(body, status)
            ) as get, patch.object(gallery, "_pause") as pause:
                self.assertEqual(
                    gallery.fetch_gallery("https://www.vinted.co.uk/items/123")[1],
                    expected,
                )
                get.assert_called_once()
                pause.assert_called_once_with(300)


class ResolveTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
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
