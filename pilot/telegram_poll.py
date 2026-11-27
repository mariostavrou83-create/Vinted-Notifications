"""Local-only Telegram update transport for a dedicated pilot bot."""

import logging
import re
import secrets
import time

from . import flow
from .db import transaction

log = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS pilot_polling (
 bot_id TEXT PRIMARY KEY, next_offset INTEGER NOT NULL DEFAULT 0,
 last_update REAL NOT NULL DEFAULT 0, next_check REAL NOT NULL DEFAULT 0,
 lease TEXT, verified_until REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'Starting'
);
"""


def _identity():
    token = flow.tg_config("TELEGRAM_BOT_TOKEN")
    username = flow.tg_config("TELEGRAM_BOT_USERNAME").lstrip("@").casefold()
    if (
        flow.tg_config("TELEGRAM_MODE") != "polling"
        or not re.fullmatch(r"[1-9]\d*:[\w-]+", token)
        or not username
    ):
        return None
    return token.partition(":")[0], username


def polling_status(conn):
    identity = _identity()
    if not identity:
        return "Configure a dedicated pilot bot username and token."
    row = conn.execute("SELECT status FROM pilot_polling WHERE bot_id=?", (identity[0],)).fetchone()
    return row[0] if row else "Waiting for the local pilot worker."


def receive_updates(conn, now):
    from .flow_worker import telegram

    identity = _identity()
    if not identity:
        return False
    bot_id, username = identity
    lease = secrets.token_hex(12)
    with transaction(conn):
        conn.execute("INSERT OR IGNORE INTO pilot_polling(bot_id) VALUES(?)", (bot_id,))
        state = conn.execute("SELECT * FROM pilot_polling WHERE bot_id=?", (bot_id,)).fetchone()
        if state["next_check"] > now:
            return False
        conn.execute(
            "UPDATE pilot_polling SET lease=?,next_check=? WHERE bot_id=?", (lease, now + 90, bot_id)
        )
    offset = state["next_offset"]
    # Telegram may start a new sequence with a random update_id after a week of inactivity.
    if now - state["last_update"] >= 7 * 86400:
        offset = 0
    delay = 0
    status = "Listening for private Telegram updates."
    verified = state["verified_until"]
    try:
        if verified <= now:
            me = telegram("getMe", {})
            if (
                str(me.get("id")) != bot_id
                or not me.get("is_bot")
                or (me.get("username") or "").casefold() != username
            ):
                status = "The configured pilot bot username and token do not match."
                delay = 300
                return False
            hooks = telegram("getWebhookInfo", {})
            if not isinstance(hooks.get("url"), str):
                raise ValueError("Missing Telegram webhook state.")
            if hooks["url"]:
                status = (
                    "Polling paused: this bot already has a webhook. Configure a separate pilot bot."
                )
                delay = 300
                return False
            verified = now + 180
        result = telegram(
            "getUpdates",
            {
                "offset": offset,
                "timeout": 18,
                "limit": 10,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        if not isinstance(result, list):
            raise TypeError("Invalid Telegram updates.")
        for update in result:
            uid = update.get("update_id") if isinstance(update, dict) else None
            if type(uid) is not int or uid < offset:
                continue
            with transaction(conn):
                if not conn.execute(
                    "SELECT 1 FROM pilot_polling WHERE bot_id=? AND lease=?", (bot_id, lease)
                ).fetchone():
                    return False
                reply = flow.telegram_update(conn, update)
                offset = uid + 1
                conn.execute(
                    "UPDATE pilot_polling SET next_offset=?,last_update=?,next_check=? WHERE bot_id=? AND lease=?",
                    (offset, time.time(), time.time() + 90, bot_id, lease),
                )
            method = reply.pop("method", None)
            if method:
                # The update and purchase queue are already committed. An unconfirmed
                # acknowledgement must never repeat a checkout submission.
                try:
                    telegram(method, reply)
                except ValueError:
                    log.info("Telegram update accepted; its acknowledgement was unconfirmed.")
        return True
    except (TypeError, ValueError, OSError):
        status = "Polling paused: verify the dedicated pilot bot and that only one receiver is running."
        verified = 0
        delay = 30
        return False
    finally:
        if delay:
            log.warning("%s", status)
        conn.execute(
            """UPDATE pilot_polling SET lease=NULL,next_check=?,verified_until=?,status=?
                     WHERE bot_id=? AND lease=?""",
            (time.time() + delay, verified, status, bot_id, lease),
        )
