"""Real signed callbacks, isolated SQLite and fake eBay/Telegram transport."""

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from test_ebay_monitor import EbayFixture, item

import alert_delivery
import db
import ebay_privacy as privacy
import search_settings
from web_ui_plugin.web_ui import create_app


class PrivacyTests(EbayFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.env = patch.dict(os.environ, {"DASHBOARD_URL": "https://example.test"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()
        self.key = ec.generate_private_key(ec.SECP256R1())

    def payload(self, name="seller-one", event="event-1"):
        return {
            "metadata": {"topic": privacy.TOPIC},
            "notification": {
                "notificationId": event,
                "data": {"username": name, "userId": "test-immutable"},
            },
        }

    def post(self, payload, tamper=False):
        body = json.dumps(payload, separators=(",", ":")).encode()
        sig = self.key.sign(body, ec.ECDSA(hashes.SHA256()))
        header = base64.b64encode(
            json.dumps(
                {
                    "kid": "test-key",
                    "alg": "ecdsa",
                    "digest": "SHA256",
                    "signature": base64.b64encode(sig).decode(),
                }
            ).encode()
        ).decode()
        with patch.object(
            privacy, "public_key", return_value=(self.key.public_key(), "SHA256")
        ):
            return self.client.post(
                privacy.PATH,
                data=body + (b" " if tamper else b""),
                content_type="application/json",
                headers={"X-EBAY-SIGNATURE": header},
            )

    def seed(self):
        search = self.enable()
        self.snapshot(search, [], 1000)
        self.snapshot(
            search,
            [
                item(123, seller={"username": "seller-one"}),
                item(456, seller={"username": "seller-two"}),
            ],
            1020,
        )
        self.batch(2, [789])
        return search

    def test_challenge_public_dashboard_private_and_token_stable(self):
        token = privacy.setup_values()["token"]
        response = self.client.get(
            privacy.PATH + "?challenge_code=abc", headers={"Host": "attacker.invalid"}
        )
        expected = hashlib.sha256(
            ("abc" + token + "https://example.test" + privacy.PATH).encode()
        ).hexdigest()
        self.assertEqual(response.json, {"challengeResponse": expected})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/connections").status_code, 302)
        self.assertEqual(self.client.post("/connections", data={}).status_code, 400)
        self.assertEqual(self.client.get(privacy.PATH).status_code, 412)
        search_settings.ensure_schema()
        self.assertEqual(privacy.setup_values()["token"], token)

    def test_signed_removal_is_targeted_idempotent_and_prevents_reinsertion(self):
        search = self.seed()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE alert_outbox SET status='sent',telegram_message_id=100 WHERE item_id='ebay:123'"
            )
        # An actual backup containing seller data must be scrubbed as well.
        backup = (
            Path(db.DB_PATH).parent
            / "backups"
            / "before-search-controls-privacy.sqlite3"
        )
        with closing(search_settings.connection()) as src, closing(
            sqlite3.connect(backup)
        ) as dst:
            src.backup(dst)
        self.assertEqual(self.post(self.payload()).status_code, 204)
        self.assertEqual(self.post(self.payload()).status_code, 204)
        self.assertEqual({r["item_id"] for r in self.outbox()}, {"ebay:456", "789"})
        with closing(sqlite3.connect(backup)) as conn:
            self.assertFalse(
                conn.execute(
                    "SELECT 1 FROM alert_outbox WHERE item_id='ebay:123'"
                ).fetchone()
            )
            self.assertTrue(
                conn.execute(
                    "SELECT 1 FROM alert_outbox WHERE item_id='ebay:456'"
                ).fetchone()
            )
        self.snapshot(
            search, [item(124, created=1030, seller={"username": "SELLER-ONE"})], 1040
        )
        self.assertNotIn("ebay:124", {r["item_id"] for r in self.outbox()})
        self.assertEqual(privacy.setup_values()["received"], 1)
        self.assertEqual(privacy.setup_values()["pending"], 1)
        bot = AsyncMock()
        self.assertTrue(asyncio.run(privacy.redact_one(bot, "123")))
        self.assertIsNone(bot.edit_message_text.call_args.kwargs["reply_markup"])
        self.assertEqual(privacy.setup_values()["pending"], 0)

    def test_unsigned_forged_malformed_and_wrong_topic_cannot_delete(self):
        self.seed()
        for headers, data in [
            ({}, json.dumps(self.payload())),
            ({"X-EBAY-SIGNATURE": "garbage"}, "{}"),
            ({"X-EBAY-SIGNATURE": base64.b64encode(b"[]").decode()}, "{}"),
        ]:
            self.assertEqual(
                self.client.post(
                    privacy.PATH,
                    data=data,
                    content_type="application/json",
                    headers=headers,
                ).status_code,
                412,
            )
        body = json.dumps(self.payload()).encode()
        signature = ec.generate_private_key(ec.SECP256R1()).sign(
            body, ec.ECDSA(hashes.SHA256())
        )
        header = base64.b64encode(
            json.dumps(
                {"kid": "test-key", "signature": base64.b64encode(signature).decode()}
            ).encode()
        ).decode()
        with patch.object(
            privacy, "public_key", return_value=(self.key.public_key(), "SHA256")
        ):
            self.assertEqual(
                self.client.post(
                    privacy.PATH,
                    data=body,
                    content_type="application/json",
                    headers={"X-EBAY-SIGNATURE": header},
                ).status_code,
                412,
            )
        wrong = self.payload()
        wrong["metadata"]["topic"] = "OTHER"
        self.assertEqual(self.post(wrong).status_code, 412)
        self.assertEqual(len(self.outbox()), 3)
        self.assertEqual(
            self.client.post(
                privacy.PATH, data="x" * 16385, content_type="application/json"
            ).status_code,
            413,
        )

    def test_inflight_accepted_message_is_queued_for_redaction(self):
        self.seed()
        row = alert_delivery.claim(1030, platform="ebay")
        self.assertEqual(row["item_id"], "ebay:123")
        self.assertEqual(self.post(self.payload()).status_code, 204)
        alert_delivery.finish(row, message_id=987, now=1031)
        self.assertEqual(privacy.setup_values()["pending"], 1)

    def test_key_outage_does_not_acknowledge_or_remove_data(self):
        self.seed()
        header = base64.b64encode(
            json.dumps({"kid": "real-key", "signature": "AAAA"}).encode()
        ).decode()
        with patch.object(
            privacy, "public_key", side_effect=privacy.VerificationUnavailable
        ):
            r = self.client.post(
                privacy.PATH,
                data=json.dumps(self.payload()),
                content_type="application/json",
                headers={"X-EBAY-SIGNATURE": header},
            )
        self.assertEqual(r.status_code, 503)
        self.assertEqual(len(self.outbox()), 3)

    def test_official_ebay_sdk_signatures_and_trusted_key_digest(self):
        fixtures = json.loads(
            (Path(__file__).parent / "ebay_notification_fixture.json").read_text()
        )
        valid = fixtures["valid"]
        pem = (
            valid["response"]["key"]
            .replace("-----BEGIN PUBLIC KEY-----", "-----BEGIN PUBLIC KEY-----\n")
            .replace("-----END PUBLIC KEY-----", "\n-----END PUBLIC KEY-----")
        )
        key = serialization.load_pem_public_key(pem.encode())
        body = json.dumps(valid["message"], separators=(",", ":")).encode()
        with patch.object(privacy, "public_key", return_value=(key, "SHA1")):
            self.assertEqual(privacy.verify(body, valid["signature"]), valid["message"])
            with self.assertRaisesRegex(ValueError, "signature_mismatch"):
                privacy.verify(body, fixtures["invalid"]["signature"])
        with patch.object(
            privacy, "public_key", return_value=(key, "SHA256")
        ), self.assertRaisesRegex(ValueError, "digest_mismatch"):
            privacy.verify(body, valid["signature"])
        client = Mock()
        client.token = "test-token"
        client.session.get.return_value.status_code = 200
        client.session.get.return_value.json.return_value = valid["response"]
        with patch.dict(privacy._keys, {}, clear=True), patch.object(
            privacy, "_next_key_fetch", 0
        ), patch("ebay_monitor.BrowseClient", return_value=client):
            first = privacy.public_key(valid["public_key"])
            self.assertEqual(first[1], "SHA1")
            self.assertEqual(privacy.public_key(valid["public_key"]), first)
            self.assertEqual(client.session.get.call_count, 1)
