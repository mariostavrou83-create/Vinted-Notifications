"""Tenant-bound listing delivery and one-attempt purchases via a trusted adapter."""

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask import current_app, g, jsonify, request

from . import checkout_profit, home_filters, pickup
from .inputs import InputError
from .db import get_db, settings_for, transaction
from .partner import PartnerError, partner_request
from .listing_metadata import display_fields, vinted_photo_url

SCHEMA = """
CREATE TABLE IF NOT EXISTS pilot_chats (
 user_id INTEGER PRIMARY KEY REFERENCES users(id), chat_id INTEGER UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_link_codes (
 code_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE REFERENCES users(id), expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_alerts (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), account_ref TEXT NOT NULL,
 listing_id TEXT NOT NULL, listing TEXT NOT NULL, source TEXT NOT NULL, origin TEXT NOT NULL DEFAULT 'connected', native_binding TEXT, created_at INTEGER NOT NULL,
 sent_at REAL, valued_at REAL, valuation TEXT, price_after INTEGER NOT NULL DEFAULT 0, price_attempts INTEGER NOT NULL DEFAULT 0,
 deliver_after INTEGER NOT NULL DEFAULT 0, message_id INTEGER, sent_chat INTEGER, delivery TEXT NOT NULL DEFAULT 'new',
 order_status TEXT, check_token TEXT, attempt_key TEXT NOT NULL UNIQUE, offer TEXT, confirmation TEXT, updated INTEGER NOT NULL,
 UNIQUE(user_id, account_ref, listing_id)
);
CREATE INDEX IF NOT EXISTS pilot_pending ON pilot_alerts(delivery,deliver_after);
CREATE TABLE IF NOT EXISTS pilot_sources (
 user_id INTEGER NOT NULL, source TEXT NOT NULL, next_at REAL NOT NULL DEFAULT 0, cursor TEXT,
 fingerprint TEXT, lease TEXT, last_ok REAL, PRIMARY KEY(user_id,source)
);
"""


def tg_config(key):
    # Never pick up the owner's existing live bot from unscoped environment variables.
    from .team import saved_telegram_setting

    return (
        current_app.config.get("PILOT_" + key)
        or os.getenv("FINDER_PILOT_" + key)
        or saved_telegram_setting(key)
    )


def telegram_linked(conn, user_id):
    return bool(conn.execute("SELECT 1 FROM pilot_chats WHERE user_id=?", (user_id,)).fetchone())


def alerts_for_user(conn, user_id):
    rows = checkout_profit.alert_rows(conn, user_id)
    now = time.time()
    answer = []
    search_names = dict(conn.execute("SELECT id,name FROM searches WHERE user_id=?", (user_id,)))
    managed = current_app.config.get("MULTIPLE_ACCOUNTS")
    if managed:
        from .connection import linked_account
        account = linked_account(conn, user_id)
    for r in rows:
        if managed and (r["origin"] != "connected" or r["account_ref"] != account):
            continue
        l = json.loads(r["listing"])
        if r["source"] == "search" and not l.get("search_name") and type(l.get("search_id")) is int:
            l["search_name"] = search_names.get(l["search_id"])
        checkout = checkout_profit.checkout_at(r, now)
        answer.append(
            {
                **display_fields(l, r["origin"], r["created_at"]),
                "id": r["id"],
                "listing_id": r["listing_id"],
                "title": l["title"],
                "listing_url": l["listing_url"],
                "source": r["source"],
                "origin": r["origin"],
                "checkout_mode": (
                    "autobuy"
                    if r["origin"] == "connected"
                    else ("native" if r["native_binding"] else "vinted")
                ),
                "subtotal_pence": l.get("subtotal_pence"),
                "item_price_pence": l["price_pence"],
                "total_pence": l.get("total_pence") if not l.get("total_excludes_shipping") else None,
                "created_at": r["created_at"],
                "order_status": r["order_status"],
                "checkout_cost": checkout,
            }
        )
    return answer


def listing_time(value):
    if type(value) in (int, float):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.timestamp() if parsed.tzinfo else None
        except (ValueError, OverflowError):
            pass
    return None


def quiet(settings, now):
    clock = datetime.fromtimestamp(now, ZoneInfo("Europe/London")).strftime("%H:%M")
    start, end = settings.get("quiet_start", ""), settings.get("quiet_end", "")
    if not start or not end or start == end:
        return False
    return start <= clock < end if start < end else clock >= start or clock < end


def _matches_choice(values, actual):
    return not values or str(actual or "").casefold() in {str(v).casefold() for v in values}


def home_matches(settings, item):
    if current_app.config.get("MULTIPLE_ACCOUNTS"):
        return home_filters.matches(settings, item)
    return (_matches_choice(settings.get("categories"), item.get("category"))
            and _matches_choice(settings.get("sizes"), item.get("size")))


def valid_listing(item, *, allow_missing_total=False):
    if (
        not isinstance(item, dict)
        or not str(item.get("id", "")).isdigit()
        or not isinstance(item.get("title"), str)
    ):
        return False
    try:
        url = urlsplit(str(item.get("listing_url", "")))
    except ValueError:
        return False
    identifier = re.fullmatch(r"/items/(\d+)(?:-[^/]*)?/?", url.path)
    return bool(
        url.scheme == "https"
        and url.netloc in {"www.vinted.co.uk", "vinted.co.uk"}
        and identifier
        and identifier[1] == str(item["id"])
        and item["title"].strip()
        and item.get("currency") == "GBP"
        and type(item.get("price_pence")) is int
        and 0 <= item["price_pence"] <= 100_000_000
        and (
            (allow_missing_total and item.get("total_pence") is None)
            or (
                type(item.get("total_pence")) is int
                and item["price_pence"] <= item["total_pence"] <= 100_000_000
            )
        )
    )


def record_listing(conn, uid, account, item, source, settings, now):
    """Private account-specific feed, delivered only with a verified account link."""
    return _record(conn, uid, account, item, source, settings, now, "connected",
                   allow_missing_total=bool(current_app.config.get("MULTIPLE_ACCOUNTS")))


def record_catalogue_listing(conn, uid, account, item, settings, now):
    """A shared listing matched to this buyer. Checkout still needs a personal quote."""
    return _record(conn, uid, account, item, "search", settings, now, "connected", allow_missing_total=True)


def record_public_listing(conn, uid, item, settings, now):
    """Public saved-search result. Public subtotals never serve as a checkout quote."""
    item = {**item, "total_pence": None, "total_excludes_shipping": True}
    if item.get("search_id") is not None:
        rule = conn.execute(
            "SELECT budget_pence FROM searches WHERE id=? AND user_id=? AND enabled=1",
            (item["search_id"], uid),
        ).fetchone()
        if rule is None:
            return None
        item["search_budget_pence"] = rule["budget_pence"]
    return _record(conn, uid, "public", item, "search", settings, now, "public_search")


def record_manual_home_listing(conn, uid, item, settings, now):
    """An explicitly imported item remains dashboard-only, even after linking an account."""
    item = {**item, "total_pence": None, "total_excludes_shipping": True}
    return _record(conn, uid, "manual", item, "home", settings, now, "browser")


def record_watched_home(conn, uid, binding, item, settings, now):
    """Called in the same write transaction that authenticated this observation."""
    return _record(conn, uid, "home:" + binding, item, "home", settings, now, "browser_home")


def record_observed_search(conn, uid, binding, item, settings, now):
    return _record(conn, uid, "catalog:" + binding, item, "search", settings, now, "browser_search")


def current_hosted_search(conn, uid, listing):
    fingerprint = listing.get("search_fingerprint")
    if fingerprint is None:
        return True
    return bool(conn.execute("""SELECT 1 FROM searches s JOIN hosted_search_baseline b ON b.search_id=s.id
        WHERE s.id=? AND s.user_id=? AND s.enabled=1 AND b.fingerprint=?""",
        (listing.get("search_id"), uid, fingerprint)).fetchone())


def native_button_binding(conn, row):
    from .catalog_watch import eligible
    from .home_watch import active
    from .native_order import capability

    binding = capability(conn, row["user_id"])
    if row["origin"] == "browser_home" and row["account_ref"] == "routed-home":
        from .home_routes import routed_active
        listing = json.loads(row["listing"])
        return binding if routed_active(conn, row["user_id"], str(listing["id"])) else None
    if binding and (
        (row["origin"] == "public_search" and row["account_ref"] == "public")
        or (
            row["origin"] == "browser_search"
            and row["account_ref"] == "catalog:" + binding
            and eligible(conn, row["user_id"], binding, row["listing"])
        )
        or (
            row["origin"] == "browser_home"
            and row["account_ref"] == "home:" + binding
            and active(conn, row["user_id"], binding)
        )
    ):
        return binding
    return None


def _record(conn, uid, account, item, source, settings, now, origin, *, allow_missing_total=False):
    if not valid_listing(item, allow_missing_total=allow_missing_total or origin != "connected"):
        return None
    if origin != "browser" and quiet(settings, now):
        return None
    subtotal = item.get("subtotal_pence")
    if subtotal is not None and (
        type(subtotal) is not int or not item["price_pence"] <= subtotal <= 100_000_000
    ):
        return None
    if source == "home":
        posted = listing_time(item.get("listed_at"))
        if posted is None or not 0 <= now - posted < 180:
            return
        if not home_matches(settings, item):
            return
    item = dict(item)
    item["photo_url"] = vinted_photo_url(item.get("photo_url"))
    # The search name belongs to this Finder user. Snapshot it on discovery.
    item.pop("search_name", None)
    if source == "search" and type(item.get("search_id")) is int:
        rule = conn.execute(
            "SELECT name FROM searches WHERE user_id=? AND id=?", (uid, item["search_id"])
        ).fetchone()
        if rule:
            item["search_name"] = rule["name"]
    delivery = "dashboard" if origin == "browser" else "new"
    inserted = conn.execute(
        """INSERT INTO pilot_alerts
      (user_id,account_ref,listing_id,listing,source,origin,created_at,attempt_key,delivery,updated)
      SELECT ?,?,?,?,?,?,?,?,?,? WHERE NOT EXISTS (
            SELECT 1 FROM pilot_alerts WHERE user_id=? AND listing_id=? AND origin!='browser'
            AND ?!='browser' AND (origin IN ('browser_search','browser_home') OR ? IN ('browser_search','browser_home'))
            AND delivery NOT IN ('dashboard','expired'))
        ON CONFLICT(user_id,account_ref,listing_id) DO NOTHING""",
        (
            uid,
            account,
            str(item["id"]),
            json.dumps(item),
            source,
            origin,
            now,
            secrets.token_hex(16),
            delivery,
            int(now),
            uid,
            str(item["id"]),
            origin,
            origin,
        ),
    )
    if origin in {"browser_home", "browser_search"} and not inserted.rowcount:
        return None
    row = conn.execute(
        "SELECT id FROM pilot_alerts WHERE user_id=? AND account_ref=? AND listing_id=? AND origin=?",
        (uid, account, str(item["id"]), origin),
    ).fetchone()
    return row["id"] if row else None


def telegram_unavailable_reason(conn):
    name = tg_config("TELEGRAM_BOT_USERNAME").lstrip("@")
    if (not re.fullmatch(r"[A-Za-z0-9_]+", name) or not tg_config("TELEGRAM_BOT_TOKEN")
            or (tg_config("TELEGRAM_MODE") != "polling" and not tg_config("TELEGRAM_WEBHOOK_SECRET"))):
        return "Private Telegram delivery has not been configured."
    if tg_config("TELEGRAM_MODE") == "polling":
        from .telegram_poll import polling_status
        status = polling_status(conn)
        if status.startswith(("Polling paused", "The configured pilot bot")):
            return status
    return ""


def telegram_available(conn):
    return not telegram_unavailable_reason(conn)


def new_link(conn, uid):
    reason = telegram_unavailable_reason(conn)
    if reason:
        return jsonify(error=reason), 503
    name = tg_config("TELEGRAM_BOT_USERNAME").lstrip("@")
    code = secrets.token_urlsafe(24)
    conn.execute(
        "INSERT OR REPLACE INTO pilot_link_codes VALUES (?,?,?)",
        (hashlib.sha256(code.encode()).hexdigest(), uid, int(time.time()) + 600),
    )
    conn.commit()
    return jsonify(url=f"https://t.me/{name}?start={code}", expires_in=600)


def pair_chat(conn, text, chat_id):
    # A savepoint works both in the webhook request and inside the polling offset transaction.
    parts = text.split()
    if len(parts) != 2 or parts[0] != "/start":
        return "Open your Finder dashboard and choose Link Telegram in Preferences."
    digest = hashlib.sha256(parts[1].encode()).hexdigest()
    conn.execute("SAVEPOINT telegram_pair")
    row = conn.execute(
        "SELECT user_id FROM pilot_link_codes WHERE code_hash=? AND expires>?",
        (digest, int(time.time())),
    ).fetchone()
    if not row:
        conn.execute("ROLLBACK TO telegram_pair")
        conn.execute("RELEASE telegram_pair")
        return "This link has expired or has already been used. Create a fresh link in your dashboard."
    try:
        conn.execute(
            """INSERT INTO pilot_chats VALUES (?,?) ON CONFLICT(user_id)
                      DO UPDATE SET chat_id=excluded.chat_id""",
            (row[0], chat_id),
        )
        conn.execute("DELETE FROM pilot_link_codes WHERE code_hash=?", (digest,))
        conn.execute("RELEASE telegram_pair")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK TO telegram_pair")
        conn.execute("RELEASE telegram_pair")
        return "This private chat is linked to another Finder account."
    return "Your private Finder alerts are connected."


def _pence(value):
    from decimal import Decimal, InvalidOperation

    if value is None or value == "":
        return None
    try:
        p = Decimal(str(value)) * 100
        return int(p) if p.is_finite() and p >= 0 and p == p.to_integral_value() else -1
    except (ValueError, InvalidOperation):
        return -1


def check_quote(quote, alert, prefs, now):
    l = json.loads(alert["listing"])
    if (
        quote.get("account_ref") != alert["account_ref"]
        or str(quote.get("listing_id")) != alert["listing_id"]
        or quote.get("currency") != "GBP"
        or not isinstance(quote.get("quote_id"), str)
        or not quote["quote_id"]
        or not (listing_time(quote.get("expires_at")) or 0) > now
    ):
        return "blocked", "The provider could not verify this quote."
    for name in ("item_pence", "shipping_pence", "total_pence"):
        if type(quote.get(name)) is not int or quote[name] < 0:
            return "blocked", "The final price is unavailable."
    if (
        not quote.get("payment_label")
        or quote["total_pence"] < quote["item_pence"] + quote["shipping_pence"]
    ):
        return "blocked", "The checkout total or payment method is incomplete."
    limits = {
        "item_pence": min(
            l["price_pence"],
            _pence(prefs.get("max_item_price"))
            if _pence(prefs.get("max_item_price")) is not None
            else l["price_pence"],
        ),
        "shipping_pence": _pence(prefs.get("max_shipping")),
        "total_pence": _pence(prefs.get("max_total")),
    }
    search_budget = l.get("search_budget_pence")
    if type(search_budget) is int and quote["total_pence"] > search_budget:
        return "blocked", "The total exceeds your saved search budget."
    if any(cap is not None and quote[k] > cap for k, cap in limits.items()):
        return "blocked", "The item, shipping or total exceeds your limit."
    preferred = str(prefs.get("preferred_courier") or "").strip().casefold()
    courier = str(quote.get("courier") or "").strip().casefold()
    if current_app.config.get("MULTIPLE_ACCOUNTS") and prefs.get("delivery_mode") == "pickup":
        canonical = pickup.carrier(quote.get("courier"))
        if (not pickup.verified(prefs, alert["account_ref"]) or canonical not in pickup.CARRIERS
                or quote.get("physical_point_id", quote.get("pickup_id")) != prefs["pickup_id"]
                or quote.get("pickup_id") != prefs["pickup_routes"].get(canonical)
                or not quote.get("pickup_label") or quote.get("delivery_mode") != "pickup"):
            return "blocked", "The courier or exact pickup point could not be verified."
        return "ok", ""
    approved = {str(x).strip().casefold() for x in prefs.get("allowed_couriers", [])}
    if (
        not preferred
        or not courier
        or (courier != preferred and not (prefs.get("allow_same_shop_fallback") and courier in approved))
    ):
        return "blocked", "Select an allowed courier in your dashboard."
    mode = prefs.get("delivery_mode")
    if mode not in {"pickup", "home"} or quote.get("delivery_mode") != mode:
        return "blocked", "The delivery method does not match your settings."
    if mode == "pickup":
        if not prefs.get("pickup_id") or not quote.get("pickup_id") or not quote.get("pickup_label"):
            return "blocked", "A verified collection point is required."
        if prefs["pickup_id"] != quote["pickup_id"]:
            return "confirm", "The collection point has changed."
    elif not prefs.get("home_address_ref") or quote.get("home_address_ref") != prefs.get(
        "home_address_ref"
    ):
        return "blocked", "A verified saved delivery address is required."
    return "ok", ""


def money(pence):
    return f"£{pence / 100:.2f}"


def quote_summary(q):
    location = q.get("pickup_label") or "Saved delivery address"
    return f"{money(q['total_pence'])} including {money(q['shipping_pence'])} shipping · {q['courier']} · {location} · {q['payment_label']}"


def _quote_terms(q):
    fields = (
        "quote_id",
        "account_ref",
        "listing_id",
        "item_pence",
        "total_pence",
        "shipping_pence",
        "currency",
        "courier",
        "delivery_mode",
        "pickup_id",
        "pickup_label",
        "home_address_ref",
        "payment_label",
    )
    return [q.get(k) for k in fields]


def request_buy(conn, uid, aid, confirmation=None):
    from .connection import linked_account

    now = int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT * FROM pilot_alerts WHERE id=? AND user_id=?", (aid, uid)).fetchone()
    account = linked_account(conn, uid)
    if row and row["origin"] != "connected":
        conn.rollback()
        return "Open this listing on Vinted to review delivery, payment and the final price.", None
    if not row or not account or row["account_ref"] != account:
        conn.rollback()
        return "Connect the account that received this listing in your dashboard.", None
    if current_app.config.get("MULTIPLE_ACCOUNTS") and not current_hosted_search(conn, uid, json.loads(row["listing"])):
        conn.rollback()
        return "This saved search changed. Review the listing on Vinted.", None
    status = row["order_status"]
    if status == "checking" and now - row["updated"] >= 60:
        status = "confirm" if row["confirmation"] else None
    if status == "confirm" and confirmation is None:
        conn.rollback()
        return (
            "The collection point has changed.\n" + quote_summary(json.loads(row["offer"])),
            {
                "inline_keyboard": [
                    [
                        {
                            "text": "Confirm pickup and buy",
                            "callback_data": f"cf:{aid}:{row['confirmation']}",
                        }
                    ]
                ]
            },
        )
    if confirmation is not None and (
        status != "confirm" or not hmac.compare_digest(confirmation, row["confirmation"] or "")
    ):
        conn.rollback()
        return "That confirmation is no longer valid.", None
    if status not in (None, "confirm"):
        conn.rollback()
        return "This purchase already has a recorded attempt. Check its status in your dashboard.", None
    check_token = secrets.token_hex(16)
    conn.execute(
        "UPDATE pilot_alerts SET order_status='checking',check_token=?,updated=? WHERE id=?",
        (check_token, now, aid),
    )
    conn.commit()
    try:
        prefs = settings_for(conn, uid)
        if (current_app.config.get("MULTIPLE_ACCOUNTS") and prefs.get("delivery_mode") == "home"
                and (prefs.get("delivery_account_ref") != account or not prefs.get("home_address_ref"))):
            raise InputError("Choose a saved home address for this buying account in Preferences.")
        payload = {"account_ref": account, "listing_id": row["listing_id"], "preferences": prefs}
        if confirmation:
            payload["quote_id"] = json.loads(row["offer"])["quote_id"]
        if current_app.config.get("MULTIPLE_ACCOUNTS") and prefs.get("delivery_mode") == "pickup":
            if confirmation:
                _status(conn, aid, "blocked", check_token)
                return "Review this item again at your verified pickup point.", None
            q = pickup.get_quote(partner_request, account, row["listing_id"], prefs)
        else:
            q = partner_request("POST", "/v1/checkout/quote", payload)
        decision, reason = check_quote(q, row, prefs, time.time())
        if confirmation and decision in ("ok", "confirm"):
            if _quote_terms(q) == _quote_terms(json.loads(row["offer"])):
                decision = "ok"
            else:
                decision, reason = "blocked", "The quote changed again. Open the listing to review it."
        if decision == "confirm":
            code = secrets.token_hex(6)
            changed = conn.execute(
                "UPDATE pilot_alerts SET order_status='confirm',offer=?,confirmation=?,updated=?,check_token=NULL WHERE id=? AND check_token=?",
                (json.dumps(q), code, now, aid, check_token),
            ).rowcount
            if not changed:
                return "A newer order check is in progress.", None
            conn.commit()
            return reason + "\n" + quote_summary(q), {
                "inline_keyboard": [
                    [{"text": "Confirm pickup and buy", "callback_data": f"cf:{aid}:{code}"}]
                ]
            }
        if decision != "ok":
            _status(conn, aid, "blocked", check_token)
            return reason, None
        # Persist the attempt before transmitting; a lost response never causes a second charge.
        conn.execute("BEGIN IMMEDIATE")
        latest = settings_for(conn, uid)
        if (
            linked_account(conn, uid) != account
            or latest != prefs
            or (current_app.config.get("MULTIPLE_ACCOUNTS") and not current_hosted_search(conn, uid, json.loads(row["listing"])))
            or not (listing_time(q.get("expires_at")) or 0) > time.time()
        ):
            conn.rollback()
            _status(conn, aid, "blocked", check_token)
            return "The buying account, preferences or quote changed. Review the listing.", None
        changed = conn.execute(
            "UPDATE pilot_alerts SET order_status='submitting',check_token=NULL,offer=?,confirmation=NULL,updated=? WHERE id=? AND check_token=?",
            (json.dumps(q), now, aid, check_token),
        ).rowcount
        conn.commit()
        if not changed:
            return "A newer order check is in progress.", None
        try:
            result = partner_request(
                "POST",
                "/v1/checkout/submit",
                {
                    "quote_id": q["quote_id"],
                    "account_ref": account,
                    "listing_id": row["listing_id"],
                    "idempotency_key": row["attempt_key"],
                },
            )
            final = order_result(result, row)
        except PartnerError:
            final = "unknown"
        _status(conn, aid, final)
        if final == "purchased":
            return "Purchased · " + quote_summary(q), None
        if final == "declined":
            return "The provider declined this order. No second attempt will be made.", None
        if final == "action_required":
            return (
                "Open Vinted to complete the provider or bank approval. Finder will check the result.",
                None,
            )
        return "The purchase result is pending. Finder will check the result without resubmitting.", None
    except InputError as error:
        _status(conn, aid, "blocked", check_token)
        return str(error), None
    except (PartnerError, ValueError, TypeError, KeyError):
        _status(conn, aid, "blocked", check_token)
        return "A verified checkout quote is unavailable.", None


def order_result(result, row):
    if (
        result.get("account_ref") != row["account_ref"]
        or str(result.get("listing_id")) != row["listing_id"]
        or result.get("idempotency_key") != row["attempt_key"]
    ):
        return "unknown"
    status = result.get("status")
    return status if status in {"purchased", "declined", "action_required", "pending"} else "unknown"


def _status(conn, aid, status, check_token=None):
    conn.execute(
        """UPDATE pilot_alerts SET order_status=?,updated=?,check_token=NULL
                    WHERE id=? AND (? IS NULL OR check_token=?)""",
        (status, int(time.time()), aid, check_token, check_token),
    )
    conn.commit()


def telegram_update(conn, update):
    """Handle a Telegram-authenticated update from the configured transport."""
    cb = update.get("callback_query") or {}
    if not isinstance(cb, dict):
        return {"ok": True}
    msg = cb.get("message") or update.get("message") or {}
    if not isinstance(msg, dict):
        return {"ok": True}
    chat = msg.get("chat") or {}
    if not isinstance(chat, dict):
        return {"ok": True}
    chat_id = chat.get("id")
    author = cb.get("from") or msg.get("from") or {}
    sender = author.get("id") if isinstance(author, dict) else None
    managed = current_app.config.get("MULTIPLE_ACCOUNTS")
    if not managed and not cb and chat.get("type") in {"group", "supergroup"}:
        from .home_groups import register_command
        register_command(conn, msg)
        return dict(ok=True)
    if chat.get("type") != "private" or type(chat_id) is not int or chat_id <= 0 or chat_id != sender:
        return dict(ok=True)
    if not cb:
        command = str(msg.get("text", "")).strip()
        if managed and command == "/reconnect":
            from .telegram_reconnect import private_reply
            return private_reply(conn, chat_id)
        message = pair_chat(conn, command, chat_id)
        if managed and message == "Your private Finder alerts are connected.":
            message += " Use /reconnect if Vinted asks you to sign in again."
        return dict(method="sendMessage", chat_id=chat_id, text=message)
    callback = re.fullmatch(r"(nbuy|ncf|buy|cf):(\d{1,16})(?::([0-9a-f]{12}))?", str(cb.get("data", "")))
    if not callback or callback[1].endswith("cf") != bool(callback[3]):
        return dict(ok=True)
    native = callback[1].startswith("n")
    row = conn.execute(
        """SELECT a.user_id,a.message_id,a.native_binding,a.origin,a.account_ref,a.listing FROM pilot_alerts a
                        JOIN pilot_chats c ON c.user_id=a.user_id
                        WHERE a.id=? AND ((?=1 AND a.origin IN ('public_search','browser_home','browser_search')) OR
                          (?=0 AND a.origin='connected')) AND c.chat_id=? AND a.sent_chat=? AND a.message_id IS NOT NULL""",
        (int(callback[2]), int(native), int(native), chat_id, chat_id),
    ).fetchone()
    if managed and row:
        from .connection import linked_account
        if native or linked_account(conn, row["user_id"]) != row["account_ref"]:
            row = None
    if row and not callback[3] and msg.get("message_id") != row["message_id"]:
        row = None
    if row and native:
        if not row["native_binding"] or native_button_binding(conn, row) != row["native_binding"]:
            row = None
    if row:
        conn.execute(
            """INSERT OR IGNORE INTO pilot_buy_jobs(update_id,user_id,chat_id,alert_id,confirmation,native)
                        VALUES(?,?,?,?,?,?)""",
            (
                update["update_id"],
                row["user_id"],
                chat_id,
                int(callback[2]),
                callback[3],
                int(native),
            ),
        )
    return dict(
        method="answerCallbackQuery",
        callback_query_id=cb.get("id"),
        text="Checking your order." if row else "Alert unavailable.",
    )


def init_flow(app):
    from .home_watch import init_home_watch

    from .catalog_watch import init_app as init_catalog_watch

    init_catalog_watch(app)
    from .telegram_poll import SCHEMA as POLLING_SCHEMA

    conn = get_db()
    conn.executescript(POLLING_SCHEMA)
    conn.executescript(SCHEMA)
    init_home_watch(app)
    from .home_groups import init_app as init_groups
    init_groups(app)
    conn.execute("""CREATE TABLE IF NOT EXISTS pilot_buy_jobs (
        update_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
        alert_id INTEGER NOT NULL, confirmation TEXT, started INTEGER DEFAULT 0,
        finished INTEGER NOT NULL DEFAULT 0, native INTEGER NOT NULL DEFAULT 0)""")

    # Upgrade pilot databases created by earlier previews, even across concurrent starts.
    with transaction(conn):
        additions = {
            "pilot_alerts": {
                "check_token": "TEXT",
                "sent_at": "REAL",
                "valued_at": "REAL",
                "origin": "TEXT NOT NULL DEFAULT 'connected'",
                "native_binding": "TEXT",
            },
            "pilot_buy_jobs": {
                "finished": "INTEGER NOT NULL DEFAULT 0",
                "native": "INTEGER NOT NULL DEFAULT 0",
            },
            "pilot_sources": {"last_ok": "REAL"},
        }
        for table, fields in additions.items():
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(" + table + ")")}
            for field, declaration in fields.items():
                if field not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {field} {declaration}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS pilot_listing_owner ON pilot_alerts(user_id,listing_id)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS pilot_alert_sent_time ON pilot_alerts(sent_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS pilot_alert_value_time ON pilot_alerts(valued_at)")

    @app.post("/api/telegram/link")
    def telegram_link():
        return new_link(get_db(), g.user["id"])

    @app.post("/api/telegram/disconnect")
    def telegram_disconnect():
        conn = get_db()
        with conn:
            conn.execute("DELETE FROM pilot_chats WHERE user_id=?", (g.user["id"],))
            conn.execute("DELETE FROM pilot_link_codes WHERE user_id=?", (g.user["id"],))
        return jsonify(linked=False)

    @app.post("/telegram/webhook", endpoint="pilot_telegram_webhook")
    def telegram_webhook():
        if tg_config("TELEGRAM_MODE") == "polling":
            return jsonify(error="Webhook is disabled in polling mode."), 403
        expected = tg_config("TELEGRAM_WEBHOOK_SECRET")
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not expected or not hmac.compare_digest(expected.encode(), header.encode()):
            return jsonify(error="Unauthorised"), 403
        update = request.get_json(silent=True) or {}
        if not isinstance(update, dict) or type(update.get("update_id")) is not int:
            return jsonify(error="Invalid update"), 400
        conn = get_db()
        with transaction(conn):
            outcome = telegram_update(conn, update)
        return jsonify(outcome)

    @app.cli.command("telegram-webhook")
    def telegram_webhook_command():
        """Register the dedicated pilot bot's webhook at the configured public origin."""
        import click

        from .flow_worker import telegram

        if tg_config("TELEGRAM_MODE") == "polling":
            raise click.ClickException("Polling mode is active. Select webhook mode first.")
        public = current_app.config.get("FINDER_PUBLIC_URL") or os.getenv("FINDER_PUBLIC_URL", "")
        origin = urlsplit(public)
        secret = tg_config("TELEGRAM_WEBHOOK_SECRET")
        username = tg_config("TELEGRAM_BOT_USERNAME").lstrip("@").casefold()
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret)
            or not username
        ):
            raise click.ClickException(
                "Configure the pilot HTTPS origin, private bot username and webhook secret."
            )
        url = public.rstrip("/") + "/telegram/webhook"
        try:
            bot = telegram("getMe", {})
            if str(bot.get("username", "")).casefold() != username:
                raise click.ClickException("The configured token and pilot bot username do not match.")
            previous = telegram("getWebhookInfo", {}).get("url")
            if previous and previous != url:
                raise click.ClickException(
                    "This bot already has a webhook at another address. Use the dedicated pilot bot."
                )
            telegram(
                "setWebhook",
                {"url": url, "secret_token": secret, "allowed_updates": ["message", "callback_query"]},
            )
            if telegram("getWebhookInfo", {}).get("url") != url:
                raise click.ClickException("Telegram did not confirm the webhook address.")
        except ValueError:
            raise click.ClickException(
                "Telegram could not verify the pilot bot configuration."
            ) from None
        click.echo("Pilot Telegram webhook verified.")

    @app.cli.command("worker")
    def worker():
        """Run the independent pilot alert, review and purchase worker."""
        from .flow_worker import run

        run(current_app._get_current_object())
