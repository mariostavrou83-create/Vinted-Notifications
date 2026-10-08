"""Opt-in Railway owner-session recovery diagnosis; no browser or key creation."""

import json
import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from flask import Flask

import db
from search_settings import connection
from supabase_backend import Client, Config, DashboardIntegration, SupabaseError
from supabase_recovery_check import existing_cipher, verify_snapshot

logger = logging.getLogger(__name__)
MARKER = "service_recovery_checked_release"


def check_recovery():
    result = {"outcome": "unverified", "stage": "existing_keys"}
    backup_timestamp = None
    try:
        database = Path(db.DB_PATH).resolve()
        # Check all existing files before constructing anything that uses them.
        # Do not call DashboardIntegration.__init__ or _private_cipher here.
        keys = {
            label: existing_cipher(database.parent / filename, label + "_key")
            for label, filename in (
                ("backup", "supabase-backup.key"),
                ("buyer", "vinted-buyer.key"),
                ("session", "supabase-session.key"),
            )
        }
        result["stage"] = "owner_session"
        config = Config.from_environment()
        if config is None:
            return result
        with closing(connection()) as conn:
            row = conn.execute(
                "SELECT token_hash FROM supabase_dashboard_sessions "
                "WHERE owner_id=? AND expires_at>? ORDER BY expires_at DESC LIMIT 1",
                (config.owner_id, time.time()),
            ).fetchone()
        if row is None:
            return result
        # Only trusted service execution can enter this path. Reuse the normal
        # token expiry/refresh and fresh configured-owner verification; keep the
        # publishable-key client and Storage/Postgres RLS effective.
        app = Flask(__name__)
        integration = object.__new__(DashboardIntegration)
        integration.app = app
        integration.database_path = lambda: str(database)
        integration.client = Client(config)
        integration.clock = time.time
        integration.cipher = keys["session"]
        with app.test_request_context(
            "/internal/authorized-recovery-check", method="POST"
        ):
            tokens = integration.tokens(authorized_session_hash=row[0])
            if not tokens:
                return result
            result["stage"] = "owner_cloud_download"
            encrypted, backup_timestamp = integration.client.download_backup(
                tokens["access_token"], with_metadata=True
            )
            result["stage"] = "isolated_recovery"
            result = verify_snapshot(encrypted, database.parent, live_database=database)
        result["stage"] = "complete"
        if any(
            result.get(key) is not True
            for key in (
                "buyer_session_decryptable",
                "settings_match_live",
                "buyer_records_match_live",
                "photo_references_match_live",
            )
        ):
            result.update(outcome="unverified", stage="remaining_checks")
        return result
    except SupabaseError as exc:
        result = {
            **getattr(exc, "observed", {}),
            "outcome": "unverified",
            "stage": getattr(exc, "stage", result["stage"]),
        }
        return result
    except Exception:  # noqa: BLE001 -- only fixed results leave the service
        result["outcome"] = "unverified"
        return result
    finally:
        if backup_timestamp:
            result["backup_timestamp"] = backup_timestamp
        logger.info(
            "Supabase service recovery check: %s", json.dumps(result, sort_keys=True)
        )


def run_once():
    release = os.environ.get("MSJ_SERVICE_RECOVERY_CHECK_ON_START", "")
    if release.lower() in ("", "0", "false", "off") or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,80}", release
    ):
        return None
    try:
        with closing(connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT value FROM parameters WHERE key=?", (MARKER,)
            ).fetchone()
            if previous and previous[0] == release:
                return None
            conn.execute(
                "INSERT OR REPLACE INTO parameters(key,value) VALUES (?,?)",
                (MARKER, release),
            )
    except (OSError, sqlite3.Error):
        logger.info(
            "Supabase service recovery startup: outcome=unverified stage=reservation"
        )
        return None
    return check_recovery()
