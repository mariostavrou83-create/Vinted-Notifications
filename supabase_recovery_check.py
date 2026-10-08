"""Recover the owner's cloud snapshot in isolation; never restore live data."""

import gzip
import io
import json
import os
import sqlite3
import stat
import tempfile
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

from supabase_backend import (
    MAX_CIPHERTEXT_BYTES,
    MAX_DATABASE_BYTES,
    SNAPSHOT_MAGIC,
    SupabaseError,
)

COUNT_TABLES = {
    "searches": "queries",
    "history": "items",
    "example_photos": "search_reference_photos",
    "budgets": "vinted_search_budgets",
    "purchase_attempts": "vinted_buy_attempts",
    "alerts": "alert_outbox",
    "web_sessions": "supabase_dashboard_sessions",
}


class RecoveryError(SupabaseError):
    def __init__(self, stage):
        super().__init__("The saved cloud backup recovery check did not pass.")
        self.stage = stage


def existing_cipher(path, label):
    """Read an existing private key; never create or replace one."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise RecoveryError(label + "_permissions")
            return Fernet(stream.read(4096))
    except FileNotFoundError:
        raise RecoveryError(label + "_missing") from None
    except RecoveryError:
        raise
    except (OSError, ValueError):
        raise RecoveryError(label + "_unavailable") from None


def verify_snapshot(encrypted, directory, *, live_database=None, expected_searches=44):
    """Return fixed checks/counts only, after deleting a private temporary copy."""
    if not isinstance(encrypted, bytes) or len(encrypted) > MAX_CIPHERTEXT_BYTES:
        raise RecoveryError("ciphertext_validation")
    directory = Path(directory)
    keys = {
        label: existing_cipher(directory / filename, label + "_key")
        for label, filename in (
            ("backup", "supabase-backup.key"),
            ("buyer", "vinted-buyer.key"),
            ("session", "supabase-session.key"),
        )
    }
    stage = "snapshot_decryption"
    try:
        payload = keys["backup"].decrypt(encrypted)
        stage = "snapshot_header"
        if not payload.startswith(SNAPSHOT_MAGIC):
            raise RecoveryError(stage)
        stage = "bounded_decompression"
        with gzip.GzipFile(fileobj=io.BytesIO(payload[len(SNAPSHOT_MAGIC) :])) as gz:
            raw = gz.read(MAX_DATABASE_BYTES + 1)
        if len(raw) > MAX_DATABASE_BYTES:
            raise RecoveryError(stage)
        stage = "sqlite_header"
        if not raw.startswith(b"SQLite format 3\x00"):
            raise RecoveryError(stage)
        with tempfile.TemporaryDirectory(prefix="msj-recovery-review-") as temporary:
            snapshot = Path(temporary) / "snapshot.sqlite"
            fd = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
            uri = "file:" + quote(str(snapshot), safe="/") + "?mode=ro&immutable=1"
            with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
                deadline = time.monotonic() + 10
                conn.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
                conn.execute("PRAGMA trusted_schema=OFF")
                conn.execute("PRAGMA query_only=ON")
                stage = "sqlite_integrity"
                if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise RecoveryError(stage)
                stage = "expected_tables"
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                required = set(COUNT_TABLES.values()) | {
                    "vinted_buyer",
                    "dashboard_media",
                    "parameters",
                }
                if not required <= tables:
                    raise RecoveryError(stage)
                counts = {
                    label: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    for label, table in COUNT_TABLES.items()
                }
                stage = "expected_searches"
                if counts["searches"] != expected_searches:
                    raise RecoveryError(stage)
                stage = "example_photo_references"
                invalid_refs = conn.execute(
                    "SELECT COUNT(*) FROM search_reference_photos p "
                    "LEFT JOIN queries q ON q.id=p.query_id "
                    "LEFT JOIN dashboard_media m ON m.id=p.media_id "
                    "WHERE q.id IS NULL OR m.id IS NULL OR length(m.image)=0"
                ).fetchone()[0]
                if not counts["example_photos"] or invalid_refs:
                    raise RecoveryError(stage)
                stage = "excluded_dashboard_sessions"
                if counts["web_sessions"]:
                    raise RecoveryError(stage)
                stage = "encrypted_buyer_records"
                buyer = conn.execute(
                    "SELECT session,pending,network FROM vinted_buyer WHERE id=1"
                ).fetchone()
                if not buyer or not buyer[0]:
                    raise RecoveryError(stage)
                for record in buyer:
                    if record is not None:
                        value = json.loads(keys["buyer"].decrypt(record))
                        if not isinstance(value, dict) or not value:
                            raise RecoveryError(stage)
                settings_match = buyer_match = refs_match = None
                if live_database is not None:
                    stage = "live_comparison"
                    live_uri = (
                        "file:"
                        + quote(str(Path(live_database).resolve()), safe="/")
                        + "?mode=ro"
                    )
                    with closing(
                        sqlite3.connect(live_uri, uri=True, timeout=5)
                    ) as live:
                        live.execute("PRAGMA query_only=ON")
                        compare_tables = ("parameters", "vinted_search_budgets")
                        settings_match = all(
                            conn.execute(
                                f'SELECT * FROM "{table}" ORDER BY 1'
                            ).fetchall()
                            == live.execute(
                                f'SELECT * FROM "{table}" ORDER BY 1'
                            ).fetchall()
                            for table in compare_tables
                        )
                        buyer_match = (
                            conn.execute(
                                "SELECT * FROM vinted_buyer ORDER BY id"
                            ).fetchall()
                            == live.execute(
                                "SELECT * FROM vinted_buyer ORDER BY id"
                            ).fetchall()
                        )
                        refs_match = (
                            conn.execute(
                                "SELECT * FROM search_reference_photos ORDER BY query_id,position"
                            ).fetchall()
                            == live.execute(
                                "SELECT * FROM search_reference_photos ORDER BY query_id,position"
                            ).fetchall()
                        )
        return {
            "outcome": "verified",
            "integrity": "ok",
            "counts": counts,
            "private_keys": {label: True for label in keys},
            "buyer_session_decryptable": True,
            "settings_match_live": settings_match,
            "buyer_records_match_live": buyer_match,
            "photo_references_match_live": refs_match,
        }
    except RecoveryError:
        raise
    except (InvalidToken, OSError, EOFError, sqlite3.Error, ValueError, TypeError):
        raise RecoveryError(stage) from None
