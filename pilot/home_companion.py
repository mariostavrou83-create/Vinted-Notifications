"""Pair a Home-only Chrome source and route the public listings it displays."""

import hashlib
import re
import secrets
import time
from decimal import Decimal, InvalidOperation

from flask import g, jsonify, request

from .db import get_db, transaction
from .listing_metadata import display_text, vinted_photo_url

SCHEMA = """
CREATE TABLE IF NOT EXISTS extra_home_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label TEXT NOT NULL, binding TEXT NOT NULL UNIQUE,
    pair_hash TEXT UNIQUE, pair_until REAL, account TEXT,
    token_hash TEXT UNIQUE, last_seen REAL
);
CREATE INDEX IF NOT EXISTS extra_home_owner ON extra_home_sources(owner);
CREATE TRIGGER IF NOT EXISTS remove_extra_home_routes AFTER DELETE ON extra_home_sources BEGIN
    DELETE FROM shared_homes WHERE owner=OLD.owner AND binding=OLD.binding;
END;
"""
DEVICE_ENDPOINTS = {"home_companion_pair", "home_companion_events"}
ID = re.compile(r"[1-9][0-9]{0,19}")
MAX_BODY = 128 * 1024


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def extra_binding(conn, owner, source_id):
    if type(source_id) is not int or not 0 < source_id < 2**63:
        return None
    return conn.execute(
        """SELECT id,binding,label AS username FROM extra_home_sources
        WHERE owner=? AND id=? AND token_hash IS NOT NULL""",
        (owner, source_id),
    ).fetchone()


def valid_extra(conn, owner, binding, now=None):
    return bool(
        conn.execute(
            """SELECT 1 FROM extra_home_sources
        WHERE owner=? AND binding=? AND token_hash IS NOT NULL""",
            (owner, binding),
        ).fetchone()
    )


def extras(conn, owner):
    return [
        dict(
            id=row["id"],
            label=row["label"],
            paired=bool(row["paired"]),
            last_seen=row["last_seen"],
        )
        for row in conn.execute(
            """SELECT id,label,token_hash IS NOT NULL AS paired,last_seen
                FROM extra_home_sources WHERE owner=? ORDER BY id""",
            (owner,),
        )
    ]


def public_item(raw, now):
    if not isinstance(raw, dict) or set(raw) - {
        "id",
        "url",
        "title",
        "price_gbp",
        "listed_at",
        "photo_url",
        "category",
        "size",
    }:
        return None
    identity = raw.get("id")
    if not isinstance(identity, str) or not ID.fullmatch(identity):
        return None
    # The companion emits canonical UK links without queries or per-user tracking.
    url = raw.get("url")
    if not isinstance(url, str) or url != "https://www.vinted.co.uk/items/" + identity:
        return None
    title = display_text(raw.get("title"), 240)
    posted = raw.get("listed_at")
    price = raw.get("price_gbp")
    if (
        not title
        or type(posted) is not int
        or not 0 <= now - posted < 180
        or type(price) not in (int, float)
    ):
        return None
    try:
        pence = Decimal(str(price)) * 100
        if (
            not pence.is_finite()
            or not 0 < pence <= 100_000_000
            or pence != pence.to_integral_value()
        ):
            return None
    except (InvalidOperation, ValueError):
        return None
    item = dict(
        id=identity,
        listing_url=url,
        title=title,
        listed_at=posted,
        price_pence=int(pence),
        currency="GBP",
        total_pence=None,
        total_excludes_shipping=True,
    )
    for key in ("category", "size"):
        if key in raw:
            value = display_text(raw[key], 80)
            if not value:
                return None
            item[key] = value
    if raw.get("photo_url"):
        photo = vinted_photo_url(raw["photo_url"])
        if photo:
            item["photo_url"] = photo
    return item


def json_body(max_bytes=MAX_BODY):
    if (
        not request.is_json
        or not request.content_length
        or request.content_length > max_bytes
    ):
        return {}
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def init_app(app):
    from .home_routes import add_route, fanout
    from .home_groups import observe
    from .home_companion_download import home_companion_zip_response

    get_db().executescript(SCHEMA)
    app.add_url_rule(
        "/extension/home-companion.zip",
        "download_home_companion",
        home_companion_zip_response,
    )

    @app.post("/api/home-routes/sources")
    def home_source_create():
        label = display_text(json_body(4096).get("label"), 64)
        if not label:
            return jsonify(error="Give this Chrome Home a name."), 400
        conn, owner, now = get_db(), g.user["id"], time.time()
        code, binding = secrets.token_urlsafe(24), "extra-" + secrets.token_urlsafe(24)
        with transaction(conn):
            cur = conn.execute(
                """INSERT INTO extra_home_sources(owner,label,binding,pair_hash,pair_until)
                VALUES(?,?,?,?,?)""",
                (owner, label, binding, digest(code), now + 600),
            )
            chat = conn.execute(
                "SELECT chat_id FROM pilot_chats WHERE user_id=?", (owner,)
            ).fetchone()
            if chat:
                add_route(conn, owner, binding, label, owner, chat[0])
        return jsonify(id=cur.lastrowid, code=code, expires_in=600)

    @app.post("/api/home-routes/sources/<int:source_id>/reconnect")
    def home_source_reconnect(source_id):
        conn, owner = get_db(), g.user["id"]
        if source_id >= 2**63:
            return jsonify(error="Home source not found."), 404
        code = secrets.token_urlsafe(24)
        with transaction(conn):
            updated = conn.execute(
                """UPDATE extra_home_sources SET pair_hash=?,pair_until=?
                WHERE id=? AND owner=?""",
                (digest(code), time.time() + 600, source_id, owner),
            )
            if not updated.rowcount:
                return jsonify(error="Home source not found."), 404
        return jsonify(id=source_id, code=code, expires_in=600)

    @app.post("/api/home-routes/sources/<int:source_id>/remove")
    def home_source_remove(source_id):
        conn, owner = get_db(), g.user["id"]
        if source_id >= 2**63:
            return jsonify(error="Home source not found."), 404
        with transaction(conn):
            row = conn.execute(
                "SELECT binding FROM extra_home_sources WHERE owner=? AND id=?",
                (owner, source_id),
            ).fetchone()
            if not row:
                return jsonify(error="Home source not found."), 404
            from .home_groups import remove_source

            remove_source(conn, owner, row["binding"])
            conn.execute(
                "DELETE FROM extra_home_sources WHERE id=? AND owner=?",
                (source_id, owner),
            )
        return jsonify(ok=True)

    @app.post("/api/home/companion/pair")
    def home_companion_pair():
        body = json_body(4096)
        code, account = body.get("code"), body.get("account_id")
        if (
            set(body) != {"code", "account_id"}
            or not isinstance(code, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32}", code)
            or not isinstance(account, str)
            or not ID.fullmatch(account)
        ):
            return (
                jsonify(
                    error="Enter the pairing code for your signed-in Home profile."
                ),
                400,
            )
        conn, now = get_db(), time.time()
        with transaction(conn):
            row = conn.execute(
                """SELECT id,account FROM extra_home_sources WHERE pair_hash=? AND pair_until>?""",
                (digest(code), now),
            ).fetchone()
            if not row:
                return jsonify(error="This pairing code expired or was used."), 403
            if row["account"] is not None and row["account"] != account:
                return (
                    jsonify(
                        error="Open the same Vinted UK profile before reconnecting this Home."
                    ),
                    403,
                )
            token = secrets.token_urlsafe(32)
            conn.execute(
                """UPDATE extra_home_sources SET pair_hash=NULL,pair_until=NULL,account=?,token_hash=?
                WHERE id=?""",
                (account, digest(token), row["id"]),
            )
        return jsonify(source_id=row["id"], token=token)

    @app.post("/api/home/companion/events")
    def home_companion_events():
        header = request.headers.get("Authorization", "")
        match = re.fullmatch(r"Bearer ([A-Za-z0-9_-]{43})", header)
        if not match:
            return jsonify(error="Pair this Home companion."), 401
        body = json_body()
        account, items = body.get("account_id"), body.get("items")
        if (
            set(body) != {"account_id", "items"}
            or not isinstance(account, str)
            or not ID.fullmatch(account)
            or not isinstance(items, list)
            or len(items) > 50
        ):
            return (
                jsonify(
                    error="Send up to 50 visible Home listings and their account ID."
                ),
                400,
            )
        conn, now = get_db(), time.time()
        with transaction(conn):
            row = conn.execute(
                "SELECT * FROM extra_home_sources WHERE token_hash=?",
                (digest(match[1]),),
            ).fetchone()
            if not row or row["account"] != account:
                return (
                    jsonify(error="The Home companion profile no longer matches."),
                    403,
                )
            if row["last_seen"] is not None and now - row["last_seen"] < 0.4:
                return (
                    jsonify(error="Wait before sending the next batch."),
                    429,
                    {"Retry-After": "1"},
                )
            conn.execute(
                "UPDATE extra_home_sources SET last_seen=? WHERE id=?", (now, row["id"])
            )
            accepted = 0
            for raw in items:
                item = public_item(raw, now)
                if item:
                    shared = fanout(conn, row["owner"], row["binding"], item, now)
                    grouped = observe(conn, row["owner"], row["binding"], item, now)
                    accepted += bool(shared or grouped)
        return jsonify(accepted=accepted)
