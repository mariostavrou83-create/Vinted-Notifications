"""Accept a structured opinion only in reply to this trial's own group message."""

import asyncio
import re
import sqlite3
import time
from contextlib import closing
from decimal import Decimal

from logger import get_logger

logger = get_logger(__name__)
FEEDBACK_TEXT = re.compile(
    r"\A\s*(?:(?:too\s+)?(?P<direction>high|low)|(?P<fair>fair)|"
    r"(?:(?P<sold>sold\s+for)|(?:i['’]d|i\s+would)\s+list(?:\s+it)?(?:\s+(?:for|at))?)?"
    r"\s*£\s*(?P<amount>\d{1,5}(?:\.\d{1,2})?))\s*[.!]?\s*\Z",
    re.IGNORECASE | re.ASCII,
)


def price_opinion(text):
    match = FEEDBACK_TEXT.fullmatch(text or "")
    if not match:
        return None
    if match["amount"]:
        pence = int(Decimal(match["amount"]) * 100)
        if pence <= 0:
            return None
        return ("sold_price" if match["sold"] else "suggested_price", pence)
    return ((match["direction"] or match["fair"]).lower(), None)


def store(path, version, role, quoted, reply, posted, now, opinion):
    # Open only an existing trial database. A deployed but inactive trial has no receipts.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=0.25)) as conn, conn:
        receipts = "SELECT item_id,reply_message AS message,sent AS shown FROM hour_alerts"
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hour_numeric_cards'").fetchone():
            receipts += """ UNION ALL
                SELECT a.item_id, a.parent_message, c.shown
                  FROM hour_alerts a JOIN hour_numeric_cards c ON c.item_id=a.item_id"""
        entry = conn.execute(
            f"""WITH receipts AS ({receipts})
               SELECT a.item_id FROM receipts a JOIN hour_window w ON w.id=1
               WHERE w.group_version=? AND w.started<=? AND ?<w.ends
                 AND a.message=? AND a.shown>=w.started
                 AND a.shown<w.ends AND CAST(a.shown AS INTEGER)<=? AND ?<w.ends""",
            (version, now, now, quoted, posted, posted),
        ).fetchone()
        if not entry:
            return False
        conn.execute("""CREATE TABLE IF NOT EXISTS hour_feedback (
            group_version TEXT NOT NULL, message_id INTEGER NOT NULL,
            item_id TEXT NOT NULL, role TEXT NOT NULL, kind TEXT NOT NULL,
            pence INTEGER, posted INTEGER NOT NULL, PRIMARY KEY(group_version,message_id))""")
        recorded = conn.execute(
            "INSERT OR IGNORE INTO hour_feedback VALUES(?,?,?,?,?,?,?)",
            (version, reply, entry[0], role, opinion[0], opinion[1], posted),
        ).rowcount
    if recorded:
        logger.info("Finder hourly valuation feedback item=%s kind=%s pence=%s", entry[0], *opinion)
    return bool(recorded)


def accept(chat, user, quoted, reply, posted, opinion):
    import telegram_shared_alerts as shared
    from trial_shared_hour import audit_path, capture_active

    if not capture_active():
        return
    path = audit_path()
    if not path.is_file():
        return
    group = shared.current()
    if shared.member(group, chat, user):
        role = "owner" if user.id == group.owner else "partner"
        store(path, group.version, role, quoted, reply, posted, time.time(), opinion)


async def receive(update, context):
    message = update.message
    quoted = message.reply_to_message if message else None
    author = quoted.from_user if quoted else None
    if not author or author.id != context.bot.id or not author.is_bot:
        return
    opinion = price_opinion(message.text)
    if not opinion:
        return
    try:
        await asyncio.to_thread(accept, update.effective_chat, update.effective_user, quoted.message_id,
                                message.message_id, int(message.date.timestamp()), opinion)
    except (OSError, sqlite3.Error):
        logger.warning("Shared resale trial feedback could not be recorded")
