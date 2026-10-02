"""Native Telegram photos for iPhone previews, with one comparison image per alert."""

import asyncio
import io
from contextlib import closing
from html import unescape

from PIL import Image, ImageDraw, ImageFont, ImageOps
from telegram import InputFile, InputMediaPhoto, LinkPreviewOptions
from telegram.error import BadRequest, TelegramError

import alert_images
import dashboard_store
import db
import vinted_gallery
from logger import get_logger
from search_settings import connection

logger = get_logger(__name__)


def enabled():
    return (
        db.get_parameter("vinted_native_photo_alerts") == "1" and not separate_panels()
    )


def separate_panels():
    return db.get_parameter("separate_photo_panels") != "0"


def enable():
    with closing(connection()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO parameters VALUES ('separate_photo_panels','0')"
        )
        conn.executemany(
            "INSERT INTO parameters(key,value) VALUES (?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value='1'",
            [("vinted_single_message_alerts",), ("vinted_native_photo_alerts",)],
        )


def short(text, units):
    """Telegram counts UTF-16 units, including two units for most emoji."""
    raw = str(text).encode("utf-16-le")
    if len(raw) <= units * 2:
        return str(text)
    return raw[: (units - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


def caption(row, details):
    from vinted_alerts import sections

    row = dict(row, title=short(row["title"], 450), price=short(row["price"], 30))
    details = dict(
        details,
        name=short(details.get("name") or row["search_name"], 100),
        brand=short(details.get("brand") or "Not specified", 120),
    )
    heading, link, listing, _, _ = sections(row, details)
    return f"{heading}\n\n{link}\n\n{listing}"


def font(size):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def wrapped_lines(text, face, width):
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines = []
    for paragraph in text.replace("\r", "").replace("\t", " ").split("\n"):
        line = ""
        for char in paragraph:
            if line and draw.textlength(line + char, font=face) > width:
                split = line.rfind(" ")
                if split > len(line) // 2:
                    lines.append(line[:split])
                    line = line[split + 1 :] + char
                else:
                    lines.append(line)
                    line = char
            else:
                line += char
        lines.append(line)
    return lines


def comparison_image(listing, reference, details):
    """Stack square photo panels, then complete guide/reminder text below them."""
    size, margin, line_height = alert_images.SIZE, 40, 48
    face = font(34)
    panels = [
        (
            "EBAY LISTING" if details.get("platform") == "ebay" else "VINTED LISTING",
            listing,
        )
    ]
    if reference:
        panels.append(("YOUR EXAMPLES", reference))
    notes = []
    for label, key in (
        ("YOUR BUYING GUIDE", "guide"),
        ("YOUR BUYING REMINDER", "reminder"),
    ):
        text = details.get(key) or ""
        # Only our own bold formatting is removed; escaped user text stays literal.
        text = " ".join(unescape(text.replace("<b>", "").replace("</b>", "")).split())
        if text:
            notes.extend(["", label, *wrapped_lines(text, face, size - margin * 2)])
    height = len(panels) * (size + 72) + len(notes) * line_height + margin
    canvas = Image.new("RGB", (size, height), "white")
    draw = ImageDraw.Draw(canvas)
    y = 0
    for label, raw in panels:
        draw.text((margin, y + 18), label, fill="#243b36", font=face)
        y += 72
        if raw:
            with Image.open(io.BytesIO(raw)) as image:
                image = ImageOps.contain(image.convert("RGB"), (size, size))
                canvas.paste(
                    image, ((size - image.width) // 2, y + (size - image.height) // 2)
                )
        else:
            draw.text(
                (margin, y + size // 2),
                "Photo unavailable — open the listing",
                fill="#555555",
                font=face,
            )
        y += size
    for line in notes:
        draw.text((margin, y), line, fill="#243b36", font=face)
        y += line_height
    result = io.BytesIO()
    canvas.save(result, "JPEG", quality=90, optimize=True)
    return result.getvalue()


async def initial_photo(details):
    try:
        return await asyncio.wait_for(
            alert_images.listing_collage(details.get("photos", [])[:1]), timeout=6
        )
    except TimeoutError:
        return None


async def send_initial(bot, chat_id, row, details, before_send, *, require_photo=False):
    """Attach the listing image to the first notification, before any silent edits."""
    listing = await initial_photo(details)
    if require_photo and listing is None:
        raise ValueError("The listing photo is unavailable. Try a newer find.")
    await before_send()
    common = {
        "chat_id": chat_id,
        "parse_mode": "HTML",
        "read_timeout": 8,
        "write_timeout": 8,
        "connect_timeout": 3,
        "pool_timeout": 3,
    }
    if listing:
        result = await bot.send_photo(
            **common,
            photo=InputFile(listing, filename="vinted-listing.jpg"),
            caption=caption(row, details),
            show_caption_above_media=True,
        )
        if not result.message_id or not result.photo:
            raise TelegramError("Telegram did not confirm the listing photo")
        logger.info(
            "Native listing photo accepted for item %s; message_id=%s",
            row["item_id"],
            result.message_id,
        )
        return result
    # An unavailable image must never prevent the deal link reaching the buyer.
    from vinted_alerts import sections

    logger.warning(
        "Native listing photo unavailable for item %s; sending one text fallback",
        row["item_id"],
    )
    return await bot.send_message(
        **common,
        text="\n\n".join(part for part in sections(row, details) if part),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def enrich(bot, chat_id, row, details, before_edit, *, persist=True):
    photos = (
        details.get("photos", [])[:4]
        if details.get("platform") == "ebay"
        else await vinted_gallery.resolve(row, details, persist=persist)
    )
    listing = (
        await initial_photo(dict(details, photos=photos))
        if len(photos) <= 1
        else await alert_images.listing_collage(photos)
    )
    reference = (
        dashboard_store.get_media(row["reference_id"]) if row["reference_id"] else None
    )
    reference_image = (
        await asyncio.to_thread(alert_images.normalize_available, [reference["image"]])
        if reference
        else None
    )
    image = await asyncio.to_thread(comparison_image, listing, reference_image, details)
    await before_edit()
    try:
        result = await bot.edit_message_media(
            chat_id=chat_id,
            message_id=row["telegram_message_id"],
            media=InputMediaPhoto(
                media=image,
                filename="vinted-comparison.jpg",
                caption=caption(row, details),
                parse_mode="HTML",
                show_caption_above_media=True,
            ),
            read_timeout=8,
            write_timeout=8,
            connect_timeout=3,
            pool_timeout=3,
        )
        if result.message_id != row["telegram_message_id"] or not result.photo:
            raise TelegramError("Telegram did not confirm the comparison photo edit")
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    return (not photos or listing is not None) and (
        not row["reference_id"] or reference_image is not None
    )
