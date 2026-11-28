"""Durable, independently acknowledged operational updates for the linked group."""

import logging
import time
from contextlib import closing

from telegram.error import BadRequest, RetryAfter, NetworkError
from search_settings import connection

LOGGER = logging.getLogger(__name__)


def enqueue(conn, key, body, when):
    from telegram_shared_alerts import current

    group = current(conn)
    if group and when >= group.linked_at:
        conn.execute("""INSERT OR IGNORE INTO finder_group_notices(version,event_key,body,created,due)
            VALUES(?,?,?,?,?)""", (group.version, key, body, when, when))
        conn.execute("DELETE FROM finder_group_notices WHERE created<?", (when - 7 * 86400,))


def queue(key, body, when=None):
    with closing(connection()) as conn, conn:
        enqueue(conn, key, body, time.time() if when is None else when)


def claim(group, now):
    from telegram_shared_alerts import current

    with closing(connection()) as conn, conn:
        query = """SELECT event_key,body,attempts FROM finder_group_notices
            WHERE version=? AND sent=0 AND due<=? ORDER BY created,event_key LIMIT 1"""
        if not conn.execute(query, (group.version, now)).fetchone():
            return None
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(query, (group.version, now)).fetchone()
        if not row or current(conn) != group:
            return None
        conn.execute("UPDATE finder_group_notices SET due=?,attempts=attempts+1 WHERE version=? AND event_key=?",
                     (now + 120, group.version, row["event_key"]))
        return dict(row)


def finish(group, row, *, retry_after=None):
    with closing(connection()) as conn, conn:
        conn.execute("""UPDATE finder_group_notices SET sent=?,due=? WHERE version=? AND event_key=? AND attempts=?""",
                     (int(retry_after is None), time.time() + (retry_after or 0), group.version,
                      row["event_key"], row["attempts"] + 1))


async def deliver(bot, group):
    from telegram_shared_alerts import current
    from photo_cards import TIMEOUTS

    row = claim(group, time.time())
    if not row or current() != group:
        return False
    try:
        message = await bot.send_message(chat_id=group.chat, text=row["body"], disable_web_page_preview=True, **TIMEOUTS)
    except RetryAfter as exc:
        delay = exc.retry_after
        finish(group, row, retry_after=(delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay)) + 1)
        raise
    except BadRequest:
        finish(group, row)
    except NetworkError:
        finish(group, row, retry_after=min(300, 5 * (row["attempts"] + 1)))
    else:
        finish(group, row)
        LOGGER.info("Finder group operational notice accepted by Telegram (message %s)", message.message_id)
    return True
