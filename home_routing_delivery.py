"""Deliver observed public listings to destinations selected by the dashboard owner."""
import asyncio
import json
import logging
import re
import secrets
import sqlite3
import time
from types import SimpleNamespace
from contextlib import closing
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, InputMediaPhoto
from telegram.error import BadRequest, ChatMigrated, Forbidden, NetworkError, RetryAfter, TelegramError

import home_routing_store as store
import telegram_shared_alerts as shared
from search_settings import connection

logger = logging.getLogger(__name__)
COMMAND = re.compile(r"/home_alerts(?:@\w+)?\s+([A-Za-z0-9_-]{24})\Z", re.I)


def pairing_update(update):
    chat = getattr(update, "effective_chat", None)
    user = getattr(update, "effective_user", None)
    msg = getattr(update, "effective_message", None)
    return bool(chat and user and not user.is_bot and msg and not getattr(msg, "sender_chat", None)
                and COMMAND.fullmatch(getattr(msg, "text", None) or "") and chat.type in {"private", *shared.GROUP_TYPES})


async def group_verified(bot, chat, owner, partner):
    if not await shared.private_group(bot, chat):
        return False
    members = await asyncio.gather(bot.get_chat_member(chat, owner), bot.get_chat_member(chat, partner))
    return all(member.status in {"creator", "administrator", "member"} or
               (member.status == "restricted" and getattr(member, "is_member", False)) for member in members)


async def pair_command(update, context):
    if not pairing_update(update):
        return
    msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
    code = COMMAND.fullmatch(msg.text).group(1)
    try:
        owner, _ = store.destination_challenge(code)
        partner = None
        if chat.type == "private":
            if chat.id != user.id:
                return
        else:
            group = shared.current()
            if user.id != owner or not group or not await group_verified(context.bot, chat.id, owner, group.partner):
                return
            partner = group.partner
        display_name = (getattr(chat, "title", None) if partner else getattr(user, "full_name", None)) or ""
        store.pair_chat(code, chat.id, partner, display_name=display_name)
    except (PermissionError, ValueError, TelegramError):
        return
    await msg.reply_text("This chat is paired for Home alerts. Select its Home sources in Finder.")


def _destination(conn, job, now):
    owner, chat, item = job["owner"], job["chat"], job["item"]
    if owner != store.owner_id(conn) or job["expires"] <= now:
        return None
    from finder_connections import gate
    blocked, resumed = gate(conn)
    if blocked:
        return None
    dests = store._destinations(conn, owner)
    for source, dest, observed in conn.execute("""SELECT o.source,o.dest,e.observed FROM home_route_origins o
            JOIN home_route_events e ON (e.owner,e.source,e.item)=(o.owner,o.source,o.item)
            WHERE o.owner=? AND o.chat=? AND o.item=?""", (owner, chat, item)):
        if observed >= resumed and dest in dests and dests[dest][1] == chat and dest in store.selected(conn, owner, source, dests):
            if source == store.ORIGINAL:
                from alert_controls import state
                if not state("home", conn).allows(observed):
                    continue
            return dest, dests[dest][2]
    return None


def claim(now=None):
    now = time.time() if now is None else now
    with closing(connection()) as conn, conn:
        # A crash between selection and the API send is safely retryable. Once Telegram
        # could have seen a send, unknown acknowledgement is terminal to avoid duplicates.
        query = """SELECT * FROM home_route_jobs WHERE state IN ('ready','checking','native_hold')
                   AND next_try<=? AND expires>? ORDER BY expires LIMIT 1"""
        if not conn.execute(query, (now, now)).fetchone():
            return None
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(query, (now, now)).fetchone()
        if row is None:
            return None
        job = dict(row)
        dest = _destination(conn, job, now)
        key = (job["owner"], job["chat"], job["item"])
        if not dest:
            conn.execute("UPDATE home_route_jobs SET state='cancelled' WHERE (owner,chat,item)=(?,?,?)", key)
            return None
        lease = secrets.token_hex(12)
        conn.execute("UPDATE home_route_jobs SET state='checking',lease=?,next_try=? WHERE (owner,chat,item)=(?,?,?)",
                     (lease, now + 30, *key))
        return dict(job, lease=lease, destination=dest[0], partner=dest[1])


def transition(job, state, *, delay=0, receipt=None, has_photo=False, before_send=False):
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if before_send:
            current = _destination(conn, job, time.time())
            if not current:
                state = "cancelled"
            elif current != (job["destination"], job["partner"]):
                state = "ready"
            else:
                delay = 30
        changed = conn.execute("""UPDATE home_route_jobs SET state=?,next_try=?,receipt=?,receipt_photo=?
                   WHERE owner=? AND chat=? AND item=? AND lease=? AND state IN ('checking','sending')""",
                               (state, time.time()+delay, receipt, has_photo, job["owner"], job["chat"], job["item"], job["lease"]))
        return bool(changed.rowcount and (not before_send or state == "sending"))


def _native_receipt(job):
    """Only quote an actual accepted alert in the SAME already-authorized chat."""
    with closing(connection()) as conn:
        item_id = job["item"]
        if job["destination"] == "owner" and store.owner_id(conn) == job["chat"]:
            row = conn.execute("""SELECT telegram_message_id FROM alert_outbox WHERE item_id=? AND platform='vinted'
                                 AND status='sent' AND telegram_message_id IS NOT NULL""", (item_id,)).fetchone()
        elif job["destination"].startswith("shared-"):
            group = shared.current(conn)
            if not group or group.chat != job["chat"] or "shared-"+group.version != job["destination"]:
                return None
            row = conn.execute("""SELECT s.message_id FROM telegram_shared_deliveries s JOIN alert_outbox a
                ON a.item_id=s.item_id AND a.telegram_message_id=s.owner_message_id
                WHERE s.item_id=? AND s.version=? AND s.message_id IS NOT NULL AND a.status='sent'
                AND a.platform='vinted'""", (item_id, group.version)).fetchone()
        else:
            return None
        return row[0] if row else None


class ExistingMessage:
    """Apply the original alert renderer to its own provisional message in the same chat."""
    def __init__(self, bot, receipt, photo):
        self.bot, self.receipt, self.photo = bot, receipt, photo

    def __getattr__(self, name):
        return getattr(self.bot, name)

    async def _edit(self, method, **kwargs):
        try:
            return await method(message_id=self.receipt, **kwargs)
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
            return SimpleNamespace(message_id=self.receipt, photo=[])

    async def send_photo(self, *, photo, caption, parse_mode=None, show_caption_above_media=False, **kwargs):
        media = InputMediaPhoto(photo, caption=caption, parse_mode=parse_mode, show_caption_above_media=show_caption_above_media)
        return await self._edit(self.bot.edit_message_media, media=media, **kwargs)

    async def send_message(self, *, text, **kwargs):
        if self.photo:
            kwargs.pop("link_preview_options", None)
            return await self._edit(self.bot.edit_message_caption, caption=text, **kwargs)
        return await self._edit(self.bot.edit_message_text, text=text, **kwargs)


async def native_transport(bot, chat_id, item):
    # Canonical saved-search or original Home evidence may arrive after a
    # browser notification. Promote the confirmed provisional receipt in place.
    for _ in range(8):
        with closing(connection()) as conn:
            if not store._enabled(conn):
                return bot
            key = (store.owner_id(conn), chat_id, str(item))
            read = """SELECT state,receipt,receipt_photo,next_try FROM home_route_jobs
                      WHERE owner=? AND chat=? AND item=?"""
            row = conn.execute(read, key).fetchone()
            if row and row[0] in {"ready", "checking"}:
                # A saved-search hit can arrive between a browser observation and
                # its Telegram write. Reserve the physical destination atomically.
                with conn:
                    conn.execute("""UPDATE home_route_jobs SET state='native_hold',lease=NULL,next_try=?
                                    WHERE owner=? AND chat=? AND item=? AND state IN ('ready','checking')""",
                                 (time.time()+30, *key))
                row = conn.execute(read, key).fetchone()
            if row and row[0] == "sending" and row[3] <= time.time():
                with conn:
                    conn.execute("""UPDATE home_route_jobs SET state='unknown' WHERE owner=? AND chat=? AND item=?
                                    AND state='sending' AND next_try<=?""", (*key, time.time()))
                # The browser card had only a link. A trusted canonical alert
                # may proceed even if the prior link-only delivery was unconfirmed.
                return bot
        if not row or row[0] != "sending":
            return ExistingMessage(bot, row[1], row[2]) if row and row[0] == "sent" and row[1] else bot
        await asyncio.sleep(0.25)
    # A live in-flight send has no known receipt yet: use the native worker's
    # existing retry path instead of issuing a second send.
    from telegram.error import RetryAfter
    raise RetryAfter(2)


async def deliver(bot, job):
    sending = False
    try:
        if job["chat"] < 0 and not await group_verified(bot, job["chat"], job["owner"], job["partner"]):
            revoke(job)
            return
        if not transition(job, "sending", before_send=True):
            return
        sending = True
        # An already delivered native card has its own idempotent Buy control.
        # If it won the race, don't send a second notice for the very same item.
        receipt = _native_receipt(job)
        if receipt:
            transition(job, "sent", receipt=receipt)
            return
        item = json.loads(job["body"])
        summary = f"🏠 <b>{escape(item['title'])}</b>\n£{item['price_gbp']}"
        options = dict(chat_id=job["chat"], parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Open on Vinted", url=item["url"])
        ]]), connect_timeout=5, read_timeout=10, write_timeout=10, pool_timeout=5)
        message = None
        if item.get("photo_url"):
            try:
                message = await bot.send_photo(**options, photo=item["photo_url"], caption=summary)
            except BadRequest:
                # This photo was explicitly rejected. It's safe to send text with the same link.
                pass
        if message is None and transition(job, "sending", before_send=True):
            message = await bot.send_message(**options, text=summary, link_preview_options=LinkPreviewOptions(is_disabled=True))
        if message:
            transition(job, "sent", receipt=message.message_id, has_photo=bool(getattr(message, "photo", None)))
    except RetryAfter as exc:
        wait = exc.retry_after
        transition(job, "ready", delay=(wait.total_seconds() if hasattr(wait, "total_seconds") else float(wait)) + 1)
    except (Forbidden, ChatMigrated):
        revoke(job)
    except BadRequest:
        transition(job, "failed")
    except (NetworkError, TelegramError):
        transition(job, "unknown" if sending else "ready", delay=3)


def revoke(job):
    """Invalid membership disables all routes to an independently enrolled destination."""
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        dest, owner = job["destination"], job["owner"]
        if dest.startswith("chat-"):
            conn.execute("DELETE FROM home_route_destinations WHERE owner=? AND id=?", (owner, dest))
            conn.execute("DELETE FROM home_routes WHERE owner=? AND dest=?", (owner, dest))
            conn.execute("DELETE FROM home_route_origins WHERE owner=? AND dest=?", (owner, dest))
        conn.execute("""UPDATE home_route_jobs SET state='cancelled' WHERE owner=? AND chat=?
                     AND state IN ('ready','sending','checking')""", (owner, job["chat"]))


async def run(bot):
    while True:
        try:
            job = await asyncio.to_thread(claim)
            if job:
                await deliver(bot, job)
            else:
                await asyncio.sleep(0.5)
        except (sqlite3.Error, TelegramError) as exc:
            logger.warning("Home destination delivery deferred: %s", type(exc).__name__)
            await asyncio.sleep(2)
