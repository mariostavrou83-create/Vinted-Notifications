"""Opt-in Home alerts from the visible page on the buyer's paired Chrome device."""

import re
import time

from flask import g, jsonify, request

from . import flow, native_order
from .db import get_db, settings_for, transaction
from .manual_import import MAX_BODY, MAX_ITEMS, _item


def active(conn, uid, binding, now=None):
    return conn.execute(
        """SELECT w.username,w.last_observed_at FROM pilot_home_watch w JOIN native_devices d
           ON d.user_id=w.user_id AND d.binding=w.binding AND d.username=w.username COLLATE NOCASE
           WHERE w.user_id=? AND w.binding=? AND w.enabled=1 AND d.expires>?""",
        (uid, binding, time.time() if now is None else now),
    ).fetchone()


def checked_item(raw, now):
    item = _item(raw, int(now), now)
    if not item:
        return None
    age = item["age_text"].strip().lower()
    if age == "just now":
        bound = 60
    elif age in {"a minute ago", "one minute ago"}:
        bound = 120
    else:
        match = re.fullmatch(r"(\d{1,5}) (second|minute)s? ago", age)
        if match:
            bound = (int(match[1]) + 1) * (60 if match[2] == "minute" else 1)
        else:
            stamp = flow.listing_time(item["age_text"])
            if stamp is None or int(stamp) != item["listed_at"]:
                return None
            bound = 0
    if now - item["listed_at"] < bound:
        return None
    item.pop("dashboard_only")
    # The visible asking price does not include a verified full checkout total.
    item.update(total_pence=None, total_excludes_shipping=True)
    return item


def init_home_watch(app):
    from .home_routes import init_routes

    get_db().execute("""CREATE TABLE IF NOT EXISTS pilot_home_watch (
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        binding TEXT NOT NULL, username TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
        last_observed_at REAL)""")
    init_routes(app)

    @app.get("/api/native/home-status")
    def native_home_status():
        conn, uid, now = get_db(), g.user["id"], time.time()
        device = conn.execute(
            "SELECT username,binding FROM native_devices WHERE user_id=? AND expires>?", (uid, now)
        ).fetchone()
        state = active(conn, uid, device["binding"], now) if device else None
        return jsonify(
            paired=bool(device),
            enabled=bool(state),
            username=device["username"] if device else None,
            last_observed_at=state["last_observed_at"] if state else None,
        )

    @app.post("/api/native/home-watch")
    def native_home_watch():
        body = request.get_json(silent=True) if (request.content_length or 0) <= MAX_BODY else None
        if not isinstance(body, dict) or type(body.get("enabled")) is not bool:
            return jsonify(error="Choose whether to watch this Home page."), 400
        conn = get_db()
        with transaction(conn):
            device = native_order.device_home_context(conn)
            if not device:
                return jsonify(error="Pair the current buying profile in Chrome."), 401
            uid, binding = device["user_id"], device["binding"]
            if body["enabled"]:
                conn.execute(
                    """INSERT INTO pilot_home_watch(user_id,binding,username,enabled) VALUES(?,?,?,1)
                   ON CONFLICT(user_id) DO UPDATE SET binding=excluded.binding,username=excluded.username,
                   last_observed_at=CASE WHEN binding=excluded.binding AND enabled=1 THEN last_observed_at END,
                   enabled=1""",
                    (uid, binding, device["username"]),
                )
            else:
                conn.execute(
                    "UPDATE pilot_home_watch SET enabled=0 WHERE user_id=? AND binding=?", (uid, binding)
                )
                native_order.cancel_home(conn, uid, binding)
        return jsonify(enabled=body["enabled"])

    @app.post("/api/native/home")
    def native_home():
        body = request.get_json(silent=True) if (request.content_length or 0) <= MAX_BODY else None
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("items"), list)
            or not 1 <= len(body["items"]) <= MAX_ITEMS
        ):
            return jsonify(error="Send between one and twelve visible listings."), 400
        conn, now = get_db(), time.time()
        with transaction(conn):
            device = native_order.device_home_context(conn)
            if not device:
                return jsonify(error="Pair the current buying profile in Chrome."), 401
            uid, binding = device["user_id"], device["binding"]
            state = active(conn, uid, binding, now)
            if not state:
                return jsonify(error="Start Home alerts for this tab in Chrome."), 403
            if state["last_observed_at"] and now - state["last_observed_at"] < 0.4:
                return jsonify(error="Wait one second before the next batch."), 429, {"Retry-After": "1"}
            prefs, accepted = settings_for(conn, uid), 0
            for raw in body["items"]:
                item = checked_item(raw, now)
                if item:
                    from .home_routes import fanout
                    from .home_groups import observe
                    own = flow.record_watched_home(conn, uid, binding, item, prefs, now)
                    shared = fanout(conn, uid, binding, item, now)
                    grouped = observe(conn, uid, binding, item, now)
                    accepted += bool(own or shared or grouped)
            conn.execute(
                "UPDATE pilot_home_watch SET last_observed_at=? WHERE user_id=? AND binding=?",
                (now, uid, binding),
            )
        return jsonify(accepted=accepted, skipped=len(body["items"]) - accepted)
