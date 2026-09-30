"""A Vinted alert is one message, enriched in place after its fast text delivery."""

import json
from contextlib import closing
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from telegram import InputFile
from telegram.error import BadRequest, TelegramError

import alert_images
import dashboard_store
import db
import vinted_gallery
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
        "name": (search.get("query_name") or keyword or "Filtered search")[:100],
        "brand": (getattr(item, "brand_title", None) or "Not specified")[:120],
        "photos": alert_images.photo_urls(item),
        "guide": "\n".join(guide),
        "reminder": escape((search.get("reminder") or "")[:800]),
    }


def sections(row, details):
    name = escape(details.get("name") or row["search_name"][:100])
    heading = f"🔎 <b>#{row['query_id'] or '—'} · {name}</b>"
    link = f'<a href="{escape(row["url"], quote=True)}">Open Vinted listing ↗</a>'
    price = str(row["price"])
    price = "£" + price if row["currency"] == "GBP" else price + " " + row["currency"]
    listing = (
        f"<b>{escape(row['title'][:500])}</b>\n"
        f"Price: <b>{escape(price)}</b>\nBrand: {escape(details['brand'])}"
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
    heading, _, listing, guide, reminder = sections(row, details)
    # A URL button sits directly below the search heading, above listing details.
    button = (
        '<tg-button-row align="left"><tg-button type="url" url="'
        + escape(row["url"], quote=True)
        + '">Open Vinted listing ↗</tg-button></tg-button-row>'
    )
    parts = [
        "<p>" + heading + "</p>",
        button,
        "<p>" + listing.replace("\n", "<br>") + "</p>",
    ]
    media, files = [], {}
    for key, label, raw in (
        ("listing", "Vinted listing", listing_image),
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


async def enrich(bot, chat_id, row, details, before_edit):
    photos = await vinted_gallery.resolve(row, details)
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
    return not missing_listing


async def preview_and_enable(query_id):
    """Exercise the actual text-to-rich edit before switching the live worker."""
    import asyncio
    from types import SimpleNamespace

    from telegram import Bot, LinkPreviewOptions

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
    search["query_name"] = "LAYOUT PREVIEW · " + (search["query_name"] or "")
    details = snapshot(item, search)
    row["reference_id"] = search["reference_id"]
    row["search_name"] = search["query_name"]
    token, chat_id = db.get_parameter("telegram_token"), db.get_parameter(
        "telegram_chat_id"
    )
    if not token or not chat_id:
        raise ValueError("Connect your Vinted Telegram bot first.")
    # Complete downloads first, so missing CDN photos never enable a partial layout.
    # Preview uses current gallery extraction, without overwriting the original
    # alert's name, guide or reminder snapshot.
    gallery_details = dict(details)
    details["photos"] = await vinted_gallery.resolve(
        row, gallery_details, persist=False
    )
    listing = await alert_images.listing_collage(details["photos"])
    if listing is None:
        raise ValueError(
            "The listing photo is unavailable. Try a search with a newer find."
        )
    reference = (
        dashboard_store.get_media(row["reference_id"]) if row["reference_id"] else None
    )
    reference_image = (
        await asyncio.to_thread(alert_images.normalize_available, [reference["image"]])
        if reference
        else None
    )
    if reference and not reference_image:
        raise ValueError(
            "The example photo could not be read. Replace it and try again."
        )
    async with Bot(token) as bot:
        first = await bot.send_message(
            chat_id,
            fast_text(item, search),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        row["telegram_message_id"] = first.message_id
        data = rich_request(row, details, listing, reference_image)
        data["chat_id"] = chat_id
        await asyncio.sleep(1.1)
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
            or result.get("message_id") != first.message_id
            or not result.get("rich_message")
        ):
            raise TelegramError("Telegram did not confirm the rich message edit")
    db.set_parameter("vinted_single_message_alerts", "1")
    return {
        "photo_count": len(details["photos"]),
        "gallery_state": gallery_details.get("gallery_state"),
    }
