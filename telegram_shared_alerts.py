"""A private Telegram group for two authorised Finder buyers."""

import asyncio
import json
import re
import sqlite3
import time
from contextlib import closing
from typing import NamedTuple
from uuid import uuid4

from telegram import BotCommandScopeChatMember, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import BadRequest, ChatMigrated, Forbidden, RetryAfter, TelegramError

import alert_controls
import photo_cards
from logger import get_logger
from search_settings import connection

logger = get_logger(__name__)
KEY = "telegram_shared_alert_group"
JOIN_PREFIX = "telegram_shared_join:"
JOIN_LIFETIME = 60 * 60
LEGACY_JOIN_LIFETIME = 15 * 60
GROUP_TYPES = {"group", "supergroup"}
BUY_PATTERN = r"^shared:(?:buy|status|stop:[0-9a-f]{16})$"
APPROVE_PATTERN = r"^shared:approve:([0-9a-f]{32})$"
SETUP_COMMAND = re.compile(r"^/share_alerts(?:@\w+)?\s*$", re.I)
JOIN_COMMAND = re.compile(r"^/join_alerts(?:@\w+)?\s*\Z", re.I)
GROUP_START = re.compile(r"^/start(?:@\w+)?\s+shared_alerts\s*\Z", re.I)
PRIVATE_COMMAND = re.compile(r"^/(start|share_alerts|purchase_status)(?:@(\w+))?\Z", re.I)
MENU_COMMANDS = [
    ("dashboard", "Open your search dashboard"),
    ("purchase_status", "Check or confirm a purchase"),
    *((command, label) for command, (_, _, label) in alert_controls.COMMANDS.items()),
    ("share_alerts", "Set up shared alerts and Autobuy"),
    ("stop_sharing", "Stop shared alerts and Autobuy"),
]
MENU_COMMAND = re.compile(
    r"^/(?:" + "|".join(command for command, _ in MENU_COMMANDS) + r")(?:@\w+)?\s*\Z",
    re.I,
)
PRIVATE_MENU_COMMAND = re.compile(
    r"^/(?:(?:dashboard|purchase[ _]status)(?:@\w+)?|start(?:@\w+)?\s+(?:dashboard|purchase_status))\s*\Z",
    re.I,
)


class Group(NamedTuple):
    chat: int
    owner: int
    partner: int
    version: str
    linked_at: float


class JoinRequest(NamedTuple):
    owner: int
    partner: int
    name: str
    token: str
    requested_at: float


def current(conn=None):
    if conn is None:
        with closing(connection()) as opened:
            return current(opened)
    values = dict(conn.execute(
        "SELECT key,value FROM parameters WHERE key IN (?, 'telegram_chat_id')",
        (KEY,),
    ))
    if not values.get(KEY):
        return None
    try:
        group = Group(**json.loads(values[KEY]))
        if (
            group.chat < 0 and group.owner > 0 and group.partner > 0
            and group.owner != group.partner and str(group.owner) == (values.get("telegram_chat_id") or "").strip()
        ):
            return group
    except (TypeError, ValueError):
        pass
    return None


def pending_join(conn, chat):
    row = conn.execute(
        "SELECT value FROM parameters WHERE key=?", (f"{JOIN_PREFIX}{chat}",)
    ).fetchone()
    if row:
        try:
            data = json.loads(row[0])
            if "expires_at" in data:
                data["requested_at"] = data.pop("expires_at") - LEGACY_JOIN_LIFETIME
            request = JoinRequest(**data)
            if time.time() < request.requested_at + JOIN_LIFETIME:
                return request
        except (TypeError, ValueError):
            pass
    return None


def save_group(chat, owner, partner, *, request=None):
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        configured = conn.execute(
            "SELECT value FROM parameters WHERE key='telegram_chat_id'"
        ).fetchone()
        if not configured or (configured[0] or "").strip() != str(owner):
            return None
        if request is not None and pending_join(conn, chat) != request:
            return None
        conn.execute("DELETE FROM parameters WHERE key=?", (f"{JOIN_PREFIX}{chat}",))
        existing = current(conn)
        if existing and existing[:3] == (chat, owner, partner):
            return existing
        # This table is only needed after the owner links a group.
        conn.execute("""CREATE TABLE IF NOT EXISTS telegram_shared_deliveries (
            version TEXT NOT NULL, item_id TEXT NOT NULL REFERENCES alert_outbox(item_id) ON DELETE CASCADE,
            owner_message_id INTEGER NOT NULL, body TEXT NOT NULL, photo_id TEXT,
            next_attempt REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            message_id INTEGER, failed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(version,item_id), UNIQUE(version,message_id))""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_telegram_shared_due
            ON telegram_shared_deliveries(version,next_attempt) WHERE message_id IS NULL AND failed=0""")
        group = Group(chat, owner, partner, uuid4().hex, time.time())
        conn.execute("INSERT OR REPLACE INTO parameters VALUES (?,?)", (KEY, json.dumps(group._asdict())))
        return group


def unlink(version=None):
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if version is None:
            conn.execute("DELETE FROM parameters WHERE key GLOB ?", (JOIN_PREFIX + "*",))
        group = current(conn)
        if group and (version is None or group.version == version):
            conn.execute("DELETE FROM parameters WHERE key=?", (KEY,))
            logger.info("Telegram shared alerts disconnected")
            return True
    return False


def member(group, chat, sender):
    return bool(group and chat and sender and chat.type in GROUP_TYPES
                and chat.id == group.chat and not sender.is_bot and sender.id in (group.owner, group.partner))


def private_partner(chat, user):
    if getattr(chat, "type", None) != "private" or not user or user.is_bot or chat.id != user.id:
        return None
    group = current()
    return group if group and group.partner == user.id else None


def control_chat(chat, user):
    if not chat or not user or user.is_bot:
        return False
    if chat.type == "private":
        from vinted_owner_controls import private_owner

        return chat.id == user.id and private_owner(user.id)
    return member(current(), chat, user)


def membership_message(message):
    return bool(message and (message.new_chat_members or message.left_chat_member or message.migrate_to_chat_id))


def allowed_update(update, owner):
    chat = update.effective_chat
    chat_type = getattr(chat, "type", None)
    if chat_type != "private" and chat_type not in GROUP_TYPES:
        return False
    message, sender = update.message, update.effective_user
    query = update.callback_query
    if chat_type == "private":
        opens_menu = message and PRIVATE_MENU_COMMAND.fullmatch(message.text or "")
        answers_purchase = query and (query.data or "").startswith("status:purchase:")
        return bool((opens_menu or answers_purchase) and private_partner(chat, sender))
    if message and sender and JOIN_COMMAND.fullmatch(message.text or ""):
        return True
    if message and sender and str(sender.id) == owner:
        if SETUP_COMMAND.fullmatch(message.text or "") or GROUP_START.fullmatch(message.text or ""):
            return True
    if message and MENU_COMMAND.fullmatch(message.text or ""):
        return member(current(), chat, sender)
    if membership_message(message):
        return True
    if not query:
        return False
    if re.fullmatch(APPROVE_PATTERN, query.data or ""):
        return True
    return bool(re.fullmatch(BUY_PATTERN, query.data or "") and member(current(), chat, sender))


async def private_group(bot, chat_id):
    chat, count = await asyncio.gather(bot.get_chat(chat_id), bot.get_chat_member_count(chat_id))
    return chat.type in GROUP_TYPES and not chat.username and not getattr(chat, "active_usernames", None) and count == 3


async def update_group_menu(bot, group, *, clear=False):
    async def update_member(user_id):
        scope = BotCommandScopeChatMember(group.chat, user_id)
        try:
            if clear:
                return await bot.delete_my_commands(scope=scope, **photo_cards.TIMEOUTS)
            return await bot.set_my_commands(MENU_COMMANDS, scope=scope, **photo_cards.TIMEOUTS)
        except TelegramError as exc:
            logger.info("Telegram shared command menus deferred: %s", type(exc).__name__)
            return False

    updated = all(await asyncio.gather(update_member(group.owner), update_member(group.partner)))
    if updated and not clear:
        logger.info("Telegram shared command menus ready for two buyers")
    return updated


async def open_private_control(update, context):
    chat, user = update.effective_chat, update.effective_user
    if not member(current(), chat, user):
        return
    command = update.effective_message.text.split(maxsplit=1)[0][1:].split("@")[0].lower()
    label = "Open dashboard" if command == "dashboard" else "Check purchase status"
    button = InlineKeyboardButton(label, url=f"https://t.me/{context.bot.username}?start={command}")
    await update.effective_message.reply_text(
        "Open this in your private Finder chat.",
        reply_markup=InlineKeyboardMarkup([[button]]),
    )


async def group_setup_error(bot, chat_id):
    try:
        if await private_group(bot, chat_id):
            return None
    except TelegramError as exc:
        logger.info("Telegram shared group verification deferred: %s", type(exc).__name__)
        return "Finder couldn't verify this group. Try again in a moment."
    return "Use a private group with just you, your buying partner and Finder."


def join_instructions(username):
    return (
        f"Have your buying partner send /join_alerts@{username} in the group. "
        "Finder will show you an approval button."
    )


def sharing_enabled(name):
    return (
        f"Shared alerts are on. You and {name} can use Autobuy here with the connected Vinted buying account. "
        "New MSJ Home and Vinted search alerts will appear here."
    )


def add_group_button(username):
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "Add Finder to your group", url=f"https://t.me/{username}?startgroup=shared_alerts"
    )]])


async def private_setup_help(update, context):
    chat = update.effective_chat
    if getattr(chat, "type", None) != "private" or not update.message:
        return
    user = update.effective_user
    words = (update.message.text or "").strip().split(maxsplit=1)
    command = PRIVATE_COMMAND.fullmatch(words[0]) if words else None
    if not user or user.is_bot or user.id != chat.id or not command:
        return
    if command[2] and command[2].lower() != context.bot.username.lower():
        return
    name = command[1].lower()
    if len(words) > 1 and (name != "start" or words[1] not in {"purchase_status", "dashboard"}):
        return
    if len(words) > 1 and words[1] == "dashboard":
        await update.message.reply_text("The Finder account owner can open the search dashboard from their private chat.")
        return
    if name == "purchase_status" or len(words) > 1:
        await update.message.reply_text(
            "Only the account owner can review a payment. Ask them to open Purchase status in their private Finder chat."
        )
        return
    group = current()
    if group and group.partner == user.id:
        await update.message.reply_text("You're already linked. Open your shared group for new Finder alerts and Autobuy.")
        return
    await update.message.reply_text(
        "Shared alerts and Autobuy work in your private group. "
        "Ask the person who already receives Finder alerts to add Finder to the group. "
        f"Then send /join_alerts@{context.bot.username} there so they can approve you."
    )


async def share_command(update, context):
    message, user = update.message, update.effective_user
    import db

    owner = str(db.get_parameter("telegram_chat_id") or "").strip()
    if not message or not user or user.is_bot:
        return
    chat = message.chat
    if str(user.id) != owner:
        if member(current(), chat, user):
            await message.reply_text("Shared alerts and Autobuy are on in this group.")
        return
    username = context.bot.username
    if chat.type == "private" and str(chat.id) == owner:
        await message.reply_text(
            "Tap the button and choose the private group you share with your buying partner. "
            "Confirm adding Finder. " + join_instructions(username),
            reply_markup=add_group_button(username),
        )
        return
    if chat.type not in GROUP_TYPES:
        return
    reply = message.reply_to_message
    partner = reply.from_user if reply else None
    if (
        GROUP_START.fullmatch(message.text or "") or not partner or partner.is_bot or partner.id == user.id or reply.chat.id != chat.id
        or reply.forward_origin or reply.sender_chat
    ):
        group = current()
        text = (
            "Shared alerts and Autobuy are already on in this group."
            if group and group.chat == chat.id
            else join_instructions(username)
        )
        await message.reply_text(text)
        return
    error = await group_setup_error(context.bot, chat.id)
    if error:
        await message.reply_text(error)
        return
    if save_group(chat.id, user.id, partner.id):
        logger.info("Telegram shared alerts linked for two buyers")
        await message.reply_text(sharing_enabled(partner.first_name))


async def join_command(update, context):
    """Get the partner's authenticated Telegram identity directly."""
    import db

    message, user = update.message, update.effective_user
    if (
        not message or message.chat.type not in GROUP_TYPES or not user or user.is_bot
        or user.id <= 0 or message.sender_chat or message.forward_origin
    ):
        return
    owner = str(db.get_parameter("telegram_chat_id") or "").strip()
    if not owner.isdigit() or int(owner) <= 0:
        return
    if user.id == int(owner):
        await message.reply_text(join_instructions(context.bot.username))
        return
    chat = message.chat.id
    error = await group_setup_error(context.bot, chat)
    if error:
        await message.reply_text(error)
        return
    group = current()
    if group and group.chat == chat and group.partner == user.id:
        await message.reply_text("You're already linked. Shared alerts and Autobuy are on in this group.")
        return
    name = " ".join(user.first_name.split())[:40] or "your partner"
    request = JoinRequest(int(owner), user.id, name, uuid4().hex, time.time())
    with closing(connection()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO parameters(key,value) VALUES (?,?)",
            (f"{JOIN_PREFIX}{chat}", json.dumps(request._asdict())),
        )
    await message.reply_text(
        f"{name} requested shared alerts and Autobuy. "
        "The Finder account owner can approve this request below.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"Approve {name}", callback_data=f"shared:approve:{request.token}")
        ]]),
    )
    logger.info("Telegram shared group join request delivered")


async def approve_join(update, context):
    from vinted_owner_controls import private_owner

    query, chat = update.callback_query, update.effective_chat
    match = re.fullmatch(APPROVE_PATTERN, query.data or "")
    if not match or getattr(chat, "type", None) not in GROUP_TYPES or not query.message:
        return
    if query.from_user.is_bot or not private_owner(query.from_user.id):
        await photo_cards.answer(query, "The Finder account owner needs to tap Approve.", alert=True)
        return
    expired = (
        "This request is no longer active. "
        f"Ask your partner to send /join_alerts@{context.bot.username} again."
    )
    with closing(connection()) as conn:
        request = pending_join(conn, chat.id)
    if not request or request.token != match[1] or request.owner != query.from_user.id:
        await photo_cards.answer(query, expired, alert=True)
        return
    error = await group_setup_error(context.bot, chat.id)
    if error:
        await photo_cards.answer(query, error, alert=True)
        return
    group = save_group(chat.id, request.owner, request.partner, request=request)
    if not group:
        await photo_cards.answer(query, expired, alert=True)
        return
    logger.info("Telegram shared alerts linked for two buyers")
    await photo_cards.answer(query, "Shared alerts are on.")
    try:
        await query.edit_message_text(sharing_enabled(request.name), **photo_cards.TIMEOUTS)
    except TelegramError as exc:
        logger.info("Telegram shared group confirmation edit deferred: %s", type(exc).__name__)


async def stop_command(update, context):
    message, user = update.message, update.effective_user
    if not message:
        return
    if message.chat.type == "private":
        if not control_chat(message.chat, user):
            return
        unlink()
    else:
        group = current()
        if not member(group, message.chat, user) or not unlink(group.version):
            return
    await message.reply_text("Shared alerts and group Autobuy are off.")


async def members_changed(update, context):
    chat = update.effective_chat.id
    with closing(connection()) as conn, conn:
        conn.execute("DELETE FROM parameters WHERE key=?", (f"{JOIN_PREFIX}{chat}",))
    group = current()
    if group and chat == group.chat:
        # Any membership change requires the owner to pair the group again.
        await suspend(context.bot, group)


async def suspend(bot, group):
    if not unlink(group.version):
        return
    try:
        await bot.send_message(
            chat_id=group.owner,
            text="Finder stopped shared alerts because it could no longer verify the private group. Run /share_alerts to link it again.",
            **photo_cards.TIMEOUTS,
        )
    except TelegramError as exc:
        logger.info("Telegram shared group notice deferred: %s", type(exc).__name__)


def enqueue(conn, row, result, now):
    """Queue a second delivery from the original accepted listing snapshot."""
    if row.get("platform", "vinted") != "vinted":
        return
    try:
        group = current(conn)
        if not group or row["found_at"] < group.linked_at:
            return
        pictures = getattr(result, "photo", None)
        photo_id = pictures[-1].file_id if pictures else None
        body = getattr(result, "caption_html" if photo_id else "text_html", None) or row["content"]
        conn.execute("""INSERT OR IGNORE INTO telegram_shared_deliveries
            (version,item_id,owner_message_id,body,photo_id,next_attempt) VALUES(?,?,?,?,?,?)""",
            (group.version, row["item_id"], result.message_id, body, photo_id, now),
        )
    except (sqlite3.Error, AttributeError, TypeError, ValueError) as exc:
        # A secondary delivery must never undo the accepted private alert.
        logger.warning("Telegram shared listing queue unavailable: %s", type(exc).__name__)


def lookup(group, message_id):
    with closing(connection()) as conn:
        if current(conn) != group:
            return None
        row = conn.execute("""SELECT o.* FROM telegram_shared_deliveries s
            JOIN alert_outbox o ON o.item_id=s.item_id AND o.telegram_message_id=s.owner_message_id
            WHERE s.version=? AND s.message_id=? AND o.platform='vinted' AND o.status='sent'""",
            (group.version, message_id),
        ).fetchone()
        return dict(row) if row else None


def claim(group, now):
    with closing(connection()) as conn, conn:
        def first():
            return conn.execute("""SELECT s.*,o.title,o.url,o.price,o.content,o.found_at,o.platform
                FROM telegram_shared_deliveries s JOIN alert_outbox o ON s.item_id=o.item_id
                WHERE s.version=? AND s.next_attempt<=? AND s.failed=0 AND s.message_id IS NULL
                AND o.status='sent' AND o.telegram_message_id=s.owner_message_id
                ORDER BY s.next_attempt,s.item_id LIMIT 1""", (group.version, now)).fetchone()
        if not first():
            return None
        conn.execute("BEGIN IMMEDIATE")
        row = first() if current(conn) == group else None
        if not row:
            return None
        conn.execute("""UPDATE telegram_shared_deliveries SET attempts=attempts+1,next_attempt=?
            WHERE version=? AND item_id=?""", (now + 120, group.version, row["item_id"]))
        return dict(row)


def finish(row, *, message_id=None, delay=0, failed=False):
    with closing(connection()) as conn, conn:
        conn.execute("""UPDATE telegram_shared_deliveries SET message_id=?,next_attempt=?,failed=?,
            body=CASE WHEN ? THEN '' ELSE body END,photo_id=CASE WHEN ? THEN NULL ELSE photo_id END
            WHERE version=? AND item_id=? AND attempts=? AND message_id IS NULL""",
            (message_id, time.time() + delay, failed, bool(message_id or failed), bool(message_id or failed),
             row["version"], row["item_id"], row["attempts"] + 1),
        )


def markup(row, state=None, stop=None):
    states = {
        "in_progress": "Autobuy in progress",
        "preparing": "Autobuy in progress",
        "stopping": "Stopping Autobuy",
        "paid": "Bought ✓",
        "needs_action": "Payment needs the owner",
        "paying": "Checking payment",
        "unknown": "Payment result unconfirmed",
        "payment_failed": "Purchase ended · check status",
        "closed": "Purchase closed",
    }
    rows = [[InlineKeyboardButton("Open Vinted listing ↗", url=row["url"])]]
    rows.append([InlineKeyboardButton(states[state], callback_data="shared:status")] if state in states
                else [InlineKeyboardButton("Autobuy", callback_data="shared:buy")])
    if stop:
        rows.append([InlineKeyboardButton("Stop Autobuy", callback_data=f"shared:stop:{stop}")])
    return InlineKeyboardMarkup(rows)


async def send(bot, group, row):
    import vinted_alerts
    import vinted_buying

    if current() != group or not alert_controls.state(alert_controls.row_source(row)).allows(row["found_at"]):
        finish(row, failed=True)
        return
    details = vinted_alerts.get_details(row) or {}
    if not vinted_alerts.home_discovery_fresh(details):
        finish(row, failed=True)
        return
    state = (vinted_buying.result(row["item_id"]) or {}).get("state")
    common = dict(chat_id=group.chat, parse_mode="HTML", reply_markup=markup(row, state), **photo_cards.TIMEOUTS)
    try:
        response = None
        if row["photo_id"]:
            try:
                response = await bot.send_photo(
                    **common, photo=row["photo_id"], caption=row["body"], show_caption_above_media=True
                )
            except BadRequest:
                pass  # The cached file or caption was rejected; preserve the deal link.
        if not response and current() == group:
            response = await bot.send_message(**common, text=row["body"], link_preview_options=LinkPreviewOptions(is_disabled=True))
        if response:
            finish(row, message_id=response.message_id)
            logger.info("Telegram accepted shared Vinted alert")
        else:
            finish(row, failed=True)
    except RetryAfter as exc:
        seconds = exc.retry_after
        finish(row, delay=(seconds.total_seconds() if hasattr(seconds, "total_seconds") else float(seconds)) + 1)
        raise
    except (Forbidden, ChatMigrated):
        await suspend(bot, group)
    except BadRequest:
        finish(row, failed=True)
    except TelegramError:
        finish(row, delay=min(60, 2 ** min(row["attempts"] + 1, 6)))


async def run(bot):
    logger.info("Telegram shared alerts ready; can_join_groups=%s", getattr(bot.bot, "can_join_groups", None))
    verified, verify_at = None, 0
    menu_group, menus_ready = None, False
    while True:
        try:
            group = current()
            if menu_group != group:
                if menu_group:
                    await update_group_menu(bot, menu_group, clear=True)
                menu_group, menus_ready = group, False
            if not group:
                verified = None
                await asyncio.sleep(2)
                continue
            now = time.time()
            if verified != group or now >= verify_at:
                if not await private_group(bot, group.chat):
                    await suspend(bot, group)
                    continue
                verified, verify_at = group, now + 60
                if not menus_ready:
                    menus_ready = await update_group_menu(bot, group)
            row = claim(group, now)
            if row:
                await send(bot, group, row)
                await asyncio.sleep(3)  # Telegram's per-group send allowance.
            else:
                await asyncio.sleep(0.25)
        except (Forbidden, ChatMigrated):
            if group:
                await suspend(bot, group)
            await asyncio.sleep(2)
        except RetryAfter as exc:
            delay = exc.retry_after
            await asyncio.sleep((delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay)) + 1)
        except (sqlite3.Error, TelegramError) as exc:
            logger.warning("Telegram shared delivery retry: %s", type(exc).__name__)
            await asyncio.sleep(3)
