"""eBay snapshots for the shared native-photo, single-message renderer."""

import asyncio
import json
from contextlib import closing
from types import SimpleNamespace

import alert_images
import photo_cards
import vinted_alerts
import vinted_native
from search_settings import connection


def track_preview(row):
    """Register preview delivery, including a deletion racing the Telegram call."""
    from ebay_privacy import queue_redaction

    with closing(connection()) as conn, conn:
        if conn.execute(
            "SELECT 1 FROM alert_outbox WHERE item_id=? AND platform='ebay'",
            (row["item_id"],),
        ).fetchone():
            conn.execute(
                "INSERT OR IGNORE INTO ebay_preview_messages VALUES (?,?)",
                (row["telegram_message_id"], row["item_id"]),
            )
        else:
            queue_redaction(conn, row["telegram_message_id"])
            return False
    return True


def enabled():
    return True


def snapshot(item, search):
    details = vinted_alerts.snapshot(
        SimpleNamespace(
            photo=item.get("photo_url"),
            brand_title=item.get("brand"),
            raw_data={"photos": item.get("photos", [])},
        ),
        search,
    )
    details.update(
        platform="ebay",
        native_photo=not vinted_native.separate_panels(),
        single_message=True,
        auction=item["auction"],
        shipping=item["shipping"],
        brand_label=item.get("brand_label", "Brand"),
    )
    return details


def get_details(row):
    with closing(connection()) as conn:
        saved = conn.execute(
            "SELECT payload FROM ebay_alert_details WHERE item_id=?", (row["item_id"],)
        ).fetchone()
    return json.loads(saved[0]) if saved else None


async def preview(query_id, *, phone_mode=None):
    from telegram import Bot
    from telegram.error import TelegramError

    from ebay_store import configuration
    from search_settings import get_search
    from vinted_alerts import enrich, send_text

    search = get_search(query_id)
    if not search:
        raise ValueError("Search not found.")
    with closing(connection()) as conn:
        recent = conn.execute(
            "SELECT * FROM alert_outbox WHERE query_id=? AND platform='ebay' ORDER BY found_at DESC LIMIT 1",
            (query_id,),
        ).fetchone()
    if not recent:
        raise ValueError(
            "This search needs an eBay find with a listing photo before sending a layout preview. Use Check saved eBay search to check its filters."
        )
    row = dict(recent)
    details = get_details(row)
    if not details:
        details = snapshot(
            {
                "photo_url": row["photo_url"],
                "auction": "Current bid" in row.get("content", ""),
                "shipping": None,
            },
            search,
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
                else "LAYOUT PREVIEW · "
            )
        )
    )
    details["name"] = label + (search["query_name"] or "eBay")
    if phone_mode == "working_photo":
        details["name"] = "WORKING PHOTO ALERT TEST · " + (
            search["query_name"] or "eBay"
        )
    details["photos"] = [
        p for p in details["photos"] if alert_images.safe_listing_photo(p)
    ]
    row["reference_id"] = search["reference_id"]
    # Refresh the owner's guide/reminder for this explicit preview only.
    current = snapshot(
        {"auction": details.get("auction", False), "shipping": details.get("shipping")},
        search,
    )
    details.update(guide=current["guide"], reminder=current["reminder"])
    config = configuration()
    if not config["telegram_token"] or not config["chat_id"]:
        raise ValueError("Connect your eBay Telegram bot first.")
    async with Bot(config["telegram_token"]) as bot:
        if phone_mode == "working_photo":

            async def ready():
                pass

            first = await photo_cards.send_initial(
                bot, config["chat_id"], row, details, ready, require_photo=True
            )
            row["telegram_message_id"] = first.message_id
            if not track_preview(row):
                return "The listing was removed during preview delivery. Its preview is being removed."
            photo_cards.enable()
            return "WORKING PHOTO ALERT TEST sent. New Vinted and eBay alerts now use the same photo delivery, with readable notes and in-message Listing photos / Your examples buttons."
        if phone_mode in ("rich_first", "native_then_rich", "native_album"):
            return await vinted_alerts.phone_layout_test(
                bot,
                config["chat_id"],
                row,
                details,
                mode=phone_mode,
                after_send=track_preview,
            )

        async def ready():
            pass

        first = await send_text(bot, config["chat_id"], row, details)
        row["telegram_message_id"] = first.message_id
        if not track_preview(row):
            return "The listing was removed during preview delivery. Its preview is being removed."
        await asyncio.sleep(1.1)
        try:
            complete = await enrich(
                bot, config["chat_id"], row, details, ready, persist=False
            )
        except TelegramError:
            return "Preview text sent. The photo-panel edit could not finish; check the bot before retrying."
        finally:
            track_preview(row)
    return (
        "eBay layout preview sent: separate listing and example panels, with readable notes in one message."
        + (" A listing or reference image was unavailable." if not complete else "")
    )
