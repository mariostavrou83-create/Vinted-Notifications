"""A private Telegram tap delegates one bounded order to its owner's paired Chrome."""

import hashlib
import hmac
import json
import re
import secrets
import time
import unicodedata

from flask import g, jsonify, request

from . import checkout_profit
from .db import get_db, settings_for, transaction
from .flow import check_quote, quote_summary
from .inputs import InputError

DEVICE_ENDPOINTS = {
    "native_home",
    "native_home_watch",
    "native_preview",
    "native_pair",
    "native_next",
    "native_result",
    "native_device_disconnect",
}
USERNAME = re.compile(r"[A-Za-z0-9_.-]{1,80}")
POSTCODE = re.compile(r"(?:GIR0AA|[A-Z]{1,2}[0-9][A-Z0-9]?[0-9][A-Z]{2})")
TOKEN = re.compile(r"[A-Za-z0-9_-]{32,100}")
DEFAULTS = {
    "delivery_mode": "pickup",
    "location_label": "",
    "postcode": "",
    "preferred_courier": "",
    "allowed_couriers": [],
    "allow_same_shop_fallback": False,
    "payment_label": "",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS native_devices (
 user_id INTEGER PRIMARY KEY REFERENCES users(id), binding TEXT NOT NULL UNIQUE,
 token_hash TEXT NOT NULL UNIQUE, username TEXT COLLATE NOCASE NOT NULL UNIQUE, expires REAL NOT NULL,
 last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS native_pair_codes (
 user_id INTEGER PRIMARY KEY REFERENCES users(id), code_hash TEXT NOT NULL UNIQUE,
 username TEXT NOT NULL, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS native_settings (user_id INTEGER PRIMARY KEY REFERENCES users(id), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS native_orders (
 alert_id INTEGER PRIMARY KEY REFERENCES pilot_alerts(id), user_id INTEGER NOT NULL REFERENCES users(id),
 chat_id INTEGER NOT NULL, username TEXT COLLATE NOCASE NOT NULL, binding TEXT NOT NULL, listing_id TEXT NOT NULL,
 stage TEXT NOT NULL, expires REAL NOT NULL, task_hash TEXT, prefs TEXT NOT NULL, quote TEXT,
 confirmation TEXT, order_ref TEXT, UNIQUE(username,listing_id)
);
CREATE INDEX IF NOT EXISTS native_order_tasks ON native_orders(binding,stage,expires);
CREATE INDEX IF NOT EXISTS native_order_deadlines ON native_orders(expires)
 WHERE stage IN ('await_quote','reading','confirm','await_commit','issued');
CREATE TABLE IF NOT EXISTS native_notifications (
 id INTEGER PRIMARY KEY, alert_id INTEGER NOT NULL REFERENCES native_orders(alert_id),
 text TEXT NOT NULL, keyboard TEXT, delivered INTEGER NOT NULL DEFAULT 0
);
"""


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _label(value, max_length=100):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_length
        or any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in value)
    ):
        raise InputError("A short visible checkout label is required.")
    return " ".join(value.split())


def _payment(value):
    label = _label(value, 70)
    if label.casefold() == "vinted balance":
        return label
    if re.fullmatch(
        r"(?:visa|mastercard|american express|amex|maestro)(?: (?:debit|credit))?"
        r"\s+(?:[·•●*.x-]{2,8}\s*|ending (?:in )?)[0-9]{4}",
        label,
        re.IGNORECASE,
    ):
        return label
    raise InputError("Use the displayed payment label, such as Visa •••• 1234 or Vinted balance.")


def _payment_identity(label):
    """Compare the displayed brand and last four independently of masking punctuation."""
    if label.casefold() == "vinted balance":
        return ("vinted balance", None)
    brand = re.match(r"visa|mastercard|american express|amex|maestro", label, re.IGNORECASE)[
        0
    ].lower()
    return ("amex" if brand == "american express" else brand, label[-4:])


def _native_settings(conn, uid):
    row = conn.execute("SELECT data FROM native_settings WHERE user_id=?", (uid,)).fetchone()
    return json.loads(row["data"]) if row else dict(DEFAULTS)


def _prefs(conn, uid):
    selected = _native_settings(conn, uid)
    general = settings_for(conn, uid)
    key = (
        "gb:"
        + selected["postcode"].replace(" ", "").lower()
        + ":"
        + selected["location_label"].casefold()
    )
    return {
        **selected,
        "pickup_id": key if selected["delivery_mode"] == "pickup" else "",
        "pickup_label": selected["location_label"] if selected["delivery_mode"] == "pickup" else "",
        "home_address_ref": key if selected["delivery_mode"] == "home" else "",
        **{k: general.get(k) for k in ("max_item_price", "max_shipping", "max_total")},
    }


def capability(conn, uid):
    device = conn.execute(
        "SELECT binding FROM native_devices WHERE user_id=? AND expires>?", (uid, time.time())
    ).fetchone()
    if device and _native_settings(conn, uid)["payment_label"]:
        return device["binding"]
    return None


def _end(conn, order, stage, text, keyboard=None):
    conn.execute(
        "UPDATE native_orders SET stage=?,task_hash=NULL WHERE alert_id=?",
        (stage, order["alert_id"]),
    )
    conn.execute(
        "UPDATE pilot_alerts SET order_status=?,updated=? WHERE id=?",
        (stage, int(time.time()), order["alert_id"]),
    )
    # A superseded confirmation is no longer actionable, even when notification delivery was deferred.
    conn.execute(
        "DELETE FROM native_notifications WHERE alert_id=? AND keyboard IS NOT NULL",
        (order["alert_id"],),
    )
    item_url = f"https://www.vinted.co.uk/items/{order['listing_id']}"
    text = f"{text}\n@{order['username']} · {item_url}"
    conn.execute(
        "INSERT INTO native_notifications(alert_id,text,keyboard) VALUES(?,?,?)",
        (order["alert_id"], text, json.dumps(keyboard) if keyboard else None),
    )


def _revoke(conn, uid, *, preserve_settings=False):
    for order in conn.execute(
        """SELECT * FROM native_orders WHERE user_id=? AND stage IN
           ('await_quote','reading','confirm','await_commit','issued')""",
        (uid,),
    ).fetchall():
        if order["stage"] == "issued":
            _end(
                conn,
                order,
                "unknown",
                "Chrome disconnected after receiving the order. Check Purchases in Vinted.",
            )
        else:
            _end(conn, order, "blocked", "Chrome disconnected before the order was authorised.")
    device = conn.execute("SELECT binding FROM native_devices WHERE user_id=?", (uid,)).fetchone()
    if device:
        conn.execute(
            "UPDATE pilot_home_watch SET enabled=0 WHERE user_id=? AND binding=?",
            (uid, device["binding"]),
        )
        cancel_home(conn, uid, device["binding"])
    if not preserve_settings:
        conn.execute("DELETE FROM native_settings WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM native_devices WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM native_pair_codes WHERE user_id=?", (uid,))


def _source_current(conn, alert, device):
    if alert["native_binding"] != device["binding"]:
        return False
    if alert["origin"] == "public_search":
        return alert["account_ref"] == "public"
    if alert["origin"] != "browser_home" or alert["account_ref"] != "home:" + device["binding"]:
        return False
    return bool(
        conn.execute(
            """SELECT 1 FROM pilot_home_watch
           WHERE user_id=? AND binding=? AND username=? COLLATE NOCASE AND enabled=1""",
            (device["user_id"], device["binding"], device["username"]),
        ).fetchone()
    )


def cancel_home(conn, uid, binding):
    """Withdraw unissued Home instructions and old buttons, including after watching resumes."""
    account = "home:" + binding
    pending = conn.execute(
        """SELECT o.* FROM native_orders o JOIN pilot_alerts a ON a.id=o.alert_id
           WHERE o.user_id=? AND o.binding=? AND a.origin='browser_home' AND a.account_ref=?
             AND o.stage IN ('await_quote','reading','confirm','await_commit')""",
        (uid, binding, account),
    ).fetchall()
    for order in pending:
        _end(conn, order, "blocked", "Home watching stopped before this purchase was authorised.")
    conn.execute(
        """UPDATE pilot_alerts SET native_binding=NULL,
                delivery=CASE WHEN delivery='new' THEN 'dashboard' ELSE delivery END
                WHERE user_id=? AND origin='browser_home' AND account_ref=?""",
        (uid, account),
    )


def request_native(conn, uid, aid, confirmation=None):
    """Called only for a validated callback on the owner's delivered Telegram alert."""
    now = time.time()
    with transaction(conn):
        row = conn.execute(
            """SELECT a.*, d.username, d.binding FROM pilot_alerts a
            JOIN native_devices d ON d.user_id=a.user_id AND d.expires>?
            JOIN pilot_chats c ON c.user_id=a.user_id AND c.chat_id=a.sent_chat
            WHERE a.id=? AND a.user_id=? AND a.origin IN ('public_search', 'browser_home')
              AND a.native_binding=d.binding AND a.message_id IS NOT NULL""",
            (now, aid, uid),
        ).fetchone()
        if not row or not _source_current(conn, row, row):
            return (
                "This alert is not paired with the active buying profile. Open the listing in Vinted.",
                None,
            )
        p = _prefs(conn, uid)
        if not p["payment_label"]:
            return "Complete your Chrome checkout preferences first.", None
        order = conn.execute(
            "SELECT * FROM native_orders WHERE username=? AND listing_id=?",
            (row["username"], row["listing_id"]),
        ).fetchone()
        if order:
            if (
                order["user_id"] != uid
                or order["alert_id"] != aid
                or order["binding"] != row["binding"]
                or order["stage"] != "confirm"
                or not confirmation
            ):
                return (
                    "A purchase attempt already exists for this listing. Check the dashboard.",
                    None,
                )
            if not hmac.compare_digest(confirmation, order["confirmation"] or ""):
                return "This checkout confirmation is invalid.", None
            if now >= order["expires"] or p != json.loads(order["prefs"]):
                _end(
                    conn,
                    order,
                    "blocked",
                    "The quote expired or your preferences changed. Open the listing in Vinted.",
                )
                return "The previous quote is no longer valid.", None
            conn.execute(
                "UPDATE native_orders SET stage='await_commit' WHERE alert_id=?",
                (aid,),
            )
            conn.execute(
                "DELETE FROM native_notifications WHERE alert_id=? AND keyboard IS NOT NULL", (aid,)
            )
            conn.execute("UPDATE pilot_alerts SET order_status='checking' WHERE id=?", (aid,))
            return "Order confirmed. Your Chrome extension will recheck the exact order.", None
        if confirmation or row["order_status"] is not None:
            return "This alert no longer has an available purchase.", None
        conn.execute(
            """INSERT INTO native_orders(alert_id,user_id,chat_id,username,binding,listing_id,stage,expires,prefs)
                     VALUES(?,?,?,?,?,?,'await_quote',?,?)""",
            (
                aid,
                uid,
                row["sent_chat"],
                row["username"],
                row["binding"],
                row["listing_id"],
                now + 120,
                json.dumps(p),
            ),
        )
        conn.execute(
            "UPDATE pilot_alerts SET order_status='checking',updated=? WHERE id=?", (now, aid)
        )
    return "Your paired Chrome will check the listing and your saved preferences.", None


def _expire(conn, now):
    for order in conn.execute(
        """SELECT * FROM native_orders WHERE expires<=?
        AND stage IN ('await_quote','reading','confirm','await_commit','issued') LIMIT 20""",
        (now,),
    ).fetchall():
        if order["stage"] == "issued":
            _end(
                conn,
                order,
                "unknown",
                "Chrome received the purchase instruction. Check Vinted Purchases for the result.",
            )
        else:
            _end(
                conn,
                order,
                "expired",
                "Chrome did not confirm an eligible checkout before the order request expired.",
            )


def notify(conn, now):
    from .flow_worker import telegram

    with transaction(conn):
        _expire(conn, now)
        item = conn.execute(
            """SELECT n.*, o.chat_id, o.user_id FROM native_notifications n JOIN native_orders o
            ON o.alert_id=n.alert_id JOIN pilot_chats c ON c.user_id=o.user_id AND c.chat_id=o.chat_id
            WHERE n.delivered<=? ORDER BY n.id LIMIT 1""",
            (int(now) - 30,),
        ).fetchone()
        if not item:
            return False
        conn.execute(
            "UPDATE native_notifications SET delivered=? WHERE id=?", (int(now), item["id"])
        )
    data = {
        "chat_id": item["chat_id"],
        "text": item["text"],
        "link_preview_options": {"is_disabled": True},
    }
    valuation, checkout_cost = checkout_profit.for_alert(
        conn, item["user_id"], item["alert_id"], now
    )
    profit = checkout_profit.profit_line(valuation)
    if profit and checkout_cost:
        data["text"] += "\n" + profit + "\n" + checkout_cost["note"]
    if item["keyboard"]:
        data["reply_markup"] = json.loads(item["keyboard"])
    telegram("sendMessage", data)
    conn.execute("DELETE FROM native_notifications WHERE id=?", (item["id"],))
    return True


def _body():
    if not request.is_json or (request.content_length or 0) > 8192:
        raise InputError("A small JSON request is required.")
    obj = request.get_json(silent=True)
    if not isinstance(obj, dict):
        raise InputError("Invalid request.")
    return obj


def _device(conn):
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme != "Bearer" or not TOKEN.fullmatch(token):
        return None
    return conn.execute(
        "SELECT * FROM native_devices WHERE token_hash=? AND expires>?", (_hash(token), time.time())
    ).fetchone()


def device_home_context(conn):
    """Authenticate a Home report within the caller's write transaction."""
    device = _device(conn)
    if not device:
        return None
    report = request.get_json(silent=True)
    username = report.get("username") if isinstance(report, dict) else None
    if not isinstance(username, str) or username.casefold() != device["username"].casefold():
        _revoke(conn, device["user_id"])
        return None
    return {field: device[field] for field in ("user_id", "username", "binding")}


def _quote(body, order, alert, p, now):
    """Whitelisted fields only. The device checks the same bounds again immediately before clicking."""
    data = body.get("quote")
    if not isinstance(data, dict) or str(data.get("listing_id", "")) != order["listing_id"]:
        return None, "blocked", "The listing identity could not be verified."
    fields = (
        "currency",
        "item_pence",
        "shipping_pence",
        "total_pence",
        "buyer_fee_pence",
        "delivery_mode",
    )
    q = {k: data.get(k) for k in fields}
    if any(type(q[k]) is not int or not 0 <= q[k] <= 100_000_000 for k in fields[1:5]):
        return None, "blocked", "The checkout amount could not be verified."
    if q["total_pence"] != q["item_pence"] + q["shipping_pence"] + q["buyer_fee_pence"]:
        return (
            None,
            "blocked",
            "The visible item, shipping and fee do not add up to the checkout total.",
        )
    try:
        q.update(
            courier=_label(data.get("courier"), 60),
            payment_label=_payment(data.get("payment_label")),
        )
        keyfield = "pickup_id" if q["delivery_mode"] == "pickup" else "home_address_ref"
        key = _label(data.get(keyfield), 180)
        code = re.fullmatch(r"gb:([a-z0-9]{5,7}):(.+)", key)
        if not code or not POSTCODE.fullmatch(code[1].upper()):
            raise InputError("Missing delivery location.")
        q.update(pickup_id="", pickup_label="", home_address_ref="")
        q[keyfield] = key
        if q["delivery_mode"] == "pickup":
            q["pickup_label"] = _label(data.get("pickup_label"), 150)
            if code[2] not in q["pickup_label"].casefold():
                raise InputError("Unverified shop label.")
    except InputError:
        return (
            None,
            "blocked",
            "The displayed destination or payment details could not be verified.",
        )
    if _payment_identity(q["payment_label"]) != _payment_identity(p["payment_label"]):
        return None, "blocked", "The displayed payment method differs from your saved preference."
    q.update(
        account_ref=alert["account_ref"],
        listing_id=order["listing_id"],
        quote_id=secrets.token_hex(16),
        expires_at=now + 120,
    )
    decision, reason = check_quote(q, alert, p, now)
    if decision == "blocked":
        return q, decision, reason
    listing = json.loads(alert["listing"])
    subtotal = listing.get("subtotal_pence")
    if type(subtotal) is int and q["item_pence"] + q["buyer_fee_pence"] > subtotal:
        return q, "blocked", "The item and buyer fee exceed the amount in your alert."
    if (
        decision == "ok"
        and not p["max_total"]
        and type(listing.get("search_budget_pence")) is not int
    ) and (type(subtotal) is not int or not p["max_shipping"]):
        return q, "confirm", "Review the full checkout total before buying."
    return q, decision, reason


def _issue_task(db, order, alert, preferences, device, now):
    commit = order["stage"] == "await_commit"
    token = secrets.token_urlsafe(30)
    quote = json.loads(order["quote"]) if commit else None
    deadline = min(order["expires"], now + (20 if commit else 90))
    if commit:
        quote["commit_deadline"] = deadline
    db.execute(
        "UPDATE native_orders SET stage=?,task_hash=?,expires=?,quote=COALESCE(?,quote) WHERE alert_id=?",
        (
            "issued" if commit else "reading",
            _hash(token),
            deadline + 45 if commit else deadline,
            json.dumps(quote) if commit else None,
            order["alert_id"],
        ),
    )
    if commit:
        db.execute(
            "UPDATE pilot_alerts SET order_status='native_submitting' WHERE id=?",
            (order["alert_id"],),
        )
    listing = json.loads(alert["listing"])
    # A confirmation token is written only for a quote requiring explicit approval.
    # Its state can reach await_commit only via the validating Telegram callback.
    confirmed = bool(commit and order["confirmation"])
    return jsonify(
        task={
            "stage": "commit" if commit else "quote",
            "terms_confirmed": confirmed,
            "pickup_change_confirmed": bool(
                confirmed
                and quote["delivery_mode"] == "pickup"
                and quote["pickup_id"] != preferences["pickup_id"]
            ),
            "alert_limits": {
                "item_pence": listing["price_pence"],
                "subtotal_pence": listing.get("subtotal_pence"),
                "total_pence": listing.get("search_budget_pence"),
            },
            "id": order["alert_id"],
            "task_token": token,
            "listing_id": order["listing_id"],
            "listing_url": listing["listing_url"],
            "vinted_username": device["username"],
            "preferences": preferences,
            "quote": quote,
            "deadline": deadline,
        }
    )


def _accept_quote(db, order, alert, preferences, body, now):
    quote, decision, reason = _quote(body, order, alert, preferences, now)
    if decision == "blocked":
        _end(db, order, "blocked", reason)
    else:
        confirmation = secrets.token_hex(6) if decision == "confirm" else None
        if confirmation:
            location_key = quote["pickup_id"] or quote["home_address_ref"]
            postcode = location_key.split(":")[1].upper()
            keyboard = {
                "inline_keyboard": [
                    [
                        {
                            "text": "Confirm order and buy",
                            "callback_data": f"ncf:{order['alert_id']}:{confirmation}",
                        }
                    ]
                ]
            }
            notice = (
                reason
                + "\n"
                + quote_summary(quote)
                + " · "
                + postcode
                + "\nItem £"
                + f"{quote['item_pence']/100:.2f}"
                + " · Buyer fee £"
                + f"{quote['buyer_fee_pence']/100:.2f}"
            )
            _end(db, order, "confirm", notice, keyboard)
        db.execute(
            "UPDATE native_orders SET stage=?,expires=?,task_hash=NULL,quote=?,confirmation=? WHERE alert_id=?",
            (
                "confirm" if confirmation else "await_commit",
                quote["expires_at"],
                json.dumps(quote),
                confirmation,
                order["alert_id"],
            ),
        )
    return jsonify(ok=True)


def _record_outcome(db, order, body, kind):
    if kind == "blocked" and order["stage"] in ("reading", "issued"):
        _end(
            db,
            order,
            "blocked",
            "Chrome stopped before placing the order. Review the listing in Vinted.",
        )
    elif kind == "action_required" and order["stage"] in ("reading", "issued"):
        _end(
            db,
            order,
            "action_required",
            "Open Vinted in Chrome to complete the requested check. Review Purchases before paying.",
        )
    elif order["stage"] == "issued" and kind in ("unknown", "purchased"):
        receipt = body.get("order_id") or body.get("reference")
        if (
            kind == "purchased"
            and isinstance(receipt, str)
            and re.fullmatch(r"#?[A-Za-z0-9_-]{2,100}", receipt)
        ):
            db.execute(
                "UPDATE native_orders SET order_ref=? WHERE alert_id=?",
                (receipt, order["alert_id"]),
            )
            _end(
                db,
                order,
                "purchased",
                "Purchased in Vinted · " + quote_summary(json.loads(order["quote"])),
            )
        else:
            _end(
                db,
                order,
                "unknown",
                "Purchase outcome is unconfirmed. Check Vinted Purchases before taking action.",
            )
    else:
        return jsonify(error="This task cannot accept that result."), 409
    return jsonify(ok=True)


def init_native(app):
    get_db().executescript(SCHEMA)

    @app.get("/api/native/status")
    def native_status():
        db, uid = get_db(), g.user["id"]
        row = db.execute(
            "SELECT * FROM native_devices WHERE user_id=? AND expires>?", (uid, time.time())
        ).fetchone()
        return jsonify(
            paired=bool(row),
            vinted_username=row["username"] if row else None,
            last_seen=row["last_seen"] if row else None,
            online=bool(row and row["last_seen"] >= time.time() - 90),
            ready=bool(capability(db, uid)),
            preferences=_native_settings(db, uid),
        )

    @app.post("/api/native/settings")
    def set_native_settings():
        b = _body()
        mode = b.get("delivery_mode")
        if mode not in ("pickup", "home"):
            raise InputError("Choose pickup or home delivery.")
        postcode = re.sub(r"\s+", "", _label(b.get("postcode"), 12)).upper()
        if not POSTCODE.fullmatch(postcode):
            raise InputError("Enter the full UK postcode shown at checkout.")
        label = _label(b.get("location_label"))
        if ":" in label:
            raise InputError("Enter the delivery nickname or shop name.")
        alt = b.get("allowed_couriers", [])
        if (
            not isinstance(alt, list)
            or len(alt) > 10
            or type(b.get("allow_same_shop_fallback", False)) is not bool
        ):
            raise InputError("Choose your approved couriers.")
        p = {
            "delivery_mode": mode,
            "location_label": label,
            "postcode": postcode,
            "preferred_courier": _label(b.get("preferred_courier"), 60),
            "allowed_couriers": list(dict.fromkeys(_label(x, 60) for x in alt)),
            "allow_same_shop_fallback": mode == "pickup"
            and b.get("allow_same_shop_fallback", False),
            "payment_label": _payment(b.get("payment_label")),
        }
        get_db().execute(
            "INSERT OR REPLACE INTO native_settings VALUES(?,?)", (g.user["id"], json.dumps(p))
        )
        return jsonify(preferences=p)

    @app.post("/api/native/pair-code")
    def native_pair_code():
        name = _body().get("vinted_username")
        if not isinstance(name, str) or not USERNAME.fullmatch(name):
            raise InputError("Enter the exact Vinted buying username.")
        db, uid, code, expires = (
            get_db(),
            g.user["id"],
            secrets.token_urlsafe(27),
            time.time() + 600,
        )
        with transaction(db):
            previous = db.execute(
                "SELECT username FROM native_devices WHERE user_id=?", (uid,)
            ).fetchone()
            _revoke(
                db,
                uid,
                preserve_settings=not previous
                or previous["username"].casefold() == name.casefold(),
            )
            db.execute(
                "INSERT INTO native_pair_codes VALUES(?,?,?,?)", (uid, _hash(code), name, expires)
            )
        return jsonify(
            code=code, expires_at=expires, pilot_username=g.user["username"], vinted_username=name
        )

    @app.post("/api/native/disconnect")
    def native_disconnect():
        with transaction(get_db()):
            _revoke(get_db(), g.user["id"])
        return jsonify(paired=False)

    @app.post("/native/device/preview", endpoint="native_preview")
    @app.post("/native/device/pair", endpoint="native_pair")
    def device_pair():
        b, db = _body(), get_db()
        code = b.get("code")
        if not isinstance(code, str) or not TOKEN.fullmatch(code):
            return jsonify(error="Invalid pairing code."), 403
        with transaction(db):
            row = db.execute(
                """SELECT p.*,u.username pilot_username FROM native_pair_codes p
                JOIN users u ON u.id=p.user_id WHERE p.code_hash=? AND p.expires>?""",
                (_hash(code), time.time()),
            ).fetchone()
            if not row:
                return jsonify(error="The pairing code has expired."), 403
            names = {"vinted_username": row["username"], "pilot_username": row["pilot_username"]}
            if request.endpoint == "native_preview":
                return jsonify(**names, expires_at=row["expires"])
            if not all(
                isinstance(b.get(k), str) and b[k].casefold() == v.casefold()
                for k, v in names.items()
            ):
                return (
                    jsonify(error="The visible buying profile or Finder account does not match."),
                    403,
                )
            existing = db.execute(
                "SELECT * FROM native_devices WHERE username=? AND user_id!=?",
                (row["username"], row["user_id"]),
            ).fetchone()
            if existing:
                if existing["expires"] > time.time():
                    return (
                        jsonify(error="This buying profile already has a paired Finder account."),
                        409,
                    )
                _revoke(db, existing["user_id"])
            token = secrets.token_urlsafe(40)
            db.execute(
                "INSERT INTO native_devices VALUES(?,?,?,?,?,?)",
                (
                    row["user_id"],
                    secrets.token_hex(20),
                    _hash(token),
                    row["username"],
                    time.time() + 30 * 86400,
                    time.time(),
                ),
            )
            db.execute("DELETE FROM native_pair_codes WHERE user_id=?", (row["user_id"],))
            return jsonify(**names, token=token)

    @app.post("/native/device/disconnect", endpoint="native_device_disconnect")
    def device_disconnect():
        db = get_db()
        with transaction(db):
            d = _device(db)
            if not d:
                return jsonify(error="Pair again in Finder."), 401
            _revoke(db, d["user_id"])
        return jsonify(paired=False)

    @app.post("/native/device/next", endpoint="native_next")
    @app.post("/native/device/result", endpoint="native_result")
    def device_work():
        b, db, now = _body(), get_db(), time.time()
        with transaction(db):
            d = _device(db)
            if not d:
                return jsonify(error="Pair again in Finder."), 401
            if str(b.get("vinted_username", "")).casefold() != d["username"].casefold():
                _revoke(db, d["user_id"])
                return jsonify(error="Buying profile changed. Pair again."), 403
            db.execute("UPDATE native_devices SET last_seen=? WHERE user_id=?", (now, d["user_id"]))
            _expire(db, now)
            if request.endpoint == "native_next":
                row = db.execute(
                    """SELECT * FROM native_orders WHERE binding=? AND stage IN ('await_quote','await_commit')
                    AND expires>? ORDER BY expires LIMIT 1""",
                    (d["binding"], now),
                ).fetchone()
                if not row:
                    return jsonify(task=None)
            else:
                key = b.get("task_token")
                if not isinstance(key, str) or not TOKEN.fullmatch(key):
                    return jsonify(error="Invalid task."), 403
                row = db.execute(
                    "SELECT * FROM native_orders WHERE binding=? AND task_hash=? AND expires>?",
                    (d["binding"], _hash(key), now),
                ).fetchone()
                if not row or str(b.get("listing_id", "")) != row["listing_id"]:
                    return jsonify(error="This task has expired or already completed."), 409
            alert = db.execute(
                "SELECT * FROM pilot_alerts WHERE id=? AND user_id=?",
                (row["alert_id"], d["user_id"]),
            ).fetchone()
            linked = db.execute(
                "SELECT 1 FROM pilot_chats WHERE user_id=? AND chat_id=?",
                (d["user_id"], row["chat_id"]),
            ).fetchone()
            prefs = _prefs(db, d["user_id"])
            if row["stage"] != "issued" and (
                not linked or prefs != json.loads(row["prefs"]) or not _source_current(db, alert, d)
            ):
                _end(
                    db,
                    row,
                    "blocked",
                    "Your connection, Home watch or checkout preferences changed. Review this listing in Vinted.",
                )
                return jsonify(task=None, error="Preferences changed."), 409
            if request.endpoint == "native_next":
                return _issue_task(db, row, alert, prefs, d, now)
            kind = b.get("kind")
            if row["stage"] == "reading" and kind == "quote":
                return _accept_quote(db, row, alert, prefs, b, now)
            return _record_outcome(db, row, b, kind)
