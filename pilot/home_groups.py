"""Opt-in item-only delivery from a paired Home to Telegram groups."""

import hashlib
import logging
import re
import secrets
import time

from flask import g, jsonify, request

from . import flow
from .db import get_db, settings_for, transaction
from .home_watch import active

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS home_group_codes (
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, binding TEXT NOT NULL,
 owner_chat INTEGER NOT NULL, bot TEXT NOT NULL, digest TEXT UNIQUE NOT NULL, expires REAL NOT NULL,
 group_chat INTEGER, title TEXT, next_check REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS home_group_links (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 binding TEXT NOT NULL, owner_chat INTEGER NOT NULL, bot TEXT NOT NULL,
 group_chat INTEGER NOT NULL CHECK(group_chat<0), title TEXT NOT NULL,
 next_send REAL NOT NULL DEFAULT 0, UNIQUE(user_id,binding,bot,group_chat)
);
CREATE TABLE IF NOT EXISTS home_group_items (
 id INTEGER PRIMARY KEY AUTOINCREMENT, link_id INTEGER NOT NULL REFERENCES home_group_links(id) ON DELETE CASCADE,
 listing TEXT NOT NULL, title TEXT NOT NULL, price INTEGER NOT NULL, posted REAL NOT NULL,
 category TEXT, size TEXT, UNIQUE(link_id,listing)
);
CREATE INDEX IF NOT EXISTS home_group_items_age ON home_group_items(posted);
CREATE INDEX IF NOT EXISTS home_group_item_order ON home_group_items(link_id,id);
CREATE INDEX IF NOT EXISTS home_group_links_due ON home_group_links(next_send,id);
CREATE TABLE IF NOT EXISTS home_group_sent (
 bot TEXT NOT NULL, group_chat INTEGER NOT NULL, listing TEXT NOT NULL, attempted REAL NOT NULL, confirmed_at REAL,
 PRIMARY KEY(bot,group_chat,listing)
);
CREATE INDEX IF NOT EXISTS home_group_sent_age ON home_group_sent(attempted);
CREATE TRIGGER IF NOT EXISTS group_owner_chat_changed AFTER UPDATE OF chat_id ON pilot_chats
 WHEN OLD.chat_id!=NEW.chat_id BEGIN
 DELETE FROM home_group_links WHERE user_id=OLD.user_id;
 DELETE FROM home_group_codes WHERE user_id=OLD.user_id;
END;
CREATE TRIGGER IF NOT EXISTS group_owner_chat_removed AFTER DELETE ON pilot_chats BEGIN
 DELETE FROM home_group_links WHERE user_id=OLD.user_id;
 DELETE FROM home_group_codes WHERE user_id=OLD.user_id;
END;
CREATE TRIGGER IF NOT EXISTS group_home_paused AFTER UPDATE OF enabled,binding ON pilot_home_watch
 WHEN NEW.enabled=0 OR OLD.binding!=NEW.binding BEGIN
 DELETE FROM home_group_items WHERE link_id IN
   (SELECT id FROM home_group_links WHERE user_id=OLD.user_id AND binding=OLD.binding);
 DELETE FROM home_group_codes WHERE user_id=OLD.user_id AND binding=OLD.binding;
END;
"""


def bot_identity(token=None):
    token = (flow.tg_config("TELEGRAM_BOT_TOKEN") or "") if token is None else token
    name = (flow.tg_config("TELEGRAM_BOT_USERNAME") or "").lstrip("@")
    if re.fullmatch(r"[1-9]\d*:[\w-]+", token) and re.fullmatch(
        r"[A-Za-z0-9_]{1,32}", name
    ):
        return token.partition(":")[0], name
    return "", ""


def _source_active(conn, uid, binding, now):
    if active(conn, uid, binding, now):
        return True
    from .home_companion import valid_extra

    return valid_extra(conn, uid, binding, now)


def remove_source(conn, uid, binding):
    conn.execute(
        "DELETE FROM home_group_codes WHERE user_id=? AND binding=?", (uid, binding)
    )
    conn.execute(
        "DELETE FROM home_group_links WHERE user_id=? AND binding=?", (uid, binding)
    )


def _device(conn, uid, now, source_id=None):
    if source_id is not None:
        from .home_companion import extra_binding

        if not re.fullmatch(r"[1-9]\d{0,16}", str(source_id)):
            return None
        source = extra_binding(conn, uid, int(source_id))
        if not source:
            return None
        chat = conn.execute(
            "SELECT chat_id FROM pilot_chats WHERE user_id=?", (uid,)
        ).fetchone()
        return {"binding": source["binding"], "chat_id": chat[0] if chat else None}
    return conn.execute(
        """SELECT d.binding, c.chat_id FROM native_devices d LEFT JOIN pilot_chats c
           ON c.user_id=d.user_id WHERE d.user_id=? AND d.expires>?""",
        (uid, now),
    ).fetchone()


def init_app(app):
    get_db().executescript(SCHEMA)

    @app.get("/api/home-groups")
    def pilot_home_groups():
        conn, uid = get_db(), g.user["id"]
        device = _device(conn, uid, time.time(), request.args.get("source_id"))
        groups = []
        if device and device["chat_id"]:
            groups = [
                dict(row)
                for row in conn.execute(
                    """SELECT id,title FROM home_group_links WHERE user_id=? AND binding=?
                   AND owner_chat=? AND bot=? ORDER BY id""",
                    (uid, device["binding"], device["chat_id"], bot_identity()[0]),
                )
            ]
        return jsonify(
            groups=groups,
            paired=bool(device),
            telegram_linked=flow.telegram_linked(conn, uid),
        )

    @app.post("/api/home-groups/link")
    def pilot_home_group_link():
        conn, uid, now = get_db(), g.user["id"], time.time()
        bot, name = bot_identity()
        if not bot or (
            flow.tg_config("TELEGRAM_MODE") != "polling"
            and not flow.tg_config("TELEGRAM_WEBHOOK_SECRET")
        ):
            return jsonify(
                error="Configure the dedicated pilot Telegram bot first."
            ), 503
        body = request.get_json(silent=True) if request.is_json else {}
        if not isinstance(body, dict) or set(body) - {"source_id"}:
            return jsonify(error="Choose a Home source from your dashboard."), 400
        code = "hg_" + secrets.token_urlsafe(24)
        with transaction(conn):
            device = _device(conn, uid, now, body.get("source_id"))
            if (
                not device
                or not device["chat_id"]
                or not _source_active(conn, uid, device["binding"], now)
            ):
                return jsonify(
                    error="Connect private Telegram and start your paired Home first."
                ), 409
            conn.execute(
                """INSERT OR REPLACE INTO home_group_codes
                   (user_id,binding,owner_chat,bot,digest,expires) VALUES(?,?,?,?,?,?)""",
                (
                    uid,
                    device["binding"],
                    device["chat_id"],
                    bot,
                    _digest(code),
                    now + 600,
                ),
            )
        return jsonify(url=f"https://t.me/{name}?startgroup={code}", expires_in=600)

    @app.delete("/api/home-groups/<int:group_id>")
    def pilot_home_group_remove(group_id):
        if group_id >= 2**63:
            return jsonify(error="Group unavailable."), 404
        deleted = get_db().execute(
            "DELETE FROM home_group_links WHERE id=? AND user_id=?",
            (group_id, g.user["id"]),
        )
        return (
            (jsonify(ok=True), 200)
            if deleted.rowcount
            else (jsonify(error="Group unavailable."), 404)
        )


def _digest(code):
    return hashlib.sha256(code.encode()).hexdigest()


def register_command(conn, msg):
    """Only save an admin check. Both Telegram transports run this inside their update transaction."""
    chat, author = msg.get("chat") or {}, msg.get("from") or {}
    if (
        not isinstance(author, dict)
        or not isinstance(chat, dict)
        or chat.get("type") not in {"group", "supergroup"}
        or type(chat.get("id")) is not int
        or chat["id"] >= 0
        or type(author.get("id")) is not int
        or author["id"] <= 0
        or author.get("is_bot")
        or msg.get("sender_chat")
        or msg.get("forward_origin")
    ):
        return
    text = msg.get("text")
    if not isinstance(text, str) or not text.startswith("/start@"):
        return
    bot, name = bot_identity()
    match = re.fullmatch(r"/start@([A-Za-z0-9_]+) (hg_[A-Za-z0-9_-]{32})", text)
    if not bot or not match or match[1].casefold() != name.casefold():
        return
    conn.execute(
        """UPDATE home_group_codes SET group_chat=?,title=?
           WHERE digest=? AND bot=? AND owner_chat=? AND group_chat IS NULL AND expires>?
           AND EXISTS (SELECT 1 FROM pilot_chats c WHERE c.user_id=home_group_codes.user_id AND c.chat_id=owner_chat)""",
        (
            chat["id"],
            str(chat.get("title") or "Telegram group")[:128],
            _digest(match[2]),
            bot,
            author["id"],
            time.time(),
        ),
    )


def _is_admin(chat, user, token):
    from .flow_worker import telegram

    admins = telegram("getChatAdministrators", {"chat_id": chat}, token=token)
    if not isinstance(admins, list):
        raise TypeError("Group administrators unavailable.")
    return any(
        isinstance(a, dict)
        and a.get("status") in {"creator", "administrator"}
        and isinstance(a.get("user"), dict)
        and a["user"].get("id") == user
        and not a["user"].get("is_bot")
        for a in admins
    )


def verify(conn, now):
    row = conn.execute(
        """SELECT * FROM home_group_codes WHERE group_chat IS NOT NULL AND expires>? AND next_check<=?
           ORDER BY next_check,user_id LIMIT 1""",
        (now, now),
    ).fetchone()
    if (
        not row
        or not conn.execute(
            "UPDATE home_group_codes SET next_check=? WHERE digest=? AND next_check<=?",
            (now + 30, row["digest"], now),
        ).rowcount
    ):
        return False
    token = flow.tg_config("TELEGRAM_BOT_TOKEN") or ""
    if bot_identity(token)[0] != row["bot"]:
        return True
    try:
        approved = _is_admin(row["group_chat"], row["owner_chat"], token)
    except (OSError, ValueError, TypeError):
        return True  # A transient failure can be rechecked before the code expires.
    with transaction(conn):
        current = time.time()
        if approved and _source_active(conn, row["user_id"], row["binding"], current):
            conn.execute(
                """INSERT INTO home_group_links(user_id,binding,owner_chat,bot,group_chat,title)
                   SELECT p.user_id,p.binding,p.owner_chat,p.bot,p.group_chat,p.title FROM home_group_codes p
                   JOIN pilot_chats c ON c.user_id=p.user_id AND c.chat_id=p.owner_chat
                   WHERE p.digest=? AND p.expires>?
                   ON CONFLICT(user_id,binding,bot,group_chat) DO UPDATE SET title=excluded.title""",
                (row["digest"], current),
            )
        conn.execute("DELETE FROM home_group_codes WHERE digest=?", (row["digest"],))
    return True


def _allowed(conn, uid, binding, category, size, posted, now):
    if not 0 <= now - posted < 180 or not _source_active(conn, uid, binding, now):
        return False
    prefs = settings_for(conn, uid)
    return (
        not flow.quiet(prefs, now)
        and flow._matches_choice(prefs.get("categories"), category)
        and flow._matches_choice(prefs.get("sizes"), size)
    )


def observe(conn, source_uid, binding, item, now):
    """Enqueue only the public item fields. This never makes a network request."""
    if not conn.execute(
        "SELECT 1 FROM home_group_links WHERE user_id=? AND binding=? LIMIT 1",
        (source_uid, binding),
    ).fetchone():
        return
    if not flow.valid_listing(item, allow_missing_total=True):
        return
    posted = flow.listing_time(item.get("listed_at"))
    if posted is None or not _allowed(
        conn, source_uid, binding, item.get("category"), item.get("size"), posted, now
    ):
        return
    inserted = conn.execute(
        """INSERT OR IGNORE INTO home_group_items(link_id,listing,title,price,posted,category,size)
           SELECT l.id,?,?,?,?,?,? FROM home_group_links l JOIN pilot_chats c
           ON c.user_id=l.user_id AND c.chat_id=l.owner_chat
           WHERE l.user_id=? AND l.binding=? AND NOT EXISTS
           (SELECT 1 FROM home_group_sent s WHERE s.bot=l.bot AND s.group_chat=l.group_chat AND s.listing=?)""",
        (
            str(item["id"]),
            item["title"][:350],
            item["price_pence"],
            posted,
            item.get("category"),
            item.get("size"),
            source_uid,
            binding,
            str(item["id"]),
        ),
    )
    return inserted.rowcount


def deliver(conn, now):
    from .flow_worker import telegram

    # One worker lane and a per-group slot keep busy Homes out of private delivery's way.
    row = conn.execute(
        """SELECT i.*,l.user_id,l.binding,l.owner_chat,l.bot,l.group_chat FROM home_group_links l
           JOIN pilot_chats c ON c.user_id=l.user_id AND c.chat_id=l.owner_chat
           JOIN home_group_items i ON i.id=(SELECT MIN(q.id) FROM home_group_items q WHERE q.link_id=l.id)
           WHERE l.next_send<=? ORDER BY l.next_send,l.id LIMIT 1""",
        (now,),
    ).fetchone()
    if not row:
        return False
    # Expired backlog consumes no Telegram calls or group send slots.
    if row["posted"] <= now - 180:
        conn.execute("DELETE FROM home_group_items WHERE posted<=?", (now - 180,))
        return True
    token = flow.tg_config("TELEGRAM_BOT_TOKEN") or ""
    bot, name = bot_identity(token)
    if bot != row["bot"] or not _allowed(
        conn,
        row["user_id"],
        row["binding"],
        row["category"],
        row["size"],
        row["posted"],
        now,
    ):
        conn.execute("DELETE FROM home_group_items WHERE id=?", (row["id"],))
        return True
    with transaction(conn):
        # All Homes targeting the same group share its Telegram posting limit.
        if not conn.execute(
            "UPDATE home_group_links SET next_send=? WHERE id=? AND next_send<=?",
            (now + 3.2, row["link_id"], now),
        ).rowcount:
            return True
        conn.execute(
            "UPDATE home_group_links SET next_send=? WHERE bot=? AND group_chat=?",
            (now + 3.2, bot, row["group_chat"]),
        )
    try:
        approved = _is_admin(row["group_chat"], row["owner_chat"], token)
    except (OSError, ValueError, TypeError):
        # No message has been sent. Retry if it is still fresh, backing off this group only.
        conn.execute(
            "UPDATE home_group_links SET next_send=MAX(next_send,?) WHERE id=?",
            (now + 15, row["link_id"]),
        )
        return True
    with transaction(conn):
        if not conn.execute(
            "DELETE FROM home_group_items WHERE id=?", (row["id"],)
        ).rowcount:
            return True
        if not approved:
            conn.execute("DELETE FROM home_group_links WHERE id=?", (row["link_id"],))
            return True
        current = time.time()
        if bot_identity()[0] != bot or not _allowed(
            conn,
            row["user_id"],
            row["binding"],
            row["category"],
            row["size"],
            row["posted"],
            current,
        ):
            return True
        marked = conn.execute(
            """INSERT OR IGNORE INTO home_group_sent(bot,group_chat,listing,attempted)
               SELECT l.bot,l.group_chat,?,? FROM home_group_links l JOIN pilot_chats c
               ON c.user_id=l.user_id AND c.chat_id=l.owner_chat WHERE l.id=? AND l.binding=? AND l.bot=?
               AND l.owner_chat=? AND l.group_chat=? AND l.user_id=?""",
            (
                row["listing"],
                current,
                row["link_id"],
                row["binding"],
                bot,
                row["owner_chat"],
                row["group_chat"],
                row["user_id"],
            ),
        )
        if not marked.rowcount:
            return True
        conn.execute(
            "DELETE FROM home_group_sent WHERE attempted<?", (current - 86400,)
        )
    url = f"https://www.vinted.co.uk/items/{row['listing']}"
    try:
        message = telegram(
            "sendMessage",
            {
                "chat_id": row["group_chat"],
                "text": f"{row['title']}\n{flow.money(row['price'])} asking price\n{url}",
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {"text": "View on Vinted", "url": url},
                            {
                                "text": "My private Finder",
                                "url": f"https://t.me/{name}",
                            },
                        ]
                    ]
                },
            },
            token=token,
        )
        if isinstance(message, dict) and type(message.get("message_id")) is int:
            conn.execute(
                "UPDATE home_group_sent SET confirmed_at=? WHERE bot=? AND group_chat=? AND listing=?",
                (time.time(), bot, row["group_chat"], row["listing"]),
            )
    except (ValueError, OSError):
        # The request might have arrived. Leave its durable per-chat mark in place.
        log.warning("Pilot Telegram group alert delivery could not be confirmed.")
    return True
