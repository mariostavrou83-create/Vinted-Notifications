"""Pilot worker. Each lane has independent network and concurrency limits."""

import hashlib
import json
import logging
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, build_opener

import requests

from . import checkout_profit, flow
from .connection import linked_account
from .db import get_db, searches_for, settings_for, transaction
from .partner import PartnerError, _NoRedirect, partner_configured, partner_request

log = logging.getLogger(__name__)


def telegram(method, payload):
    token = flow.tg_config("TELEGRAM_BOT_TOKEN")
    if not re.fullmatch(r"\d+:[\w-]+", token):
        raise ValueError("Pilot Telegram bot is unconfigured.")
    req = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with build_opener(_NoRedirect).open(
            req, timeout=28 if method == "getUpdates" else 8
        ) as response:
            obj = json.load(response)
        if obj.get("ok"):
            return obj["result"]
    except (ValueError, OSError):
        raise ValueError("Telegram delivery could not be confirmed.") from None
    raise ValueError("Telegram declined the operation.")


def search_matches(item, search):
    title = item["title"].casefold()
    text = title + " " + str(item.get("description") or "").casefold()
    if any(x.casefold() in title for x in search["exclude_keywords"]):
        return False
    if search["include_keywords"] and not any(x.casefold() in text for x in search["include_keywords"]):
        return False
    return search["budget_pence"] is None or item["total_pence"] <= search["budget_pence"]


def discover(conn, now):
    if not partner_configured():
        return False
    with transaction(conn):
        # A connected account may use the dashboard without linking Telegram.
        conn.execute("""INSERT OR IGNORE INTO pilot_sources(user_id,source)
                SELECT b.user_id,s.source FROM pilot_buyer_link b
                CROSS JOIN (SELECT 'home' AS source UNION ALL SELECT 'search') s
                WHERE b.revoke_pending=0""")
        src = conn.execute(
            """SELECT s.* FROM pilot_sources s JOIN pilot_buyer_link b ON b.user_id=s.user_id
                   WHERE b.revoke_pending=0 AND s.next_at<=? ORDER BY s.next_at,s.user_id,s.source LIMIT 1""",
            (now,),
        ).fetchone()
        if not src:
            return False
        uid, source = src["user_id"], src["source"]
        lease = secrets.token_hex(8)
        conn.execute(
            "UPDATE pilot_sources SET next_at=?,lease=? WHERE user_id=? AND source=?",
            (now + 30, lease, uid, source),
        )
    account = linked_account(conn, uid)
    prefs = settings_for(conn, uid)
    # Never pass local dashboard IDs for other users or private reference-photo addresses.
    searches = []
    if source == "search":
        searches = [
            {k: s[k] for k in ("id", "url", "include_keywords", "exclude_keywords", "budget_pence")}
            for s in searches_for(conn, uid)
            if s["enabled"]
        ]
    fingerprint = hashlib.sha256(json.dumps([account, searches], sort_keys=True).encode()).hexdigest()
    cursor = src["cursor"] if fingerprint == src["fingerprint"] else None
    if source == "search" and (not src["last_ok"] or now - src["last_ok"] >= 1200):
        cursor = None
    next_cursor = cursor
    interval = 1 if source == "home" else 5
    try:
        if account and not flow.quiet(prefs, now) and (source == "home" or searches):
            result = partner_request(
                "POST",
                "/v1/feed",
                {"account_ref": account, "source": source, "cursor": cursor, "searches": searches},
            )
            if (
                result.get("account_ref") != account
                or result.get("source") != source
                or not isinstance(result.get("listings"), list)
                or not isinstance(result.get("next_cursor"), str)
                or len(result["next_cursor"]) > 4096
            ):
                raise PartnerError("Invalid account feed.")
            next_cursor = result["next_cursor"]
            by_id = {s["id"]: s for s in searches}
            with transaction(conn):
                if linked_account(conn, uid) != account:
                    return True
                if not conn.execute(
                    "SELECT 1 FROM pilot_sources WHERE user_id=? AND source=? AND lease=?",
                    (uid, source, lease),
                ).fetchone():
                    return True
                latest = settings_for(conn, uid)
                if source == "home" or cursor is not None:
                    for item in result["listings"]:
                        if not flow.valid_listing(item):
                            continue
                        if source == "search":
                            s = by_id.get(item.get("search_id"))
                            if not s or not search_matches(item, s):
                                continue
                            item = {**item, "search_budget_pence": s["budget_pence"]}
                        flow.record_listing(conn, uid, account, item, source, latest, time.time())
                conn.execute(
                    "UPDATE pilot_sources SET cursor=?,fingerprint=?,last_ok=? WHERE user_id=? AND source=?",
                    (next_cursor, fingerprint, now, uid, source),
                )
        else:
            interval = 10
            # Quiet hours are a quiet baseline for searches on resumption.
            conn.execute(
                "UPDATE pilot_sources SET cursor=NULL WHERE user_id=? AND source=? AND lease=?",
                (uid, source, lease),
            )
    except (ValueError, TypeError, PartnerError):
        interval = 30
        log.warning("Pilot feed could not be verified for one connected account.")
    finally:
        conn.execute(
            "UPDATE pilot_sources SET next_at=?,lease=NULL WHERE user_id=? AND source=? AND lease=?",
            (max(now + interval, time.time()), uid, source, lease),
        )
    return True


def render_alert(row, valuation=None, checkout_cost=None):
    item = json.loads(row["listing"])
    price = flow.money(item["price_pence"])
    if checkout_cost and checkout_cost["total_pence"] is not None:
        label = (
            "Checkout at purchase" if checkout_cost["basis"] == "purchase" else "Quoted checkout"
        )
        price += f" · {label} {flow.money(checkout_cost['total_pence'])}"
    elif row["origin"] == "connected":
        price += f" · Estimated total {flow.money(item['total_pence'])}"
    elif type(item.get("subtotal_pence")) is int:
        price += f" · {flow.money(item['subtotal_pence'])} including buyer fee · shipping extra"
    else:
        price += " · Check fees and delivery on Vinted"
    parts = [item["title"][:350], price, item["listing_url"]]
    if valuation:
        price = valuation.get("resale_pence")
        if type(price) is int:
            parts.append("Est. resale " + flow.money(price))
            profit = checkout_profit.profit_line(valuation)
            if profit and (row["origin"] == "connected" or checkout_cost):
                parts.append(profit)
        if checkout_cost:
            parts.append(checkout_cost["note"])
        explanation = valuation.get("explanation")
        if explanation:
            parts.append(str(explanation)[:500])
        for c in (valuation.get("evidence") or [])[:3]:
            if c.get("url"):
                parts.append(f"{flow.money(c['ask_pence'])} · {c['url']}")
    return "\n".join(parts)[:3900]


def listing_keyboard(row):
    if row["origin"] == "connected":
        button = {"text": "AutoBuy", "callback_data": f"buy:{row['id']}"}
    elif row["origin"] in {"public_search", "browser_home"} and row["native_binding"]:
        button = {"text": "AutoBuy", "callback_data": f"nbuy:{row['id']}"}
    else:
        button = {"text": "Buy on Vinted", "url": json.loads(row["listing"])["listing_url"]}
    return {"inline_keyboard": [[button]]}


def deliver(conn, now):
    if not flow.tg_config("TELEGRAM_BOT_TOKEN"):
        return False
    with transaction(conn):
        row = conn.execute(
            """SELECT a.*,c.chat_id FROM pilot_alerts a JOIN pilot_chats c ON c.user_id=a.user_id
                     WHERE delivery='new' AND deliver_after<=? AND (a.origin IN ('public_search','browser_home') OR
                        (a.origin='connected' AND EXISTS (SELECT 1 FROM pilot_buyer_link b WHERE b.user_id=a.user_id
                          AND b.account_ref=a.account_ref AND b.revoke_pending=0))) ORDER BY a.id LIMIT 1""",
            (now,),
        ).fetchone()
        if not row:
            return False
        uid, aid = row["user_id"], row["id"]
        l = json.loads(row["listing"])
        from .home_watch import active

        prefs = settings_for(conn, uid)
        if (
            flow.quiet(prefs, now)
            or (
                row["origin"] == "browser_home"
                and (
                    not active(conn, uid, row["account_ref"].removeprefix("home:"), now)
                    or not flow._matches_choice(prefs.get("categories"), l.get("category"))
                    or not flow._matches_choice(prefs.get("sizes"), l.get("size"))
                )
            )
            or (row["source"] == "home" and now - (flow.listing_time(l.get("listed_at")) or 0) >= 180)
            or now - row["created_at"] > 1200
        ):
            conn.execute("UPDATE pilot_alerts SET delivery='expired' WHERE id=?", (aid,))
            return True
        # Freeze the native purchase principal when this alert's button is issued.
        # A subsequently paired browser cannot inherit an existing alert's authority.
        if row["origin"] in {"public_search", "browser_home"}:
            bound = flow.native_button_binding(conn, row)
            conn.execute("UPDATE pilot_alerts SET native_binding=? WHERE id=?", (bound, aid))
            row = {**dict(row), "native_binding": bound}
        conn.execute("UPDATE pilot_alerts SET delivery='sending',updated=? WHERE id=?", (int(now), aid))
    try:
        valuation = json.loads(row["valuation"]) if row["valuation"] else None
        out = telegram(
            "sendMessage",
            {
                "chat_id": row["chat_id"],
                "text": render_alert(row, valuation),
                "reply_markup": listing_keyboard(row),
            },
        )
        msg_id = out["message_id"]
    except (ValueError, KeyError, TypeError):
        # A timeout could follow successful dispatch. Mark the outcome rather than create duplicate buttons.
        conn.execute("UPDATE pilot_alerts SET delivery='unconfirmed' WHERE id=?", (aid,))
        return True
    conn.execute(
        "UPDATE pilot_alerts SET delivery='sent',message_id=?,sent_chat=?,sent_at=? WHERE id=?",
        (msg_id, row["chat_id"], time.time(), aid),
    )
    if row["source"] == "search" and type(l.get("search_id")) is int:
        send_references(conn, uid, l["search_id"], row["chat_id"], msg_id)
    return True


def send_references(conn, uid, search_id, chat, msg_id):
    photos = conn.execute(
        """SELECT p.image FROM search_photos p JOIN searches s ON s.id=p.search_id
                         JOIN pilot_chats c ON c.user_id=s.user_id
                         WHERE s.user_id=? AND s.id=? AND c.chat_id=? ORDER BY p.id LIMIT 8""",
        (uid, search_id, chat),
    ).fetchall()
    if not photos:
        return
    files = {f"pic{i}": (f"reference-{i + 1}.jpg", row[0], "image/jpeg") for i, row in enumerate(photos)}
    data = {"chat_id": chat, "reply_parameters": json.dumps({"message_id": msg_id})}
    if len(photos) == 1:
        method = "sendPhoto"
        files = {"photo": files["pic0"]}
        data["caption"] = "Your search reference"
    else:
        method = "sendMediaGroup"
        media = [{"type": "photo", "media": f"attach://pic{i}"} for i in range(len(photos))]
        media[0]["caption"] = "Your search references"
        data["media"] = json.dumps(media)
    token = flow.tg_config("TELEGRAM_BOT_TOKEN")
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            data=data,
            files=files,
            timeout=10,
            allow_redirects=False,
        )
        if not response.ok:
            log.warning("Search reference photo delivery was not confirmed.")
    except requests.RequestException:
        log.warning("Search reference photos remain available in the dashboard.")


def review(conn, now):
    from flask import current_app

    from .market import comparisons_for
    from .pricing import estimate_resale, optional_mistral_reviewer

    with transaction(conn):
        row = conn.execute(
            """SELECT * FROM pilot_alerts WHERE valuation IS NULL AND price_after<=? AND price_attempts<3
                         AND delivery!='sending' AND (delivery!='new' OR NOT EXISTS
                         (SELECT 1 FROM pilot_chats c WHERE c.user_id=pilot_alerts.user_id))
                         ORDER BY id LIMIT 1""",
            (now,),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE pilot_alerts SET price_after=?,price_attempts=price_attempts+1 WHERE id=?",
            (now + 120, row["id"]),
        )
    item = json.loads(row["listing"])
    total = (
        item.get("total_pence")
        if row["origin"] == "connected" and not item.get("total_excludes_shipping")
        else None
    )
    target = {**item, "url": item["listing_url"], "all_in_pence": total}
    if row["origin"] == "connected" and linked_account(conn, row["user_id"]) != row["account_ref"]:
        return True
    comps = comparisons_for(conn, target, config=current_app.config)
    valuation = estimate_resale(target, comps, review=optional_mistral_reviewer())
    conn.execute(
        "UPDATE pilot_alerts SET valuation=?,valued_at=? WHERE id=?",
        (json.dumps(valuation), time.time(), row["id"]),
    )
    if (
        row["delivery"] == "sent"
        and conn.execute(
            "SELECT 1 FROM pilot_chats WHERE user_id=? AND chat_id=?", (row["user_id"], row["sent_chat"])
        ).fetchone()
    ):
        try:
            valuation, checkout_cost = checkout_profit.for_alert(
                conn, row["user_id"], row["id"], time.time()
            )
            telegram(
                "editMessageText",
                {
                    "chat_id": row["sent_chat"],
                    "message_id": row["message_id"],
                    "text": render_alert(row, valuation, checkout_cost),
                    "reply_markup": listing_keyboard(row),
                },
            )
        except ValueError:
            log.warning("Valuation available on dashboard; chat edit was not confirmed.")
    return True


def buy_job(conn, now):
    with transaction(conn):
        row = conn.execute(
            """SELECT j.* FROM pilot_buy_jobs j JOIN pilot_chats c
                 ON c.user_id=j.user_id AND c.chat_id=j.chat_id WHERE j.finished=0 AND j.started<?
                 ORDER BY j.update_id LIMIT 1""",
            (int(now) - 90,),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE pilot_buy_jobs SET started=? WHERE update_id=?", (int(now), row["update_id"])
        )
    if row["native"]:
        from .native_order import request_native

        issued = conn.execute(
            "SELECT user_id,origin,account_ref,native_binding FROM pilot_alerts WHERE id=? AND user_id=?",
            (row["alert_id"], row["user_id"]),
        ).fetchone()
        if (
            not issued
            or not issued["native_binding"]
            or issued["native_binding"] != flow.native_button_binding(conn, issued)
        ):
            msg, keyboard = (
                "The linked buying browser changed. Open the listing on Vinted to continue.",
                None,
            )
        else:
            msg, keyboard = request_native(conn, row["user_id"], row["alert_id"], row["confirmation"])
    else:
        msg, keyboard = flow.request_buy(conn, row["user_id"], row["alert_id"], row["confirmation"])
    if not conn.execute(
        "SELECT 1 FROM pilot_chats WHERE user_id=? AND chat_id=?", (row["user_id"], row["chat_id"])
    ).fetchone():
        conn.execute("UPDATE pilot_buy_jobs SET finished=1 WHERE update_id=?", (row["update_id"],))
        return True
    params = {"chat_id": row["chat_id"], "text": msg}
    if keyboard:
        params["reply_markup"] = keyboard
    telegram("sendMessage", params)
    conn.execute("UPDATE pilot_buy_jobs SET finished=1 WHERE update_id=?", (row["update_id"],))
    return True


def reconcile(conn, now):
    with transaction(conn):
        row = conn.execute(
            """SELECT * FROM pilot_alerts WHERE origin='connected' AND order_status IN ('unknown','submitting','pending','action_required')
                         AND updated<? ORDER BY updated LIMIT 1""",
            (now - 45,),
        ).fetchone()
        if not row:
            return False
        conn.execute("UPDATE pilot_alerts SET updated=? WHERE id=?", (int(now), row["id"]))
    if linked_account(conn, row["user_id"]) != row["account_ref"]:
        return True
    result = partner_request(
        "POST",
        "/v1/checkout/status",
        {
            "account_ref": row["account_ref"],
            "listing_id": row["listing_id"],
            "idempotency_key": row["attempt_key"],
        },
    )
    status = flow.order_result(result, row)
    if status in {"purchased", "declined", "action_required", "pending"}:
        flow._status(conn, row["id"], status)
        if (
            status != row["order_status"]
            and status in {"purchased", "declined"}
            and row["sent_chat"]
            and conn.execute(
                "SELECT 1 FROM pilot_chats WHERE user_id=? AND chat_id=?",
                (row["user_id"], row["sent_chat"]),
            ).fetchone()
        ):
            telegram(
                "sendMessage",
                {
                    "chat_id": row["sent_chat"],
                    "text": "Purchase result: "
                    + status
                    + " · "
                    + json.loads(row["listing"])["title"][:280],
                },
            )
    return True


def run(app, stop=None):
    from threading import Event

    from .native_order import notify as native_notify
    from .public_feed import public_discover
    from .telegram_poll import receive_updates

    stop = stop if stop is not None else Event()

    def loop(fn, idle):
        while not stop.is_set():
            delay = idle
            try:
                with app.app_context():
                    if fn(get_db(), time.time()):
                        delay = 0.01
            except Exception as exc:  # noqa: BLE001 -- keep independent worker lanes alive after a failed item
                log.warning("Pilot %s paused after %s", fn.__name__, type(exc).__name__)
                delay = 2
            stop.wait(delay)

    lanes = [(discover, 0.25)] * 4 + [
        (public_discover, 1),
        (receive_updates, 2),
        (deliver, 0.2),
        (review, 1),
        (buy_job, 0.2),
        (native_notify, 0.5),
        (reconcile, 3),
    ]
    with ThreadPoolExecutor(max_workers=len(lanes)) as pool:
        for fn, idle in lanes:
            pool.submit(loop, fn, idle)
