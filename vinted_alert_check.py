"""Private evidence from a fresh sent alert; never send or edit a message."""

import io
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone

from PIL import Image

from listing_text import clean_description
from search_settings import connection

logger = logging.getLogger(__name__)
MAX_ALERT_AGE = 3600
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MARKER = "alert_checked_release"


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


def readable_reference(media_id):
    if not media_id:
        return False
    with closing(connection()) as conn:
        row = conn.execute(
            "SELECT image FROM dashboard_media WHERE id=? AND length(image)<=?",
            (media_id, MAX_IMAGE_BYTES),
        ).fetchone()
    if row is None:
        return False
    try:
        with Image.open(io.BytesIO(row[0])) as image:
            if image.width * image.height > 20_000_000:
                return False
            image.verify()
        return True
    except (OSError, ValueError, Image.DecompressionBombError):
        return False


def check_latest_alert(*, require_examples=False):
    import photo_cards
    import vinted_alerts
    import vinted_gallery

    result = {
        "check": "latest_with_examples" if require_examples else "latest_alert",
        "outcome": "unverified",
        "stage": "fresh_alert",
        "messages_sent": 0,
        "messages_edited": 0,
        "checkout_created": False,
        "payment_submitted": False,
        "photo_buttons_exercised": False,
    }
    try:
        with closing(connection()) as conn:
            row = conn.execute(
                "SELECT * FROM alert_outbox WHERE platform='vinted' AND status='sent' "
                + ("AND reference_id IS NOT NULL " if require_examples else "")
                + "ORDER BY sent_at DESC LIMIT 1"
            ).fetchone()
            health = conn.execute(
                "SELECT last_success FROM telegram_control_health WHERE platform='vinted'"
            ).fetchone()
        if row is None or row["sent_at"] is None:
            return result
        age = time.time() - row["sent_at"]
        result.update(
            alert_age_seconds=max(0, int(age)),
            alert_sent_at=timestamp(row["sent_at"]),
            last_photo_control_success=timestamp(health[0]) if health else None,
        )
        if not 0 <= age <= MAX_ALERT_AGE:
            return result
        row = dict(row)
        result["stage"] = "saved_alert"
        saved = photo_cards.load("vinted", row["telegram_message_id"])
        if saved:
            row, details, card = saved
            result["original_photo_recorded"] = bool(card["listing_file_id"])
            result["selected_photo_view"] = (
                card["view"] if card["view"] in ("listing", "examples") else "other"
            )
        else:
            details = vinted_alerts.get_details(row)
            result["original_photo_recorded"] = False
        if not isinstance(details, dict):
            return result
        callbacks = {
            button.callback_data
            for buttons in photo_cards.markup(row, details).inline_keyboard
            for button in buttons
            if button.callback_data
        }
        result.update(
            autobuy_callback_present="buy:click" in callbacks,
            example_reference_present=bool(row["reference_id"]),
            example_image_readable=readable_reference(row["reference_id"]),
            listing_and_example_controls_present={"card:listing", "card:examples"}
            <= callbacks,
        )
        result["stage"] = "listing_description"
        listing = vinted_gallery.fetch_listing(row["url"])
        state = listing.get("state")
        result["listing_state"] = (
            state
            if state
            in (
                "ready",
                "cooldown",
                "access_limited",
                "network_error",
                "download_limit",
                "invalid_url",
                "parse_error",
                "http_404",
                "http_410",
                "http_500",
                "http_502",
                "http_503",
                "http_504",
            )
            else "unverified"
        )
        if state != "ready":
            return result
        current = clean_description(listing.get("description"))
        stored = clean_description(details.get("description"))
        result.update(
            listing_photo_count=min(4, len(listing.get("photos", []))),
            listing_description_chars=len(current),
            alert_description_chars=len(stored),
            description_matches=bool(current) and current == stored,
        )
        result.update(
            outcome="matched" if result["description_matches"] else "mismatch",
            stage="complete",
        )
        return result
    except Exception:  # noqa: BLE001 -- fixed results, never upstream bodies or secrets
        result["outcome"] = "unverified"
        return result
    finally:
        logger.info(
            "Vinted private alert check: %s", json.dumps(result, sort_keys=True)
        )


def run_once():
    release = os.environ.get("MSJ_ALERT_CHECK_ON_START", "")
    if release.lower() in ("", "0", "false", "off") or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,80}", release
    ):
        return None
    try:
        with closing(connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT value FROM parameters WHERE key=?", (MARKER,)
            ).fetchone()
            if previous and previous[0] == release:
                return None
            conn.execute(
                "INSERT OR REPLACE INTO parameters(key,value) VALUES (?,?)",
                (MARKER, release),
            )
    except (OSError, sqlite3.Error):
        logger.info(
            "Vinted private alert startup: outcome=unverified stage=reservation"
        )
        return None
    result = check_latest_alert()
    if result.get("example_reference_present") is False:
        check_latest_alert(require_examples=True)
    return result


def summary(result):
    if result["stage"] != "complete":
        return (
            "Fresh-alert verification is unverified at " + result["stage"] + ". "
            "No messages were sent or edited; no checkout or payment was created."
        )
    match = (
        "matches its listing"
        if result["description_matches"]
        else "does not match its listing"
    )
    return (
        "The latest alert's seller description " + match + ". "
        f"Listing photos: {result['listing_photo_count']}. "
        "Example image: "
        + ("readable" if result["example_image_readable"] else "absent or unreadable")
        + ". "
        "Listing/example controls: "
        + ("present" if result["listing_and_example_controls_present"] else "absent")
        + ". "
        "The buttons still need a fresh Telegram tap to confirm their live behavior. "
        "No messages were sent or edited; no checkout or payment was created."
    )
