"""Private per-user Finder settings and search storage."""
import json
import sqlite3
from contextlib import contextmanager

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invitations (
    digest TEXT PRIMARY KEY, expires_at INTEGER NOT NULL, used_at INTEGER
);
CREATE TABLE IF NOT EXISTS login_attempts (
    bucket TEXT PRIMARY KEY, started_at INTEGER NOT NULL, attempts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL, url TEXT NOT NULL, include_keywords TEXT NOT NULL,
    exclude_keywords TEXT NOT NULL, budget_pence INTEGER, enabled INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS searches_by_owner ON searches(user_id,id);
CREATE TRIGGER IF NOT EXISTS enforce_search_limit BEFORE INSERT ON searches
WHEN (SELECT COUNT(*) FROM searches WHERE user_id = NEW.user_id) >= 100
BEGIN SELECT RAISE(ABORT, 'search limit reached'); END;
CREATE TABLE IF NOT EXISTS search_photos (
    id INTEGER PRIMARY KEY, search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    image BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS photos_by_search ON search_photos(search_id,id);
"""

DEFAULT_SETTINGS = {
    "categories": [], "sizes": [], "quiet_start": "23:00", "quiet_end": "07:00",
    "delivery_mode": "pickup", "pickup_id": "", "pickup_label": "", "home_address_ref": "",
    "preferred_courier": "", "allowed_couriers": [], "allow_same_shop_fallback": False,
    "max_shipping": "4.99", "max_total": "", "max_item_price": "",
}


def get_db():
    if "pilot_db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"], timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        g.pilot_db = conn
    return g.pilot_db


def close_db(_error=None):
    conn = g.pop("pilot_db", None)
    if conn is not None:
        conn.close()


@contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def settings_for(conn, user_id):
    settings = {**DEFAULT_SETTINGS, "categories": [], "sizes": [], "allowed_couriers": [],
                "home_address_label": "", "pickup_summary": "", "delivery_couriers": []}
    row = conn.execute("SELECT data FROM settings WHERE user_id=?", (user_id,)).fetchone()
    if row:
        settings.update(json.loads(row["data"]))
    return settings


def searches_for(conn, user_id):
    records = {}
    for row in conn.execute("SELECT * FROM searches WHERE user_id=? ORDER BY id DESC", (user_id,)):
        data = dict(row)
        del data["user_id"]
        data["enabled"] = bool(data["enabled"])
        for key in ("include_keywords", "exclude_keywords"):
            data[key] = json.loads(data[key])
        pence = data["budget_pence"]
        data["budget"] = "" if pence is None else f"{pence // 100}.{pence % 100:02d}"
        data["photos"] = []
        records[data["id"]] = data
    for row in conn.execute(
        """SELECT p.id,p.search_id FROM search_photos p JOIN searches s ON s.id=p.search_id
           WHERE s.user_id=? ORDER BY p.id""", (user_id,)
    ):
        records[row["search_id"]]["photos"].append({
            "id": row["id"], "url": f"/api/searches/{row['search_id']}/photos/{row['id']}",
        })
    return list(records.values())
