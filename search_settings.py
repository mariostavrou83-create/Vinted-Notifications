"""Additive search preferences. No changes to URLs, watermarks or seen items."""

import json
import os
import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import db

SCHEMA_VERSION = "8"


def connection():
    conn = sqlite3.connect(db.DB_PATH, timeout=15)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema():
    """Back up the real database before a one-time, transactional migration.

    Called in the parent before any child processes start. A failed backup or
    integrity check aborts startup; an existing deployment can be rolled back.
    """
    with closing(connection()) as conn:
        version = conn.execute(
            "SELECT value FROM parameters WHERE key='msj_search_schema'"
        ).fetchone()
        if version and version[0] == SCHEMA_VERSION:
            return None
        directory = Path(db.DB_PATH).resolve().parent / "backups"
        directory.mkdir(mode=0o700, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = directory / f"before-search-controls-{stamp}.sqlite3"
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(backup)) as destination:
            conn.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Database backup failed integrity check")
        with conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS search_preferences (
                query_id INTEGER PRIMARY KEY REFERENCES queries(id) ON DELETE CASCADE,
                reminder TEXT NOT NULL DEFAULT '',
                exclusions TEXT NOT NULL DEFAULT '[]'
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS search_filtered_items (
                query_id INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
                item TEXT NOT NULL,
                PRIMARY KEY(query_id, item)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS search_health (
                query_id INTEGER PRIMARY KEY REFERENCES queries(id) ON DELETE CASCADE,
                last_attempt REAL, last_success REAL, duration REAL,
                actual_interval REAL, failures INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT ''
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_items_item ON items(item)")
            conn.execute("""CREATE TABLE IF NOT EXISTS search_dashboard (
                query_id INTEGER PRIMARY KEY REFERENCES queries(id) ON DELETE CASCADE,
                paused INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
                reference_id TEXT, revision INTEGER NOT NULL DEFAULT 0,
                rebaseline INTEGER NOT NULL DEFAULT 0)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS dashboard_media (
                id TEXT PRIMARY KEY, image BLOB NOT NULL, telegram_file_id TEXT,
                created REAL NOT NULL)""")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS dashboard_auth (
                id INTEGER PRIMARY KEY CHECK(id=1), password_hash TEXT,
                setup_hash TEXT NOT NULL, session_key TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, window_start REAL NOT NULL DEFAULT 0)"""
            )
            conn.execute("""CREATE TABLE IF NOT EXISTS search_folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS search_buying_guide (
                query_id INTEGER PRIMARY KEY REFERENCES queries(id) ON DELETE CASCADE,
                max_buy INTEGER, resale_low INTEGER, resale_high INTEGER,
                must_have TEXT NOT NULL DEFAULT '',
                folder_id INTEGER REFERENCES search_folders(id) ON DELETE SET NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS alert_outbox (
                item_id TEXT PRIMARY KEY,
                query_id INTEGER REFERENCES queries(id) ON DELETE SET NULL,
                search_name TEXT NOT NULL, content TEXT NOT NULL, url TEXT NOT NULL,
                title TEXT NOT NULL, price TEXT NOT NULL, currency TEXT NOT NULL,
                photo_url TEXT, reference_id TEXT, found_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, telegram_message_id INTEGER,
                sent_at REAL, error TEXT NOT NULL DEFAULT '',
                photo_status TEXT NOT NULL DEFAULT 'none',
                photo_attempts INTEGER NOT NULL DEFAULT 0,
                photo_next_attempt REAL NOT NULL DEFAULT 0, photo_error TEXT NOT NULL DEFAULT '',
                lease_token TEXT, leased_until REAL NOT NULL DEFAULT 0,
                user_status TEXT NOT NULL DEFAULT 'new')""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_delivery ON alert_outbox(status,next_attempt)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_photo ON alert_outbox(photo_status,photo_next_attempt)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_found ON alert_outbox(found_at DESC)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS delivery_runtime (key TEXT PRIMARY KEY,value REAL NOT NULL)"
            )
            conn.execute("""CREATE TABLE IF NOT EXISTS listing_frontiers (
                query_id INTEGER PRIMARY KEY REFERENCES queries(id) ON DELETE CASCADE,
                max_item_id INTEGER NOT NULL, last_check REAL NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS listing_checkpoints (
                minute INTEGER PRIMARY KEY, max_item_id INTEGER NOT NULL)""")
            # Seed the cutoff from existing history without rewriting any item.
            import time

            conn.execute(
                """INSERT OR IGNORE INTO listing_checkpoints
                SELECT ?,COALESCE(MAX(CAST(item AS INTEGER)),0) FROM items
                WHERE NOT EXISTS(SELECT 1 FROM listing_checkpoints)""",
                (int(time.time() / 60) - 21,),
            )
            from ebay_schema import migrate

            migrate(conn)
            conn.execute("""CREATE TABLE IF NOT EXISTS vinted_alert_details (
                item_id TEXT PRIMARY KEY REFERENCES alert_outbox(item_id) ON DELETE CASCADE,
                payload TEXT NOT NULL)""")
            conn.execute("""INSERT OR IGNORE INTO parameters
                VALUES ('vinted_single_message_alerts', '0')""")
            conn.execute(
                "INSERT OR REPLACE INTO parameters VALUES ('msj_search_schema', ?)",
                (SCHEMA_VERSION,),
            )
            # Mario requested faster checking. Upgrade the previous three-second
            # setting once, keeping intentionally slower intervals untouched.
            conn.execute(
                "UPDATE parameters SET value='1' WHERE key='query_refresh_delay' AND value='3'"
            )
        return str(backup)


def get_search(query_id):
    with closing(connection()) as conn:
        row = conn.execute(
            """SELECT q.id, q.query, q.last_item, q.query_name,
            COALESCE(p.reminder, '') AS reminder,
            COALESCE(p.exclusions, '[]') AS exclusions,
            COALESCE(d.paused, 0) AS paused, COALESCE(d.archived, 0) AS archived,
            COALESCE(d.rebaseline, 0) AS rebaseline, d.reference_id,
            COALESCE(d.revision, 0) AS revision,
            g.max_buy, g.resale_low, g.resale_high, COALESCE(g.must_have,'') AS must_have,
            g.folder_id, f.name AS folder_name
            FROM queries q LEFT JOIN search_preferences p ON p.query_id=q.id
            LEFT JOIN search_dashboard d ON d.query_id=q.id
            LEFT JOIN search_buying_guide g ON g.query_id=q.id
            LEFT JOIN search_folders f ON f.id=g.folder_id
            WHERE q.id=?""",
            (query_id,),
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["exclusions"] = json.loads(result["exclusions"])
    from ebay_store import platform_details

    result.update(platform_details(query_id))
    return result


def active_queries():
    with closing(connection()) as conn:
        return [tuple(row) for row in conn.execute("""SELECT q.* FROM queries q
            LEFT JOIN search_dashboard d ON d.query_id=q.id
            LEFT JOIN search_platforms s ON s.query_id=q.id
            WHERE COALESCE(d.paused,0)=0 AND COALESCE(d.archived,0)=0
            AND COALESCE(s.vinted_enabled,1)=1""")]


def finish_baseline(query_id, url):
    with closing(connection()) as conn, conn:
        conn.execute(
            """UPDATE search_dashboard SET rebaseline=0 WHERE query_id=?
            AND EXISTS(SELECT 1 FROM queries WHERE id=? AND query=?)""",
            (query_id, query_id, url),
        )


def update_search(query_id, field, value):
    limits = {"query_name": 100, "reminder": 800}
    if field in limits:
        value = value.strip()
        if len(value) > limits[field]:
            raise ValueError(f"Please use at most {limits[field]} characters.")
    elif field == "exclusions":
        value = json.dumps(parse_exclusions(value), ensure_ascii=False)
    else:
        raise ValueError("Unknown search field")
    with closing(connection()) as conn, conn:
        if not conn.execute("SELECT 1 FROM queries WHERE id=?", (query_id,)).fetchone():
            raise ValueError("That search no longer exists. Use /queries.")
        if field == "query_name":
            conn.execute(
                "UPDATE queries SET query_name=? WHERE id=?", (value, query_id)
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO search_preferences(query_id) VALUES (?)",
                (query_id,),
            )
            conn.execute(
                f"UPDATE search_preferences SET {field}=? WHERE query_id=?",
                (value, query_id),
            )


def parse_exclusions(text):
    phrases = list(
        dict.fromkeys(
            phrase.strip()
            for phrase in re.split(r"[;\n]|\|\|\|", text)
            if phrase.strip()
        )
    )
    if len(phrases) > 30 or any(len(p) > 60 for p in phrases):
        raise ValueError("Use up to 30 phrases, each at most 60 characters.")
    if any(not tokens(p) for p in phrases):
        raise ValueError("Each exclusion must contain a word.")
    return phrases


def tokens(text):
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold())


def excluded_by(title, phrases):
    """Case-insensitive whole words/phrases; hyphens act like spaces."""
    words = tokens(title)
    for phrase in phrases:
        needle = tokens(phrase)
        if needle and any(
            words[i : i + len(needle)] == needle
            for i in range(len(words) - len(needle) + 1)
        ):
            return phrase
    return None


def filtered_ids(query_id, item_ids=None):
    if item_ids is not None and not item_ids:
        return set()
    with closing(connection()) as conn:
        sql = "SELECT item FROM search_filtered_items WHERE query_id=?"
        params = [query_id]
        if item_ids is not None:
            sql += " AND item IN (" + ",".join("?" for _ in item_ids) + ")"
            params.extend(str(item) for item in item_ids)
        return {row[0] for row in conn.execute(sql, params)}


def remember_filtered(query_id, item_ids):
    if not item_ids:
        return
    with closing(connection()) as conn, conn:
        # A search can be deleted while a response is being processed.
        if conn.execute("SELECT 1 FROM queries WHERE id=?", (query_id,)).fetchone():
            conn.executemany(
                "INSERT OR IGNORE INTO search_filtered_items VALUES (?, ?)",
                [(query_id, str(item_id)) for item_id in item_ids],
            )


def record_health(query_id, attempted, duration, actual_interval, error=""):
    with closing(connection()) as conn, conn:
        if not conn.execute("SELECT 1 FROM queries WHERE id=?", (query_id,)).fetchone():
            return
        conn.execute(
            """INSERT INTO search_health
            (query_id, last_attempt, last_success, duration, actual_interval, failures, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(query_id) DO UPDATE SET
            last_attempt=excluded.last_attempt,
            last_success=COALESCE(excluded.last_success, search_health.last_success),
            duration=excluded.duration, actual_interval=excluded.actual_interval,
            failures=CASE WHEN excluded.error='' THEN 0 ELSE search_health.failures+1 END,
            error=excluded.error""",
            (
                query_id,
                attempted,
                None if error else attempted + duration,
                duration,
                actual_interval,
                int(bool(error)),
                error,
            ),
        )


def health_rows():
    with closing(connection()) as conn:
        return [dict(row) for row in conn.execute("""SELECT q.id, h.* FROM queries q
            LEFT JOIN search_health h ON q.id=h.query_id ORDER BY q.id""")]


def listing_cutoff(query_id, item_ids, now):
    """Conservative ID-order heuristic when Vinted omits creation dates.

    Existing IDs do not become new when the price changes. The per-search
    frontier rejects resurfaced lower IDs; a shared twenty-minute frontier
    catches old IDs entering a quiet search's price range for the first time.
    After a long search outage, its first page is quiet to avoid stale alerts.
    """
    with closing(connection()) as conn:
        row = conn.execute(
            "SELECT * FROM listing_frontiers WHERE query_id=?", (query_id,)
        ).fetchone()
        global_floor = conn.execute(
            "SELECT COALESCE(MAX(max_item_id),0) FROM listing_checkpoints WHERE minute<=?",
            (int((now - 1200) / 60),),
        ).fetchone()[0]
    floor = max(global_floor, row["max_item_id"] if row else 0)
    if row and now - row["last_check"] > 1200:
        floor = max([floor] + [int(i) for i in item_ids if str(i).isdigit()])
    return floor


def remember_listing_frontier(query_id, item_ids, now):
    """Only advance after the whole batch's alert transactions succeeded."""
    maximum = max([0] + [int(i) for i in item_ids if str(i).isdigit()])
    with closing(connection()) as conn, conn:
        conn.execute(
            """INSERT INTO listing_frontiers VALUES (?,?,?) ON CONFLICT(query_id)
            DO UPDATE SET max_item_id=MAX(max_item_id,excluded.max_item_id),last_check=excluded.last_check""",
            (query_id, maximum, now),
        )
        global_max = conn.execute(
            "SELECT COALESCE(MAX(max_item_id),0) FROM listing_checkpoints"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO listing_checkpoints VALUES (?,?) ON CONFLICT(minute)
            DO UPDATE SET max_item_id=MAX(max_item_id,excluded.max_item_id)""",
            (int(now / 60), max(maximum, global_max)),
        )
        # Retain the last old checkpoint as the boundary, plus the recent window.
        conn.execute(
            """DELETE FROM listing_checkpoints WHERE minute <
            (SELECT MAX(minute) FROM listing_checkpoints WHERE minute<=?)""",
            (int((now - 1200) / 60),),
        )
