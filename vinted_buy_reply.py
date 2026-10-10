"""Owner-only BUY replies, bound to the original saved Vinted alert."""

import asyncio
import re

from telegram.error import TelegramError

import db
import photo_cards
import vinted_buyer as buyer
import vinted_buying as buying
from logger import get_logger
from vinted_telegram_progress import TelegramProgress

logger = get_logger(__name__)
BUY_REPLY = re.compile(r"\A\s*buy\s*\Z", re.IGNORECASE)


async def send_status(message, text):
    try:
        await message.reply_text(text, do_quote=True, **photo_cards.TIMEOUTS)
    except TelegramError as exc:
        # A missing status notification must never replay a purchase.
        logger.warning("BUY reply status failed: %s", type(exc).__name__)


async def stop_preparation(preparation):
    """Drain the informational reply before displaying the saved result."""
    if not preparation.done():
        preparation.cancel()
    try:
        await preparation
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # noqa: BLE001 -- no UI-based replay
        logger.warning("BUY preparation status failed: error=%s", type(exc).__name__)


async def reply_buy(update, context):
    message = update.message
    if not message or not BUY_REPLY.fullmatch(message.text or ""):
        return
    chat_id = str(db.get_parameter("telegram_chat_id") or "").strip()
    if (
        not chat_id.isdigit()
        or int(chat_id) <= 0
        or message.chat.type != "private"
        or str(message.chat.id) != chat_id
        or not message.from_user
        or message.from_user.is_bot
        or str(message.from_user.id) != chat_id
    ):
        return
    source = message.reply_to_message
    if (
        not source
        or source.chat.id != message.chat.id
        or not source.from_user
        or not source.from_user.is_bot
        or source.from_user.id != context.bot.id
        or source.forward_origin
    ):
        await send_status(
            message,
            "Reply BUY to this bot's original Vinted alert so I can identify the item. "
            "No purchase was started.",
        )
        return

    # Use the replied-to message ID, never a newer alert or a caption's URL.
    async with buying.purchase_lock(source.message_id):
        saved = photo_cards.recover("vinted", source)
        if not saved:
            await send_status(
                message,
                "This saved Vinted alert is no longer available. "
                "Open Recent Finds in your dashboard. No purchase was started.",
            )
            return
        row, _, _ = saved
        photo_cards.control_health("vinted", "click")
        previous = buying.result(row["item_id"])
        progress = None
        if previous and previous["state"] not in ("failed_before_payment", "preparing"):
            outcome = previous
            text = previous["message"] + "\nNo payment was sent again."
        else:
            preparation = None
            try:
                buying.ready(row)
                # This reply is purchase permission; Telegram status delivery
                # must not postpone the existing account and listing checks.
                preparation = asyncio.create_task(
                    send_status(message, "Checking your buyer account…")
                )
                progress = TelegramProgress(context.bot, source, row["item_id"])
                # BUY is the purchase instruction. The existing buyer rechecks
                # live prices and limits, and retains the temporary test gates.
                outcome = await progress.execute(buying.buy, row)
            except buyer.BuyerError as exc:
                outcome = {
                    "state": "setup_required",
                    "message": str(exc),
                    "reason": exc.reason,
                }
            except Exception as exc:  # noqa: BLE001 -- never retry uncertain payment
                logger.warning(
                    "BUY reply failed item=%s error=%s",
                    row["item_id"],
                    type(exc).__name__,
                )
                outcome = buying.result(row["item_id"]) or {
                    "state": "unknown",
                    "message": (
                        "The purchase result could not be confirmed. "
                        "Check your Vinted purchases before trying again."
                    ),
                }
            finally:
                try:
                    if progress:
                        await progress.finish()
                finally:
                    if preparation:
                        # Finish the initial informational reply before the
                        # final result; it must never arrive after "Paid".
                        cleanup = stop_preparation(preparation)
                        if progress:
                            await progress.settle(cleanup)
                        else:
                            await cleanup
            text = (outcome or {}).get(
                "message"
            ) or "Check the status on your Vinted alert."
        if outcome:
            feedback = buying.show_purchase_feedback(context.bot, source, outcome)
            outcome = await progress.settle(feedback) if progress else await feedback
            text = outcome.get("message") or text
            if previous and previous["state"] not in (
                "failed_before_payment",
                "preparing",
            ):
                text += "\nNo payment was sent again."
        notification = send_status(message, text)
        if progress:
            await progress.settle(notification)
        else:
            await notification
        if progress and progress.cancelled:
            raise asyncio.CancelledError
