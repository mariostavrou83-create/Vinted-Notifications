import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pilot.app import create_app
from pilot.db import get_db


class SourcingDashboardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        app = create_app({"TESTING": True, "DATABASE": str(Path(temp.name) / "finder.sqlite")})
        with app.app_context():
            get_db().execute("INSERT INTO users(id,username,password_hash) VALUES(1,'buyer','test')")
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=1, csrf_token="test")

    def test_new_buyer_can_find_chrome_and_empty_source_filters(self):
        page = self.client.get("/").get_data(as_text=True)
        for value in ('Sourcing board', 'data-source="all"', 'data-source="home"', 'data-source="search"',
                      'href="/extension/install"', 'href="#new-search"', 'data-all-empty'):
            self.assertIn(value, page)
        self.assertNotIn('class="feed-card"', page)

    def test_refresh_returns_real_photo_metadata_and_optional_prices(self):
        alert = {
            "id": 1, "source": "search", "origin": "public_search", "checkout_mode": "vinted",
            "title": "Linen top", "listing_url": "https://www.vinted.co.uk/items/100-linen-top",
            "photo_url": "https://images1.vinted.net/t/01/photo.jpeg", "search_name": "Summer linen",
            "size": "M", "condition": "", "brand": "", "created_at": 1000, "listed_at": None,
            "item_price_pence": 500, "total_pence": None, "valuation": None,
        }
        with patch("pilot.flow.alerts_for_user", return_value=[alert]):
            html = self.client.get("/alerts/fragment").get_data(as_text=True)
        self.assertIn('src="https://images1.vinted.net/t/01/photo.jpeg"', html)
        self.assertIn('data-listing-source="search"', html)
        self.assertIn('data-time-kind="Found"', html)
        self.assertIn('Summer linen', html)
        self.assertIn('£5.00', html)
        self.assertIn('Resale estimate pending.', html)
        self.assertNotIn('Meets £15 target', html)
        self.assertNotIn('AutoBuy in your Telegram alert', html)
