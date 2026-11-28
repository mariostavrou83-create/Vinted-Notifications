"""Look up delivery choices for the current buyer on the configured partner."""
import json
import re
import time

from flask import current_app, g, jsonify, request

from . import pickup
from .connection import linked_account
from .db import get_db, transaction
from .inputs import InputError
from .partner import PartnerError, partner_configured, partner_request

CHOICE_TTL = 24 * 3600
DELIVERY_FIELDS = {"delivery_mode", "pickup_id", "pickup_label", "home_address_ref", "preferred_courier",
                   "allowed_couriers", "allow_same_shop_fallback"}


def init_destinations(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS pilot_destination_choices (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        account_ref TEXT NOT NULL, mode TEXT NOT NULL, id TEXT NOT NULL,
        label TEXT NOT NULL, summary TEXT NOT NULL, couriers TEXT NOT NULL, observed INTEGER NOT NULL,
        PRIMARY KEY(user_id,account_ref,mode,id))""")
    with transaction(conn):
        columns = {r[1] for r in conn.execute("PRAGMA table_info(pilot_destination_choices)")}
        if "routes" not in columns:
            conn.execute("ALTER TABLE pilot_destination_choices ADD COLUMN routes TEXT NOT NULL DEFAULT '{}'")


def display(value, limit):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        return ""
    return value.strip()


def normalize(rows, mode):
    result = {}
    if not isinstance(rows, list):
        raise PartnerError("Invalid delivery choices.")
    for item in rows[:100]:
        if not isinstance(item, dict):
            continue
        key = display(item.get("id"), 180)
        label = display(item.get("label"), 160)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,179}", key) or not label or key in result:
            continue
        couriers = item.get("couriers", [])
        if not isinstance(couriers, list):
            continue
        names = [display(c, 80) for c in couriers[:35]]
        summary = display(item.get("summary"), 240)
        if mode == "home":
            # A saved address is represented by its nickname and outward postcode only.
            district = display(item.get("postcode_area"), 4).upper()
            summary = district if re.fullmatch(r"[A-Z]{1,2}[0-9][0-9A-Z]?", district) else ""
        result[key] = {"id": key, "label": label, "summary": summary,
                       "couriers": list(dict.fromkeys(c for c in names if c))}
        if len(result) >= 40:
            break
    return list(result.values())


def apply_selection(conn, uid, current, saved):
    account = linked_account(conn, uid)
    managed = current_app.config.get("MULTIPLE_ACCOUNTS")
    active_row = None
    for mode, field, label_field in (("pickup", "pickup_id", "pickup_label"),
                                     ("home", "home_address_ref", "home_address_label")):
        selected = saved.get(field, "")
        if not selected:
            saved[label_field] = ""
            if mode == "pickup":
                saved["pickup_summary"] = ""
            if saved["delivery_mode"] == mode:
                saved["delivery_couriers"] = []
            continue
        changed = selected != current.get(field, "")
        row = conn.execute("""SELECT label,summary,couriers,routes FROM pilot_destination_choices
                            WHERE user_id=? AND account_ref=? AND mode=? AND id=? AND observed>=?""",
                           (uid, account, mode, selected, int(time.time()) - CHOICE_TTL)).fetchone()
        if mode == saved["delivery_mode"]:
            active_row = row
        switched = managed and mode == saved["delivery_mode"] and current.get("delivery_mode") != mode
        if (changed or switched) and not row:
            raise InputError("Find and select a delivery location for your connected buying account.")
        if row:
            saved[label_field] = row["label"]
            if mode == "pickup":
                saved["pickup_summary"] = row["summary"]
            if saved["delivery_mode"] == mode:
                saved["delivery_couriers"] = json.loads(row["couriers"])
        else:
            # Other preferences remain editable when an older saved choice is offline.
            saved[label_field] = current.get(label_field, "")
            if current["delivery_mode"] != saved["delivery_mode"] and saved["delivery_mode"] == mode:
                saved["delivery_couriers"] = []
    if managed:
        if saved["delivery_mode"] == "home" and active_row:
            saved["delivery_account_ref"] = account
        pickup.save_policy(current=current, saved=saved, account=account, row=active_row)
        if saved["delivery_mode"] == "pickup":
            return saved
    if saved["delivery_mode"] == "home":
        saved["allow_same_shop_fallback"] = False
    mode_field = "pickup_id" if saved["delivery_mode"] == "pickup" else "home_address_ref"
    changed_choice = any(saved.get(key) != current.get(key)
                         for key in ("delivery_mode", mode_field, "preferred_courier", "allowed_couriers"))
    if changed_choice and saved.get("delivery_couriers"):
        supported = {name.casefold() for name in saved["delivery_couriers"]}
        requested = [saved["preferred_courier"], *saved["allowed_couriers"]]
        if any(name and name.casefold() not in supported for name in requested):
            raise InputError("Choose a courier offered for your selected delivery location.")
    return saved


def register_destinations(app):
    @app.post("/api/delivery/destinations")
    def destination_lookup():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get("mode") not in ("pickup", "home"):
            raise InputError("Choose pickup points or saved addresses.")
        if set(payload) - {"mode", "query"}:
            raise InputError("Invalid delivery search fields.")
        mode = payload["mode"]
        query = display(payload.get("query", ""), 80)
        if mode == "pickup" and (len(query) < 2 or not re.fullmatch(r"[\w .’'\-]+", query)):
            raise InputError("Enter a UK postcode or town.")
        if mode == "home" and "query" in payload:
            raise InputError("Saved addresses come from your connected buying account.")
        if not partner_configured():
            return jsonify(error="Delivery lookup is waiting for the buying integration."), 503
        conn, uid = get_db(), g.user["id"]
        account = linked_account(conn, uid)
        if not account:
            return jsonify(error="Connect your Vinted buying account to find delivery locations."), 409
        params = {"account_ref": account, "mode": mode, "country": "GB"}
        if mode == "pickup":
            params["query"] = query
        try:
            result = partner_request("POST", "/v1/delivery/destinations", params)
            if result.get("account_ref") != account:
                raise PartnerError("Invalid delivery account.")
            strict = mode == "pickup" and current_app.config.get("MULTIPLE_ACCOUNTS")
            rejected = set()
            rows = result.get("destinations")
            if strict and isinstance(rows, list):
                options, rejected = pickup.directory(rows, display)
            else:
                options = normalize(rows, mode)
        except PartnerError:
            return jsonify(error="Your delivery locations could not be verified. Try again later."), 502
        now = int(time.time())
        with transaction(conn):
            if linked_account(conn, uid) != account:
                return jsonify(error="Your buying account changed. Search again."), 409
            conn.execute("DELETE FROM pilot_destination_choices WHERE observed<?", (now - CHOICE_TTL,))
            if rejected:
                conn.executemany("DELETE FROM pilot_destination_choices WHERE user_id=? AND account_ref=? AND mode='pickup' AND id=?",
                                 ((uid, account, point) for point in rejected))
                prefs = conn.execute("SELECT data FROM settings WHERE user_id=?", (uid,)).fetchone()
                if prefs:
                    saved = json.loads(prefs[0])
                    if saved.get("delivery_account_ref") == account and saved.get("pickup_id") in rejected:
                        saved["pickup_routes"] = {}
                        if saved.get("delivery_mode") == "pickup":
                            saved.update(delivery_couriers=[], allowed_couriers=[], allow_same_shop_fallback=False)
                        conn.execute("UPDATE settings SET data=? WHERE user_id=?", (json.dumps(saved), uid))
            conn.executemany("""INSERT INTO pilot_destination_choices(user_id,account_ref,mode,id,label,summary,couriers,observed,routes)
                            VALUES(?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(user_id,account_ref,mode,id) DO UPDATE SET label=excluded.label,
                            summary=excluded.summary,couriers=excluded.couriers,observed=excluded.observed,routes=excluded.routes""",
                             ((uid, account, mode, item["id"], item["label"], item["summary"],
                               json.dumps(item["couriers"]), now, json.dumps(item.get("routes", {}))) for item in options))
        return jsonify(destinations=[{k:v for k,v in item.items() if k != "routes"} for item in options])
