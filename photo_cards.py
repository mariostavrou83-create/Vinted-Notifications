"""Native photo alerts with readable captions and in-message example switching."""

import asyncio
import json
import re
import sqlite3
from contextlib import closing
from html import escape, unescape
from weakref import WeakValueDictionary

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaPhoto,
)
from telegram.error import BadRequest, TelegramError

import alert_images
import dashboard_store
import db
from logger import get_logger
from search_settings import connection

logger = get_logger(__name__)
TIMEOUTS = {
    "read_timeout": 8,
    "write_timeout": 8,
    "connect_timeout": 3,
    "pool_timeout": 3,
}
_locks = WeakValueDictionary()


def enabled():
    return db.get_parameter("native_photo_cards") == "1"


def enable():
    with closing(connection()) as conn, conn:
        conn.executemany(
            "INSERT OR REPLACE INTO parameters VALUES (?, '1')",
            [("native_photo_cards",), ("vinted_single_message_alerts",)],
        )


def plain(value):
    return unescape(re.sub(r"<[^>]+>", "", value))


def units(value):
    return len(value.encode("utf-16-le")) // 2


def pages(value, limit=850):
    """Split complete notes without losing characters or splitting an emoji."""
    result, part, length = [], "", 0
    for char in value:
        width = units(char)
        if length + width > limit:
            result.append(part)
            part, length = "", 0
        part += char
        length += width
    return result + [part]


def captions(row, details):
    from vinted_alerts import sections
    from vinted_native import short

    parts = sections(row, details)
    full = "\n\n".join(p for p in parts if p)
    notes = "\n\n".join(plain(p) for p in parts[3:] if p)
    if units(plain(full)) <= 1024:
        return full, []
    # Preserve all notes via the Full notes pages if the normal caption is too long.
    row = dict(row, title=short(row["title"], 150), price=short(row["price"], 30))
    details = dict(
        details,
        name=short(details.get("name") or row["search_name"], 80),
        brand=short(details.get("brand") or "Not specified", 60),
    )
    base = "\n\n".join(sections(row, details)[:3])
    if units(plain(base)) + units(notes) + 2 <= 1024:
        return base + ("\n\n" + escape(notes) if notes else ""), []
    suffix = "\n\nTap Full notes below for the complete guide and reminder."
    available = max(0, 1024 - units(plain(base)) - units(suffix) - 2)
    return base + "\n\n" + escape(short(notes, available)) + suffix, pages(notes)


def markup(row, details, *, view="listing", note_page=None):
    marketplace = "eBay" if details.get("platform") == "ebay" else "Vinted"
    buttons = [
        [InlineKeyboardButton("Open " + marketplace + " listing ↗", url=row["url"])]
    ]
    if row.get("reference_id"):
        buttons.append(
            [
                InlineKeyboardButton("Listing photos", callback_data="card:listing"),
                InlineKeyboardButton("Your examples", callback_data="card:examples"),
            ]
        )
    elif note_page is not None:
        buttons.append(
            [InlineKeyboardButton("Back to listing", callback_data="card:listing")]
        )
    _, note_pages = captions(row, details)
    if note_pages:
        nav = []
        if note_page is None:
            nav.append(InlineKeyboardButton("Full notes", callback_data="card:notes:0"))
        else:
            if note_page:
                nav.append(
                    InlineKeyboardButton(
                        "← Previous notes", callback_data=f"card:notes:{note_page - 1}"
                    )
                )
            if note_page + 1 < len(note_pages):
                nav.append(
                    InlineKeyboardButton(
                        "Next notes →", callback_data=f"card:notes:{note_page + 1}"
                    )
                )
        if nav:
            buttons.append(nav)
    return InlineKeyboardMarkup(buttons)


def lock(platform, message_id):
    key = (id(asyncio.get_running_loop()), platform, message_id)
    result = _locks.get(key)
    if result is None:
        result = asyncio.Lock()
        _locks[key] = result
    return result


def load(platform, message_id):
    with closing(connection()) as conn:
        card = conn.execute(
            "SELECT * FROM telegram_photo_cards WHERE platform=? AND message_id=?",
            (platform, message_id),
        ).fetchone()
        if card is None:
            return None
        row = conn.execute(
            "SELECT * FROM alert_outbox WHERE item_id=? AND platform=?",
            (card["item_id"], platform),
        ).fetchone()
    if row is None:
        return None
    return (
        dict(row, reference_id=card["reference_id"], telegram_message_id=message_id),
        json.loads(card["details"]),
        dict(card),
    )


def record(row, details, result):
    platform = details.get("platform", "vinted")
    file_id = result.photo[-1].file_id if getattr(result, "photo", None) else None
    try:
        with closing(connection()) as conn, conn:
            if not conn.execute(
                "SELECT 1 FROM alert_outbox WHERE item_id=? AND platform=?",
                (row["item_id"], platform),
            ).fetchone():
                if platform == "ebay":
                    from ebay_privacy import queue_redaction

                    queue_redaction(conn, result.message_id)
                return False
            conn.execute(
                """INSERT INTO telegram_photo_cards
                (platform,message_id,item_id,reference_id,details,listing_file_id)
                VALUES (?,?,?,?,?,?)""",
                (
                    platform,
                    result.message_id,
                    row["item_id"],
                    row.get("reference_id"),
                    json.dumps(details),
                    file_id,
                ),
            )
    except sqlite3.Error:
        # A bookkeeping failure must not resend an already accepted notification.
        logger.exception(
            "Could not record native photo controls for message %s", result.message_id
        )
    return True


def after_edit(platform, message_id, *, view, file_id=None):
    with closing(connection()) as conn, conn:
        if not conn.execute(
            "SELECT 1 FROM telegram_photo_cards WHERE platform=? AND message_id=?",
            (platform, message_id),
        ).fetchone():
            if platform == "ebay":
                from ebay_privacy import queue_redaction

                queue_redaction(conn, message_id)
            return
        conn.execute(
            "UPDATE telegram_photo_cards SET view=? WHERE platform=? AND message_id=?",
            (view, platform, message_id),
        )
        if file_id:
            column = "example_file_id" if view == "examples" else "listing_file_id"
            conn.execute(
                f"UPDATE telegram_photo_cards SET {column}=? WHERE platform=? AND message_id=?",
                (file_id, platform, message_id),
            )


async def listing_photo(details):
    try:
        return await asyncio.wait_for(
            alert_images.listing_collage(details.get("photos", [])[:4]), 6
        )
    except TimeoutError:
        return None


async def send_initial(bot, chat_id, row, details, before_send, *, require_photo=False):
    raw = await listing_photo(details)
    if raw is None and require_photo:
        raise ValueError("The listing photo is unavailable. Try a newer find.")
    await before_send()
    # Recheck after the download: an eBay deletion may have raced it.
    if details.get("platform") == "ebay":
        with closing(connection()) as conn:
            if not conn.execute(
                "SELECT 1 FROM alert_outbox WHERE item_id=? AND platform='ebay'",
                (row["item_id"],),
            ).fetchone():
                raise TelegramError("Listing removed before photo delivery")
    caption, _ = captions(row, details)
    common = dict(
        chat_id=chat_id,
        parse_mode="HTML",
        reply_markup=markup(row, details),
        **TIMEOUTS,
    )
    if raw:
        result = await bot.send_photo(
            **common,
            photo=InputFile(raw, filename="listing.jpg"),
            caption=caption,
            show_caption_above_media=True,
        )
        if not result.message_id or not result.photo:
            raise TelegramError("Telegram did not confirm the listing photo")
    else:
        from telegram import LinkPreviewOptions

        result = await bot.send_message(
            **common,
            text=caption,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    record(row, details, result)
    logger.info(
        "Photo alert accepted for item %s; message_id=%s; native_photo=%s",
        row["item_id"],
        result.message_id,
        bool(raw),
    )
    return result


async def enrich(bot, chat_id, row, details, before_edit):
    """Normally a no-op: the initial request already contains the full collage."""
    platform = details.get("platform", "vinted")
    message_id = row["telegram_message_id"]
    async with lock(platform, message_id):
        saved = load(platform, message_id)
        if not saved:
            return True
        if saved[2]["listing_file_id"] or saved[2]["view"] != "listing":
            return True
        raw = await listing_photo(details)
        if raw is None:
            return False
        await before_edit()
        if load(platform, message_id) is None:
            return True
        result = await bot.edit_message_media(
            chat_id=chat_id,
            message_id=message_id,
            media=InputMediaPhoto(
                raw,
                filename="listing.jpg",
                caption=captions(row, details)[0],
                parse_mode="HTML",
                show_caption_above_media=True,
            ),
            reply_markup=markup(row, details),
            **TIMEOUTS,
        )
        after_edit(
            platform, message_id, view="listing", file_id=result.photo[-1].file_id
        )
        return True


async def handle_callback(bot, query, platform, chat_id):
    message = query.message
    if not message or str(message.chat.id) != str(chat_id):
        await query.answer("This is a private notification bot.", show_alert=True)
        return
    data = query.data or ""
    if not re.fullmatch(r"card:(listing|examples|notes:[0-9]{1,2})", data):
        await query.answer()
        return
    async with lock(platform, message.message_id):
        saved = load(platform, message.message_id)
        if not saved:
            await query.answer(
                "This saved alert is no longer available.", show_alert=True
            )
            return
        await query.answer()
        row, details, card = saved
        view = data[5:]
        try:
            if view.startswith("notes:"):
                index = int(view.split(":")[1])
                note_pages = captions(row, details)[1]
                if index >= len(note_pages):
                    return
                text = (
                    f"<b>Buying guide and reminder · {index + 1}/{len(note_pages)}</b>\n\n"
                    + escape(note_pages[index])
                )
                if load(platform, message.message_id) is None:
                    return
                kwargs = dict(
                    chat_id=chat_id,
                    message_id=message.message_id,
                    parse_mode="HTML",
                    reply_markup=markup(row, details, note_page=index),
                    **TIMEOUTS,
                )
                if card["listing_file_id"] or card["example_file_id"]:
                    await bot.edit_message_caption(
                        **kwargs, caption=text, show_caption_above_media=True
                    )
                else:
                    from telegram import LinkPreviewOptions

                    await bot.edit_message_text(
                        **kwargs,
                        text=text,
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    )
                after_edit(platform, message.message_id, view=view)
                return
            file_key = "example_file_id" if view == "examples" else "listing_file_id"
            media = card[file_key]
            if not media:
                if view == "examples":
                    reference = (
                        dashboard_store.get_media(row["reference_id"])
                        if row["reference_id"]
                        else None
                    )
                    media = (
                        await asyncio.to_thread(
                            alert_images.normalize_available, [reference["image"]]
                        )
                        if reference
                        else None
                    )
                else:
                    media = await listing_photo(details)
            if media is None:
                return
            if load(platform, message.message_id) is None:
                return
            result = await bot.edit_message_media(
                chat_id=chat_id,
                message_id=message.message_id,
                media=InputMediaPhoto(
                    media,
                    filename=view + ".jpg",
                    caption=captions(row, details)[0],
                    parse_mode="HTML",
                    show_caption_above_media=True,
                ),
                reply_markup=markup(row, details, view=view),
                **TIMEOUTS,
            )
            after_edit(
                platform,
                message.message_id,
                view=view,
                file_id=result.photo[-1].file_id,
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
        finally:
            # An API timeout may still have applied the edit. Re-queue removal
            # if account deletion raced either a successful or uncertain edit.
            if platform == "ebay" and load(platform, message.message_id) is None:
                from ebay_privacy import queue_redaction

                with closing(connection()) as conn, conn:
                    queue_redaction(conn, message.message_id)


async def vinted_callback(update, context):
    try:
        await handle_callback(
            context.bot,
            update.callback_query,
            "vinted",
            db.get_parameter("telegram_chat_id"),
        )
    except TelegramError:
        logger.warning("Could not update Vinted photo controls")


async def poll_ebay_callbacks(bot, chat_id):
    """Long-poll Telegram only; consumes no eBay Browse API calls."""
    offset = None
    while True:
        try:
            updates = await bot.get_updates(
                offset=offset,
                timeout=30,
                read_timeout=35,
                allowed_updates=["callback_query"],
            )
            for update in updates:
                offset = update.update_id + 1
                if update.callback_query:
                    try:
                        await handle_callback(
                            bot, update.callback_query, "ebay", chat_id
                        )
                    except TelegramError:
                        logger.warning("Could not update eBay photo controls")
        except TelegramError:
            logger.warning("eBay photo-control connection will retry")
            await asyncio.sleep(5)
