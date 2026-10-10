"""Real SQLite/Fernet recovery and owner/CSRF endpoint regression checks."""

import gzip
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from flask import Flask, redirect, url_for

import supabase_backend as backend
import supabase_recovery_check as recovery

OWNER = "cc14b72d-50bb-48d8-bf9a-49893a2cda7f"
SETTINGS = {
    "SUPABASE_URL": "https://test-project.supabase.co",
    "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_offline_test_key",
    "SUPABASE_OWNER_ID": OWNER,
}
STAMP = "2026-10-08T18:10:48.814158+00:00"


class Fixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.database = self.directory / "live.sqlite"
        self.keys = {}
        for name in ("backup", "buyer", "session"):
            filename = (
                "vinted-buyer.key" if name == "buyer" else "supabase-" + name + ".key"
            )
            key = Fernet.generate_key()
            path = self.directory / filename
            path.write_bytes(key)
            path.chmod(0o600)
            self.keys[name] = Fernet(key)
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("CREATE TABLE parameters(key TEXT PRIMARY KEY,value TEXT)")
            conn.execute(
                "INSERT INTO parameters VALUES ('preserve','fictional-setting-secret')"
            )
            for table in recovery.COUNT_TABLES.values():
                if table not in (
                    "search_reference_photos",
                    "supabase_dashboard_sessions",
                ):
                    conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)')
                    conn.execute(f'INSERT INTO "{table}" VALUES (1)')
            conn.executemany(
                "INSERT INTO queries VALUES (?)", [(i,) for i in range(2, 45)]
            )
            conn.execute("CREATE TABLE dashboard_media(id TEXT PRIMARY KEY,image BLOB)")
            conn.execute("INSERT INTO dashboard_media VALUES ('photo',x'010203')")
            conn.execute(
                "CREATE TABLE search_reference_photos(query_id INTEGER,position INTEGER,media_id TEXT)"
            )
            conn.execute("INSERT INTO search_reference_photos VALUES (1,0,'photo')")
            conn.execute(
                "CREATE TABLE supabase_dashboard_sessions(token_hash TEXT PRIMARY KEY,owner_id TEXT,encrypted_tokens BLOB,expires_at REAL)"
            )
            conn.execute(
                "CREATE TABLE vinted_buyer(id INTEGER PRIMARY KEY,session BLOB,pending BLOB,network BLOB,enabled INTEGER)"
            )
            encrypted = self.keys["buyer"].encrypt(
                json.dumps({"cookies": "fictional-buyer-secret"}).encode()
            )
            conn.execute(
                "INSERT INTO vinted_buyer VALUES (1,?,NULL,NULL,0)", (encrypted,)
            )
            conn.execute(
                "CREATE TABLE supabase_login_guard(id INTEGER PRIMARY KEY,attempts INTEGER,window_start REAL)"
            )
            conn.execute("INSERT INTO supabase_login_guard VALUES (1,0,0)")

    def tearDown(self):
        self.temp.cleanup()

    def encrypted(self, raw=None, prefix=backend.SNAPSHOT_MAGIC):
        if raw is None:
            raw = self.database.read_bytes()
        return self.keys["backup"].encrypt(prefix + gzip.compress(raw))

    def search_definitions(self, saved=250):
        """Fictional stable configuration and runtime state for scale checks."""
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.executemany(
                "INSERT INTO queries VALUES (?)", [(i,) for i in range(45, saved + 1)]
            )
            conn.execute("ALTER TABLE queries ADD COLUMN query TEXT")
            conn.execute("ALTER TABLE queries ADD COLUMN query_name TEXT")
            conn.execute("ALTER TABLE queries ADD COLUMN last_item REAL")
            conn.execute(
                "UPDATE queries SET query='https://example.test/?q='||id,"
                "query_name='Fictional search '||id,last_item=100"
            )
            conn.execute(
                "CREATE TABLE search_dashboard(query_id INTEGER PRIMARY KEY,"
                "archived INTEGER,paused INTEGER,rebaseline INTEGER,revision INTEGER)"
            )
            conn.execute("INSERT INTO search_dashboard VALUES (1,0,0,0,1)")
            conn.execute(
                "CREATE TABLE search_preferences(query_id INTEGER PRIMARY KEY,"
                "reminder TEXT,exclusions TEXT)"
            )
            conn.execute(
                "INSERT INTO search_preferences VALUES (1,'fictional-note','[]')"
            )
            conn.execute(
                "CREATE TABLE search_buying_guide(query_id INTEGER PRIMARY KEY,"
                "max_buy INTEGER,must_have TEXT,folder_id INTEGER)"
            )
            conn.execute("INSERT INTO search_buying_guide VALUES (1,900,'fictional',1)")
            conn.execute(
                "CREATE TABLE search_folders(id INTEGER PRIMARY KEY,name TEXT)"
            )
            conn.execute("INSERT INTO search_folders VALUES (1,'Fictional folder')")
            conn.execute(
                "CREATE TABLE search_platforms(query_id INTEGER PRIMARY KEY,"
                "vinted_enabled INTEGER,ebay_enabled INTEGER,ebay_config TEXT,"
                "ebay_generation INTEGER)"
            )
            conn.execute("INSERT INTO search_platforms VALUES (1,1,0,'{}',2)")
            conn.execute(
                "CREATE TABLE vinted_keyword_variants(id INTEGER PRIMARY KEY,"
                "query_id INTEGER,position INTEGER,keyword TEXT,url TEXT,"
                "primed INTEGER,last_item REAL,max_item_id INTEGER,last_check REAL,"
                "last_attempt REAL,last_success REAL,actual_interval REAL,"
                "failures INTEGER,error TEXT)"
            )
            conn.execute(
                "INSERT INTO vinted_keyword_variants VALUES "
                "(1,1,0,'fictional','https://example.test/variant',1,100,10,100,100,100,1,0,'')"
            )


class SnapshotTests(Fixture, unittest.TestCase):
    def verify_live(self, encrypted=None):
        return recovery.verify_snapshot(
            self.encrypted() if encrypted is None else encrypted,
            self.directory,
            live_database=self.database,
            expected_searches=None,
        )

    def test_live_bound_250_searches_verify_without_changing_live_data_or_keys(self):
        self.search_definitions()
        before = self.database.read_bytes()
        keys = {p.name: p.read_bytes() for p in self.directory.glob("*.key")}
        result = self.verify_live()
        self.assertEqual(result["outcome"], "verified")
        self.assertEqual(result["counts"]["expected_searches"], 250)
        self.assertEqual(result["counts"]["saved_searches"], 250)
        self.assertTrue(result["search_definitions_match_live"])
        self.assertEqual(before, self.database.read_bytes())
        self.assertEqual(
            keys, {p.name: p.read_bytes() for p in self.directory.glob("*.key")}
        )
        self.assertNotIn("fictional-note", json.dumps(result))
        self.assertNotIn("example.test", json.dumps(result))
        # Standalone/default and explicit baseline checks retain the old bound.
        for kwargs in ({}, {"expected_searches": 44}):
            with self.subTest(kwargs=kwargs), self.assertRaises(
                recovery.RecoveryError
            ) as error:
                recovery.verify_snapshot(self.encrypted(), self.directory, **kwargs)
            self.assertEqual(error.exception.stage, "expected_searches")

    def test_live_bound_check_rejects_stale_44_search_backup_at_250(self):
        encrypted = self.encrypted()
        self.search_definitions()
        with self.assertRaises(recovery.RecoveryError) as error:
            self.verify_live(encrypted)
        self.assertEqual(error.exception.stage, "expected_searches")
        self.assertEqual(error.exception.observed["counts"]["saved_searches"], 44)
        self.assertEqual(error.exception.observed["counts"]["expected_searches"], 250)

    def test_same_count_changed_definitions_are_rejected_including_archived_rows(self):
        self.search_definitions()
        changes = (
            "UPDATE queries SET query='https://example.test/changed' WHERE id=1",
            "UPDATE queries SET query_name='Changed name' WHERE id=1",
            "UPDATE search_preferences SET exclusions='[\"fictional exclusion\"]'",
            "UPDATE search_dashboard SET paused=1",
            "UPDATE search_buying_guide SET max_buy=800",
            "UPDATE search_folders SET name='Changed folder'",
            "UPDATE search_platforms SET ebay_config='{\"fictional\":true}'",
            "UPDATE vinted_keyword_variants SET keyword='Changed word'",
            "UPDATE vinted_keyword_variants SET url='https://example.test/changed'",
        )
        for change in changes:
            encrypted = self.encrypted()
            with closing(sqlite3.connect(self.database)) as conn, conn:
                conn.execute(change)
            with self.subTest(change=change), self.assertRaises(
                recovery.RecoveryError
            ) as error:
                self.verify_live(encrypted)
            self.assertEqual(error.exception.stage, "live_search_definitions")
            self.assertFalse(error.exception.observed["search_definitions_match_live"])
            self.assertEqual(
                error.exception.observed["counts"]["expected_searches"], 250
            )
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute(
                "INSERT INTO queries VALUES (251,'archived URL','Archived',100)"
            )
            conn.execute("INSERT INTO search_dashboard VALUES (251,1,0,0,1)")
        encrypted = self.encrypted()
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("UPDATE queries SET query_name='Edited archived' WHERE id=251")
        with self.assertRaises(recovery.RecoveryError) as error:
            self.verify_live(encrypted)
        self.assertEqual(error.exception.stage, "live_search_definitions")
        self.assertEqual(error.exception.observed["counts"]["archived_searches"], 1)

    def test_live_bound_definition_check_ignores_only_routine_polling_state(self):
        self.search_definitions()
        encrypted = self.encrypted()
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("UPDATE queries SET last_item=200")
            conn.execute("UPDATE search_dashboard SET rebaseline=1")
            conn.execute("UPDATE search_platforms SET ebay_generation=3")
            conn.execute(
                "UPDATE vinted_keyword_variants SET primed=0,last_item=200,"
                "max_item_id=20,last_check=200,last_attempt=200,last_success=200,"
                "actual_interval=2,failures=1,error='fictional retry'"
            )
        result = self.verify_live(encrypted)
        self.assertTrue(result["search_definitions_match_live"])

    def test_live_count_and_comparisons_use_one_consistent_read_transaction(self):
        self.search_definitions()
        encrypted = self.encrypted()
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        original = recovery._saved_search_count

        def read_then_edit(live):
            self.assertTrue(live.in_transaction)
            count = original(live)
            with closing(sqlite3.connect(self.database)) as writer, writer:
                writer.execute(
                    "UPDATE queries SET query_name='Concurrent edit' WHERE id=1"
                )
            return count

        with patch.object(recovery, "_saved_search_count", side_effect=read_then_edit):
            result = self.verify_live(encrypted)
        self.assertEqual(result["outcome"], "verified")
        self.assertTrue(result["search_definitions_match_live"])
        # A later verification observes the committed edit and rejects it.
        with self.assertRaises(recovery.RecoveryError) as error:
            self.verify_live(encrypted)
        self.assertEqual(error.exception.stage, "live_search_definitions")

    def test_live_count_fails_closed_for_missing_database_or_schema(self):
        encrypted = self.encrypted()
        missing = self.directory / "missing.sqlite"
        with self.assertRaises(recovery.RecoveryError) as error:
            recovery.verify_snapshot(
                encrypted, self.directory, live_database=missing, expected_searches=None
            )
        self.assertEqual(error.exception.stage, "live_comparison")
        self.assertFalse(missing.exists())
        empty = self.directory / "empty.sqlite"
        with closing(sqlite3.connect(empty)):
            pass
        with self.assertRaises(recovery.RecoveryError) as error:
            recovery.verify_snapshot(
                encrypted, self.directory, live_database=empty, expected_searches=None
            )
        self.assertEqual(error.exception.stage, "live_search_count")
        with self.assertRaises(recovery.RecoveryError) as error:
            recovery.verify_snapshot(encrypted, self.directory, expected_searches=None)
        self.assertEqual(error.exception.stage, "live_database_required")

    def test_live_definition_schema_and_read_errors_fail_closed(self):
        self.search_definitions()
        encrypted = self.encrypted()
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute(
                "ALTER TABLE search_preferences ADD COLUMN future_setting TEXT"
            )
        with self.assertRaises(recovery.RecoveryError) as error:
            self.verify_live(encrypted)
        self.assertEqual(error.exception.stage, "live_search_definitions")
        with patch.object(
            recovery,
            "_search_definitions_match",
            side_effect=sqlite3.OperationalError("fictional SQL"),
        ), self.assertRaises(recovery.RecoveryError) as error:
            self.verify_live(encrypted)
        self.assertEqual(error.exception.stage, "live_search_definitions")
        self.assertNotIn("fictional SQL", str(error.exception))

    def test_roundtrip_counts_settings_buyer_and_live_file_unchanged(self):
        before = self.database.read_bytes()
        keys = {p.name: p.read_bytes() for p in self.directory.glob("*.key")}
        created = []
        original = tempfile.TemporaryDirectory

        def tracked(*args, **kwargs):
            tmp = original(*args, **kwargs)
            created.append(Path(tmp.name))
            return tmp

        with patch.object(recovery.tempfile, "TemporaryDirectory", tracked):
            result = recovery.verify_snapshot(
                self.encrypted(), self.directory, live_database=self.database
            )
        self.assertEqual(result["integrity"], "ok")
        self.assertEqual(result["counts"]["searches"], 44)
        self.assertEqual(result["counts"]["example_photos"], 1)
        self.assertEqual(result["counts"]["web_sessions"], 0)
        self.assertTrue(result["buyer_session_decryptable"])
        self.assertTrue(result["settings_match_live"])
        self.assertTrue(result["buyer_records_match_live"])
        self.assertTrue(result["photo_references_match_live"])
        self.assertNotIn("fictional-buyer-secret", json.dumps(result))
        self.assertNotIn("fictional-setting-secret", json.dumps(result))
        self.assertEqual(before, self.database.read_bytes())
        self.assertEqual(
            keys, {p.name: p.read_bytes() for p in self.directory.glob("*.key")}
        )
        self.assertTrue(created)
        self.assertTrue(all(not p.exists() for p in created))

    def test_missing_backup_key_is_reported_and_not_recreated(self):
        encrypted = self.encrypted()
        path = self.directory / "supabase-backup.key"
        path.unlink()
        with self.assertRaises(recovery.RecoveryError) as error:
            recovery.verify_snapshot(encrypted, self.directory)
        self.assertEqual(error.exception.stage, "backup_key_missing")
        self.assertFalse(path.exists())

    def test_public_and_symlink_keys_are_rejected(self):
        encrypted = self.encrypted()
        path = self.directory / "supabase-backup.key"
        path.chmod(0o644)
        with self.assertRaises(recovery.RecoveryError) as error:
            recovery.verify_snapshot(encrypted, self.directory)
        self.assertEqual(error.exception.stage, "backup_key_permissions")
        path.chmod(0o600)
        original = self.directory / "key-original"
        path.rename(original)
        path.symlink_to(original)
        with self.assertRaises(recovery.RecoveryError):
            recovery.verify_snapshot(encrypted, self.directory)

    def test_corrupt_encryption_header_and_database_are_rejected(self):
        for encrypted, stage in (
            (b"gAAAA-invalid", "snapshot_decryption"),
            (self.encrypted(prefix=b"wrong\n"), "snapshot_header"),
            (self.encrypted(raw=b"not sqlite"), "sqlite_header"),
        ):
            with self.subTest(stage=stage), self.assertRaises(
                recovery.RecoveryError
            ) as error:
                recovery.verify_snapshot(encrypted, self.directory)
            self.assertEqual(error.exception.stage, stage)

    def test_ciphertext_and_decompression_are_bounded(self):
        with patch.object(recovery, "MAX_CIPHERTEXT_BYTES", 32), self.assertRaises(
            recovery.RecoveryError
        ) as error:
            recovery.verify_snapshot(b"x" * 33, self.directory)
        self.assertEqual(error.exception.stage, "ciphertext_validation")
        with patch.object(recovery, "MAX_DATABASE_BYTES", 1024), self.assertRaises(
            recovery.RecoveryError
        ) as error:
            recovery.verify_snapshot(self.encrypted(raw=b"x" * 2048), self.directory)
        self.assertEqual(error.exception.stage, "bounded_decompression")

    def test_tampered_sqlite_is_rejected(self):
        raw = bytearray(self.database.read_bytes())
        raw[16:18] = b"\x00\x03"
        with self.assertRaises(recovery.RecoveryError) as error:
            recovery.verify_snapshot(self.encrypted(raw=raw), self.directory)
        self.assertEqual(error.exception.stage, "sqlite_integrity")

    def test_missing_search_and_orphan_photo_are_rejected(self):
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("DELETE FROM queries WHERE id=44")
        with self.assertRaises(recovery.RecoveryError) as error:
            recovery.verify_snapshot(self.encrypted(), self.directory)
        self.assertEqual(error.exception.stage, "expected_searches")
        self.assertEqual(error.exception.observed["counts"]["searches"], 43)
        self.assertEqual(error.exception.observed["integrity"], "ok")
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("INSERT INTO queries VALUES (44)")
            conn.execute("DELETE FROM dashboard_media")
        with self.assertRaises(recovery.RecoveryError) as error:
            recovery.verify_snapshot(self.encrypted(), self.directory)
        self.assertEqual(error.exception.stage, "example_photo_references")

    def test_archived_total_is_exposed_and_saved_searches_are_checked(self):
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute(
                "CREATE TABLE search_dashboard(query_id INTEGER PRIMARY KEY,archived INTEGER)"
            )
            conn.executemany("INSERT INTO queries VALUES (?)", [(45,), (46,)])
            conn.executemany(
                "INSERT INTO search_dashboard VALUES (?,1)", [(45,), (46,)]
            )
        result = recovery.verify_snapshot(self.encrypted(), self.directory)
        counts = result["counts"]
        self.assertEqual(counts["searches"], 46)
        self.assertEqual(counts["saved_searches"], 44)
        self.assertEqual(counts["archived_searches"], 2)
        self.assertEqual(counts["expected_searches"], 44)
        self.assertNotIn("fictional-buyer-secret", json.dumps(result))
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("UPDATE search_dashboard SET archived=0 WHERE query_id=45")
        with self.assertRaises(recovery.RecoveryError) as error:
            recovery.verify_snapshot(self.encrypted(), self.directory)
        self.assertEqual(error.exception.stage, "expected_searches")
        self.assertEqual(error.exception.observed["counts"]["saved_searches"], 45)

    def test_sessions_in_cloud_and_wrong_buyer_key_are_rejected(self):
        encrypted = self.encrypted()
        path = self.directory / "vinted-buyer.key"
        original = path.read_bytes()
        path.write_bytes(Fernet.generate_key())
        with self.assertRaises(recovery.RecoveryError) as error:
            recovery.verify_snapshot(encrypted, self.directory)
        self.assertEqual(error.exception.stage, "encrypted_buyer_records")
        path.write_bytes(original)
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute(
                "INSERT INTO supabase_dashboard_sessions VALUES ('hash','owner',x'01',0)"
            )
        with self.assertRaises(recovery.RecoveryError) as error:
            recovery.verify_snapshot(self.encrypted(), self.directory)
        self.assertEqual(error.exception.stage, "excluded_dashboard_sessions")


class EndpointTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cloud = self.encrypted()
        self.now = time.time()
        self.provider = Mock(config=backend.Config.from_environment(SETTINGS))
        self.provider.verify_owner.return_value = OWNER
        self.provider.sign_in.return_value = {
            "access_token": "fictional-owner-access",
            "refresh_token": "fictional-owner-refresh",
            "expires_at": self.now + 3600,
        }
        self.provider.download_backup.return_value = (self.cloud, STAMP)
        self.app = Flask(__name__)
        self.app.config.update(SECRET_KEY="x" * 64, TESTING=True)
        self.integration = backend.install_dashboard(
            self.app,
            lambda: str(self.database),
            environ=SETTINGS,
            client=self.provider,
            clock=lambda: self.now,
        )

        @self.app.get("/")
        def dashboard():
            if not self.integration.is_authenticated():
                return redirect(url_for(self.integration.login_endpoint))
            return "owner dashboard"

        self.web = self.app.test_client()

    def login(self):
        self.web.get("/supabase/login")
        with self.web.session_transaction() as state:
            csrf = state["csrf"]
        result = self.web.post(
            "/supabase/login",
            data={
                "csrf": csrf,
                "email": "owner@example.test",
                "password": "fictional-password",
            },
        )
        self.assertEqual(result.location, "/")
        with self.web.session_transaction() as state:
            return state["csrf"]

    def test_owner_authenticated_post_verifies_and_preserves_live_records(self):
        csrf = self.login()
        with closing(sqlite3.connect(self.database)) as conn:
            before = conn.execute("SELECT * FROM vinted_buyer").fetchall()
        with self.assertLogs(self.app.logger, level="INFO") as logs:
            response = self.web.post("/supabase/backup/verify", data={"csrf": csrf})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"recovery verified", response.data)
        self.assertIn(STAMP.encode(), response.data)
        self.assertIn(b"44", response.data)
        self.provider.download_backup.assert_called_once_with(
            "fictional-owner-access", with_metadata=True
        )
        self.provider.verify_owner.assert_called_with("fictional-owner-access")
        self.assertNotIn("fictional-owner-access", " ".join(logs.output))
        self.assertNotIn("fictional-buyer-secret", " ".join(logs.output))
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(
                before, conn.execute("SELECT * FROM vinted_buyer").fetchall()
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM supabase_dashboard_sessions"
                ).fetchone()[0],
                1,
            )

    def test_anonymous_and_owner_cookie_without_session_cannot_download(self):
        self.assertEqual(
            self.web.post(
                "/supabase/backup/verify", data={"csrf": "anything"}
            ).location,
            "/supabase/login",
        )
        with self.web.session_transaction() as state:
            state["owner"] = True
        self.assertEqual(
            self.web.post(
                "/supabase/backup/verify", data={"csrf": "anything"}
            ).location,
            "/supabase/login",
        )
        self.provider.download_backup.assert_not_called()

    def test_search_count_failure_reports_only_completed_checks_and_counts(self):
        original = self.database.read_bytes()
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("DELETE FROM queries WHERE id=44")
        self.provider.download_backup.return_value = (self.encrypted(), STAMP)
        self.database.write_bytes(original)
        csrf = self.login()
        before = self.database.read_bytes()
        with self.assertLogs(self.app.logger, level="INFO") as logs:
            result = self.web.post("/supabase/backup/verify", data={"csrf": csrf})
        self.assertEqual(result.status_code, 422)
        self.assertIn(b"Result: unverified", result.data)
        self.assertIn(b"SQLite integrity: ok", result.data)
        self.assertIn(b"<td>searches</td><td>43</td>", result.data)
        self.assertNotIn(b"Encrypted buyer session: readable", result.data)
        self.assertIn('"searches": 43', " ".join(logs.output))
        self.assertNotIn("fictional-buyer-secret", " ".join(logs.output))
        self.assertNotIn(b"fictional-owner-access", result.data)
        self.assertEqual(before, self.database.read_bytes())

    def test_owner_endpoint_verifies_250_current_searches(self):
        self.search_definitions()
        self.provider.download_backup.return_value = (self.encrypted(), STAMP)
        csrf = self.login()
        with self.assertLogs(self.app.logger, level="INFO"):
            result = self.web.post("/supabase/backup/verify", data={"csrf": csrf})
        self.assertEqual(result.status_code, 200)
        self.assertIn(b"recovery verified", result.data)
        self.assertIn(b"<td>expected searches</td><td>250</td>", result.data)
        self.assertIn(b"Search definitions match live: True", result.data)

    def test_owner_endpoint_rejects_changed_same_count_search_with_specific_reason(
        self,
    ):
        self.search_definitions()
        self.provider.download_backup.return_value = (self.encrypted(), STAMP)
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("UPDATE queries SET query_name='Changed search' WHERE id=1")
        csrf = self.login()
        with self.assertLogs(self.app.logger, level="INFO") as logs:
            result = self.web.post("/supabase/backup/verify", data={"csrf": csrf})
        self.assertEqual(result.status_code, 422)
        self.assertIn(b"Result: unverified", result.data)
        self.assertIn(b"saved search settings differ", result.data)
        self.assertNotIn("Changed search", str(logs.output))

    def test_stale_settings_buyer_or_photo_record_never_reports_endpoint_verified(self):
        csrf = self.login()
        for comparison in ("settings", "buyer_records", "photo_references"):
            encrypted = self.encrypted()
            # Cloud snapshots intentionally exclude owner dashboard sessions.
            with closing(sqlite3.connect(self.database)) as conn, conn:
                if comparison == "settings":
                    conn.execute("UPDATE parameters SET value='Changed setting'")
                elif comparison == "buyer_records":
                    rotated = self.keys["buyer"].encrypt(
                        json.dumps({"cookies": "fictional-rotated-session"}).encode()
                    )
                    conn.execute("UPDATE vinted_buyer SET session=?", (rotated,))
                else:
                    conn.execute(
                        "INSERT INTO dashboard_media VALUES ('other-photo',x'04')"
                    )
                    conn.execute(
                        "UPDATE search_reference_photos SET media_id='other-photo'"
                    )
            # Build the older, session-free cloud copy without altering live.
            with tempfile.TemporaryDirectory() as temporary:
                snapshot = Path(temporary) / "snapshot.sqlite"
                payload = self.keys["backup"].decrypt(encrypted)
                snapshot.write_bytes(
                    gzip.decompress(payload[len(backend.SNAPSHOT_MAGIC) :])
                )
                with closing(sqlite3.connect(snapshot)) as backup, backup:
                    backup.execute("DELETE FROM supabase_dashboard_sessions")
                cloud = self.encrypted(raw=snapshot.read_bytes())
            self.provider.download_backup.return_value = (cloud, STAMP)
            with self.subTest(comparison=comparison), self.assertLogs(
                self.app.logger, level="INFO"
            ) as logs:
                result = self.web.post("/supabase/backup/verify", data={"csrf": csrf})
            self.assertEqual(result.status_code, 422)
            self.assertIn(b"Result: unverified", result.data)
            self.assertIn(b"Stage: remaining_checks", result.data)
            label = {
                "settings": "Saved settings",
                "buyer_records": "Buyer records",
                "photo_references": "Photo references",
            }[comparison]
            self.assertIn((label + " match live: False").encode(), result.data)
            self.assertNotIn(b"Saved cloud backup recovery verified", result.data)
            self.assertNotIn("fictional-rotated-session", str(logs.output))

    def test_non_owner_server_verification_denies_request(self):
        csrf = self.login()
        self.provider.verify_owner.side_effect = backend.SupabaseError("other owner")
        result = self.web.post("/supabase/backup/verify", data={"csrf": csrf})
        self.assertEqual(result.location, "/supabase/login")
        self.provider.download_backup.assert_not_called()

    def test_csrf_errors_are_separate_and_get_is_not_allowed(self):
        self.login()
        for data in ({}, {"csrf": "expired"}):
            with self.subTest(data=data), self.assertLogs(
                self.app.logger, level="INFO"
            ) as logs:
                result = self.web.post("/supabase/backup/verify", data=data)
            self.assertEqual(result.status_code, 400)
            self.assertIn("stage=csrf", " ".join(logs.output))
        self.assertEqual(self.web.get("/supabase/backup/verify").status_code, 405)
        self.provider.download_backup.assert_not_called()

    def test_expired_session_and_refresh_failure_have_distinct_stages(self):
        csrf = self.login()
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("UPDATE supabase_dashboard_sessions SET expires_at=0")
        with self.assertLogs(self.app.logger, level="INFO") as logs:
            result = self.web.post("/supabase/backup/verify", data={"csrf": csrf})
        self.assertEqual(result.location, "/supabase/login")
        self.assertIn("stage=expired_session", " ".join(logs.output))
        csrf = self.login()
        self.now += 3590
        self.provider.refresh.side_effect = backend.SupabaseError("refresh failed")
        with self.assertLogs(self.app.logger, level="WARNING") as logs:
            result = self.web.post("/supabase/backup/verify", data={"csrf": csrf})
        self.assertEqual(result.location, "/supabase/login")
        self.assertIn("stage=session_refresh", " ".join(logs.output))
        self.provider.download_backup.assert_not_called()

    def test_normal_refresh_preserves_owner_verification_flow(self):
        csrf = self.login()
        self.now += 3590
        self.provider.refresh.return_value = {
            "access_token": "rotated-owner-access",
            "refresh_token": "rotated-owner-refresh",
            "expires_at": self.now + 3600,
        }
        result = self.web.post("/supabase/backup/verify", data={"csrf": csrf})
        self.assertEqual(result.status_code, 200)
        self.provider.refresh.assert_called_once_with(
            "fictional-owner-refresh", now=self.now
        )
        self.provider.download_backup.assert_called_once_with(
            "rotated-owner-access", with_metadata=True
        )

    def test_corrupt_oversized_and_missing_keys_return_safe_failures(self):
        csrf = self.login()
        cases = [
            (b"invalid", "snapshot_decryption"),
            (
                self.keys["backup"].encrypt(
                    backend.SNAPSHOT_MAGIC + gzip.compress(b"x" * 2048)
                ),
                "bounded_decompression",
            ),
        ]
        for ciphertext, stage in cases:
            self.provider.download_backup.return_value = (ciphertext, STAMP)
            with patch.object(recovery, "MAX_DATABASE_BYTES", 1024):
                result = self.web.post("/supabase/backup/verify", data={"csrf": csrf})
            self.assertEqual(result.status_code, 422)
            self.assertIn(stage.encode(), result.data)
            self.assertNotIn(b"fictional-owner-access", result.data)
        self.provider.download_backup.return_value = (self.cloud, STAMP)
        path = self.directory / "supabase-backup.key"
        path.unlink()
        result = self.web.post("/supabase/backup/verify", data={"csrf": csrf})
        self.assertEqual(result.status_code, 422)
        self.assertIn(b"backup_key_missing", result.data)
        self.assertFalse(path.exists())

    def test_cloud_failure_does_not_claim_success_or_upload(self):
        csrf = self.login()
        self.provider.download_backup.side_effect = backend.SupabaseError(
            "provider-secret"
        )
        result = self.web.post("/supabase/backup/verify", data={"csrf": csrf})
        self.assertEqual(result.status_code, 502)
        self.assertIn(b"owner_cloud_download", result.data)
        self.assertNotIn(b"provider-secret", result.data)
        self.provider.upload_backup.assert_not_called()


class MetadataTests(unittest.TestCase):
    def test_owner_scoped_download_timestamp_and_default_bytes_contract(self):
        client = backend.Client(backend.Config.from_environment(SETTINGS))
        client.verify_owner = Mock(return_value=OWNER)
        client._request = Mock(
            return_value=[{"ciphertext": "gAAAAfake", "updated_at": STAMP}]
        )
        self.assertEqual(client.download_backup("fictional-access"), b"gAAAAfake")
        self.assertEqual(
            client.download_backup("fictional-access", with_metadata=True),
            (b"gAAAAfake", STAMP),
        )
        call = client._request.call_args
        self.assertIn("owner_id=eq." + OWNER, call.args[1])
        self.assertIn("select=ciphertext,updated_at", call.args[1])
        for value in (None, "bad timestamp", "2026-10-08"):
            client._request.return_value = [
                {"ciphertext": "gAAAAfake", "updated_at": value}
            ]
            with self.assertRaises(backend.SupabaseError):
                client.download_backup("fictional-access", with_metadata=True)


if __name__ == "__main__":
    unittest.main()
