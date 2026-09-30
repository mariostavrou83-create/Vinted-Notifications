"""Durable, at-least-once Telegram delivery. No network retries block the queue.

Seen items and pending alerts are committed together by db.add_item_to_db.
An ambiguous HTTP timeout can still produce a duplicate: Telegram has no
idempotency key for sendMessage. Confirmed messages are never retried for a
reference-photo failure. A crashed worker's lease expires automatically.
"""

import asyncio
import io
import secrets
import time
from contextlib import closing

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    ReplyParameters,
)
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
)

import dashboard_store
from logger import get_logger
from search_settings import connection

logger = get_logger(__name__)


def claim(now=None, preferred_photo=None, platform="vinted", allow_photos=True):
    now = time.time() if now is None else now
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        cooldown = conn.execute(
            "SELECT value FROM delivery_runtime WHERE key=?",
            (
                (
                    "cooldown_until"
                    if platform == "vinted"
                    else platform + "_cooldown_until"
                ),
            ),
        ).fetchone()
        if cooldown and cooldown[0] > now:
            return None
        # A buying example must never take the next send slot from a new deal.
        row = conn.execute(
            """SELECT * FROM alert_outbox WHERE platform=? AND status='pending'
            AND next_attempt<=? AND leased_until<=? ORDER BY found_at,item_id LIMIT 1""",
            (platform, now, now),
        ).fetchone()
        kind = "listing"
        if row is None and allow_photos and preferred_photo:
            row = conn.execute(
                """SELECT * FROM alert_outbox WHERE platform=? AND item_id=? AND status='sent'
                AND photo_status='pending' AND photo_attempts=0 AND photo_next_attempt<=?
                AND leased_until<=?""",
                (platform, preferred_photo, now, now),
            ).fetchone()
            if row:
                kind = "photo"
        if row is None and allow_photos:
            kind = "photo"
            row = conn.execute(
                """SELECT * FROM alert_outbox WHERE platform=? AND status='sent'
                AND photo_status='pending' AND photo_next_attempt<=? AND leased_until<=?
                ORDER BY found_at,item_id LIMIT 1""",
                (platform, now, now),
            ).fetchone()
        if row is None:
            return None
        token = secrets.token_hex(16)
        conn.execute(
            "UPDATE alert_outbox SET lease_token=?,leased_until=? WHERE item_id=?",
            (token, now + 120, row["item_id"]),
        )
        return dict(row, kind=kind, lease_token=token)


def finish(
    row,
    *,
    message_id=None,
    failure=None,
    delay=0,
    permanent=False,
    cooldown=False,
    now=None,
):
    now = time.time() if now is None else now
    photo = row["kind"] == "photo"
    prefix = "photo_" if photo else ""
    with closing(connection()) as conn, conn:
        if cooldown:
            conn.execute(
                """INSERT INTO delivery_runtime VALUES (?,?)
                ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)""",
                (
                    (
                        "cooldown_until"
                        if row.get("platform", "vinted") == "vinted"
                        else row["platform"] + "_cooldown_until"
                    ),
                    now + delay,
                ),
            )
        if failure is None:
            extra = "" if photo else ",telegram_message_id=?,sent_at=?"
            args = [] if photo else [message_id, now]
            conn.execute(
                f"""UPDATE alert_outbox SET {prefix}status='sent', {prefix}error='',
                {prefix}attempts={prefix}attempts+1, lease_token=NULL,leased_until=0 {extra}
                WHERE item_id=? AND lease_token=?""",
                (*args, row["item_id"], row["lease_token"]),
            )
        else:
            conn.execute(
                f"""UPDATE alert_outbox SET {prefix}status=?,{prefix}error=?,
                {prefix}attempts={prefix}attempts+1,{prefix}next_attempt=?,lease_token=NULL,leased_until=0
                WHERE item_id=? AND lease_token=?""",
                (
                    "failed" if permanent else "pending",
                    failure,
                    now + delay,
                    row["item_id"],
                    row["lease_token"],
                ),
            )


class DeliveryWorker:
    disable_previews = False

    def __init__(self, bot, chat_id, platform="vinted", bot_id=None):
        self.bot, self.chat_id = bot, chat_id
        self.platform, self.bot_id = platform, bot_id
        self.preferred_photo = None
        self.last_send_started = None

    def send_slot_delay(self):
        if self.last_send_started is None:
            return 0.0
        return max(0.0, self.last_send_started + 1.05 - time.monotonic())

    async def tick(self, now=None):
        row = claim(now, self.preferred_photo, self.platform)
        self.preferred_photo = None
        if row is None:
            return False
        return await self.deliver(row, now)

    async def deliver(self, row, now=None):
        """Send one leased job. Listing and photo acknowledgements stay separate."""
        photo = row["kind"] == "photo"
        media = None
        try:
            if photo:
                media = dashboard_store.get_media(row["reference_id"])
                if not media:
                    finish(
                        row,
                        failure="Example photo unavailable",
                        permanent=True,
                        now=now,
                    )
                    return True
                if self.platform == "ebay":
                    with closing(connection()) as conn:
                        cached = conn.execute(
                            "SELECT file_id FROM platform_media_cache WHERE media_id=? AND bot_id=?",
                            (row["reference_id"], self.bot_id),
                        ).fetchone()
                    media["telegram_file_id"] = cached[0] if cached else None
                image = media["telegram_file_id"] or io.BytesIO(media["image"])
                self.last_send_started = time.monotonic()
                result = await self.bot.send_photo(
                    chat_id=self.chat_id,
                    photo=image,
                    caption=f"Your example · #{row['query_id'] or '—'} · {row['search_name'][:100]}",
                    disable_notification=True,
                    reply_parameters=ReplyParameters(
                        message_id=row["telegram_message_id"],
                        allow_sending_without_reply=True,
                    ),
                    read_timeout=8,
                    write_timeout=8,
                    connect_timeout=5,
                    pool_timeout=5,
                )
                # Record success first. A cache failure must never resend a confirmed photo.
                finish(row, now=now)
                try:
                    self.cache_photo(row["reference_id"], result.photo[-1].file_id)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Could not cache reference file ID for item %s", row["item_id"]
                    )
            else:
                send_started = self.last_send_started = time.monotonic()
                extra = {}
                if self.platform == "ebay" or self.disable_previews:
                    # The clickable listing must not depend on an external image fetch.
                    extra["link_preview_options"] = LinkPreviewOptions(is_disabled=True)
                result = await self.bot.send_message(
                    **extra,
                    chat_id=self.chat_id,
                    text=row["content"],
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    (
                                        "Open eBay"
                                        if self.platform == "ebay"
                                        else "Open Vinted"
                                    ),
                                    url=row["url"],
                                )
                            ]
                        ]
                    ),
                    read_timeout=10,
                    write_timeout=10,
                    connect_timeout=5,
                    pool_timeout=5,
                )
                finish(row, message_id=result.message_id, now=now)
                self.preferred_photo = row["item_id"]
                logger.info(
                    "Telegram accepted item %s; message_id=%s; outbox-to-accepted %.3fs; send %.3fs",
                    row["item_id"],
                    result.message_id,
                    max(0, time.time() - row["found_at"]),
                    time.monotonic() - send_started,
                )
        except RetryAfter as exc:
            delay = (
                exc.retry_after.total_seconds()
                if hasattr(exc.retry_after, "total_seconds")
                else float(exc.retry_after)
            )
            finish(
                row,
                failure="Telegram rate limit",
                delay=delay + 1,
                cooldown=True,
                now=now,
            )
        except (BadRequest, Forbidden) as exc:
            if (
                photo
                and media
                and media["telegram_file_id"]
                and isinstance(exc, BadRequest)
            ):
                self.cache_photo(row["reference_id"], None)
                finish(row, failure="Retrying example upload", delay=2, now=now)
            else:
                finish(row, failure=type(exc).__name__, permanent=True, now=now)
                logger.warning(
                    "Telegram rejected %s for item %s: %s",
                    row["kind"],
                    row["item_id"],
                    type(exc).__name__,
                )
        except (NetworkError, TelegramError) as exc:
            attempts = row["photo_attempts" if photo else "attempts"] + 1
            finish(
                row,
                failure=type(exc).__name__,
                delay=min(60, 2 ** min(attempts, 6)),
                permanent=photo and attempts >= 6,
                now=now,
            )
            logger.warning(
                "Telegram %s retry scheduled for item %s", row["kind"], row["item_id"]
            )
        return True

    def cache_photo(self, media_id, file_id):
        if self.platform == "vinted":
            dashboard_store.cache_telegram_photo(media_id, file_id)
        else:
            with closing(connection()) as conn, conn:
                if file_id:
                    conn.execute(
                        "INSERT OR REPLACE INTO platform_media_cache VALUES (?,?,?)",
                        (media_id, self.bot_id, file_id),
                    )
                else:
                    conn.execute(
                        "DELETE FROM platform_media_cache WHERE media_id=? AND bot_id=?",
                        (media_id, self.bot_id),
                    )

    async def run(self):
        while True:
            try:
                # Space starts, not completions: a 0.5s HTTP call already uses
                # half of the one-second chat budget. Choose the next job only
                # after this wait so new listings can jump ahead of examples.
                delay = self.send_slot_delay()
                if delay:
                    await asyncio.sleep(delay)
                worked = await self.tick()
                if not worked:
                    await asyncio.sleep(0.05)
            except Exception as exc:  # noqa: BLE001
                # Leave the durable lease in place; another tick can reclaim it
                # after expiry. Never terminate the queue worker on one bad item.
                logger.error(
                    "Delivery worker will recover after %s", type(exc).__name__
                )
                await asyncio.sleep(2)


class EbayDeliveryWorker(DeliveryWorker):
    """One photo may remain in flight while the next listing uses its send slot.

    A single dispatcher owns chat pacing. Only photos are detached; listing
    sends remain serial, and every claim checks the shared durable cooldown.
    """

    def __init__(self, bot, chat_id, bot_id=None):
        super().__init__(bot, chat_id, platform="ebay", bot_id=bot_id)
        self.photo_task = None

    async def tick(self, now=None):
        while delay := self.send_slot_delay():
            await asyncio.sleep(delay)
        if self.photo_task is not None and self.photo_task.done():
            try:
                self.photo_task.result()
            except Exception as exc:  # noqa: BLE001
                # Unexpected failures keep their lease for normal restart recovery.
                logger.warning(
                    "Example photo will recover after %s", type(exc).__name__
                )
            self.photo_task = None
        row = claim(
            now,
            self.preferred_photo,
            self.platform,
            allow_photos=self.photo_task is None,
        )
        self.preferred_photo = None
        if row is None:
            return False
        if row["kind"] == "photo":
            self.photo_task = asyncio.create_task(self.deliver(row, now))
            # Let the upload record its start before calculating another send slot.
            await asyncio.sleep(0)
            return True
        return await self.deliver(row, now)

    async def close(self):
        if self.photo_task is not None:
            self.photo_task.cancel()
            await asyncio.gather(self.photo_task, return_exceptions=True)
            self.photo_task = None


class VintedDeliveryWorker(EbayDeliveryWorker):
    """One fast alert, with photos edited into that same message in the background."""

    @property
    def disable_previews(self):
        from vinted_alerts import enabled

        return enabled()

    def __init__(self, bot, chat_id):
        super().__init__(bot, chat_id)
        self.platform = "vinted"

    async def photo_slot(self):
        # Downloads happen before reserving a Telegram send slot. Recheck after
        # every await so a listing cannot race an edit into the same chat slot.
        while True:
            delay = self.send_slot_delay()
            with closing(connection()) as conn:
                cooldown = conn.execute(
                    "SELECT value FROM delivery_runtime WHERE key='cooldown_until'"
                ).fetchone()
                pending = conn.execute(
                    """SELECT 1 FROM alert_outbox WHERE platform='vinted' AND status='pending'
                    AND next_attempt<=? AND leased_until<=? LIMIT 1""",
                    (time.time(), time.time()),
                ).fetchone()
            if cooldown:
                delay = max(delay, cooldown[0] - time.time())
            if delay > 0 or pending:
                await asyncio.sleep(min(1, max(delay, 0.05)))
                continue
            self.last_send_started = time.monotonic()
            return

    async def deliver(self, row, now=None):
        from vinted_alerts import enabled

        if not enabled():
            return await DeliveryWorker.deliver(self, row, now)
        if row["kind"] != "photo":
            return await DeliveryWorker.deliver(self, row, now)
        from vinted_alerts import enrich, get_details

        details = get_details(row)
        if details is None:
            # Historical in-flight alerts have no structured snapshot. Preserve
            # their original text rather than attaching a separate reply.
            finish(
                row,
                failure="Older alert has no collage snapshot",
                permanent=True,
                now=now,
            )
            return True
        try:
            # This task is detached by the dispatcher; downloads and uploads
            # cannot hold up the next listing's link.
            complete = await enrich(
                self.bot, self.chat_id, row, details, self.photo_slot
            )
            logger.info(
                "Vinted collage edit accepted for item %s; message_id=%s",
                row["item_id"],
                row["telegram_message_id"],
            )
            finish(
                row,
                failure=None if complete else "Listing photo temporarily unavailable",
                delay=10,
                permanent=not complete and row["photo_attempts"] >= 2,
                now=now,
            )
        except RetryAfter as exc:
            delay = (
                exc.retry_after.total_seconds()
                if hasattr(exc.retry_after, "total_seconds")
                else float(exc.retry_after)
            )
            finish(
                row,
                failure="Telegram rate limit",
                delay=delay + 1,
                cooldown=True,
                now=now,
            )
        except (BadRequest, Forbidden) as exc:
            finish(row, failure=type(exc).__name__, permanent=True, now=now)
            logger.warning(
                "Telegram collage edit rejected for item %s: %s",
                row["item_id"],
                type(exc).__name__,
            )
        except (NetworkError, TelegramError) as exc:
            attempts = row["photo_attempts"] + 1
            finish(
                row,
                failure=type(exc).__name__,
                delay=min(60, 2 ** min(attempts, 6)),
                permanent=attempts >= 6,
                now=now,
            )
        return True
