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


def acknowledge_listing(conn, row, message_id, *, now=None):
    """Save a known native send in the same transaction as its photo controls.

    Keep the lease until ``finish`` releases it. If the process stops between
    the native-send bookkeeping and ``finish``, an expired lease then schedules
    enrichment of the accepted message instead of another notification. Owner
    photo tests and callback recovery have no listing job and cannot acknowledge
    an unrelated pending delivery.
    """
    if row.get("kind") != "listing" or not row.get("lease_token"):
        return False
    changed = conn.execute(
        """UPDATE alert_outbox SET status='sent',telegram_message_id=?,sent_at=?,error=''
        WHERE item_id=? AND platform=? AND status='pending' AND lease_token=?""",
        (
            message_id,
            time.time() if now is None else now,
            row["item_id"],
            row.get("platform", "vinted"),
            row["lease_token"],
        ),
    )
    return changed.rowcount == 1


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
        # A closure can arrive while Telegram is accepting an in-flight send.
        if (
            row.get("platform") == "ebay"
            and failure is None
            and not conn.execute(
                "SELECT 1 FROM alert_outbox WHERE item_id=?", (row["item_id"],)
            ).fetchone()
        ):
            from ebay_privacy import queue_redaction

            queue_redaction(conn, message_id or row.get("telegram_message_id"))
            return
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
        if self.platform == "ebay":
            with closing(connection()) as conn:
                if not conn.execute(
                    "SELECT 1 FROM alert_outbox WHERE item_id=? AND lease_token=?",
                    (row["item_id"], row["lease_token"]),
                ).fetchone():
                    return True
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
                send_started = time.monotonic()
                result = await self.send_listing(row)
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

    async def send_listing(self, row):
        extra = {}
        if self.platform == "ebay" or self.disable_previews:
            # The clickable listing must not depend on an external image fetch.
            extra["link_preview_options"] = LinkPreviewOptions(is_disabled=True)
        self.last_send_started = time.monotonic()
        return await self.bot.send_message(
            **extra,
            chat_id=self.chat_id,
            text=row["content"],
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            ("Open eBay" if self.platform == "ebay" else "Open Vinted"),
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
        if self.platform == "ebay":
            from ebay_privacy import redact_one

            if await redact_one(self.bot, self.chat_id):
                self.last_send_started = time.monotonic()
                return True
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
    """One notification; native listing photos precede a silent comparison edit."""

    @property
    def disable_previews(self):
        return self.alert_module.enabled()

    @property
    def alert_module(self):
        if self.platform == "ebay":
            import ebay_alerts

            return ebay_alerts
        import vinted_alerts

        return vinted_alerts

    def __init__(self, bot, chat_id):
        super().__init__(bot, chat_id)
        self.platform = "vinted"

    async def send_listing(self, row):
        from vinted_native import send_initial

        details = self.alert_module.get_details(row)
        if details and details.get("photo_card"):
            import photo_cards

            async def reserve_photo_slot():
                while delay := self.send_slot_delay():
                    await asyncio.sleep(delay)
                self.last_send_started = time.monotonic()

            return await photo_cards.send_initial(
                self.bot, self.chat_id, row, details, reserve_photo_slot
            )
        if details and not details.get("native_photo"):
            from vinted_alerts import send_text

            self.last_send_started = time.monotonic()
            return await send_text(self.bot, self.chat_id, row, details)
        if not details or not details.get("native_photo"):
            return await super().send_listing(row)

        async def reserve_slot():
            while delay := self.send_slot_delay():
                await asyncio.sleep(delay)
            self.last_send_started = time.monotonic()

        return await send_initial(self.bot, self.chat_id, row, details, reserve_slot)

    async def photo_slot(self):
        # Downloads happen before reserving a Telegram send slot. Recheck after
        # every await so a listing cannot race an edit into the same chat slot.
        while True:
            delay = self.send_slot_delay()
            with closing(connection()) as conn:
                cooldown = conn.execute(
                    "SELECT value FROM delivery_runtime WHERE key=?",
                    (
                        (
                            "ebay_cooldown_until"
                            if self.platform == "ebay"
                            else "cooldown_until"
                        ),
                    ),
                ).fetchone()
                pending = conn.execute(
                    """SELECT 1 FROM alert_outbox WHERE platform=? AND status='pending'
                    AND next_attempt<=? AND leased_until<=? LIMIT 1""",
                    (self.platform, time.time(), time.time()),
                ).fetchone()
            if cooldown:
                delay = max(delay, cooldown[0] - time.time())
            if delay > 0 or pending:
                await asyncio.sleep(min(1, max(delay, 0.05)))
                continue
            self.last_send_started = time.monotonic()
            return

    async def deliver(self, row, now=None):
        if not self.alert_module.enabled():
            return await DeliveryWorker.deliver(self, row, now)
        if row["kind"] != "photo":
            return await DeliveryWorker.deliver(self, row, now)
        details = self.alert_module.get_details(row)
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
            async def edit_slot():
                await self.photo_slot()
                if self.platform == "ebay":
                    with closing(connection()) as conn:
                        if not conn.execute(
                            "SELECT 1 FROM alert_outbox WHERE item_id=? AND lease_token=?",
                            (row["item_id"], row["lease_token"]),
                        ).fetchone():
                            raise TelegramError("Listing removed before photo edit")

            if details.get("photo_card"):
                import photo_cards

                complete = await photo_cards.enrich(
                    self.bot, self.chat_id, row, details, edit_slot
                )
            elif details.get("native_photo"):
                from vinted_native import enrich as native_enrich

                complete = await native_enrich(
                    self.bot, self.chat_id, row, details, edit_slot
                )
            else:
                from vinted_alerts import enrich

                complete = await enrich(self.bot, self.chat_id, row, details, edit_slot)
            if self.platform == "vinted" and details.get("photo_card"):
                import photo_cards

                await photo_cards.enrichment_failures(
                    self.platform, row["telegram_message_id"], failed=False
                )
            logger.info(
                "Listing photo %s for item %s; message_id=%s",
                "ready" if complete else "unavailable; retry pending",
                row["item_id"],
                row["telegram_message_id"],
            )
            delay = 10
            permanent = not complete and row["photo_attempts"] >= 2
            if self.platform == "vinted" and details.get("photo_card") and not complete:
                import photo_cards
                from vinted_gallery import retry_pending

                saved = photo_cards.load("vinted", row["telegram_message_id"])
                if saved:
                    pending_description = retry_pending(saved[1])
                    # Deferrals by the shared Vinted cooldown are not failed
                    # downloads. Keep the two bounded retry counts separate.
                    permanent = (
                        not pending_description
                        and saved[1].get("listing_image_failures", 0) >= 3
                    )
                    if pending_description:
                        retry_at = min(
                            saved[1].get("listing_retry_after", 0),
                            saved[1].get("listing_retry_until", 0),
                        )
                        delay = max(delay, retry_at - time.time())
            finish(
                row,
                failure=None if complete else "Listing photo temporarily unavailable",
                delay=delay,
                permanent=permanent,
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
            if self.platform == "vinted" and details.get("photo_card"):
                import photo_cards

                attempts = await photo_cards.enrichment_failures(
                    self.platform, row["telegram_message_id"], failed=True
                )
            finish(
                row,
                failure=type(exc).__name__,
                delay=min(60, 2 ** min(attempts, 6)),
                permanent=attempts >= 6,
                now=now,
            )
        return True


class EbayPhotoDeliveryWorker(VintedDeliveryWorker):
    """eBay uses the same photo-first, in-place comparison layout as Vinted."""

    def __init__(self, bot, chat_id, bot_id=None):
        super().__init__(bot, chat_id)
        self.platform = "ebay"
        self.bot_id = bot_id
