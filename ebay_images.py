"""Recover missing eBay listing photos within the existing shared API budget."""

import asyncio
import json
import time
from contextlib import closing

import requests

import alert_images
from listing_text import clean_description
from logger import get_logger
from search_settings import connection

logger = get_logger(__name__)


def extract(raw):
    def url(image):
        return image.get("imageUrl") if isinstance(image, dict) else None

    primary = url(raw.get("image"))
    extras = raw.get("additionalImages") or []
    thumbnails = raw.get("thumbnailImages") or []
    extras = extras if isinstance(extras, list) else []
    thumbnails = thumbnails if isinstance(thumbnails, list) else []
    candidates = [primary] + [url(p) for p in extras[:20]]
    photos = list(
        dict.fromkeys(p for p in candidates if alert_images.safe_listing_photo(p))
    )
    # Thumbnail entries describe the same primary picture, not extra views.
    if not photos:
        photos = [
            p
            for p in (url(p) for p in thumbnails[:10])
            if alert_images.safe_listing_photo(p)
        ][:1]
    return photos[:4]


def fetch_details(row):
    from ebay_monitor import BrowseClient, EbayError, retry_delay
    from ebay_store import configuration, reserve_call

    item_id = row["item_id"]
    legacy_id = item_id.removeprefix("ebay:")
    if not legacy_id.isdigit():
        return {}
    config = configuration()
    if (
        config["source"] != "browse"
        or not config["client_id"]
        or not config["client_secret"]
    ):
        return {}
    now = time.time()
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute(
            "SELECT 1 FROM alert_outbox WHERE item_id=? AND platform='ebay'", (item_id,)
        ).fetchone():
            return {}
        last = conn.execute(
            "SELECT attempted FROM ebay_photo_lookups WHERE item_id=?", (item_id,)
        ).fetchone()
        if last and now - last[0] < 60:
            return {}
        conn.execute(
            "INSERT OR REPLACE INTO ebay_photo_lookups VALUES (?,?)", (item_id, now)
        )
    wait = reserve_call(config, now, media=True)
    if wait:
        logger.info(
            "eBay photo lookup deferred by shared request budget for %s", item_id
        )
        return {}
    client = None
    try:
        client = BrowseClient(config)
        client.authenticate()
        response = client.session.get(
            "https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id",
            params={"legacy_item_id": legacy_id},
            headers={
                "Authorization": "Bearer " + client.token,
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
                "Accept-Language": "en-GB",
            },
            timeout=(2, 4),
        )
        if response.status_code == 429:
            with closing(connection()) as conn, conn:
                conn.execute(
                    "INSERT INTO delivery_runtime VALUES ('ebay_api_cooldown',?) ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)",
                    (time.time() + retry_delay(response.headers.get("Retry-After")),),
                )
        if response.status_code != 200:
            logger.warning(
                "eBay photo lookup returned HTTP %s for %s",
                response.status_code,
                item_id,
            )
            return {}
        data = response.json()
        if not isinstance(data, dict):
            return {}
        returned_id = str(data.get("legacyItemId") or "")
        if not returned_id:
            returned_id = str(data.get("itemId", "")).split("|")[1:2]
            returned_id = returned_id[0] if returned_id else ""
        if returned_id != legacy_id:
            return {}
        photos = extract(data)
        logger.info(
            "eBay photo lookup recovered %s photos for %s", len(photos), item_id
        )
        return {
            "photos": photos,
            "description": clean_description(data.get("description"), html=True)
            or clean_description(data.get("shortDescription"), html=True),
            "description_checked": True,
        }
    except (requests.RequestException, ValueError, EbayError):
        logger.warning("eBay photo lookup unavailable for %s", item_id)
        return {}
    finally:
        if client:
            client.session.close()


def cache_description(row, data):
    with closing(connection()) as conn, conn:
        saved = conn.execute(
            "SELECT d.payload FROM ebay_alert_details d JOIN alert_outbox o ON o.item_id=d.item_id WHERE d.item_id=? AND o.platform='ebay'",
            (row["item_id"],),
        ).fetchone()
        if saved:
            payload = json.loads(saved[0])
            payload.update(
                description=data.get("description", ""), description_checked=True
            )
            conn.execute(
                "UPDATE ebay_alert_details SET payload=? WHERE item_id=?",
                (json.dumps(payload), row["item_id"]),
            )


def fetch_missing(row):
    data = fetch_details(row)
    cache_description(row, data)
    return data.get("photos", [])


async def resolve(row, details, *, include_description=False):
    photos = list(
        dict.fromkeys(
            p
            for p in [*details.get("photos", []), row.get("photo_url")]
            if alert_images.safe_listing_photo(p)
        )
    )[:4]
    if not photos:
        photos = await asyncio.to_thread(fetch_missing, row)
        from ebay_alerts import get_details

        saved = get_details(row) or {}
        if saved.get("description_checked"):
            details.update(
                description=saved.get("description", ""), description_checked=True
            )
    elif include_description and not details.get("description_checked"):
        if details.get("description"):
            details["description_checked"] = True
        else:
            data = await asyncio.to_thread(fetch_details, row)
            photos = list(dict.fromkeys(data.get("photos", []) + photos))[:4]
            details.update(
                description=data.get("description", ""), description_checked=True
            )
        cache_description(row, details)
    if photos:
        # A closure notification can remove the item while the API read runs.
        with closing(connection()) as conn, conn:
            if not conn.execute(
                "SELECT 1 FROM alert_outbox WHERE item_id=? AND platform='ebay'",
                (row["item_id"],),
            ).fetchone():
                return []
            details["photos"] = photos
            row["photo_url"] = photos[0]
            conn.execute(
                "UPDATE alert_outbox SET photo_url=? WHERE item_id=?",
                (photos[0], row["item_id"]),
            )
            saved = conn.execute(
                "SELECT payload FROM ebay_alert_details WHERE item_id=?",
                (row["item_id"],),
            ).fetchone()
            if saved:
                payload = json.loads(saved[0])
                payload["photos"] = photos
                conn.execute(
                    "UPDATE ebay_alert_details SET payload=? WHERE item_id=?",
                    (json.dumps(payload), row["item_id"]),
                )
    return photos
