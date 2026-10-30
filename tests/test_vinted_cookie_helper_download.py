"""Authenticated helper ZIP includes reviewed static sources and no account data."""

import io
import json
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from test_search_controls import DatabaseFixture
from werkzeug.security import generate_password_hash

import search_settings
import vinted_buyer as buyer
from web_ui_plugin.web_ui import create_app

URL = "/connections/vinted-cookie-helper.zip"
NAMES = {"manifest.json", "popup.html", "popup.js", "cookie_records.mjs", "README.md"}
PRIVATE = "RUNTIME-COOKIE-NEVER-IN-HELPER-0123456789"


class CookieHelperDownloadTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app({"TESTING": True, "SESSION_COOKIE_SECURE": False})
        self.client = self.app.test_client()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute("UPDATE dashboard_auth SET password_hash=?", (generate_password_hash("offline owner password"),))
            conn.execute(
                "UPDATE vinted_buyer SET session=?,user_id='99',verified_at=1 WHERE id=1",
                (buyer.encrypt({"cookies": {"access_token_web": PRIVATE}}),),
            )

    def owner(self):
        with self.client.session_transaction() as session:
            session["owner"] = True
            session["csrf"] = "offline-helper-csrf"

    def test_download_requires_owner_authentication(self):
        response = self.client.get(URL)
        self.assertEqual(response.location, "/login")
        self.assertNotEqual(response.mimetype, "application/zip")

    def test_known_sources_only_no_runtime_data_or_marketplace_calls(self):
        self.owner()
        with patch.object(buyer, "Client", side_effect=AssertionError("No account requests")):
            response = self.client.get(URL + "?file=../../data/database.db")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/zip")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(set(archive.namelist()), {"msj-vinted-cookie-helper/" + name for name in NAMES})
            for name in archive.namelist():
                self.assertNotIn(PRIVATE.encode(), archive.read(name))
                self.assertNotIn("..", name)
            manifest = json.loads(archive.read("msj-vinted-cookie-helper/manifest.json"))
            self.assertEqual(manifest["host_permissions"], ["https://vinted.co.uk/*", "https://www.vinted.co.uk/*"])
            self.assertEqual(manifest["permissions"], ["cookies", "clipboardWrite"])

    def test_missing_source_fails_closed(self):
        self.owner()
        original = Path.is_file

        def exists(path):
            return False if path.name == "popup.js" else original(path)

        with patch.object(Path, "is_file", exists):
            response = self.client.get(URL)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(PRIVATE.encode(), response.data)

    def test_symlink_source_fails_closed(self):
        self.owner()
        original = Path.is_symlink

        def linked(path):
            return path.name == "popup.js" or original(path)

        with patch.object(Path, "is_symlink", linked):
            response = self.client.get(URL)
        self.assertEqual(response.status_code, 503)
