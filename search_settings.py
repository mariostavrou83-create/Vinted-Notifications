"""Additive search preferences. No changes to URLs, watermarks or seen items."""
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import unicodedata

import db

SCHEMA_VERSION = "1"


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
            conn.execute("INSERT OR REPLACE INTO parameters VALUES ('msj_search_schema', ?)",
                         (SCHEMA_VERSION,))
        return str(backup)


def get_search(query_id):
    with closing(connection()) as conn:
        row = conn.execute("""SELECT q.id, q.query, q.last_item, q.query_name,
            COALESCE(p.reminder, '') AS reminder,
            COALESCE(p.exclusions, '[]') AS exclusions
            FROM queries q LEFT JOIN search_preferences p ON p.query_id=q.id
            WHERE q.id=?""", (query_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["exclusions"] = json.loads(result["exclusions"])
    return result


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
            conn.execute("UPDATE queries SET query_name=? WHERE id=?", (value, query_id))
        else:
            conn.execute("INSERT OR IGNORE INTO search_preferences(query_id) VALUES (?)", (query_id,))
            conn.execute(f"UPDATE search_preferences SET {field}=? WHERE query_id=?", (value, query_id))


def parse_exclusions(text):
    phrases = list(dict.fromkeys(
        phrase.strip() for phrase in re.split(r"[;\n]|\|\|\|", text) if phrase.strip()
    ))
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
        if needle and any(words[i:i + len(needle)] == needle
                          for i in range(len(words) - len(needle) + 1)):
            return phrase
    return None


def filtered_ids(query_id, item_ids=None):
    if item_ids is not None and not item_ids:
        return set()
    with closing(connection()) as conn:
        sql = 'SELECT item FROM search_filtered_items WHERE query_id=?'
        params = [query_id]
        if item_ids is not None:
            sql += ' AND item IN (' + ','.join('?' for _ in item_ids) + ')'
            params.extend(str(item) for item in item_ids)
        return {row[0] for row in conn.execute(sql, params)}


def remember_filtered(query_id, item_ids):
    if not item_ids:
        return
    with closing(connection()) as conn, conn:
        # A search can be deleted while a response is being processed.
        if conn.execute("SELECT 1 FROM queries WHERE id=?", (query_id,)).fetchone():
            conn.executemany("INSERT OR IGNORE INTO search_filtered_items VALUES (?, ?)",
                             [(query_id, str(item_id)) for item_id in item_ids])


def record_health(query_id, attempted, duration, actual_interval, error=""):
    with closing(connection()) as conn, conn:
        if not conn.execute("SELECT 1 FROM queries WHERE id=?", (query_id,)).fetchone():
            return
        conn.execute("""INSERT INTO search_health
            (query_id, last_attempt, last_success, duration, actual_interval, failures, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(query_id) DO UPDATE SET
            last_attempt=excluded.last_attempt,
            last_success=COALESCE(excluded.last_success, search_health.last_success),
            duration=excluded.duration, actual_interval=excluded.actual_interval,
            failures=CASE WHEN excluded.error='' THEN 0 ELSE search_health.failures+1 END,
            error=excluded.error""",
            (query_id, attempted, None if error else attempted + duration, duration,
             actual_interval, int(bool(error)), error))


def health_rows():
    with closing(connection()) as conn:
        return [dict(row) for row in conn.execute("""SELECT q.id, h.* FROM queries q
            LEFT JOIN search_health h ON q.id=h.query_id ORDER BY q.id""")]
