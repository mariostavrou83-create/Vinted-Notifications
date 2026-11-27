"""Look up delivery choices for the current buyer on the configured partner."""
import json
import re
import time

from flask import g, jsonify, request

from .connection import linked_account
from .db import get_db, transaction
from .inputs import InputError
from .partner import PartnerError, partner_configured, partner_request

CHOICE_TTL = 24 * 3600


def init_destinations(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS pilot_destination_choices (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        account_ref TEXT NOT NULL, mode TEXT NOT NULL, id TEXT NOT NULL,
        label TEXT NOT NULL, summary TEXT NOT NULL, couriers TEXT NOT NULL, observed INTEGER NOT NULL,
        PRIMARY KEY(user_id,account_ref,mode,id))""")


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
        row = conn.execute("""SELECT label,summary,couriers FROM pilot_destination_choices
                            WHERE user_id=? AND account_ref=? AND mode=? AND id=? AND observed>=?""",
                           (uid, account, mode, selected, int(time.time()) - CHOICE_TTL)).fetchone()
        if changed and not row:
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
            options = normalize(result.get("destinations"), mode)
        except PartnerError:
            return jsonify(error="Your delivery locations could not be verified. Try again later."), 502
        now = int(time.time())
        with transaction(conn):
            if linked_account(conn, uid) != account:
                return jsonify(error="Your buying account changed. Search again."), 409
            conn.execute("DELETE FROM pilot_destination_choices WHERE observed<?", (now - CHOICE_TTL,))
            conn.executemany("""INSERT INTO pilot_destination_choices VALUES(?,?,?,?,?,?,?,?)
                            ON CONFLICT(user_id,account_ref,mode,id) DO UPDATE SET label=excluded.label,
                            summary=excluded.summary,couriers=excluded.couriers,observed=excluded.observed""",
                             ((uid, account, mode, item["id"], item["label"], item["summary"],
                               json.dumps(item["couriers"]), now) for item in options))
        return jsonify(destinations=options)
