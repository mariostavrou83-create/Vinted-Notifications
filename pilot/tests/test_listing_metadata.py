import unittest
from pilot.listing_metadata import display_fields, vinted_photo_url


class ListingMetadataTest(unittest.TestCase):
    def test_remote_images_are_public_item_cdn_only(self):
        valid = "https://images1.vinted.net/t/01_0123/f800/image.webp?s=123"
        self.assertEqual(vinted_photo_url(valid), valid)
        for invalid in (
            "https://images1.vinted.net.evil.example/t/1/image.webp",
            "http://images1.vinted.net/t/1/a.jpg",
            "https://user@images1.vinted.net/t/1/a.jpg",
            "https://images1.vinted.net:9443/t/image.webp",
            "https://images1.vinted.net/t/1/../../evil.svg",
            "data:image/png;base64,x",
        ):
            self.assertIsNone(vinted_photo_url(invalid))

    def test_landing_time_vs_visible_age_bound(self):
        row = {
            "age_text": "just now",
            "listed_at": 900,
            "condition": "Good",
            "photo_url": "https://evil.example/profile",
        }
        view = display_fields(row, "browser_home", 1000)
        self.assertEqual(
            (view["discovered_at"], view["listed_at"], view["listed_age_text"]), (1000, None, "just now")
        )
        self.assertIsNone(view["photo_url"])
        row.update(age_text="1970-01-01T00:15:00Z")
        self.assertEqual(display_fields(row, "browser_search", 1000)["listed_at"], 900)
        self.assertIsNone(display_fields({"listed_at": 1100}, "connected", 1000)["listed_at"])
