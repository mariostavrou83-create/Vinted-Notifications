import base64
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image
import trial_photo_price as price


URL = "https://images1.vinted.net/t/04_example/f800/image.webp?s=public"


class TrialPhotoPriceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.database = Path(root.name) / "finder.db"
        self.target = {"id": "343", "condition": "Good", "ask_pence": 43113, "seller_id": "private-seller"}
        self.counters = SimpleNamespace(path=Path(root.name) / "allowance.db")
        with sqlite3.connect(self.database) as conn:
            conn.executescript("""
                CREATE TABLE alert_outbox(item_id TEXT, title TEXT, platform TEXT, status TEXT, currency TEXT, photo_url TEXT);
                CREATE TABLE vinted_alert_details(item_id TEXT, payload TEXT);
                INSERT INTO alert_outbox VALUES('343','Hollister fitted jacket @someprivatehandle 01234 567890','vinted','sent','GBP',NULL);
            """)
            conn.execute("INSERT INTO vinted_alert_details VALUES('343',?)", (json.dumps({
                "photos": [URL], "brand": "Hollister", "description": "Faux leather, zip front. Email never-share@example.com"
            }),))
        self._patch("os.getenv", return_value="test-key")
        self.available = self._patch("budget.available", return_value=True)
        self.reserve = self._patch("budget.reserve", return_value=4)
        self.usage = self._patch("budget.record_usage")
        buf = io.BytesIO()
        exif = Image.Exif()
        exif[315] = "Image author must be removed"
        Image.new("RGB", (800, 300), "red").save(buf, "WEBP", exif=exif)
        self.download = self._patch("download_photo", return_value=buf.getvalue())
        self.payload = {"low_pence": 1400, "quick_pence": 1600, "high_pence": 1800,
                        "explanation": "Fitted shape with a full front zip.", "visible_traits": ["fitted", "zip front"]}

    def _patch(self, name, **kwargs):
        obj = patch("trial_photo_price." + name, **kwargs)
        self.addCleanup(obj.stop)
        return obj.start()

    def client(self, data=None):
        return SimpleNamespace(post=AsyncMock(return_value=httpx.Response(200, request=httpx.Request("POST","https://api.mistral.ai"),
            json={"choices": [{"finish_reason":"stop", "message":{"content": json.dumps(data or self.payload)}}],
                  "usage":{"prompt_tokens":130,"completion_tokens":40,"total_tokens":170}})))

    async def test_verified_image_payload_and_paired_usage(self):
        client = self.client()
        r = await price.rough_estimate(self.target, self.database, client, self.counters)
        self.assertEqual((r["photo_status"], r["quick_pence"], r["model_calls"]), ("estimated", 1600, 1))
        self.assertEqual(r["visible_traits"], ["fitted", "zip front"])
        self.assertIsNone(r["photo_family"])  # an earlier response without the new field
        self.assertGreaterEqual(r["provider_ms"], 0)
        call = client.post.await_args.kwargs
        prompt = json.dumps(call["json"])
        for blocked in ("never-share", "private-seller", "43113", "images1.vinted.net", "someprivatehandle", "01234"):
            self.assertNotIn(blocked, prompt)
        self.assertFalse(call["follow_redirects"])
        self.assertTrue(price.budget._bounded(call["json"]))
        self.assertEqual(call["json"]["model"], "mistral-small-2603")
        image = call["json"]["messages"][1]["content"][1]["image_url"]
        with Image.open(io.BytesIO(base64.b64decode(image.split(",")[1]))) as decoded:
            self.assertLessEqual(max(decoded.size), 512)
            self.assertEqual(decoded.format, "JPEG")
            self.assertFalse(decoded.getexif())
        self.usage.assert_called_once_with(self.counters.path, 4, {"prompt_tokens":130,"completion_tokens":40,"total_tokens":170})
        self.assertEqual(self.reserve.call_args.args, (self.counters.path,call["json"]))

    async def test_exhausted_and_unsafe_or_corrupt_photos_do_not_spend(self):
        client = self.client()
        self.available.return_value = False
        r = await price.rough_estimate(self.target,self.database,client,self.counters)
        self.assertEqual(r["photo_status"], "hour_limit")
        self.download.assert_not_called()
        self.available.return_value = True
        self.download.return_value = b"a non-image"
        r = await price.rough_estimate(self.target,self.database,client,self.counters)
        self.assertEqual(r["photo_status"], "photo_unavailable")
        self.reserve.assert_not_called()
        for url in ("https://images1.vinted.net.evil/t/1/test.webp", "https://i.ebayimg.com/t/1/test.jpg",
                    "http://images1.vinted.net/t/1/test.jpg", "https://images1.vinted.net/private?secret=1",
                    "https://foo@images1.vinted.net/t/1/img.jpg","https://images1.vinted.net/t/1/../../x"):
            self.assertIsNone(price._cover_url(url))

    async def test_invalid_response_still_counts_usage(self):
        client = self.client(dict(self.payload, low_pence=2500))
        result = await price.rough_estimate(self.target,self.database,client,self.counters)
        self.assertEqual(result["photo_status"], "provider_unavailable")
        self.assertEqual(result["model_calls"], 1)
        self.assertIsNone(result["quick_pence"])
        self.usage.assert_called_once()
        client = self.client(dict(self.payload, low_pence=None, high_pence=None, quick_pence=None, visible_traits=[]))
        r = await price.rough_estimate(self.target,self.database,client,self.counters)
        self.assertEqual(r["photo_status"], "unidentifiable")

    async def test_photo_family_is_independent_of_title_and_survives_abstention(self):
        # The title says jacket. A contradictory visual observation must reach the mismatch gate.
        client = self.client(dict(self.payload, photo_family="skirt", low_pence=None,
                                  high_pence=None, quick_pence=None))
        result = await price.rough_estimate(self.target,self.database,client,self.counters)
        self.assertEqual((result["photo_family"], result["photo_status"], result["quick_pence"]),
                         ("skirt", "unidentifiable", None))
        for family in ("jacket", None):
            result = await price.rough_estimate(self.target,self.database,self.client(
                dict(self.payload, photo_family=family)),self.counters)
            self.assertEqual(result["photo_family"], family)
        unknown = await price.rough_estimate(self.target,self.database,self.client(
            dict(self.payload, photo_family="mystery")),self.counters)
        self.assertEqual((unknown["photo_status"], unknown["photo_family"]), ("provider_unavailable", None))

    async def test_original_outbox_cover_without_details(self):
        with sqlite3.connect(self.database) as conn:
            conn.execute("DELETE FROM vinted_alert_details")
            conn.execute("UPDATE alert_outbox SET photo_url=?", (URL,))
        result = await price.rough_estimate(self.target,self.database,self.client(),self.counters)
        self.assertEqual(result["photo_status"], "estimated")
        self.download.assert_called_once_with(URL,max_bytes=3_000_000,max_seconds=3)

    async def test_atomic_reservation_stops_races(self):
        self.reserve.return_value = None
        client = self.client()
        r = await price.rough_estimate(self.target,self.database,client,self.counters)
        self.assertEqual((r["photo_status"], r["model_calls"]), ("hour_limit", 0))
        client.post.assert_not_awaited()
