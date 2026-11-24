"""Autobuy on an exact shared alert, using Finder's existing buying account."""

import asyncio
import re

from telegram.error import TelegramError

import photo_cards
import telegram_shared_alerts as shared
import vinted_buyer as buyer
import vinted_buying as buying
from finder_incidents import queue_buy_failure
from logger import get_logger
from vinted_telegram_progress import TelegramProgress, active_item, stop_purchase

logger = get_logger(__name__)
RETRYABLE = {"preparing", "failed_before_payment", "cancelled"}
RESULT_TEXT = {
    "paid": "Bought with the connected Vinted buying account.",
    "paying": "Finder is checking the payment result.",
    "needs_action": "The account owner needs to finish payment confirmation in the private bot chat.",
    "unknown": "The payment outcome is unconfirmed. The account owner can check it in Finder.",
    "payment_failed": "This payment failed. The account owner can review the result in Finder.",
    "closed": "This purchase is closed.",
    "cancelled": "Autobuy was stopped before payment.",
}
REASON_TEXT = {
    "earlier_payment_uncertain": "The account owner needs to confirm an earlier purchase first.",
    "price_increased": "The listing price rose above the alert price.",
    "shipping_over_limit": "Shipping costs £5 or more.",
    "shipping_unverified": "Finder could not confirm the shipping charge.",
    "item_unavailable": "This item is no longer available.",
    "item_reserved": "This item is reserved.",
    "search_inactive": "The saved search is no longer active.",
    "disabled": "The account owner has turned off Autobuy.",
}


async def callback(update, context):
    query, group = update.callback_query, shared.current()
    message, sender = query.message, query.from_user
    if (
        not message or not shared.member(group, message.chat, sender)
        or not re.fullmatch(shared.BUY_PATTERN, query.data or "")
    ):
        return
    row = shared.lookup(group, message.message_id)
    if not row:
        await photo_cards.answer(query, "This shared alert is no longer available.", alert=True)
        return
    item_id = row["item_id"]
    key = (group.chat, message.message_id)
    previous = buying.result(item_id)

    def authorised():
        return shared.current() == group

    async def display(state, stop=None):
        async with photo_cards.lock("shared-buy", key):
            if not authorised():
                return
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=group.chat, message_id=message.message_id,
                    reply_markup=shared.markup(row, state, stop), **photo_cards.TIMEOUTS,
                )
            except TelegramError as exc:
                logger.info("Telegram shared purchase display deferred: %s", type(exc).__name__)

    if query.data.startswith("shared:stop:"):
        outcome = stop_purchase(key, item_id, query.data.rsplit(":", 1)[1])
        await photo_cards.answer(query, outcome["message"], alert=True)
        if outcome["state"] == "stopping" and active_item(item_id):
            await display("stopping")
        return
    if active_item(item_id):
        await photo_cards.answer(query, "Autobuy is already running for this listing.", alert=True)
        return
    if query.data == "shared:status" or (previous and previous["state"] not in RETRYABLE):
        state = previous["state"] if previous else None
        await photo_cards.answer(query, RESULT_TEXT.get(state, "Check this listing's status in Finder."), alert=True)
        await display(state)
        return
    role = "owner" if sender.id == group.owner else "partner"
    logger.info("Telegram shared Autobuy tap: role=%s", role)
    progress = TelegramProgress(context.bot, message, item_id, display=display, authorise=authorised)
    acknowledgement = asyncio.create_task(photo_cards.answer(query, "Checking this listing for Autobuy…"))
    try:
        try:
            outcome = await progress.execute(buying.buy, row)
        except buyer.BuyerError as exc:
            outcome = {"state": "setup_required", "reason": exc.reason, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 -- consult durable state; never replay payment
            logger.warning("Telegram shared Autobuy stopped: %s", type(exc).__name__)
            outcome = buying.result(item_id) or {"state": "unknown", "message": "Check the payment result in Finder."}
        finally:
            await progress.finish()
        state = outcome["state"]
        await progress.settle(queue_buy_failure(row, outcome, query.id))
        await progress.settle(display(state))
        if state in ("setup_required", "failed_before_payment"):
            text = REASON_TEXT.get(outcome.get("reason"), "Autobuy stopped before payment. The account owner can check Finder for details.")
            if authorised():
                try:
                    await context.bot.send_message(
                        chat_id=group.chat, text=text, reply_to_message_id=message.message_id,
                        disable_notification=True, **photo_cards.TIMEOUTS,
                    )
                except TelegramError as exc:
                    logger.info("Telegram shared purchase note deferred: %s", type(exc).__name__)
        logger.info("Telegram shared Autobuy outcome: state=%s", state)
    finally:
        progress.close()
        await acknowledgement
    if progress.cancelled:
        raise asyncio.CancelledError
