"""Apply one trial estimate to the accepted private alert and its authorised group copy."""

import asyncio
import logging
import re
import sqlite3
import time
from collections import OrderedDict
from contextlib import closing
from html import escape

from telegram import LinkPreviewOptions
from telegram.error import BadRequest, RetryAfter, TelegramError

import photo_cards
from resale_pricing import valuation_html
from search_settings import connection

logger = logging.getLogger(__name__)
_messages = OrderedDict()
_lines = OrderedDict()
_tasks = set()
_enabled = False
_accepting = False
CAPTION_RESERVE = 110


def configure(enabled):
    global _enabled, _accepting
    _enabled = enabled
    _accepting = enabled
    if not enabled:
        for task in list(_tasks):
            task.cancel()
        _messages.clear()
        _lines.clear()


def finish_sample():
    global _accepting
    _accepting = False


async def close():
    tasks = list(_tasks)
    configure(False)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def reserving():
    return _accepting


def append_line(body, line):
    if not line or not body or line in photo_cards.plain(body):
        return body
    if line.startswith("Est. profit: "):
        # Only replace the four fields in Finder's final price recap. Seller text,
        # active checkout controls and progress below the recap stay intact.
        head, split, tail = body.rpartition("\nTotal: ")
        if split and "\nBrand: " in head:
            tail, n = re.subn(
                r"(?m)^Est\. profit: [^\n]*\nEst\. resell: [^\n]*\nBuy: [^\n]*\nConf: [^\n]*",
                lambda _: valuation_html(line),
                tail,
                count=1,
            )
            if n:
                return head + split + tail
        return body + "\n" + valuation_html(line)
    return body + "\n" + escape(line)


def message_text(message):
    for field in ("caption", "text", "caption_html", "text_html"):
        value = getattr(message, field, None)
        if isinstance(value, str) and value:
            return photo_cards.plain(value) if field.endswith("_html") else value
    return ""


def initial_price(message):
    footer = message_text(message).rpartition("\nTotal: ")[2]
    if "\nEst. resell: £" in footer:
        return "priced"
    return "unknown" if "\nConf: " in footer else "absent"


def visible_guidance(message):
    text = message_text(message)
    if not text:
        return "unverified"
    keyboard = (
        getattr(getattr(message, "reply_markup", None), "inline_keyboard", ()) or ()
    )
    labels = (
        [b.text for row in keyboard for b in row]
        if isinstance(keyboard, (tuple, list))
        else []
    )
    has_heading = re.search(
        r"(?im)^(?:💷 |📝 )?(?:Your )?Buying (?:guide|reminder)(?:[ ·:]|$)", text
    )
    has_button = any(
        "buying guide" in label.lower() or "buying reminder" in label.lower()
        for label in labels
    )
    return "visible" if has_heading or has_button else "hidden"


def group_body(identity, body):
    return append_line(body, _lines.get(str(identity)))


def restore_lines(records):
    if _enabled:
        _lines.update((str(identity), line) for identity, line in records if line)


def _schedule(identity, role):
    state = _messages.get((identity, role))
    if not state or not _lines.get(identity) or state.get("scheduled"):
        return
    state["scheduled"] = True
    task = asyncio.create_task(_edit(identity, role, state))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def remember(identity, message, *, group=None):
    identity = str(identity)
    if (
        not _enabled
        or (not _accepting and identity not in _lines)
        or not getattr(message, "message_id", None)
    ):
        return
    role = "group" if group else "private"
    _messages[identity, role] = {
        "message": message,
        "group": group,
        "received": time.monotonic(),
    }
    _messages.move_to_end((identity, role))
    if len(_messages) > 260:
        _messages.popitem(last=False)
    _schedule(identity, role)


def group_revision(identity, message):
    state = _messages.get((str(identity), "group"))
    if (
        state
        and getattr(message, "message_id", None) == state["message"].message_id
        and str(getattr(getattr(message, "chat", None), "id", ""))
        == str(state["group"].chat)
    ):
        state["message"] = message


def publish(identity, line):
    if not _enabled:
        return
    identity = str(identity)
    _lines[identity] = line
    if len(_lines) > 130:
        _lines.popitem(last=False)
    for role in ("private", "group"):
        _schedule(identity, role)


async def _edit(identity, role, state):
    msg = state["message"]
    group = state["group"]
    lock = (
        photo_cards.lock("shared-buy", (group.chat, msg.message_id))
        if group
        else photo_cards.lock("vinted", msg.message_id)
    )
    for attempt in range(2):
        try:
            async with lock:
                if not _enabled or _messages.get((identity, role)) is not state:
                    return
                line = _lines.get(identity)
                if not line:
                    return
                msg = state["message"]
                is_photo = bool(msg.photo)
                body = msg.caption_html if is_photo else msg.text_html
                keyboard = msg.reply_markup
                if group:
                    import telegram_shared_alerts as shared

                    if shared.current() != group or str(msg.chat.id) != str(group.chat):
                        return
                    if line in photo_cards.plain(body or ""):
                        return
                    body = append_line(body, line)
                else:
                    saved = photo_cards.load("vinted", msg.message_id)
                    if saved:
                        row, details, card = saved
                        if str(row["item_id"]) != identity:
                            return
                        # Merge into the latest snapshot under the same lock as photo and purchase edits.
                        with closing(connection()) as conn, conn:
                            conn.execute(
                                """UPDATE telegram_photo_cards SET details=json_set(details,'$.resale_line',?)
                                WHERE platform='vinted' AND item_id=? AND message_id=?""",
                                (line, identity, msg.message_id),
                            )
                        details["resale_line"] = line
                        if line in photo_cards.plain(body or ""):
                            return
                        if card["view"].startswith("notes:"):
                            return  # The main view will include it when the owner returns.
                        body, _ = photo_cards.captions(row, details)
                        keyboard = photo_cards.markup(row, details, view=card["view"])
                        is_photo = photo_cards.has_photo(card, details)
                    else:
                        # Modern Finder cards own their controls. A legacy card with dynamic
                        # buttons has no authoritative later snapshot to re-render safely.
                        if keyboard:
                            return
                        body = append_line(body, line)
                if not body or photo_cards.units(photo_cards.plain(body)) > (
                    1024 if is_photo else 4096
                ):
                    return
                options = dict(
                    parse_mode="HTML", reply_markup=keyboard, **photo_cards.TIMEOUTS
                )
                if is_photo:
                    result = await msg.edit_caption(
                        caption=body, show_caption_above_media=True, **options
                    )
                else:
                    result = await msg.edit_text(
                        text=body,
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                        **options
                    )
                if group:
                    group_revision(identity, result)
                logger.info(
                    "Telegram accepted resale estimate role=%s since_alert_ms=%d",
                    role,
                    round(1000 * (time.monotonic() - state["received"])),
                )
                return
        except RetryAfter as exc:
            wait = exc.retry_after
            seconds = (
                wait.total_seconds() if hasattr(wait, "total_seconds") else float(wait)
            )
            if attempt == 0 and seconds <= 30:
                await asyncio.sleep(seconds + 0.1)
                continue
            logger.info("Telegram resale estimate rate-limited role=%s", role)
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.info("Telegram resale estimate rejected role=%s", role)
        except (
            TelegramError,
            sqlite3.Error,
            ValueError,
            TypeError,
            AttributeError,
        ) as exc:
            logger.info(
                "Telegram resale estimate unavailable role=%s reason=%s",
                role,
                type(exc).__name__,
            )
        return
