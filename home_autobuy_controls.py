"""Display buying controls on the original Home alert. No purchase is made here."""
import asyncio
import logging
import time
from contextlib import closing

from telegram import ReplyParameters
from telegram.error import BadRequest, TelegramError

import photo_cards
from search_settings import connection
from vinted_home_activity import HOME_ALERT

logger = logging.getLogger(__name__)


async def show(bot, chat_id, message_id):
    async with photo_cards.lock("vinted", message_id):
        saved = photo_cards.load("vinted", message_id)
        if not saved:
            return False
        row, details, card = saved
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id,
                reply_markup=photo_cards.markup(row, details, view=card["view"]))
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
        return True


async def open_alert(message, bot, item_id):
    with closing(connection()) as conn:
        row = conn.execute("SELECT telegram_message_id FROM alert_outbox WHERE " + HOME_ALERT
                           + " AND status='sent' AND item_id=?", (item_id,)).fetchone()
    if row and row[0]:
        try:
            if await show(bot, message.chat.id, row[0]):
                await bot.send_message(chat_id=message.chat.id, text="Tap the quoted listing to open its Autobuy button.",
                                       reply_parameters=ReplyParameters(row[0]))
                return
        except TelegramError:
            pass
    await message.reply_text("The original Home alert could not be opened.")


async def refresh(bot, chat_id):
    if not str(chat_id).isdigit() or not bot.username:
        return
    with closing(connection()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO parameters VALUES ('home_telegram_bot_username',?)",(bot.username,))
        if conn.execute("SELECT 1 FROM parameters WHERE key='home_buy_buttons_refreshed_v1'").fetchone():
            return
        ids = conn.execute("SELECT telegram_message_id FROM alert_outbox WHERE " + HOME_ALERT
                           + " AND status='sent' AND query_id IS NULL AND found_at>=? ORDER BY found_at DESC",
                           (time.time()-86400,)).fetchall()
    count = 0
    for (mid,) in ids:
        try:
            if await show(bot, chat_id, mid):
                count += 1
            await asyncio.sleep(1.2)
        except BadRequest:
            continue
        except TelegramError as exc:
            logger.warning("Home Autobuy controls deferred: %s", type(exc).__name__)
            return
    with closing(connection()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO parameters VALUES ('home_buy_buttons_refreshed_v1','1')")
    logger.info("Home Autobuy controls updated: existing_alerts=%d dashboard_link=True", count)
