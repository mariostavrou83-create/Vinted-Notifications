import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pilot import public_feed
from pilot.app import create_app
from pilot.db import get_db


class SourcingDashboardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        app = create_app({"TESTING": True, "DATABASE": str(Path(temp.name) / "finder.sqlite")})
        with app.app_context():
            get_db().execute("INSERT INTO users(id,username,password_hash) VALUES(1,'buyer','test')")
        self.app = app
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=1, csrf_token="test")

    def test_new_buyer_can_find_chrome_and_empty_source_filters(self):
        page = self.client.get("/").get_data(as_text=True)
        for value in ('Sourcing board', 'data-source="all"', 'data-source="home"', 'data-source="search"',
                      'href="/extension/install"', 'href="#new-search"', 'data-all-empty'):
            self.assertIn(value, page)
        self.assertNotIn('class="feed-card"', page)

    def test_polled_search_health_is_private_and_keeps_only_successful_check_time(self):
        self.app.config.update(PILOT_SCAVIO_API_KEY="test-key", PILOT_PUBLIC_SEARCH_ENABLED="1",
                               PILOT_PUBLIC_SEARCH_MAX_REQUESTS=2, PILOT_PUBLIC_SEARCH_DAILY_LIMIT=2)
        url = "https://www.vinted.co.uk/catalog?search_text=linen"
        now = int(time.time())
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO users VALUES(2,'second-buyer','test')")
            for owner in (1, 2):
                db.execute("INSERT INTO searches VALUES(?,?,?,?,'[]','[]',NULL,1)", (owner, owner, "Linen", url))
                db.execute("INSERT INTO settings VALUES(?,?)", (owner, '{"quiet_start":"","quiet_end":""}'))
                db.execute("INSERT INTO public_search_checkpoint VALUES(?,?,?,?)", (owner, 'test', now-owner, 1))
            digest = public_feed.digest_for(public_feed.parameters(url))
            db.execute("INSERT INTO public_query_health VALUES(?,?,?)", (digest, now//5, 'checked'))
            response = self.client.get("/api/native/search-status")
            self.assertIn("no-store", response.headers["Cache-Control"])
            self.assertEqual(set(response.json["public_searches"]), {"1"})
            self.assertEqual(response.json["public_searches"]["1"]["state"], 'checked')
            db.execute("UPDATE public_query_health SET state='failed'")
            failed = self.client.get("/api/native/search-status").json["public_searches"]["1"]
            self.assertEqual((failed["state"], failed["last_checked_at"]), ('failed', now-1))
            db.execute("UPDATE public_budget SET used=2")
            limited = self.client.get("/api/native/search-status").json["public_searches"]["1"]
            self.assertEqual((limited["state"], limited["last_checked_at"]), ('budget', now-1))
            self.assertEqual(set(limited), {"message","state","last_checked_at","target_seconds"})
            self.assertEqual(self.app.test_client().get("/api/native/search-status").status_code, 401)

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
        self.assertNotIn('resale', html.lower())
        self.assertNotIn('Meets £15 target', html)
        self.assertNotIn('AutoBuy in your Telegram alert', html)
