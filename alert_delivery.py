"""Durable, at-least-once Telegram delivery. No network retries block the queue.

Seen items and pending alerts are committed together by db.add_item_to_db.
An ambiguous HTTP timeout can still produce a duplicate: Telegram has no
idempotency key for sendMessage. Confirmed messages are never retried for a
reference-photo failure. A crashed worker's lease expires automatically.
"""
import asyncio
from contextlib import closing
import io
import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
import dashboard_store
from search_settings import connection
from logger import get_logger

logger = get_logger(__name__)


def claim(now=None, preferred_photo=None):
    now = time.time() if now is None else now
    with closing(connection()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        cooldown = conn.execute("SELECT value FROM delivery_runtime WHERE key='cooldown_until'").fetchone()
        if cooldown and cooldown[0] > now:
            return None
        row, kind = None, 'listing'
        if preferred_photo:
            row = conn.execute('''SELECT * FROM alert_outbox WHERE item_id=? AND status='sent'
                AND photo_status='pending' AND photo_attempts=0 AND photo_next_attempt<=?
                AND leased_until<=?''', (preferred_photo,now,now)).fetchone()
            if row:
                kind = 'photo'
        if row is None:
            row = conn.execute('''SELECT * FROM alert_outbox WHERE status='pending'
                AND next_attempt<=? AND leased_until<=? ORDER BY found_at,item_id LIMIT 1''', (now,now)).fetchone()
        if row is None:
            kind = 'photo'
            row = conn.execute('''SELECT * FROM alert_outbox WHERE status='sent'
                AND photo_status='pending' AND photo_next_attempt<=? AND leased_until<=?
                ORDER BY found_at,item_id LIMIT 1''', (now,now)).fetchone()
        if row is None:
            return None
        token = secrets.token_hex(16)
        conn.execute('UPDATE alert_outbox SET lease_token=?,leased_until=? WHERE item_id=?',
                     (token,now+120,row['item_id']))
        return dict(row, kind=kind, lease_token=token)


def finish(row, *, message_id=None, failure=None, delay=0, permanent=False, cooldown=False, now=None):
    now = time.time() if now is None else now
    photo = row['kind'] == 'photo'
    prefix = 'photo_' if photo else ''
    with closing(connection()) as conn, conn:
        if cooldown:
            conn.execute('''INSERT INTO delivery_runtime VALUES ('cooldown_until',?)
                ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)''', (now+delay,))
        if failure is None:
            extra = '' if photo else ',telegram_message_id=?,sent_at=?'
            args = [] if photo else [message_id,now]
            conn.execute(f'''UPDATE alert_outbox SET {prefix}status='sent', {prefix}error='',
                {prefix}attempts={prefix}attempts+1, lease_token=NULL,leased_until=0 {extra}
                WHERE item_id=? AND lease_token=?''', (*args,row['item_id'],row['lease_token']))
        else:
            conn.execute(f'''UPDATE alert_outbox SET {prefix}status=?,{prefix}error=?,
                {prefix}attempts={prefix}attempts+1,{prefix}next_attempt=?,lease_token=NULL,leased_until=0
                WHERE item_id=? AND lease_token=?''',
                ('failed' if permanent else 'pending',failure,now+delay,row['item_id'],row['lease_token']))


class DeliveryWorker:
    def __init__(self, bot, chat_id):
        self.bot, self.chat_id = bot, chat_id
        self.preferred_photo = None

    async def tick(self, now=None):
        row = claim(now, self.preferred_photo)
        self.preferred_photo = None
        if row is None:
            return False
        photo = row['kind'] == 'photo'
        media = None
        try:
            if photo:
                media = dashboard_store.get_media(row['reference_id'])
                if not media:
                    finish(row,failure='Example photo unavailable',permanent=True,now=now)
                    return True
                image = media['telegram_file_id'] or io.BytesIO(media['image'])
                result = await self.bot.send_photo(chat_id=self.chat_id, photo=image,
                    caption=f"Your example · #{row['query_id'] or '—'} · {row['search_name'][:100]}",
                    disable_notification=True,
                    reply_parameters=ReplyParameters(message_id=row['telegram_message_id'],allow_sending_without_reply=True),
                    read_timeout=8,write_timeout=8,connect_timeout=5,pool_timeout=5)
                # Record success first. A cache failure must never resend a confirmed photo.
                finish(row,now=now)
                try:
                    dashboard_store.cache_telegram_photo(row['reference_id'],result.photo[-1].file_id)
                except Exception:
                    logger.warning('Could not cache reference file ID for item %s',row['item_id'])
            else:
                result = await self.bot.send_message(chat_id=self.chat_id,text=row['content'],parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Open Vinted',url=row['url'])]]),
                    read_timeout=10,write_timeout=10,connect_timeout=5,pool_timeout=5)
                finish(row,message_id=result.message_id,now=now)
                self.preferred_photo = row['item_id']
                logger.info('Telegram accepted item %s; message_id=%s',row['item_id'],result.message_id)
        except RetryAfter as exc:
            delay = exc.retry_after.total_seconds() if hasattr(exc.retry_after,'total_seconds') else float(exc.retry_after)
            finish(row,failure='Telegram rate limit',delay=delay+1,cooldown=True,now=now)
        except (BadRequest, Forbidden) as exc:
            if photo and media and media['telegram_file_id'] and isinstance(exc,BadRequest):
                dashboard_store.cache_telegram_photo(row['reference_id'],None)
                finish(row,failure='Retrying example upload',delay=2,now=now)
            else:
                finish(row,failure=type(exc).__name__,permanent=True,now=now)
                logger.warning('Telegram rejected %s for item %s: %s',row['kind'],row['item_id'],type(exc).__name__)
        except (NetworkError, TelegramError) as exc:
            attempts = row['photo_attempts' if photo else 'attempts'] + 1
            finish(row,failure=type(exc).__name__,delay=min(60,2**min(attempts,6)),
                   permanent=photo and attempts>=6,now=now)
            logger.warning('Telegram %s retry scheduled for item %s',row['kind'],row['item_id'])
        return True

    async def run(self):
        while True:
            try:
                worked = await self.tick()
                await asyncio.sleep(1.05 if worked else 0.15)
            except Exception as exc:
                # Leave the durable lease in place; another tick can reclaim it
                # after expiry. Never terminate the queue worker on one bad item.
                logger.error('Delivery worker will recover after %s',type(exc).__name__)
                await asyncio.sleep(2)
