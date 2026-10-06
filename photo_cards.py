"""Native photo alerts with readable captions and in-message example switching."""

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from html import escape, unescape
from weakref import WeakValueDictionary

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaPhoto,
)
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

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


def control_health(platform, event, error=""):
    if event not in ("poll", "click", "success", "error"):
        raise ValueError("Unknown control event")
    with closing(connection()) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO telegram_control_health(platform) VALUES (?)",
            (platform,),
        )
        if event != "error":
            conn.execute(
                f"UPDATE telegram_control_health SET last_{event}=? WHERE platform=?",
                (time.time(), platform),
            )
        if event in ("success", "error"):
            conn.execute(
                "UPDATE telegram_control_health SET error=? WHERE platform=?",
                (error, platform),
            )


def cache_key(bot, platform):
    # Telegram file IDs belong to one bot. Rotating a token invalidates its cache.
    return hashlib.sha256(
        (platform + ":" + str(getattr(bot, "token", "offline"))).encode()
    ).hexdigest()


def cached_example(bot, platform, reference_id):
    with closing(connection()) as conn:
        row = conn.execute(
            "SELECT file_id FROM telegram_example_cache WHERE bot_key=? AND reference_id=?",
            (cache_key(bot, platform), reference_id),
        ).fetchone()
    return row[0] if row else None


def cache_example(bot, platform, reference_id, file_id):
    if not reference_id:
        return
    with closing(connection()) as conn, conn:
        if file_id:
            conn.execute(
                """INSERT INTO telegram_example_cache SELECT ?,id,? FROM dashboard_media WHERE id=?
                ON CONFLICT(bot_key,reference_id) DO UPDATE SET file_id=excluded.file_id""",
                (cache_key(bot, platform), file_id, reference_id),
            )
        else:
            conn.execute(
                "DELETE FROM telegram_example_cache WHERE bot_key=? AND reference_id=?",
                (cache_key(bot, platform), reference_id),
            )


async def answer(query, text=None, *, alert=False):
    """A delayed/expired callback acknowledgement must not cancel a valid edit."""
    try:
        await asyncio.wait_for(query.answer(text, show_alert=alert, cache_time=0), 2)
    except (TelegramError, TimeoutError):
        pass


def recover(platform, message):
    saved = load(platform, message.message_id)
    if saved:
        return saved
    with closing(connection()) as conn:
        row = conn.execute(
            "SELECT * FROM alert_outbox WHERE platform=? AND telegram_message_id=?",
            (platform, message.message_id),
        ).fetchone()
    if row:
        import ebay_alerts
        import vinted_alerts

        details = (ebay_alerts if platform == "ebay" else vinted_alerts).get_details(
            dict(row)
        )
        if details:
            record(dict(row), details, message)
            return load(platform, message.message_id)
    return None


async def edit_with_retry(bot, kwargs):
    # Editing the same message is idempotent; never re-send the notification.
    for attempt in range(2):
        try:
            return await bot.edit_message_media(**kwargs)
        except RetryAfter as exc:
            delay = exc.retry_after
            delay = delay.total_seconds() if hasattr(delay, "total_seconds") else delay
            if attempt or delay > 4:
                raise
            await asyncio.sleep(delay + 0.1)
        except NetworkError:
            if attempt:
                raise
            await asyncio.sleep(0.25)


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
    if details.get("platform") == "ebay":
        from ebay_images import resolve

        await resolve(row, details)
    elif not details.get("photos") and alert_images.safe_listing_photo(
        row.get("photo_url")
    ):
        details["photos"] = [row["photo_url"]]
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
        if platform == "ebay" and not details.get("photos"):
            from ebay_images import resolve

            await resolve(row, details)
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
        await answer(query, "This is a private notification bot.", alert=True)
        return
    data = query.data or ""
    if not re.fullmatch(r"card:(listing|examples|notes:[0-9]{1,2})", data):
        await answer(query)
        return
    control_health(platform, "click")
    # Acknowledge before taking a message lock or touching the image. Separate
    # messages may proceed concurrently, while taps on one message stay ordered.
    await answer(query)
    async with lock(platform, message.message_id):
        saved = recover(platform, message)
        if not saved:
            await answer(
                query,
                "This saved alert is no longer available. Open Recent Finds in your dashboard.",
                alert=True,
            )
            return
        row, details, card = saved
        view = data[5:]
        started = time.monotonic()
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
                control_health(platform, "success")
                return
            file_key = "example_file_id" if view == "examples" else "listing_file_id"
            media = card[file_key]
            if view == "examples" and not media:
                media = cached_example(bot, platform, row.get("reference_id"))
            if not media:
                if view == "examples":
                    reference = (
                        dashboard_store.get_media(row["reference_id"])
                        if row["reference_id"]
                        else None
                    )
                    # Uploads are already normalized and collaged when saved.
                    media = reference["image"] if reference else None
                else:
                    media = await listing_photo(details)
            if media is None:
                control_health(
                    platform,
                    "error",
                    (
                        "Example image unavailable"
                        if view == "examples"
                        else "Listing image unavailable"
                    ),
                )
                await answer(
                    query,
                    "The saved image is unavailable. Check this search’s photos in your dashboard.",
                    alert=True,
                )
                return
            if load(platform, message.message_id) is None:
                return
            kwargs = dict(
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
            try:
                result = await edit_with_retry(bot, kwargs)
            except BadRequest as exc:
                # Cached file IDs can become invalid. Recover from our stored
                # JPEG once; never share file IDs between the two Telegram bots.
                if (
                    not isinstance(media, str)
                    or view != "examples"
                    or not any(
                        term in str(exc).lower()
                        for term in ("file", "identifier", "media")
                    )
                ):
                    raise
                cache_example(bot, platform, row.get("reference_id"), None)
                reference = dashboard_store.get_media(row.get("reference_id"))
                if not reference:
                    raise
                kwargs["media"] = InputMediaPhoto(
                    reference["image"],
                    filename="examples.jpg",
                    caption=captions(row, details)[0],
                    parse_mode="HTML",
                    show_caption_above_media=True,
                )
                result = await edit_with_retry(bot, kwargs)
            after_edit(
                platform,
                message.message_id,
                view=view,
                file_id=result.photo[-1].file_id,
            )
            if view == "examples":
                cache_example(
                    bot, platform, row.get("reference_id"), result.photo[-1].file_id
                )
            control_health(platform, "success")
            logger.info(
                "%s photo control accepted; message_id=%s view=%s elapsed=%.3fs",
                platform,
                message.message_id,
                view,
                time.monotonic() - started,
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
            after_edit(platform, message.message_id, view=view)
            control_health(platform, "success")
        finally:
            # An API timeout may still have applied the edit. Re-queue removal
            # if account deletion raced either a successful or uncertain edit.
            if platform == "ebay" and load(platform, message.message_id) is None:
                from ebay_privacy import queue_redaction

                with closing(connection()) as conn, conn:
                    queue_redaction(conn, message.message_id)


async def vinted_callback(update, context):
    await dispatch_callback(
        context.bot,
        update.callback_query,
        "vinted",
        db.get_parameter("telegram_chat_id"),
    )


async def dispatch_callback(bot, query, platform, chat_id):
    try:
        await handle_callback(bot, query, platform, chat_id)
    except Exception as exc:  # noqa: BLE001 -- isolate a failed image from the listener
        control_health(platform, "error", type(exc).__name__)
        logger.warning("%s photo control failed: %s", platform, type(exc).__name__)
        await answer(
            query,
            "Telegram could not update the picture. Please tap the button again.",
            alert=True,
        )


async def poll_ebay_callbacks(bot, chat_id):
    """Long-poll Telegram only; consumes no eBay Browse API calls."""
    offset = None
    tasks = set()
    semaphore = asyncio.Semaphore(6)

    async def dispatch(query):
        async with semaphore:
            await dispatch_callback(bot, query, "ebay", chat_id)

    while True:
        try:
            updates = await bot.get_updates(
                offset=offset,
                timeout=30,
                read_timeout=35,
                allowed_updates=["callback_query"],
            )
            control_health("ebay", "poll")
            for update in updates:
                offset = update.update_id + 1
                if update.callback_query:
                    if len(tasks) >= 32:
                        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    task = asyncio.create_task(dispatch(update.callback_query))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        except TelegramError:
            control_health("ebay", "error", "Telegram connection will retry")
            logger.warning("eBay photo-control connection will retry")
            await asyncio.sleep(5)


def health_summary():
    from datetime import datetime, timezone

    with closing(connection()) as conn:
        rows = {
            r["platform"]: dict(r)
            for r in conn.execute("SELECT * FROM telegram_control_health")
        }
    result = []
    for platform in ("vinted", "ebay"):
        row = rows.get(platform, {})
        value = row.get("last_success")
        result.append(
            {
                "platform": platform,
                "name": "Vinted" if platform == "vinted" else "eBay",
                "last_success": (
                    datetime.fromtimestamp(value, timezone.utc).strftime(
                        "%d %b %H:%M:%S UTC"
                    )
                    if value
                    else "Not checked yet"
                ),
                "error": row.get("error", ""),
            }
        )
    return result


async def test_controls(platform):
    """Exercise real Telegram media edits on one labelled, owner-requested test."""
    from types import SimpleNamespace

    from telegram import Bot

    import vinted_alerts

    with closing(connection()) as conn:
        saved = conn.execute(
            "SELECT * FROM alert_outbox WHERE platform=? AND reference_id IS NOT NULL ORDER BY found_at DESC LIMIT 1",
            (platform,),
        ).fetchone()
    if not saved:
        raise ValueError("A recent alert with example photos is needed for this test.")
    row = dict(saved)
    if platform == "ebay":
        import ebay_alerts
        from ebay_store import configuration

        config = configuration()
        token, chat_id = config["telegram_token"], config["chat_id"]
        details = ebay_alerts.get_details(row)
    else:
        token, chat_id = db.get_parameter("telegram_token"), db.get_parameter(
            "telegram_chat_id"
        )
        details = vinted_alerts.get_details(row)
    if not details or not token or not chat_id:
        raise ValueError("The saved alert or Telegram connection is unavailable.")
    details = dict(
        details, name="PHOTO & BUTTON TEST · " + details.get("name", platform)
    )

    async def ready(*args, **kwargs):
        pass

    async with Bot(token) as bot:
        first = await send_initial(
            bot, chat_id, row, details, ready, require_photo=True
        )
        row["telegram_message_id"] = first.message_id
        if platform == "ebay" and not ebay_alerts.track_preview(row):
            raise ValueError("This listing was removed during the test.")
        query = SimpleNamespace(message=first, answer=ready, data="card:examples")
        for view in ("examples", "listing"):
            await asyncio.sleep(1.1)
            query.data = "card:" + view
            await handle_callback(bot, query, platform, chat_id)
            saved = load(platform, first.message_id)
            field = "example_file_id" if view == "examples" else "listing_file_id"
            if not saved or saved[2]["view"] != view or not saved[2][field]:
                raise ValueError(
                    "The test photo was sent but Telegram did not confirm both picture buttons."
                )
    return f"{platform.title()} PHOTO & BUTTON TEST sent. Telegram confirmed the initial listing photo, your saved examples, and switching back in the same message. Hold the new notification on your iPhone to check its preview."
