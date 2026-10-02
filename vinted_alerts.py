"""A Vinted alert is one message, enriched in place after its fast text delivery."""

import json
from contextlib import closing
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from telegram import InputFile, InputMediaPhoto, LinkPreviewOptions
from telegram.error import BadRequest, TelegramError

import alert_images
import dashboard_store
import db
import photo_cards
import vinted_gallery
import vinted_native
from search_settings import connection


def enabled():
    return (
        Path(db.DB_PATH).is_file()
        and db.get_parameter("vinted_single_message_alerts") == "1"
    )


def snapshot(item, search):
    keyword = parse_qs(urlparse(search["query"]).query).get("search_text", [""])[0]
    guide = []
    if search.get("max_buy") is not None:
        guide.append(f"Buy up to <b>£{search['max_buy']/100:.2f}</b>")
    low, high = search.get("resale_low"), search.get("resale_high")
    if low is not None or high is not None:
        target = (
            f"£{low/100:.2f}–£{high/100:.2f}"
            if low is not None and high is not None
            else f"from £{low/100:.2f}" if low is not None else f"up to £{high/100:.2f}"
        )
        guide.append("Your resale target: " + target)
    if search.get("must_have"):
        guide.append("Must have: " + escape(search["must_have"][:400]))
    return {
        "version": 1,
        "single_message": enabled(),
        "native_photo": enabled() and vinted_native.enabled(),
        "photo_card": photo_cards.enabled(),
        "name": (search.get("query_name") or keyword or "Filtered search")[:100],
        "brand": (getattr(item, "brand_title", None) or "Not specified")[:120],
        "photos": alert_images.photo_urls(item),
        "guide": "\n".join(guide),
        "reminder": escape((search.get("reminder") or "")[:800]),
    }


def sections(row, details):
    ebay = details.get("platform") == "ebay"
    marketplace = "eBay" if ebay else "Vinted"
    name = escape(details.get("name") or row["search_name"][:100])
    heading = f"🔎 <b>{'eBay · ' if ebay else ''}#{row['query_id'] or '—'} · {name}</b>"
    link = (
        f'<a href="{escape(row["url"], quote=True)}">Open {marketplace} listing ↗</a>'
    )
    price = str(row["price"])
    price = "£" + price if row["currency"] == "GBP" else price + " " + row["currency"]
    listing = (
        f"<b>{escape(row['title'][:500])}</b>\n"
        f"{'Current bid' if ebay and details.get('auction') else 'Price'}: <b>{escape(price)}</b>\n{escape(details.get('brand_label', 'Brand'))}: {escape(details['brand'])}"
    )
    if ebay:
        shipping = details.get("shipping")
        listing += "\nPostage: " + (
            f"£{shipping / 100:.2f}" if shipping is not None else "check listing"
        )
    guide = (
        "💷 <b>Your buying guide</b>\n" + details["guide"]
        if details.get("guide")
        else ""
    )
    reminder = (
        "📝 <b>Your buying reminder</b>\n" + details["reminder"]
        if details.get("reminder")
        else ""
    )
    return heading, link, listing, guide, reminder


def fast_text(item, search, details=None):
    row = {
        "query_id": search["id"],
        "search_name": "",
        "url": item.url,
        "title": item.title,
        "price": item.price,
        "currency": item.currency,
    }
    return "\n\n".join(
        section
        for section in sections(row, details or snapshot(item, search))
        if section
    )


def get_details(row):
    with closing(connection()) as conn:
        saved = conn.execute(
            "SELECT payload FROM vinted_alert_details WHERE item_id=?",
            (row["item_id"],),
        ).fetchone()
    return json.loads(saved[0]) if saved else None


def rich_request(row, details, listing_image, reference_image):
    marketplace = "eBay" if details.get("platform") == "ebay" else "Vinted"
    heading, _, listing, guide, reminder = sections(row, details)
    # A URL button sits directly below the search heading, above listing details.
    button = (
        '<tg-button-row align="left"><tg-button type="url" url="'
        + escape(row["url"], quote=True)
        + '">Open '
        + marketplace
        + " listing ↗</tg-button></tg-button-row>"
    )
    parts = [
        "<p>" + heading + "</p>",
        button,
        "<p>" + listing.replace("\n", "<br>") + "</p>",
    ]
    media, files = [], {}
    for key, label, raw in (
        ("listing", marketplace + " listing", listing_image),
        ("reference", "Your examples", reference_image),
    ):
        if raw:
            parts.append(
                f'<figure><img src="tg://photo?id={key}"/><figcaption>{label}</figcaption></figure>'
            )
            media.append(
                {"id": key, "media": {"type": "photo", "media": "attach://" + key}}
            )
            files[key] = InputFile(raw, filename=key + ".jpg")
    parts.extend(
        "<p>" + section.replace("\n", "<br>") + "</p>"
        for section in (guide, reminder)
        if section
    )
    return {
        "chat_id": None,
        "message_id": row["telegram_message_id"],
        "rich_message": {
            "html": "".join(parts),
            "media": media,
            "skip_entity_detection": True,
        },
        "reply_markup": {"inline_keyboard": []},
        **files,
    }


async def send_text(bot, chat_id, row, details):
    return await bot.send_message(
        chat_id=chat_id,
        text="\n\n".join(part for part in sections(row, details) if part),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        read_timeout=8,
        write_timeout=8,
        connect_timeout=3,
        pool_timeout=3,
    )


async def enrich(bot, chat_id, row, details, before_edit, *, persist=True):
    photos = (
        details.get("photos", [])[:4]
        if details.get("platform") == "ebay"
        else await vinted_gallery.resolve(row, details, persist=persist)
    )
    listing = await alert_images.listing_collage(photos)
    reference = (
        dashboard_store.get_media(row["reference_id"]) if row["reference_id"] else None
    )
    # Old single reference photos also get a square frame. Stored new collages
    # are already square and keep their full resolution.
    reference_image = reference["image"] if reference else None
    if reference_image:
        import asyncio

        reference_image = await asyncio.to_thread(
            alert_images.normalize_available, [reference_image]
        )
    if details.get("photos") and listing is None:
        # Retry only this edit; the original link has already been delivered.
        # Send any available reference image before retrying the missing listing.
        missing_listing = True
    else:
        missing_listing = False
    data = rich_request(row, details, listing, reference_image)
    data["chat_id"] = chat_id
    await before_edit()
    try:
        result = await bot.do_api_request(
            "editMessageText",
            api_kwargs=data,
            read_timeout=8,
            write_timeout=8,
            connect_timeout=3,
            pool_timeout=3,
        )
        if (
            not isinstance(result, dict)
            or result.get("message_id") != row["telegram_message_id"]
            or not result.get("rich_message")
        ):
            raise TelegramError("Telegram did not confirm the rich message edit")
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    return not missing_listing and (
        not row["reference_id"] or reference_image is not None
    )


async def phone_layout_test(bot, chat_id, row, details, *, mode, after_send=None):
    """Compare native-first and original rich-first transport; never enable live mode."""
    import asyncio

    # Use already supplied photos. A phone transport test must not fetch galleries
    # or rewrite the original listing snapshot.
    listing = await alert_images.listing_collage(details.get("photos", [])[:4])
    if listing is None:
        raise ValueError("The listing photo is unavailable. Try a newer find.")
    reference = (
        dashboard_store.get_media(row["reference_id"]) if row["reference_id"] else None
    )
    reference_image = (
        await asyncio.to_thread(alert_images.normalize_available, [reference["image"]])
        if reference
        else None
    )
    if row["reference_id"] and reference_image is None:
        raise ValueError("The saved example photo is unavailable; no test was sent.")
    data = rich_request(row, details, listing, reference_image)
    data["chat_id"] = chat_id
    timeouts = {
        "read_timeout": 8,
        "write_timeout": 8,
        "connect_timeout": 3,
        "pool_timeout": 3,
    }
    if mode == "native_album":
        import re
        from html import unescape

        # One grouped album with separate native photos and an ordinary caption.
        # Never silently drop the owner's notes to fit Telegram's caption limit.
        text = "\n\n".join(part for part in sections(row, details) if part)
        plain = unescape(re.sub(r"<[^>]+>", "", text))
        if len(plain.encode("utf-16-le")) // 2 > 1024:
            raise ValueError(
                "This album test needs a shorter title or notes to fit Telegram's photo caption limit. No test was sent."
            )
        if reference_image:
            results = await bot.send_media_group(
                chat_id=chat_id,
                media=[
                    InputMediaPhoto(
                        listing, filename="listing.jpg", caption=text, parse_mode="HTML"
                    ),
                    InputMediaPhoto(reference_image, filename="examples.jpg"),
                ],
                **timeouts,
            )
        else:
            results = [
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=InputFile(listing, filename="listing.jpg"),
                    caption=text,
                    parse_mode="HTML",
                    **timeouts,
                )
            ]
        for sent in results:
            if after_send:
                after_send(dict(row, telegram_message_id=sent.message_id))
        if len(results) != (2 if reference_image else 1) or any(
            not sent.photo for sent in results
        ):
            raise TelegramError("Telegram did not confirm the album photos")
        if reference_image and (
            not results[0].media_group_id
            or results[0].media_group_id != results[1].media_group_id
        ):
            raise TelegramError("Telegram did not confirm the grouped album")
        return "PHOTO ALBUM TEST sent: separate listing and example collages, with readable caption notes. Check the expanded iPhone notification and album layout. Live alerts are unchanged."
    if mode == "rich_first":
        data.pop("message_id")
        result = await bot.do_api_request(
            "sendRichMessage", api_kwargs=data, **timeouts
        )
        if (
            not isinstance(result, dict)
            or not result.get("message_id")
            or not result.get("rich_message")
        ):
            raise TelegramError("Telegram did not confirm the initial rich message")
        row["telegram_message_id"] = result["message_id"]
        if after_send:
            after_send(row)
        return "ORIGINAL LAYOUT TEST sent with both photo panels in the first message. Check the expanded iPhone notification. Live alerts are unchanged."

    first = await bot.send_photo(
        chat_id=chat_id,
        photo=InputFile(listing, filename="listing.jpg"),
        caption=vinted_native.caption(row, details),
        parse_mode="HTML",
        show_caption_above_media=True,
        **timeouts,
    )
    if not first.message_id or not first.photo:
        raise TelegramError("Telegram did not confirm the initial photo")
    row["telegram_message_id"] = first.message_id
    if after_send and after_send(row) is False:
        return "The listing was removed during the test. Its preview is being removed."
    data["message_id"] = first.message_id
    await asyncio.sleep(1.1)
    if after_send and after_send(row) is False:
        return "The listing was removed before the test edit. Its preview is being removed."
    try:
        result = await bot.do_api_request(
            "editMessageText", api_kwargs=data, **timeouts
        )
        if (
            not isinstance(result, dict)
            or result.get("message_id") != first.message_id
            or not result.get("rich_message")
        ):
            raise TelegramError("Telegram did not confirm the same-message rich edit")
    except BadRequest as exc:
        from logger import get_logger

        get_logger(__name__).warning(
            "iPhone native-to-rich test rejected: %s", str(exc)[:200]
        )
        return "SAME MESSAGE TEST photo sent, but Telegram refused the separate-panel edit. The original photo remains; no second message was sent. Live alerts are unchanged."
    finally:
        if after_send:
            after_send(row)
    return "SAME MESSAGE TEST accepted: Telegram received a standard photo first, then the separate panels and readable notes on the same message ID. Check its expanded iPhone notification. Live alerts are unchanged."


async def preview_and_enable(query_id, *, photo_first=False, phone_mode=None):
    """Enable verified native photo delivery, or send a one-photo diagnostic."""
    import asyncio
    from types import SimpleNamespace

    from telegram import Bot

    from search_settings import get_search

    search = get_search(query_id)
    if not search:
        raise ValueError("Search not found.")
    with closing(connection()) as conn:
        recent = conn.execute(
            """SELECT * FROM alert_outbox WHERE query_id=? AND platform='vinted'
            AND photo_url IS NOT NULL AND photo_url!='' ORDER BY found_at DESC LIMIT 1""",
            (query_id,),
        ).fetchone()
    if not recent:
        raise ValueError(
            "This search needs a previous Vinted find with a photo before previewing."
        )
    row = dict(recent)
    saved = get_details(row) or {}
    item = SimpleNamespace(
        title=row["title"],
        price=row["price"],
        currency=row["currency"],
        url=row["url"],
        photo=row["photo_url"],
        brand_title=saved.get("brand"),
        raw_data={"photos": saved.get("photos", [])},
    )
    label = (
        "PHOTO ALBUM TEST · "
        if phone_mode == "native_album"
        else (
            "ORIGINAL LAYOUT TEST · "
            if phone_mode == "rich_first"
            else (
                "SAME MESSAGE TEST · "
                if phone_mode == "native_then_rich"
                else "STANDARD PHOTO TEST · " if photo_first else "LAYOUT PREVIEW · "
            )
        )
    )
    search["query_name"] = label + (search["query_name"] or "")
    details = snapshot(item, search)
    if phone_mode == "working_photo":
        details["name"] = "WORKING PHOTO ALERT TEST · " + (
            get_search(query_id)["query_name"] or ""
        )
    row["reference_id"] = search["reference_id"]
    row["search_name"] = search["query_name"]
    token, chat_id = db.get_parameter("telegram_token"), db.get_parameter(
        "telegram_chat_id"
    )
    if not token or not chat_id:
        raise ValueError("Connect your Vinted Telegram bot first.")
    if phone_mode == "working_photo":
        async with Bot(token) as bot:

            async def ready():
                pass

            await photo_cards.send_initial(
                bot, chat_id, row, details, ready, require_photo=True
            )
        photo_cards.enable()
        return {
            "photo_count": len(details["photos"]),
            "gallery_state": "catalogue",
            "phone_test": "WORKING PHOTO ALERT TEST sent. New Vinted and eBay alerts now use the same photo delivery, with readable notes and in-message Listing photos / Your examples buttons.",
        }
    if phone_mode in ("rich_first", "native_then_rich", "native_album"):
        async with Bot(token) as bot:
            message = await phone_layout_test(
                bot, chat_id, row, details, mode=phone_mode
            )
        return {
            "photo_count": len(details["photos"]),
            "gallery_state": "catalogue",
            "phone_test": message,
        }
    if photo_first:
        # Keep the known-good one-photo diagnostic independent of live settings.
        details["photos"] = details["photos"][:1]
        async with Bot(token) as bot:

            async def ready():
                pass

            await vinted_native.send_initial(
                bot, chat_id, row, details, ready, require_photo=True
            )
        return {"photo_count": 1, "gallery_state": "catalogue"}

    # Separate image panels and actual text, all edited into the same message.
    async with Bot(token) as bot:

        async def ready():
            pass

        first = await send_text(bot, chat_id, row, details)
        row["telegram_message_id"] = first.message_id

        async def paced_edit():
            await asyncio.sleep(1.1)

        complete = await enrich(bot, chat_id, row, details, paced_edit, persist=False)
        if not complete:
            raise ValueError(
                "Preview sent, but a comparison photo is unavailable. Live settings are unchanged."
            )
    with closing(connection()) as conn, conn:
        conn.executemany(
            "INSERT OR REPLACE INTO parameters VALUES (?,?)",
            [
                ("vinted_single_message_alerts", "1"),
                ("vinted_native_photo_alerts", "0"),
                ("separate_photo_panels", "1"),
            ],
        )
    return {
        "photo_count": len(details["photos"]),
        "gallery_state": details.get("gallery_state"),
    }
