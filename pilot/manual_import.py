"""Private, dashboard-only Vinted listings deliberately imported from visible browser UI."""
import hashlib
import json
import re
import secrets
import time
from flask import g, jsonify, render_template, request, session

from .db import get_db, settings_for, transaction
from .listing_metadata import vinted_photo_url

TTL = 90
MAX_ITEMS = 12
MAX_BODY = 24_000
URL = re.compile(r"https://(?:www\.)?vinted\.co\.uk/items/(\d{1,20})(?:-[\w%-]+)?/?\Z")


def init_manual_import(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS pilot_manual_previews(
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        digest TEXT NOT NULL, items TEXT NOT NULL, expires INTEGER NOT NULL)""")


def _digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _item(raw, captured, now):
    if not isinstance(raw, dict):
        return None
    url = raw.get("listing_url")
    match = URL.fullmatch(url) if isinstance(url, str) and len(url) <= 400 else None
    if not match or str(raw.get("id")) != match[1] or raw.get("currency") != "GBP":
        return None
    title, price, posted, evidence = (raw.get(k) for k in ("title", "price_pence", "listed_at", "age_text"))
    if (not isinstance(title, str) or not title.strip() or len(title) > 240 or
        type(price) is not int or not 0 < price <= 100_000_000 or type(posted) is not int or
        not posted <= captured <= now + 2 or not 0 <= now - posted < 180 or
        not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 64):
        return None
    item = {"id": match[1], "listing_url": "https://www.vinted.co.uk/items/"+match[1], "title": title.strip(),
            "currency": "GBP", "price_pence": price, "listed_at": posted, "age_text": evidence,
            "dashboard_only": True}
    photo = vinted_photo_url(raw.get("photo_url"))
    if photo:
        item["photo_url"] = photo
    seller = raw.get("seller_id")
    if isinstance(seller, str) and re.fullmatch(r"\d{1,20}", seller):
        item["seller_id"] = seller
    for field in ("category", "size", "condition", "brand", "model", "material"):
        val = raw.get(field)
        if isinstance(val, str) and 0 < len(val.strip()) <= 80:
            item[field] = val.strip()
    return item


def register_manual_import(app):
    @app.get("/import/vinted")
    def manual_review():
        return render_template("manual_review.html", username=g.user["username"], csrf_token=session.get("csrf_token"))

    @app.post("/api/imports/preview")
    def manual_preview():
        if not request.is_json or (request.content_length or 0) > MAX_BODY:
            return jsonify(error="The listing selection is too large or invalid."), 400
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Open the extension on your Vinted Home page or a listing."), 400
        now = int(time.time())
        captured, raw_items = data.get("observed_at"), data.get("items")
        if (type(captured) is not int or not -2 <= now - captured <= TTL or not isinstance(raw_items,list)
                or not 1 <= len(raw_items) <= MAX_ITEMS or data.get("source") not in ("home","listing")):
            return jsonify(error="This capture is invalid or expired. Check the Vinted tab again."), 400
        items = {}
        for raw in raw_items:
            item = _item(raw, captured, now)
            if item:
                items.setdefault(item["id"], item)
        if not items:
            return jsonify(error="No complete listing is confirmed under three minutes old."), 422
        token = secrets.token_urlsafe(24)
        conn = get_db()
        with transaction(conn):
            conn.execute("DELETE FROM pilot_manual_previews WHERE expires<=?", (now,))
            conn.execute("INSERT OR REPLACE INTO pilot_manual_previews VALUES(?,?,?,?)",
                         (g.user["id"], _digest(token), json.dumps(list(items.values())), now + TTL))
        skipped = sum(x for x in (data.get("missingAge"),data.get("incomplete"))
                      if type(x) is int and 0 <= x <= 1000) + len(raw_items) - len(items)
        profile_hint = data.get("profile_hint")
        return jsonify(token=token, items=list(items.values()), source=data["source"], skipped=skipped,
                       profile_hint=profile_hint if isinstance(profile_hint,str) and re.fullmatch(r"[\w.-]{1,80}", profile_hint) else None)

    @app.post("/api/imports/confirm")
    def manual_confirm():
        from .flow import record_manual_home_listing
        if not request.is_json or (request.content_length or 0) > 2500:
            return jsonify(error="Invalid import confirmation."), 400
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Invalid import confirmation."), 400
        token, selected = data.get("token"),data.get("selected")
        if (not isinstance(token,str) or not re.fullmatch(r"[a-zA-Z0-9_-]{32}",token) or
            not isinstance(selected,list) or not 1<=len(selected)<=MAX_ITEMS or
            not all(isinstance(x,str) and re.fullmatch(r"\d{1,20}",x) for x in selected)):
            return jsonify(error="Choose a listing from a current preview."), 400
        owner, now, conn = g.user["id"], time.time(), get_db()
        with transaction(conn):
            record = conn.execute("SELECT items FROM pilot_manual_previews WHERE user_id=? AND digest=? AND expires>?",
                                  (owner,_digest(token),int(now))).fetchone()
            if not record:
                return jsonify(error="The preview expired or was already used. Check the Vinted tab again."),409
            items = {i["id"]: i for i in json.loads(record[0])}
            if not set(selected).issubset(items):
                return jsonify(error="The selection differs from the preview."),400
            conn.execute("DELETE FROM pilot_manual_previews WHERE user_id=?", (owner,))
            accepted = 0
            prefs=settings_for(conn,owner)
            for id_ in dict.fromkeys(selected):
                item=items[id_]
                # Check age against trusted server time at confirmation as well as preview.
                if 0<=now - item["listed_at"]<180:
                    if record_manual_home_listing(conn,owner,item,prefs,now) is not None:
                        accepted+=1
        return jsonify(available=accepted, skipped=len(set(selected))-accepted)
