"""Pilot worker. Each lane has independent network and concurrency limits."""

import hashlib
import json
import logging
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, build_opener

import requests
from flask import current_app

from . import flow
from .connection import linked_account
from .db import get_db, searches_for, settings_for, transaction
from .partner import PartnerError, _NoRedirect, partner_configured, partner_request

log = logging.getLogger(__name__)


class TelegramRetry(ValueError):
    def __init__(self, seconds):
        super().__init__("Telegram requested a delivery pause.")
        self.seconds = seconds


def telegram(method, payload, *, token=None):
    token = flow.tg_config("TELEGRAM_BOT_TOKEN") if token is None else token
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
    except HTTPError as exc:
        if exc.code == 429:
            try:
                parameters = json.loads(exc.read(4096)).get("parameters", {})
                delay = parameters.get("retry_after")
            except (ValueError, OSError, AttributeError, TypeError):
                delay = None
            raise TelegramRetry(delay if type(delay) is int and delay > 0 else 30) from None
        raise ValueError("Telegram delivery could not be confirmed.") from None
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


def render_alert(row):
    item = json.loads(row["listing"])
    price = flow.money(item["price_pence"])
    if row["origin"] == "connected" and type(item.get("total_pence")) is int and not item.get("total_excludes_shipping"):
        price += f" · Estimated total {flow.money(item['total_pence'])}"
    elif type(item.get("subtotal_pence")) is int:
        price += f" · {flow.money(item['subtotal_pence'])} including buyer fee · shipping extra"
    else:
        price += " · Check fees and delivery on Vinted"
    parts = [item["title"][:350], price, item["listing_url"]]
    return "\n".join(parts)[:3900]


def listing_keyboard(row):
    if row["origin"] == "connected":
        button = {"text": "AutoBuy", "callback_data": f"buy:{row['id']}"}
    elif row["origin"] in {"public_search", "browser_home", "browser_search"} and row["native_binding"]:
        button = {"text": "AutoBuy", "callback_data": f"nbuy:{row['id']}"}
    else:
        button = {"text": "Buy on Vinted", "url": json.loads(row["listing"])["listing_url"]}
    return {"inline_keyboard": [[button]]}


def deliver(conn, now):
    if not flow.tg_config("TELEGRAM_BOT_TOKEN"):
        return False
    managed = bool(current_app.config.get("MULTIPLE_ACCOUNTS"))
    if not conn.execute("SELECT 1 FROM pilot_alerts WHERE delivery='new' AND deliver_after<=? LIMIT 1", (now,)).fetchone():
        return False
    with transaction(conn):
        queue_order = " ORDER BY a.id LIMIT 1"
        if managed:
            recent = conn.execute("SELECT count(*) FROM hosted_deliveries WHERE last_at>?", (now - 1,)).fetchone()[0]
            if recent >= 25:
                return False
            queue_order = """ AND coalesce((SELECT last_at FROM hosted_deliveries WHERE chat_id=c.chat_id),0)<=?
                ORDER BY coalesce((SELECT last_at FROM hosted_deliveries WHERE chat_id=c.chat_id),0),a.id LIMIT 1"""
        row = conn.execute(
            """SELECT a.*,c.chat_id FROM pilot_alerts a JOIN pilot_chats c ON c.user_id=a.user_id
                     WHERE delivery='new' AND deliver_after<=? AND ((?=0 AND a.origin IN ('public_search','browser_home','browser_search')) OR
                        (a.origin='connected' AND EXISTS (SELECT 1 FROM pilot_buyer_link b JOIN account_health h USING(user_id)
                          WHERE b.user_id=a.user_id AND b.account_ref=a.account_ref AND b.revoke_pending=0 AND h.expired=0)))""" + queue_order,
            (now, int(managed), now - 1) if managed else (now, 0),
        ).fetchone()
        if not row:
            return False
        uid, aid = row["user_id"], row["id"]
        l = json.loads(row["listing"])
        from .home_watch import active
        from .home_routes import routed_active
        from .catalog_watch import eligible

        prefs = settings_for(conn, uid)
        if (
            flow.quiet(prefs, now)
            or (managed and not flow.current_hosted_search(conn, uid, l))
            or (managed and row["source"] == "home" and not flow.home_matches(prefs, l))
            or (
                row["origin"] == "browser_search"
                and (
                    not eligible(conn, uid, row["account_ref"].removeprefix("catalog:"), l)
                    or not 0 <= now - (flow.listing_time(l.get("listed_at")) or 0) < 180
                )
            )
            or (
                row["origin"] == "browser_home"
                and (
                    not (routed_active(conn, uid, row["listing_id"], now)
                         if row["account_ref"] == "routed-home"
                         else active(conn, uid, row["account_ref"].removeprefix("home:"), now))
                    or not flow._matches_choice(prefs.get("categories"), l.get("category"))
                    or not flow._matches_choice(prefs.get("sizes"), l.get("size"))
                )
            )
            or ((row["source"] == "home" or current_app.config.get("MULTIPLE_ACCOUNTS"))
                and not 0 <= now - (flow.listing_time(l.get("listed_at")) or 0) < 180)
            or now - row["created_at"] > 1200
        ):
            conn.execute("UPDATE pilot_alerts SET delivery='expired' WHERE id=?", (aid,))
            return True
        # Freeze the native purchase principal when this alert's button is issued.
        # A subsequently paired browser cannot inherit an existing alert's authority.
        if row["origin"] in {"public_search", "browser_home", "browser_search"}:
            bound = flow.native_button_binding(conn, row)
            conn.execute("UPDATE pilot_alerts SET native_binding=? WHERE id=?", (bound, aid))
            row = {**dict(row), "native_binding": bound}
        if managed:
            conn.execute("INSERT INTO hosted_deliveries VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET last_at=excluded.last_at",
                         (row["chat_id"], now))
        conn.execute("UPDATE pilot_alerts SET delivery='sending',updated=? WHERE id=?", (int(now), aid))
    try:
        out = telegram(
            "sendMessage",
            {
                "chat_id": row["chat_id"],
                "text": render_alert(row),
                "reply_markup": listing_keyboard(row),
            },
        )
        msg_id = out["message_id"]
        if type(msg_id) is not int or msg_id <= 0:
            raise ValueError("Missing Telegram receipt.")
    except TelegramRetry as exc:
        after = time.time() + exc.seconds
        with transaction(conn):
            conn.execute("UPDATE pilot_alerts SET delivery='new',deliver_after=? WHERE id=?", (after, aid))
            if managed:
                conn.execute("UPDATE hosted_deliveries SET last_at=max(last_at,?) WHERE chat_id=?", (after - 1, row["chat_id"]))
        return True
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


def buy_job(conn, now):
    with transaction(conn):
        row = conn.execute(
            """SELECT j.* FROM pilot_buy_jobs j JOIN pilot_chats c
                 ON c.user_id=j.user_id AND c.chat_id=j.chat_id WHERE j.finished=0 AND j.started<? AND (?=0 OR j.native=0)
                 ORDER BY j.update_id LIMIT 1""",
            (int(now) - 90, int(bool(current_app.config.get("MULTIPLE_ACCOUNTS")))),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE pilot_buy_jobs SET started=? WHERE update_id=?", (int(now), row["update_id"])
        )
    if row["native"]:
        from .native_order import request_native

        issued = conn.execute(
            "SELECT user_id,origin,account_ref,native_binding,listing FROM pilot_alerts WHERE id=? AND user_id=?",
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

    from . import home_groups
    from .native_order import notify as native_notify
    from .public_feed import public_discover
    from .telegram_poll import receive_updates

    stop = stop if stop is not None else Event()

    def loop(fn, idle):
        while not stop.is_set():
            delay = idle
            started = time.time()
            try:
                with app.app_context():
                    if fn(get_db(), started):
                        delay = 0.01
            except Exception as exc:  # noqa: BLE001 -- keep independent worker lanes alive after a failed item
                log.warning("Pilot %s paused after %s", fn.__name__, type(exc).__name__)
                delay = 2
            if fn is public_discover:
                # Start a new round at the next fixed five-second boundary, independent of request latency.
                finished = time.time()
                delay = 0 if int(started // 5) < int(finished // 5) else 5 - finished % 5
            stop.wait(delay)

    lanes = [(discover, 0.25)] * 4 + [
        (public_discover, 1),
        (receive_updates, 2),
        (deliver, 0.2),
        (home_groups.verify, 1),
        (home_groups.deliver, 0.5),
        (buy_job, 0.2),
        (native_notify, 0.5),
        (reconcile, 3),
    ]
    managed = app.config.get("MULTIPLE_ACCOUNTS")
    if managed:
        from . import hosted_feeds, telegram_reconnect
        lanes = [(receive_updates, 2), (buy_job, 0.2), (reconcile, 3), (telegram_reconnect.notify, 2)]
        lanes.extend([(deliver, 0.15)] * 4)
    with ThreadPoolExecutor(max_workers=len(lanes) + (2 if managed else 0)) as pool:
        if managed:
            pool.submit(hosted_feeds.home_loop, app, stop)
            pool.submit(hosted_feeds.catalogue_loop, app, stop)
        for fn, idle in lanes:
            pool.submit(loop, fn, idle)
