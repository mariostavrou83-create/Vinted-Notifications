"""Opt-in sharing between a watched Home profile and a verified Telegram recipient."""

import hashlib
import re
import secrets
import time

from flask import g, jsonify, request

from .db import get_db, settings_for, transaction
from . import home_companion


def init_routes(app):
    get_db().executescript("""
        CREATE TABLE IF NOT EXISTS home_route_codes (
            recipient INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            chat INTEGER NOT NULL, digest TEXT NOT NULL UNIQUE, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS shared_homes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            binding TEXT NOT NULL, home TEXT NOT NULL,
            recipient INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, chat INTEGER NOT NULL,
            min_alert_id INTEGER NOT NULL DEFAULT 0,
            UNIQUE(owner,binding,recipient,chat));
        CREATE INDEX IF NOT EXISTS shared_homes_recipient ON shared_homes(recipient);
        CREATE TABLE IF NOT EXISTS shared_home_matches (
            alert_id INTEGER NOT NULL REFERENCES pilot_alerts(id) ON DELETE CASCADE,
            route_id INTEGER NOT NULL REFERENCES shared_homes(id) ON DELETE CASCADE,
            PRIMARY KEY(alert_id,route_id));
        CREATE TRIGGER IF NOT EXISTS revoke_home_matches AFTER UPDATE OF enabled ON pilot_home_watch
            WHEN OLD.enabled=1 AND NEW.enabled=0 BEGIN
            DELETE FROM shared_home_matches WHERE route_id IN
                (SELECT id FROM shared_homes WHERE owner=OLD.user_id AND binding=OLD.binding);
        END;
        CREATE TRIGGER IF NOT EXISTS revoke_recipient_home_links AFTER DELETE ON pilot_chats BEGIN
            DELETE FROM shared_homes WHERE recipient=OLD.user_id AND chat=OLD.chat_id;
            DELETE FROM home_route_codes WHERE recipient=OLD.user_id;
        END;
        CREATE TRIGGER IF NOT EXISTS change_recipient_home_links AFTER UPDATE OF chat_id ON pilot_chats
            WHEN OLD.chat_id != NEW.chat_id BEGIN
            DELETE FROM shared_homes WHERE recipient=OLD.user_id AND chat=OLD.chat_id;
            DELETE FROM home_route_codes WHERE recipient=OLD.user_id;
        END;
    """)

    conn = get_db()
    with transaction(conn):
        if "min_alert_id" not in {
            row["name"] for row in conn.execute("PRAGMA table_info(shared_homes)")
        }:
            conn.execute(
                "ALTER TABLE shared_homes ADD COLUMN min_alert_id INTEGER NOT NULL DEFAULT 0"
            )
    home_companion.init_app(app)

    @app.get("/api/home-routes")
    def home_routes():
        uid, db = g.user["id"], get_db()
        dev = db.execute(
            "SELECT username FROM native_devices WHERE user_id=? AND expires>?",
            (uid, time.time()),
        ).fetchone()
        incoming = db.execute(
            """SELECT r.id,r.home FROM shared_homes r JOIN pilot_chats c
            ON c.user_id=r.recipient AND c.chat_id=r.chat WHERE r.recipient=? AND r.owner<>r.recipient ORDER BY r.home""",
            (uid,),
        )
        outgoing = db.execute(
            """SELECT r.id,r.home,u.username AS recipient FROM shared_homes r JOIN users u ON u.id=r.recipient
            WHERE r.owner=? AND r.owner<>r.recipient ORDER BY u.username,r.home,r.id""",
            (uid,),
        )
        return jsonify(
            own_home=dev[0] if dev else None,
            telegram_linked=bool(
                db.execute(
                    "SELECT 1 FROM pilot_chats WHERE user_id=?", (uid,)
                ).fetchone()
            ),
            extras=home_companion.extras(db, uid),
            incoming=[dict(r) for r in incoming],
            outgoing=[dict(r) for r in outgoing],
        )

    @app.post("/api/home-routes/invite")
    def home_route_invite():
        db, uid = get_db(), g.user["id"]
        token = secrets.token_urlsafe(24)
        with transaction(db):
            chat = db.execute(
                "SELECT chat_id FROM pilot_chats WHERE user_id=?", (uid,)
            ).fetchone()
            if not chat:
                return jsonify(error="Connect your receiving Telegram chat first."), 409
            db.execute(
                "INSERT OR REPLACE INTO home_route_codes VALUES (?,?,?,?)",
                (
                    uid,
                    chat[0],
                    hashlib.sha256(token.encode()).hexdigest(),
                    int(time.time()) + 600,
                ),
            )
        return jsonify(code=token, expires_in=600)

    @app.post("/api/home-routes/accept")
    def home_route_accept():
        db, owner = get_db(), g.user["id"]
        body = request.get_json(silent=True)
        token = body.get("code") if isinstance(body, dict) else None
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
            return jsonify(
                error="Enter the one-use code from the receiving Finder dashboard."
            ), 400
        with transaction(db):
            now = time.time()
            if "source_id" in body:
                dev = home_companion.extra_binding(db, owner, body["source_id"])
            else:
                dev = db.execute(
                    "SELECT binding,username FROM native_devices WHERE user_id=? AND expires>?",
                    (owner, now),
                ).fetchone()
            if not dev:
                return jsonify(error="Pair this Home profile in Chrome first."), 409
            link = db.execute(
                """SELECT k.* FROM home_route_codes k JOIN pilot_chats c
                ON c.user_id=k.recipient AND c.chat_id=k.chat WHERE k.digest=? AND k.expires>?""",
                (hashlib.sha256(token.encode()).hexdigest(), now),
            ).fetchone()
            if not link:
                return jsonify(
                    error="This invitation expired or was used. Create a new one."
                ), 400
            if owner == link["recipient"]:
                return jsonify(error="This Home already uses your own Telegram."), 409
            add_route(
                db,
                owner,
                dev["binding"],
                dev["username"],
                link["recipient"],
                link["chat"],
            )
            db.execute(
                "DELETE FROM home_route_codes WHERE recipient=?", (link["recipient"],)
            )
        return jsonify(ok=True)

    @app.post("/api/home-routes/<int:route_id>/remove")
    def home_route_remove(route_id):
        db, uid = get_db(), g.user["id"]
        with transaction(db):
            removed = db.execute(
                "DELETE FROM shared_homes WHERE id=? AND (owner=? OR recipient=?)",
                (route_id, uid, uid),
            ).rowcount
        return (
            (jsonify(ok=True), 200)
            if removed
            else (jsonify(error="Home connection not found."), 404)
        )


def add_route(conn, owner, binding, label, recipient, chat):
    # A newly authorised route cannot adopt already queued items from an old link.
    conn.execute(
        """INSERT OR IGNORE INTO shared_homes(owner,binding,home,recipient,chat,min_alert_id)
        SELECT ?,?,?,?,?,COALESCE(MAX(id),0) FROM pilot_alerts""",
        (owner, binding, label, recipient, chat),
    )


def fanout(conn, owner, binding, item, now):
    from .flow import _record

    received = False
    rows = conn.execute(
        """SELECT r.id,r.recipient,r.min_alert_id FROM shared_homes r JOIN pilot_chats c
        ON c.user_id=r.recipient AND c.chat_id=r.chat WHERE r.owner=? AND r.binding=?""",
        (owner, binding),
    ).fetchall()
    for route in rows:
        uid = route["recipient"]
        aid = _record(
            conn,
            uid,
            "routed-home",
            item,
            "home",
            settings_for(conn, uid),
            now,
            "browser_home",
        )
        if not aid:
            # Add provenance for overlapping Homes only on an already accepted copy.
            row = conn.execute(
                """SELECT id FROM pilot_alerts WHERE user_id=? AND account_ref='routed-home'
                AND listing_id=? AND origin='browser_home' AND delivery IN ('new','sent','sending')""",
                (uid, str(item["id"])),
            ).fetchone()
            aid = row[0] if row else None
        else:
            received = True
        if aid and aid > route["min_alert_id"]:
            conn.execute(
                "INSERT OR IGNORE INTO shared_home_matches VALUES(?,?)",
                (aid, route["id"]),
            )
    return received


def routed_active(conn, uid, listing_id, now=None):
    return bool(
        conn.execute(
            """SELECT 1 FROM shared_home_matches m JOIN shared_homes r ON r.id=m.route_id
        JOIN pilot_alerts a ON a.id=m.alert_id AND a.user_id=r.recipient
        JOIN pilot_chats c ON c.user_id=r.recipient AND c.chat_id=r.chat
        WHERE a.user_id=? AND a.listing_id=? AND a.account_ref='routed-home' AND (
          EXISTS (SELECT 1 FROM pilot_home_watch w JOIN native_devices d ON d.user_id=w.user_id
            AND d.binding=w.binding AND d.username=w.username COLLATE NOCASE
            WHERE w.user_id=r.owner AND w.binding=r.binding AND w.enabled=1 AND d.expires>?) OR
          EXISTS (SELECT 1 FROM extra_home_sources e WHERE e.owner=r.owner
            AND e.binding=r.binding AND e.token_hash IS NOT NULL)) LIMIT 1""",
            (uid, listing_id, time.time() if now is None else now),
        ).fetchone()
    )
