"""Withdraw the verified November 27 Morgan skirt misquote from its original shared trial reply."""

import asyncio
import json
import logging
import re
import sqlite3
import time
from contextlib import closing

from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

ITEM = "10994172333"
COMPS = {"9049695543", "8629621605", "10446033743"}
TEXT = (
    "Correction: Finder withdraws the £9.50 resale estimate for this Morgan de Toi skirt. "
    "The comparison included jackets. Please disregard that price."
)
logger = logging.getLogger(__name__)
_task = None


def schedule(bot, group, path):
    global _task
    if _task is None:
        _task = asyncio.create_task(correct(bot, group, path))


def archived(conn, version, now):
    """Prepare the correction and snapshot the original valuation once, in the same transaction."""
    window = conn.execute(
        "SELECT started,ends FROM hour_window WHERE id=1 AND group_version=?", (version,)
    ).fetchone()
    if not window or not window[0] <= now < window[1]:
        return None
    row = conn.execute(
        """SELECT parent_message,reply_message,accepted,ready,sent,status,result,publish_state
        FROM hour_alerts WHERE item_id=? AND sent BETWEEN ? AND ?""",
        (ITEM, *window),
    ).fetchone()
    if not row or not row[1] or row[0] == row[1]:
        return None
    parent, reply, accepted, ready, sent, status, source, publication = row
    if status == "withdrawn":
        prior = conn.execute(
            "SELECT reply_message,parent_message FROM hour_wrong_price_original WHERE item_id=? AND group_version=?",
            (ITEM, version),
        ).fetchone()
        return (parent, reply, window[1]) if prior == (reply, parent) else None
    if status != "estimated" or publication != "accepted" or not source:
        return None
    record = json.loads(source)
    evidence = record.get("evidence") or []
    if (
        record.get("resale_pence") != 950
        or len(evidence) != len(COMPS)
        or {str(entry.get("id")) for entry in evidence} != COMPS
    ):
        return None
    for entry in evidence:
        match = re.fullmatch(
            r"https://(?:www\.)?vinted\.co\.uk/items/([0-9]+)(?:-[^/?#]+)?/?",
            str(entry.get("url") or ""),
        )
        if not match or match.group(1) != str(entry["id"]):
            return None
    conn.execute("""CREATE TABLE IF NOT EXISTS hour_wrong_price_original(
        item_id TEXT PRIMARY KEY, group_version TEXT NOT NULL, parent_message INTEGER NOT NULL, reply_message INTEGER NOT NULL,
        accepted REAL, ready REAL, sent REAL, old_status TEXT, original_json TEXT NOT NULL, old_publish_state TEXT)""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS hour_wrong_price_immutable BEFORE UPDATE ON hour_wrong_price_original
        BEGIN SELECT RAISE(ABORT,'Immutable original valuation'); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS hour_wrong_price_no_delete BEFORE DELETE ON hour_wrong_price_original
        BEGIN SELECT RAISE(ABORT,'Immutable original valuation'); END""")
    conn.execute("""CREATE TABLE IF NOT EXISTS hour_wrong_price_delivery(
        item_id TEXT PRIMARY KEY, state TEXT, attempts INTEGER, next_at REAL, ack_at REAL, correction_message INTEGER)""")
    conn.execute(
        "INSERT INTO hour_wrong_price_original VALUES(?,?,?,?,?,?,?,?,?,?)",
        (ITEM, version, parent, reply, accepted, ready, sent, status, source, publication),
    )
    corrected = {
        "status": "withdrawn",
        "resale_pence": None,
        "baseline_pence": None,
        "evidence": [],
        "explanation": "Finder withdrew its £9.50 quote for this skirt. Its comparisons included jackets.",
        "withdrawn_at": now,
    }
    conn.execute(
        "UPDATE hour_alerts SET status='withdrawn',result=?,publish_state='withdrawn_awaiting_correction' WHERE item_id=?",
        (json.dumps(corrected), ITEM),
    )
    conn.execute("INSERT INTO hour_wrong_price_delivery VALUES(?,'edit_pending',0,0,NULL,NULL)", (ITEM,))
    logger.info(
        "Finder hourly valuation withdrawn item=%s old_pence=950 reason=skirt_compared_to_jackets", ITEM
    )
    return parent, reply, window[1]


def prepare(path, version):
    with closing(sqlite3.connect(path, timeout=1)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        return archived(conn, version, time.time())


def claim(path, now):
    with closing(sqlite3.connect(path, timeout=1)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT state,attempts,next_at FROM hour_wrong_price_delivery WHERE item_id=?", (ITEM,)
        ).fetchone()
        if not row or row[0] not in ("edit_pending", "fallback_pending"):
            return "done", 0
        state, attempts, next_at = row
        if next_at > now:
            return "wait", next_at - now
        if state == "fallback_pending":
            conn.execute(
                "UPDATE hour_wrong_price_delivery SET state='fallback_attempted' WHERE item_id=?",
                (ITEM,),
            )
            return "fallback", 0
        if attempts >= 3:
            conn.execute(
                "UPDATE hour_wrong_price_delivery SET state='edit_unconfirmed' WHERE item_id=?", (ITEM,)
            )
            return "unconfirmed", 0
        conn.execute(
            "UPDATE hour_wrong_price_delivery SET attempts=attempts+1,next_at=? WHERE item_id=?",
            (now + 16, ITEM),
        )
        return "edit", 0


def update(path, state, message=None, delay=0):
    now = time.time()
    with closing(sqlite3.connect(path, timeout=1)) as conn, conn:
        conn.execute(
            "UPDATE hour_wrong_price_delivery SET state=?,next_at=?,ack_at=?,correction_message=? WHERE item_id=?",
            (state, now + delay, now if message else None, message, ITEM),
        )
        if message:
            conn.execute(
                "UPDATE hour_alerts SET publish_state=? WHERE item_id=?", ("withdrawn_" + state, ITEM)
            )
    logger.info(
        "Finder hourly valuation correction item=%s state=%s telegram_ack=%s",
        ITEM,
        state,
        int(bool(message)),
    )


async def _authorised(group, parent):
    import telegram_shared_alerts as shared

    if await asyncio.to_thread(shared.current) != group:
        return False
    original = await asyncio.to_thread(shared.lookup, group, parent)
    return bool(original and str(original.get("item_id")) == ITEM)


async def correct(bot, group, path):
    import telegram_shared_alerts as shared
    from trial_shared_hour import eligible_service

    if not eligible_service() or await asyncio.to_thread(shared.current) != group:
        return
    try:
        data = await asyncio.to_thread(prepare, path, group.version)
        if not data:
            return
        parent, reply, ends = data
        while time.time() < ends and await _authorised(group, parent):
            action, delay = await asyncio.to_thread(claim, path, time.time())
            if action == "done":
                return
            if action == "unconfirmed":
                await asyncio.to_thread(update, path, "edit_unconfirmed")
                return
            if action == "wait":
                await asyncio.sleep(min(ends - time.time(), delay))
                continue
            if time.time() >= ends or not await _authorised(group, parent):
                return
            kwargs = {
                "chat_id": group.chat,
                "text": TEXT,
                "connect_timeout": 3,
                "pool_timeout": 2,
                "write_timeout": 4,
                "read_timeout": 5,
            }
            try:
                if action == "edit":
                    result = await bot.edit_message_text(message_id=reply, **kwargs)
                else:
                    result = await bot.send_message(
                        reply_to_message_id=parent, allow_sending_without_reply=False, **kwargs
                    )
            except BadRequest as exc:
                why = str(exc).lower()
                if action == "edit" and "message is not modified" in why:
                    await asyncio.to_thread(update, path, "edit_confirmed", reply)
                    return
                if action == "edit" and any(
                    reason in why
                    for reason in (
                        "message to edit not found",
                        "message can't be edited",
                        "message can not be edited",
                        "message cannot be edited",
                        "there is no text in the message",
                    )
                ):
                    await asyncio.to_thread(update, path, "fallback_pending")
                    continue
                await asyncio.to_thread(update, path, action + "_rejected")
                return
            except Forbidden:
                await asyncio.to_thread(update, path, action + "_forbidden")
                return
            except RetryAfter as exc:
                retry = exc.retry_after
                seconds = (retry.total_seconds() if hasattr(retry, "total_seconds") else retry) + 1
                if action == "edit" and time.time() + seconds < ends:
                    await asyncio.to_thread(update, path, "edit_pending", None, seconds)
                    continue
                await asyncio.to_thread(update, path, action + "_throttled")
                return
            except TelegramError:
                if action == "edit":
                    await asyncio.to_thread(update, path, "edit_pending", None, 2)
                    continue
                await asyncio.to_thread(update, path, "fallback_unconfirmed")
                return
            message = getattr(result, "message_id", None)
            if action == "edit" and (message == reply or result is True):
                await asyncio.to_thread(update, path, "edit_confirmed", reply)
            elif action == "fallback" and type(message) is int and message not in (parent, reply):
                await asyncio.to_thread(update, path, "fallback_confirmed", message)
            else:
                await asyncio.to_thread(update, path, action + "_unconfirmed")
            return
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "Finder hourly valuation correction item=%s state=unavailable reason=%s",
            ITEM,
            type(exc).__name__,
        )
