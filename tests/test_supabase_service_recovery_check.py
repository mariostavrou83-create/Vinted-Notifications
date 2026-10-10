"""Authorized in-service reads reuse owner verification and existing keys."""

import gzip
import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from test_search_controls import DatabaseFixture
from test_supabase_backend import OWNER, SETTINGS

import db
import search_settings
import supabase_backend as backend
import supabase_service_recovery_check as check
from supabase_recovery_check import RecoveryError


class ServiceRecoveryTests(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.directory = Path(db.DB_PATH).parent
        for filename in (
            "supabase-backup.key",
            "vinted-buyer.key",
            "supabase-session.key",
        ):
            path = self.directory / filename
            if not path.exists():
                path.write_bytes(Fernet.generate_key())
                path.chmod(0o600)
        self.keys = {
            path.name: path.read_bytes() for path in self.directory.glob("*.key")
        }
        self.digest = hashlib.sha256(b"offline-private-reference").hexdigest()
        cipher = Fernet(self.keys["supabase-session.key"])
        self.tokens = {
            "access_token": "private-access-never-export",
            "refresh_token": "private-refresh-never-export",
            "expires_at": time.time() + 3600,
        }
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "CREATE TABLE supabase_dashboard_sessions(token_hash TEXT PRIMARY KEY,owner_id TEXT,encrypted_tokens BLOB,expires_at REAL)"
            )
            conn.execute(
                "INSERT INTO supabase_dashboard_sessions VALUES (?,?,?,?)",
                (
                    self.digest,
                    OWNER,
                    cipher.encrypt(json.dumps(self.tokens).encode()),
                    time.time() + 7200,
                ),
            )
        self.client = Mock(config=backend.Config.from_environment(SETTINGS))
        self.client.verify_owner.return_value = OWNER
        self.client.download_backup.return_value = (
            b"offline-encrypted-backup",
            "2026-10-08T18:10:48+00:00",
        )

    def execute(self, *, failure=None):
        counts = {
            "searches": 134,
            "saved_searches": 44,
            "archived_searches": 90,
            "expected_searches": 44,
        }
        with patch.dict("os.environ", SETTINGS), patch.object(
            check, "Client", return_value=self.client
        ), patch.object(backend, "_private_cipher") as create, patch.object(
            check,
            "verify_snapshot",
            side_effect=failure
            or RecoveryError(
                "expected_searches", observed={"integrity": "ok", "counts": counts}
            ),
        ) as verify, self.assertLogs(
            check.logger, level="INFO"
        ) as logs:
            result = check.check_recovery()
        create.assert_not_called()
        for token in self.tokens.values():
            if isinstance(token, str):
                self.assertNotIn(token, json.dumps(result) + str(logs.output))
        self.assertEqual(
            {path.name: path.read_bytes() for path in self.directory.glob("*.key")},
            self.keys,
        )
        return result, verify

    def test_counts_exposed_through_fresh_owner_verification_with_live_bound_criterion(
        self,
    ):
        result, verify = self.execute()
        self.assertEqual(result["stage"], "expected_searches")
        self.assertEqual(result["outcome"], "unverified")
        self.assertEqual(result["counts"]["searches"], 134)
        self.client.verify_owner.assert_called_once_with(self.tokens["access_token"])
        self.client.download_backup.assert_called_once_with(
            self.tokens["access_token"], with_metadata=True
        )
        self.assertIsNone(verify.call_args.kwargs["expected_searches"])
        self.assertEqual(result["backup_timestamp"], "2026-10-08T18:10:48+00:00")

    def test_missing_backup_key_is_reported_and_never_recreated(self):
        path = self.directory / "supabase-backup.key"
        path.unlink()
        del self.keys["supabase-backup.key"]
        result, verify = self.execute()
        self.assertEqual(result["stage"], "backup_key_missing")
        self.assertFalse(path.exists())
        self.client.verify_owner.assert_not_called()
        self.client.download_backup.assert_not_called()
        verify.assert_not_called()

    def test_authorized_service_verifies_real_250_search_snapshot_and_rejects_staleness(
        self,
    ):
        buyer = Fernet(self.keys["vinted-buyer.key"]).encrypt(
            json.dumps({"cookies": "fictional-session"}).encode()
        )
        with closing(search_settings.connection()) as conn, conn:
            conn.executemany(
                "INSERT INTO queries(id,query,last_item,query_name) VALUES (?,?,?,?)",
                [
                    (i, f"https://example.test/?q={i}", 100, f"Fictional search {i}")
                    for i in range(45, 251)
                ],
            )
            conn.execute("UPDATE vinted_buyer SET session=? WHERE id=1", (buyer,))
            conn.execute(
                "INSERT INTO dashboard_media(id,image,created) VALUES ('fictional-photo',x'01',0)"
            )
            conn.execute(
                "INSERT INTO search_reference_photos VALUES (1,0,'fictional-photo')"
            )
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / "snapshot.sqlite"
            with closing(search_settings.connection()) as source, closing(
                sqlite3.connect(snapshot)
            ) as destination:
                source.backup(destination)
                with destination:
                    destination.execute("DELETE FROM supabase_dashboard_sessions")
            encrypted = Fernet(self.keys["supabase-backup.key"]).encrypt(
                backend.SNAPSHOT_MAGIC + gzip.compress(snapshot.read_bytes())
            )
        self.client.download_backup.return_value = (
            encrypted,
            "2026-10-10T08:00:00+00:00",
        )
        with patch.dict("os.environ", SETTINGS), patch.object(
            check, "Client", return_value=self.client
        ), patch.object(backend, "_private_cipher") as create, self.assertLogs(
            check.logger, level="INFO"
        ) as logs:
            result = check.check_recovery()
            self.assertEqual(result["outcome"], "verified")
            self.assertEqual(result["counts"]["expected_searches"], 250)
            self.assertTrue(result["search_definitions_match_live"])
            with closing(search_settings.connection()) as conn, conn:
                conn.execute(
                    "UPDATE parameters SET value='Changed setting' WHERE key='query_refresh_delay'"
                )
            stale = check.check_recovery()
        create.assert_not_called()
        self.assertEqual(stale["outcome"], "unverified")
        self.assertEqual(stale["stage"], "remaining_checks")
        self.assertFalse(stale["settings_match_live"])
        self.assertNotIn("fictional-session", str(logs.output))
        self.assertNotIn("Changed setting", str(logs.output))
        self.assertEqual(
            {path.name: path.read_bytes() for path in self.directory.glob("*.key")},
            self.keys,
        )

    def test_rejected_owner_or_other_stored_owner_cannot_download(self):
        self.client.verify_owner.side_effect = backend.SupabaseError(
            "Fixed owner rejection"
        )
        result, verify = self.execute()
        self.assertEqual(result["stage"], "owner_session")
        self.client.download_backup.assert_not_called()
        verify.assert_not_called()
        with closing(search_settings.connection()) as conn, conn:
            conn.execute(
                "UPDATE supabase_dashboard_sessions SET owner_id='another-owner'"
            )
        self.client.verify_owner.reset_mock()
        self.execute()
        self.client.verify_owner.assert_not_called()
