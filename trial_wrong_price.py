"""Correct three identified November 27 shared alerts and preserve their original valuations."""

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
MANGO = "10994339595"
JEANS = "10994350813"
MESSAGES = {
    ITEM: TEXT,
    MANGO: "Correction: Finder withdraws the £9.00 resale estimate for the Mango chocolate brown leather jacket. "
    "The comparison included a children’s jacket. Please disregard that price.",
    JEANS: "Correction: Finder withdraws the £15–£30 photo estimate for “Jeans (don’t buy listing)”. "
    "The seller says this listing is not for purchase. Please disregard the estimate.",
}
REASONS = {
    ITEM: "skirt_compared_to_jackets",
    MANGO: "adult_compared_to_child",
    JEANS: "not_for_purchase",
}


def schedule(bot, group, path):
    global _task
    if _task is None:
        _task = asyncio.create_task(_all(bot, group, path))


async def _all(bot, group, path):
    for item in MESSAGES:
        await correct(bot, group, path, item)


def _title(text):
    return " ".join(str(text or "").casefold().replace("’", "'").split())


def _matches(item, record, status, title, publication, label):
    if item == JEANS:
        photo = record.get("rough") or {}
        return (
            status == "insufficient"
            and record.get("resale_pence") is None
            and photo.get("status") == "estimated"
            and photo.get("basis") == "photo_brand"
            and (photo.get("low_pence"), photo.get("quick_pence"), photo.get("high_pence"))
            == (1500, 2200, 3000)
            and _title(title) == "jeans (don't buy listing)"
            and label == "Photo estimate £15.00–£30.00 · 0 verified comparables"
        )
    evidence = record.get("evidence") or []
    if (
        status != "estimated"
        or publication != "accepted"
        or record.get("resale_pence") != {ITEM: 950, MANGO: 900}[item]
    ):
        return False
    ids = set()
    for entry in evidence:
        match = re.fullmatch(
            r"https://(?:www\.)?vinted\.co\.uk/items/([0-9]+)(?:-[^/?#]+)?/?",
            str(entry.get("url") or ""),
        )
        if not match or match.group(1) != str(entry.get("id")):
            return False
        ids.add(match.group(1))
    if item == MANGO:
        return "10924241338" in ids and _title(title) == "mango chocolate brown leather jacket"
    return len(evidence) == len(COMPS) and ids == COMPS


def archived(conn, version, now, item=ITEM, title=None):
    """Prepare the correction and snapshot the original valuation once, in the same transaction."""
    window = conn.execute(
        "SELECT started,ends FROM hour_window WHERE id=1 AND group_version=?", (version,)
    ).fetchone()
    if item not in MESSAGES or not window or not window[0] <= now < window[1]:
        return None
    row = conn.execute(
        """SELECT parent_message,reply_message,accepted,ready,sent,status,result,publish_state
        FROM hour_alerts WHERE item_id=? AND accepted BETWEEN ? AND ?""",
        (item, *window),
    ).fetchone()
    if (
        not row
        or row[0] == row[1]
        or (item != JEANS and (not row[1] or not row[4] or not window[0] <= row[4] < window[1]))
    ):
        return None
    parent, reply, accepted, ready, sent, status, source, publication = row
    if status == "withdrawn":
        snapshot = conn.execute(
            "SELECT reply_message,parent_message FROM hour_wrong_price_original WHERE item_id=? AND group_version=?",
            (item, version),
        ).fetchone()
        return (parent, reply, window[1]) if snapshot == (reply or 0, parent) else None
    label = None
    if item in (MANGO, JEANS):
        entry = conn.execute("SELECT label FROM hour_numeric_cards WHERE item_id=?", (item,)).fetchone()
        label = entry[0] if entry else None
    if not source or not _matches(item, json.loads(source), status, title, publication, label):
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
        (item, version, parent, reply or 0, accepted, ready, sent, status, source, publication),
    )
    if item in (MANGO, JEANS):
        conn.execute("""CREATE TABLE IF NOT EXISTS hour_wrong_price_card(
            item_id TEXT PRIMARY KEY, old_label TEXT, action TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            ack_at REAL, next_at REAL NOT NULL DEFAULT 0)""")
        conn.execute(
            "INSERT INTO hour_wrong_price_card(item_id,old_label,action) VALUES(?, ?, ?)",
            (item, label, "pending" if label else "never_shown"),
        )
    corrected = {
        "status": "withdrawn",
        "resale_pence": None,
        "baseline_pence": None,
        "evidence": [],
        "explanation": MESSAGES[item],
        "withdrawn_at": now,
    }
    conn.execute(
        "UPDATE hour_alerts SET status='withdrawn',result=?,publish_state='withdrawn_awaiting_correction' WHERE item_id=?",
        (json.dumps(corrected), item),
    )
    conn.execute(
        "INSERT INTO hour_wrong_price_delivery VALUES(?,?,0,0,NULL,NULL)",
        (item, "edit_pending" if reply else "fallback_pending"),
    )
    logger.info("Finder hourly valuation withdrawn item=%s reason=%s", item, REASONS[item])
    return parent, reply, window[1]


def prepare(path, version, item=ITEM, title=None):
    with closing(sqlite3.connect(path, timeout=1)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        return archived(conn, version, time.time(), item, title)


def original_parent(path, item):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)) as conn:
        row = conn.execute("SELECT parent_message FROM hour_alerts WHERE item_id=?", (item,)).fetchone()
        return row[0] if row else None


def claim(path, now, item=ITEM):
    with closing(sqlite3.connect(path, timeout=1)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT state,attempts,next_at FROM hour_wrong_price_delivery WHERE item_id=?", (item,)
        ).fetchone()
        if not row or row[0] not in ("edit_pending", "fallback_pending"):
            return "done", 0
        state, attempts, next_at = row
        if next_at > now:
            return "wait", next_at - now
        if state == "fallback_pending":
            conn.execute(
                "UPDATE hour_wrong_price_delivery SET state='fallback_attempted' WHERE item_id=?",
                (item,),
            )
            return "fallback", 0
        if attempts >= 3:
            conn.execute(
                "UPDATE hour_wrong_price_delivery SET state='edit_unconfirmed' WHERE item_id=?", (item,)
            )
            return "unconfirmed", 0
        conn.execute(
            "UPDATE hour_wrong_price_delivery SET attempts=attempts+1,next_at=? WHERE item_id=?",
            (now + 16, item),
        )
        return "edit", 0


def update(path, state, message=None, delay=0, item=ITEM):
    now = time.time()
    with closing(sqlite3.connect(path, timeout=1)) as conn, conn:
        conn.execute(
            "UPDATE hour_wrong_price_delivery SET state=?,next_at=?,ack_at=?,correction_message=? WHERE item_id=?",
            (state, now + delay, now if message else None, message, item),
        )
        if message:
            conn.execute(
                "UPDATE hour_alerts SET publish_state=? WHERE item_id=?", ("withdrawn_" + state, item)
            )
    logger.info(
        "Finder hourly valuation correction item=%s state=%s telegram_ack=%s",
        item,
        state,
        int(bool(message)),
    )


async def _authorised(group, parent, item=ITEM):
    import telegram_shared_alerts as shared

    if await asyncio.to_thread(shared.current) != group:
        return False
    original = await asyncio.to_thread(shared.lookup, group, parent)
    return original if original and str(original.get("item_id")) == item else None


async def correct(bot, group, path, item=ITEM):
    import telegram_shared_alerts as shared
    from trial_shared_hour import eligible_service

    if not eligible_service() or await asyncio.to_thread(shared.current) != group:
        return
    try:
        parent = await asyncio.to_thread(original_parent, path, item)
        original = await _authorised(group, parent, item) if parent else None
        if not original:
            return
        data = await asyncio.to_thread(prepare, path, group.version, item, original.get("title"))
        if not data:
            return
        parent, reply, ends = data
        if item in (MANGO, JEANS):
            import trial_resale_button

            trial_resale_button.withdraw(item)

        async def save(state, message=None, delay=0):
            await asyncio.to_thread(update, path, state, message, delay, item)

        snapshot = await _deliver(bot, group, path, item, parent, reply, ends, save)
        if item in (MANGO, JEANS):
            await correct_card(bot, group, path, item, parent, ends, snapshot)
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "Finder hourly valuation correction item=%s state=unavailable reason=%s",
            item,
            type(exc).__name__,
        )


async def _deliver(bot, group, path, item, parent, reply, ends, save):
    while time.time() < ends and await _authorised(group, parent, item):
        action, delay = await asyncio.to_thread(claim, path, time.time(), item)
        if action == "done":
            return
        if action == "unconfirmed":
            await save("edit_unconfirmed")
            return
        if action == "wait":
            await asyncio.sleep(min(ends - time.time(), delay))
            continue
        if time.time() >= ends or not await _authorised(group, parent, item):
            return
        kwargs = {
            "chat_id": group.chat,
            "text": MESSAGES[item],
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
                await save("edit_confirmed", reply)
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
                await save("fallback_pending")
                continue
            await save(action + "_rejected")
            return
        except Forbidden:
            await save(action + "_forbidden")
            return
        except RetryAfter as exc:
            retry = exc.retry_after
            seconds = (retry.total_seconds() if hasattr(retry, "total_seconds") else retry) + 1
            if action == "edit" and time.time() + seconds < ends:
                await save("edit_pending", None, seconds)
                continue
            await save(action + "_throttled")
            return
        except TelegramError:
            if action == "edit":
                await save("edit_pending", None, 2)
                continue
            await save("fallback_unconfirmed")
            return
        message = getattr(result, "message_id", None)
        if action == "edit" and (message == reply or result is True):
            await save("edit_confirmed", reply)
        elif action == "fallback" and type(message) is int and message not in (parent, reply):
            await save("fallback_confirmed", message)
        else:
            await save(action + "_unconfirmed")
        return getattr(result, "reply_to_message", None)


def card_claim(path, item):
    with closing(sqlite3.connect(path, timeout=0.5)) as conn, conn:
        now = time.time()
        return bool(
            conn.execute(
                """UPDATE hour_wrong_price_card SET attempts=attempts+1,next_at=?
               WHERE item_id=? AND action IN ('pending','editing') AND attempts<3 AND next_at<=?""",
                (now + 14, item, now),
            ).rowcount
        )


def card_state(path, item, state):
    with closing(sqlite3.connect(path, timeout=0.5)) as conn, conn:
        conn.execute(
            "UPDATE hour_wrong_price_card SET action=?,ack_at=? WHERE item_id=?",
            (state, time.time() if state == "confirmed" else None, item),
        )
        if state == "confirmed":
            conn.execute(
                "UPDATE hour_numeric_cards SET label='Resale · withdrawn' WHERE item_id=?", (item,)
            )
    logger.info(
        "Finder hourly valuation correction card item=%s state=%s telegram_ack=%s",
        item,
        state,
        int(state == "confirmed"),
    )


async def correct_card(bot, group, path, item, parent, ends, snapshot):
    from telegram import InlineKeyboardMarkup

    import photo_cards
    import telegram_shared_alerts as shared
    import trial_resale_button
    import vinted_buying
    from vinted_telegram_progress import active_item

    if time.time() >= ends or not await asyncio.to_thread(card_claim, path, item):
        return
    key = (group.chat, parent)
    lock = photo_cards.lock("shared-buy", key)
    if lock.locked() or vinted_buying.purchase_lock(key).locked() or active_item(item):
        await asyncio.to_thread(card_state, path, item, "deferred_to_trade_refresh")
        return
    async with lock:
        original = await _authorised(group, parent, item)
        if not original or time.time() >= ends:
            return
        previous = await asyncio.to_thread(vinted_buying.result, item)
        state = (previous or {}).get("state")
        received = (
            snapshot
            and getattr(snapshot, "message_id", None) == parent
            and getattr(getattr(snapshot, "chat", None), "id", None) == group.chat
        )
        keyboard = getattr(snapshot, "reply_markup", None) if received else None
        if state:
            # Only a verified current terminal keyboard may be relabelled; a trade in
            # motion updates its own keyboard from the withdrawn status on its next refresh.
            expected = {"paid": "Bought ✓", "closed": "Purchase closed"}.get(state)
            actual = [
                b.text
                for row in getattr(keyboard, "inline_keyboard", ())
                for b in row
                if b.callback_data == "shared:status"
            ]
            if not expected or expected not in actual:
                await asyncio.to_thread(card_state, path, item, "deferred_to_trade_refresh")
                return
        if keyboard is None:
            # This precise original earlier received a numeric label via _render, which
            # edits only pristine 3-row keyboards. No purchase state or interaction exists.
            keyboard = shared.markup(original, show_resale=True)
        if (
            not any(
                b.callback_data == trial_resale_button.CALLBACK
                for row in keyboard.inline_keyboard
                for b in row
            )
            or vinted_buying.purchase_lock(key).locked()
            or active_item(item)
            or await asyncio.to_thread(shared.current) != group
            or time.time() >= ends
        ):
            return
        updated = InlineKeyboardMarkup(
            [
                [
                    trial_resale_button.button(item)
                    if b.callback_data == trial_resale_button.CALLBACK
                    else b
                    for b in row
                ]
                for row in keyboard.inline_keyboard
            ]
        )
        try:
            response = await bot.edit_message_reply_markup(
                chat_id=group.chat,
                message_id=parent,
                reply_markup=updated,
                connect_timeout=3,
                pool_timeout=2,
                read_timeout=5,
                write_timeout=4,
            )
        except BadRequest as exc:
            state = "confirmed" if "message is not modified" in str(exc).lower() else "rejected"
        except TelegramError:
            state = "unconfirmed"
        else:
            state = (
                "confirmed"
                if response is True or getattr(response, "message_id", None) == parent
                else "unconfirmed"
            )
        await asyncio.to_thread(card_state, path, item, state)
