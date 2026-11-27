"""Accept visible UK catalogue observations from a user-enabled, paired Chrome tab."""

import hashlib
import json
import re
import secrets
import time
from urllib.parse import parse_qsl, urlsplit

from flask import g, jsonify, request

from . import flow, native_order, public_feed
from .db import get_db, settings_for, transaction
from .home_watch import checked_item
from .inputs import InputError, money, search_url
from .manual_import import MAX_ITEMS

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_watches(
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 source_key TEXT NOT NULL, binding TEXT NOT NULL, consent TEXT NOT NULL UNIQUE,
 baselined INTEGER NOT NULL DEFAULT 0, last_checked REAL, last_changed REAL, rendered_count INTEGER, PRIMARY KEY(user_id,source_key)
);
CREATE TABLE IF NOT EXISTS catalog_seen(
 consent TEXT NOT NULL REFERENCES catalog_watches(consent) ON DELETE CASCADE,
 listing_id TEXT NOT NULL, seen_at REAL NOT NULL, PRIMARY KEY(consent,listing_id)
);
"""


def key_for(url):
    try:
        parsed = urlsplit(search_url(url))
        query = sorted(
            parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=150), key=lambda pair: pair[0]
        )
        value = json.dumps([parsed.path.rstrip("/"), query], separators=(",", ":"))
    except (InputError, TypeError, ValueError):
        return None
    return hashlib.sha256(value.encode()).hexdigest()


def rules_for(conn, uid, source_key):
    rows = conn.execute("SELECT * FROM searches WHERE user_id=? AND enabled=1 ORDER BY id", (uid,))
    return [row for row in rows if key_for(row["url"]) == source_key]


def matches(rule, item, prefs):
    title = item["title"].casefold()
    text = title + " " + str(item.get("description") or "").casefold()
    required = json.loads(rule["include_keywords"])
    if (required and not any(k.casefold() in text for k in required)) or any(
        k.casefold() in title for k in json.loads(rule["exclude_keywords"])
    ):
        return False
    if rule["budget_pence"] is None:
        return True
    shipping = money(prefs.get("max_shipping"))
    subtotal = item.get("subtotal_pence")
    return type(subtotal) is int and shipping is not None and subtotal + shipping <= rule["budget_pence"]


def eligible(conn, uid, binding, listing):
    item = json.loads(listing) if isinstance(listing, str) else listing
    if not isinstance(item, dict) or not binding or not item.get("catalog_consent"):
        return False
    row = conn.execute(
        """SELECT w.source_key FROM catalog_watches w JOIN native_devices d
      ON d.user_id=w.user_id AND d.binding=w.binding AND d.expires>?
      WHERE w.user_id=? AND w.binding=? AND w.consent=?""",
        (time.time(), uid, binding, item["catalog_consent"]),
    ).fetchone()
    if not row:
        return False
    rule = conn.execute(
        "SELECT * FROM searches WHERE id=? AND user_id=? AND enabled=1", (item.get("search_id"), uid)
    ).fetchone()
    if not rule or key_for(rule["url"]) != row["source_key"]:
        return False
    # Editing a search's rules must not revive its old buying instruction.
    return item.get("catalog_rule") == rule_signature(rule)


def rule_signature(rule):
    return hashlib.sha256(
        json.dumps([rule["include_keywords"], rule["exclude_keywords"], rule["budget_pence"]]).encode()
    ).hexdigest()


def stop(conn, uid, source_key):
    watch = conn.execute(
        "SELECT consent FROM catalog_watches WHERE user_id=? AND source_key=?", (uid, source_key)
    ).fetchone()
    if not watch:
        return
    pending = conn.execute(
        """SELECT o.* FROM native_orders o JOIN pilot_alerts a ON o.alert_id=a.id
      WHERE o.user_id=? AND a.origin='browser_search' AND json_extract(a.listing,'$.catalog_consent')=?
      AND o.stage IN ('await_quote','reading','confirm','await_commit')""",
        (uid, watch["consent"]),
    ).fetchall()
    for order in pending:
        native_order._end(
            conn,
            order,
            "blocked",
            "Chrome stopped watching this search before the purchase was authorised.",
        )
    conn.execute(
        """UPDATE pilot_alerts SET native_binding=NULL,delivery=CASE WHEN delivery='new' THEN 'dashboard' ELSE delivery END
      WHERE user_id=? AND origin='browser_search' AND json_extract(listing,'$.catalog_consent')=?""",
        (uid, watch["consent"]),
    )
    conn.execute("DELETE FROM catalog_watches WHERE user_id=? AND source_key=?", (uid, source_key))


def valid_body():
    data = request.get_json(silent=True) if (request.content_length or 0) <= 48000 else None
    key = key_for(data.get("url")) if isinstance(data, dict) else None
    return data, key


def init_app(app):
    get_db().executescript(SCHEMA)

    @app.get("/api/native/search-status")
    def native_search_status():
        conn, uid, now = get_db(), g.user["id"], time.time()
        watches = {
            r["source_key"]: r
            for r in conn.execute(
                """SELECT w.* FROM catalog_watches w JOIN native_devices d
              ON d.user_id=w.user_id AND d.binding=w.binding AND d.expires>? WHERE w.user_id=?""",
                (now, uid),
            )
        }
        results = {}
        for row in conn.execute("SELECT id,url,enabled FROM searches WHERE user_id=?", (uid,)):
            w = watches.get(key_for(row["url"])) if row["enabled"] else None
            checked = w["last_checked"] if w else None
            state = (
                "inactive"
                if not w
                else "starting"
                if not checked or not w["baselined"]
                else "watching"
                if now - checked <= 125
                else "stale"
            )
            results[str(row["id"])] = dict(
                enabled=bool(w),
                state=state,
                last_checked_at=checked,
                last_contact_at=checked,
                last_changed_at=w["last_changed"] if w else None,
                rendered_count=w["rendered_count"] if w else None,
                target_seconds=5,
            )
        response = jsonify(searches=results, public_searches=public_feed.status(conn, uid)["searches"])
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/native/search-watch")
    def native_search_watch():
        data, key = valid_body()
        if not key or type(data.get("enabled")) is not bool:
            return jsonify(error="Choose a saved Vinted UK catalogue search."), 400
        conn = get_db()
        with transaction(conn):
            device = native_order.device_home_context(conn)
            if not device:
                return jsonify(error="Pair the buying profile shown in this tab."), 401
            uid, binding = device["user_id"], device["binding"]
            rules = rules_for(conn, uid, key)
            if data["enabled"] and not rules:
                return jsonify(error="Save this exact search link and enable it in Finder first."), 403
            consent = data.get("watch_id")
            if data["enabled"]:
                consent = secrets.token_hex(16)
                # A fresh start invalidates buttons from a previous watch session.
                stop(conn, uid, key)
                conn.execute(
                    "INSERT INTO catalog_watches(user_id,source_key,binding,consent) VALUES(?,?,?,?)",
                    (uid, key, binding, consent),
                )
            else:
                current = conn.execute(
                    "SELECT consent FROM catalog_watches WHERE user_id=? AND source_key=? AND binding=?",
                    (uid, key, binding),
                ).fetchone()
                if current and current["consent"] != consent:
                    return jsonify(error="This tab's watch has been replaced."), 409
                stop(conn, uid, key)
        return jsonify(
            enabled=data["enabled"], matched=len(rules), watch_id=consent if data["enabled"] else None
        )

    @app.post("/api/native/search-items")
    def native_search_items():
        data, key = valid_body()
        if not key or not isinstance(data.get("items"), list) or len(data["items"]) > MAX_ITEMS:
            return jsonify(error="Send up to twelve visible catalogue listings."), 400
        items = data["items"]
        baseline = data.get("baseline", False)
        rendered = data.get("rendered_ids", [])
        if (
            type(baseline) is not bool
            or not isinstance(rendered, list)
            or len(rendered) > 1000
            or any(not isinstance(v, str) or not re.fullmatch(r"\d{1,20}", v) for v in rendered)
            or (baseline and items)
            or (rendered and not baseline)
        ):
            return jsonify(error="Invalid initial set of visible listing IDs."), 400
        conn, now = get_db(), time.time()
        with transaction(conn):
            device = native_order.device_home_context(conn)
            if not device:
                return jsonify(error="Pair the buying profile shown in this tab."), 401
            uid, binding = device["user_id"], device["binding"]
            watch = conn.execute(
                "SELECT * FROM catalog_watches WHERE user_id=? AND source_key=? AND binding=?",
                (uid, key, binding),
            ).fetchone()
            rules = rules_for(conn, uid, key)
            if not watch or not rules:
                return jsonify(error="Start watching this enabled saved search in Chrome."), 403
            consent = watch["consent"]
            if data.get("watch_id") != consent:
                return jsonify(error="This tab's watch has been replaced."), 409
            changed = data.get("last_changed")
            if type(changed) is not int or not 0 < changed <= now:
                changed = None
            count = data.get("rendered_count")
            if type(count) is not int or not 0 <= count <= 1000:
                count = None
            if baseline and not watch["baselined"]:
                conn.executemany(
                    "INSERT OR IGNORE INTO catalog_seen VALUES(?,?,?)",
                    ((consent, i, now) for i in rendered),
                )
                conn.execute(
                    "UPDATE catalog_watches SET baselined=1,last_checked=?,last_changed=?,rendered_count=? WHERE consent=?",
                    (now, changed, len(rendered), consent),
                )
                return jsonify(accepted=0, skipped=0, matched=len(rules), baseline=True)
            if not watch["baselined"]:
                return jsonify(
                    error="Confirm the visible starting catalogue before sending new listings."
                ), 409
            if not items and watch["last_checked"] and now - watch["last_checked"] < 0.75:
                return jsonify(accepted=0, skipped=0, matched=len(rules))
            prefs, accepted = settings_for(conn, uid), 0
            for raw in items:
                item = checked_item(raw, now)
                if not item:
                    continue
                if not conn.execute(
                    "INSERT OR IGNORE INTO catalog_seen VALUES(?,?,?)", (consent, item["id"], now)
                ).rowcount:
                    continue
                description = raw.get("description")
                if isinstance(description, str) and len(description) <= 2400:
                    item["description"] = description
                subtotal = raw.get("subtotal_pence")
                if type(subtotal) is int and item["price_pence"] <= subtotal <= 100_000_000:
                    item["subtotal_pence"] = subtotal
                options = [r for r in rules if matches(r, item, prefs)]
                if not options:
                    continue
                rule = min(
                    options, key=lambda r: (r["budget_pence"] is None, r["budget_pence"], r["id"])
                )
                item.update(
                    search_id=rule["id"],
                    search_budget_pence=rule["budget_pence"],
                    catalog_consent=consent,
                    catalog_rule=rule_signature(rule),
                )
                if flow.record_observed_search(conn, uid, binding, item, prefs, now):
                    accepted += 1
            conn.execute(
                "UPDATE catalog_watches SET last_checked=?,last_changed=COALESCE(?,last_changed),rendered_count=COALESCE(?,rendered_count) WHERE consent=?",
                (now, changed, count, consent),
            )
            conn.execute(
                "DELETE FROM catalog_seen WHERE consent=? AND seen_at<?", (consent, now - 86400)
            )
        return jsonify(accepted=accepted, skipped=len(items) - accepted, matched=len(rules))
