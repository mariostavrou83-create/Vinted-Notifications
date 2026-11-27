"""Autobuy on an exact shared alert, using Finder's existing buying account."""

import asyncio
import re
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import BadRequest, TelegramError

import photo_cards
import resale_telegram
import telegram_shared_alerts as shared
import vinted_buyer as buyer
import vinted_buying as buying
from finder_incidents import queue_buy_failure
from logger import get_logger
from vinted_owner_controls import descriptor
from vinted_progress import STAGE_LABELS
from vinted_telegram_progress import TelegramProgress, active_item, active_status, stop_purchase

logger = get_logger(__name__)
RETRYABLE = {"preparing", "failed_before_payment", "cancelled"}
PROGRESS_HEADING = "<b>Autobuy progress</b>\n"
STOPPING_LABEL = "Stopping Autobuy…"
CONFIRM_PROMPT = (
    "<b>Confirm purchase</b>\nDid you get this item? Choose Yes or No once you know the outcome."
)
DISPLAY_SECTIONS = {
    f"{PROGRESS_HEADING}{escape(label)}" for label in (*STAGE_LABELS.values(), STOPPING_LABEL)
} | {CONFIRM_PROMPT}
UNCONFIRMED = {"unknown", "needs_action", "paying"}
RESULT_TEXT = {
    "paid": "Bought with the connected Vinted buying account.",
    "paying": "Finder is checking the payment result.",
    "needs_action": "The account owner needs to finish payment confirmation in the private bot chat.",
    "unknown": "The payment outcome is unconfirmed. Check the options on this alert.",
    "payment_failed": "This payment failed. The account owner can review the result in Finder.",
    "closed": "This purchase is closed.",
    "cancelled": "Autobuy was stopped before payment.",
}
REASON_TEXT = {
    "earlier_payment_uncertain": "An earlier payment is unconfirmed. Tap its status in this group to answer Yes or No, or use /purchase_status in your private Finder chat.",
    "price_increased": "The listing price rose above the alert price.",
    "shipping_over_limit": "Shipping costs £5 or more.",
    "shipping_unverified": "Finder could not confirm the shipping charge.",
    "item_unavailable": "This item is no longer available.",
    "item_reserved": "This item is reserved.",
    "search_inactive": "The saved search is no longer active.",
    "disabled": "The account owner has turned off Autobuy.",
}


def status_button(bot):
    return InlineKeyboardButton(
        "Open private purchase status",
        url=f"https://t.me/{bot.username}?start=purchase_status",
    )


def insert_status(body, status):
    head, separator, recap = body.rpartition("\n\n")
    if separator and "\nBrand: " in recap and "\nEst. total incl. shipping: " in recap:
        # Match the private alert and keep the price recap immediately above the photos.
        return "\n\n".join((head, status, recap))
    return "\n\n".join((body, status))


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
    edit_lock = photo_cards.lock("shared-buy", key)
    attempt_lock = buying.purchase_lock(key)
    previous = buying.result(item_id)
    has_photo = bool(getattr(message, "photo", None))
    body = getattr(message, "caption_html" if has_photo else "text_html", None) or ""
    body = "\n\n".join(part for part in body.split("\n\n") if part not in DISPLAY_SECTIONS)

    def authorised():
        return shared.current() == group

    async def display(state, stop=None, *, stage=None):
        """Render under the alert's edit lock, preserving its original listing text."""
        if not authorised():
            return
        label = STOPPING_LABEL if state == "stopping" else STAGE_LABELS.get(stage)
        offer = None
        if state in UNCONFIRMED:
            saved = buying.result(item_id)
            if saved:
                state = saved["state"]
            offer = descriptor(saved, from_status=True, shared_group=group)
        section = CONFIRM_PROMPT if offer else ""
        if label:
            section = f"{PROGRESS_HEADING}{escape(label)}"
        current_body = resale_telegram.group_body(item_id, body)
        candidate = insert_status(current_body, section) if current_body and section else ""
        caption_fits = bool(candidate) and photo_cards.units(photo_cards.plain(candidate)) <= (
            1024 if has_photo else 4096
        )

        def controls(show_caption):
            button_label = None if show_caption else label
            rows = list(shared.markup(row, state, stop, progress=button_label).inline_keyboard)
            if offer:
                token = offer["token"]
                rows.append((
                    InlineKeyboardButton("Yes, I got it", callback_data=f"shared:resolve:got:{token}"),
                    InlineKeyboardButton("No, I didn't get it", callback_data=f"shared:resolve:miss:{token}"),
                ))
            if state == "needs_action" or (state == "unknown" and not offer):
                rows.append((status_button(context.bot),))
            return InlineKeyboardMarkup(rows)

        kwargs = dict(chat_id=group.chat, message_id=message.message_id, **photo_cards.TIMEOUTS)
        try:
            edited = False
            revision = None
            if current_body:
                text = candidate if caption_fits else current_body
                keyboard = controls(caption_fits)
                try:
                    if has_photo:
                        revision = await context.bot.edit_message_caption(
                            **kwargs, caption=text, parse_mode="HTML", reply_markup=keyboard,
                            show_caption_above_media=True,
                        )
                    else:
                        revision = await context.bot.edit_message_text(
                            **kwargs, text=text, parse_mode="HTML", reply_markup=keyboard,
                            link_preview_options=LinkPreviewOptions(is_disabled=True),
                        )
                    edited = True
                except BadRequest as exc:
                    edited = "message is not modified" in str(exc).lower()
            if not edited:
                # Long or unavailable captions can still show the stage on the status button.
                revision = await context.bot.edit_message_reply_markup(
                    **kwargs, reply_markup=controls(False),
                )
        except TelegramError as exc:
            logger.info("Telegram shared purchase display deferred: %s", type(exc).__name__)
        else:
            resale_telegram.group_revision(item_id, revision)
            if stage:
                logger.info("Telegram shared Autobuy stage displayed: stage=%s", stage)
            if offer:
                logger.info("Telegram shared purchase choices displayed")

    async def display_result(state):
        async with edit_lock:
            await display(state)

    if query.data.startswith("shared:stop:"):
        outcome = stop_purchase(key, item_id, query.data.rsplit(":", 1)[1])
        await photo_cards.answer(query, outcome["message"], alert=True)
        if outcome["state"] == "stopping":
            async with edit_lock:
                if active_status(key, item_id, buying.result(item_id)):
                    await display("stopping")
        return
    if query.data.startswith("shared:resolve:"):
        if active_item(item_id) or attempt_lock.locked():
            await photo_cards.answer(query, "Finder is still handling this purchase. Check the alert again shortly.", alert=True)
            return
        async with attempt_lock:
            _, _, decision, token = query.data.split(":")
            offer = descriptor(previous, from_status=True, shared_group=group)
            if not offer or token != offer["token"]:
                await photo_cards.answer(query, "The purchase status changed. Check the updated alert.", alert=True)
                await display_result((previous or {}).get("state"))
                return
            try:
                outcome = await asyncio.to_thread(
                    buying.resolve_owned, item_id, offer["updated"], decision,
                    from_purchase_status=True, shared_group=group,
                )
            except buyer.BuyerError as exc:
                await photo_cards.answer(query, str(exc)[:190], alert=True)
                await display_result((buying.result(item_id) or {}).get("state"))
                return
            await photo_cards.answer(query, "Purchase confirmation saved.")
            await display_result(outcome["state"])
            logger.info("Telegram shared purchase confirmation recorded: decision=%s", decision)
        return
    if active_item(item_id) or attempt_lock.locked():
        stage = active_status(key, item_id, previous) if query.data == "shared:status" else None
        await photo_cards.answer(query, stage or "Autobuy is already running for this listing.", alert=True)
        return
    if query.data == "shared:status" or (previous and previous["state"] not in RETRYABLE):
        state = previous["state"] if previous else None
        await photo_cards.answer(query, RESULT_TEXT.get(state, "Check this listing's status in Finder."), alert=True)
        async with edit_lock:
            if not active_item(item_id) and not attempt_lock.locked():
                await display((buying.result(item_id) or {}).get("state"))
        return
    async with attempt_lock:
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
            await progress.settle(display_result(state))
            if state in ("setup_required", "failed_before_payment"):
                text = REASON_TEXT.get(outcome.get("reason"), "Autobuy stopped before payment. The account owner can check Finder for details.")
                if authorised():
                    keyboard = None
                    if outcome.get("reason") == "earlier_payment_uncertain":
                        keyboard = InlineKeyboardMarkup([[status_button(context.bot)]])
                    try:
                        await context.bot.send_message(
                            chat_id=group.chat, text=text, reply_to_message_id=message.message_id,
                            reply_markup=keyboard,
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
