"""Owner decisions on existing payments; never start or repeat a purchase."""

import asyncio
import hashlib
import math
import re
import sqlite3
from contextlib import closing
from types import SimpleNamespace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

import db
import photo_cards
import vinted_buyer as buyer
from logger import get_logger
from search_settings import connection

logger = get_logger(__name__)
CALLBACK_PATTERN = r"^buy:resolve:(got|miss):([0-9]{1,24}):([0-9a-f]{16})$"
REMINDER_PATTERN = r"^(?:review|status):purchase:(got|miss):([0-9]{1,24}):([0-9a-f]{16})$"
REMINDER_LABEL = "⚠️ Confirm earlier purchase"


def private_owner(chat_id):
    identity = str(chat_id)
    return identity.isdigit() and int(identity) > 0 and identity == str(
        db.get_parameter("telegram_chat_id") or ""
    ).strip()


def purchase_access(chat, user, *, from_status):
    if getattr(chat, "type", None) != "private" or not user or chat.id != user.id or getattr(user, "is_bot", False):
        return False, None
    if private_owner(chat.id):
        return True, None
    if from_status:
        from telegram_shared_alerts import private_partner

        group = private_partner(chat, user)
        return group is not None, group
    return False, None


def pending_reminder(chat_id):
    """Read only the oldest attempt requiring an owner decision."""
    try:
        with db.connection_scope():
            if not private_owner(chat_id):
                return None
            with closing(connection()) as conn:
                saved = conn.execute(
                    """SELECT item_id,state,updated FROM vinted_buy_attempts
                    WHERE state IN ('unknown','needs_action') AND item_id NOT IN
                    (SELECT item_id FROM vinted_payment_watch) ORDER BY updated,item_id LIMIT 1"""
                ).fetchone()
                return descriptor(dict(saved)) if saved else None
    except sqlite3.Error as exc:
        logger.warning("Purchase reminder temporarily unavailable: %s", type(exc).__name__)
        return None


def reminder_rows(offer, platform="vinted"):
    if not offer:
        return []
    label = f"{REMINDER_LABEL} #{offer['item_id']}"
    if platform == "ebay":
        # The separate eBay bot does not handle Vinted payment decisions.
        from vinted_connection_notices import dashboard_url

        url = dashboard_url()
        return [[InlineKeyboardButton(label + " ↗", url=url)]] if url else []
    url = f"{buyer.BASE}/items/{offer['item_id']}"
    return [[InlineKeyboardButton(label + " ↗", url=url)]] + buttons(
        {"owner_resolution": offer}, prefix="review:purchase"
    )


def reminder_markup(chat_id, platform="vinted"):
    rows = reminder_rows(pending_reminder(chat_id), platform)
    return {"reply_markup": InlineKeyboardMarkup(rows)} if rows else {}


def current_reminder(offer, conn):
    saved = conn.execute(
        "SELECT item_id,state,updated FROM vinted_buy_attempts WHERE item_id=?",
        (offer["item_id"],),
    ).fetchone()
    return saved is not None and descriptor(dict(saved)) == offer


async def confirm_reminder(update, context):
    """Confirm only the earlier attempt, leaving the new alert actionable."""
    from vinted_buying import resolve_owned, result

    query = update.callback_query
    message = query.message
    data = query.data or ""
    match = re.fullmatch(REMINDER_PATTERN, data)
    from_status = data.startswith("status:purchase:")
    allowed, group = purchase_access(
        message.chat if message else None, query.from_user, from_status=from_status
    )
    if not match or not allowed:
        await photo_cards.answer(query, "Only your private Telegram chat can confirm purchases.", alert=True)
        return
    decision, item_id, token = match.groups()
    offer = descriptor(result(item_id), from_status=from_status, shared_group=group)
    if not offer or offer["token"] != token:
        await photo_cards.answer(
            query, "This purchase reminder is out of date. Open /purchase_status for the latest result.", alert=True
        )
        if from_status:
            return
    else:
        try:
            await asyncio.to_thread(
                resolve_owned, item_id, offer["updated"], decision,
                from_purchase_status=from_status, shared_group=group,
            )
        except buyer.BuyerError as exc:
            text = str(exc)
            if from_status:
                text = text[:140] + " Open /purchase_status to try again."
            await photo_cards.answer(query, text, alert=True)
            return
        await photo_cards.answer(query, "Purchase confirmation saved.")
        if from_status:
            answer = "you got" if decision == "got" else "you didn't get"
            try:
                await query.edit_message_text(f"You confirmed {answer} item {item_id}.")
            except TelegramError as exc:
                logger.info("Purchase status edit unavailable: %s", type(exc).__name__)
            return
    markup = getattr(message, "reply_markup", None)
    if not markup:
        return
    async with photo_cards.lock("vinted", message.message_id):
        rows = [
            row for row in markup.inline_keyboard
            if not any(button.text.startswith(REMINDER_LABEL) or
                       (button.callback_data or "").startswith("review:purchase:") for button in row)
        ]
        if len(rows) == len(markup.inline_keyboard):
            return
        saved = photo_cards.load("vinted", message.message_id)
        if saved:
            row, details, card = saved
            view = card["view"]
            note_page = int(view.split(":")[1]) if view.startswith("notes:") else None
            updated_markup = photo_cards.markup(row, details, view=view, note_page=note_page)
        else:
            updated_markup = InlineKeyboardMarkup(rows) if rows else None
        try:
            await query.edit_message_reply_markup(reply_markup=updated_markup)
        except TelegramError as exc:
            logger.info("Purchase reminder edit unavailable: %s", type(exc).__name__)


def pending_attempt(item_id=None, *, exclude=False):
    """Read an exact pending payment or the first one blocking a new purchase."""
    where, args = "", ()
    if item_id is not None:
        where = " AND a.item_id<>?" if exclude else " AND a.item_id=?"
        args = (str(item_id),)
    with closing(connection()) as conn:
        saved = conn.execute(
            "SELECT a.item_id,a.state,a.updated,w.item_id IS NOT NULL AS watching, "
            "COALESCE(w.title,(SELECT o.title FROM alert_outbox o WHERE o.platform='vinted' "
            "AND o.item_id=a.item_id ORDER BY o.found_at DESC LIMIT 1)) AS title FROM vinted_buy_attempts a "
            "LEFT JOIN vinted_payment_watch w ON w.item_id=a.item_id AND w.checkout_id=a.checkout_id "
            "WHERE a.state IN ('paying','unknown','needs_action')" + where +
            " ORDER BY a.updated,a.item_id LIMIT 1", args,
        ).fetchone()
    return dict(saved) if saved else None


def pending_other(item_id):
    return pending_attempt(item_id, exclude=True)


def purchase_prompt(saved, *, from_status=False, shared_group=None):
    """Pair the question and controls from the same durable attempt."""
    if not saved:
        return "There are no purchases awaiting confirmation.", None
    item = str(saved["item_id"])
    label = f"item {item}" if re.fullmatch(r"[0-9]{1,24}", item) else "the earlier item"
    title = " ".join(str(saved.get("title") or "").split())[:90]
    heading = title + "\n" if title else ""
    if saved.get("watching") and not from_status:
        return (
            heading + f"Finder is checking the payment for {label} automatically for up to an hour. "
            "If your bank requests approval, open the payment check on the original alert. "
            "Open /purchase_status to record your answer now.", None
        )
    offer = descriptor(saved, from_status=from_status, shared_group=shared_group)
    if offer:
        if from_status:
            return (
                heading + f"Did you get item {item}? Check your Vinted purchases and choose below. "
                "If payment is still pending, leave this unanswered.", offer
            )
        return (
            heading + f"Did you get item {item}? Check that exact item in Vinted Purchases "
            "and any pending bank payment, then choose below.", offer
        )
    return heading + f"The payment for {label} is still unconfirmed. Check its original payment status in Finder. Use /purchase_status for an update.", None


async def purchase_status_command(update, context):
    """Recover the current decision even if the original alert is missing."""
    chat, user = update.effective_chat, update.effective_user
    allowed, group = purchase_access(chat, user, from_status=True)
    if not allowed:
        return
    saved = await asyncio.to_thread(pending_attempt)
    if group:
        from telegram_shared_alerts import private_partner

        if private_partner(chat, user) != group:
            return
    text, offer = purchase_prompt(saved, from_status=True, shared_group=group)
    rows = buttons({"owner_resolution": offer}, prefix="status:purchase")
    if rows:
        rows.append([InlineKeyboardButton("View item on Vinted", url=f"{buyer.BASE}/items/{offer['item_id']}")])
    await update.effective_message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(rows) if rows else None, **photo_cards.TIMEOUTS
    )


def descriptor(saved, *, from_status=False, shared_group=None):
    eligible = ("unknown", "needs_action", "paying") if from_status else ("unknown", "needs_action")
    if not saved or saved.get("state") not in eligible:
        return None
    identity = saved.get("item_id")
    updated = saved.get("updated")
    if (
        not isinstance(identity, str)
        or not re.fullmatch(r"[0-9]{1,24}", identity)
        or type(updated) not in (float, int)
        or not math.isfinite(updated)
        or updated <= 0
    ):
        return None
    version = str(float(updated))
    binding = f"{identity}:{saved['state']}:{version}"
    if shared_group is not None:
        binding += f":{shared_group.version}"
    token = hashlib.sha256(binding.encode("ascii")).hexdigest()[:16]
    return {"item_id": identity, "updated": version, "token": token}


def with_offer(row, outcome):
    """Bind displayed decisions to an exact saved item and state version."""
    result = dict(outcome)
    result.pop("owner_resolution", None)
    blocked = outcome.get("reason") == "earlier_payment_uncertain"
    if blocked:
        saved = pending_other(row["item_id"])
    elif outcome.get("state") in ("unknown", "needs_action"):
        saved = pending_attempt(row["item_id"])
    else:
        return result
    prompt, offer = purchase_prompt(saved)
    if offer:
        result["owner_resolution"] = offer
    if blocked and outcome.get("state") in ("setup_required", "failed_before_payment"):
        result["message"] = "No payment was started for this alert. " + prompt
    elif offer:
        message = outcome.get("message") or "Payment remains unconfirmed."
        if outcome.get("owner_resolution"):
            message = message.split("\n\n", 1)[0]
        result["message"] = message + "\n\n" + prompt
    return result


def buttons(feedback, *, prefix="buy:resolve"):
    offer = feedback.get("owner_resolution")
    if not isinstance(offer, dict):
        return []
    identity, token = offer.get("item_id"), offer.get("token")
    if (
        not isinstance(identity, str)
        or not re.fullmatch(r"[0-9]{1,24}", identity)
        or not isinstance(token, str)
        or not re.fullmatch(r"[0-9a-f]{16}", token)
    ):
        return []
    return [
        [
            InlineKeyboardButton(
                "Yes, I got it" if prefix == "status:purchase" else "I got it",
                callback_data=f"{prefix}:got:{identity}:{token}"
            ),
            InlineKeyboardButton(
                "No, I didn't get it" if prefix == "status:purchase" else "I didn't get it",
                callback_data=f"{prefix}:miss:{identity}:{token}"
            ),
        ]
    ]


async def handle(query, context, saved):
    """Caller already verified the configured private owner and saved alert."""
    import vinted_buying as buying

    match = re.fullmatch(CALLBACK_PATTERN, query.data or "")
    if not match or not saved:
        await photo_cards.answer(
            query, "This purchase decision is no longer available.", alert=True
        )
        return
    decision, identity, token = match.groups()
    row, details, _ = saved
    offer = (details.get("buy_feedback") or {}).get("owner_resolution")
    if (
        not isinstance(offer, dict)
        or offer.get("item_id") != identity
        or offer.get("token") != token
    ):
        await photo_cards.answer(
            query,
            "This purchase result changed. Tap its status to refresh it.",
            alert=True,
        )
        return
    await photo_cards.answer(query, "Recording your purchase confirmation…")
    async with buying.purchase_lock(query.message.message_id):
        try:
            resolved = await asyncio.to_thread(
                buying.resolve_owned,
                identity,
                offer.get("updated"),
                decision,
                close_item_id=(
                    str(row["item_id"]) if identity != str(row["item_id"]) else None
                ),
            )
        except buyer.BuyerError as exc:
            # Preserve all saved payment state, including a stale or live result.
            logger.info(
                "Autobuy owner decision rejected item=%s reason=%s",
                identity,
                exc.reason,
            )
            current = buying.result(str(row["item_id"]))
            feedback = (
                current
                if current
                and current.get("state") not in ("failed_before_payment", "preparing")
                else {
                    "state": "setup_required",
                    "reason": "earlier_payment_uncertain",
                    "message": str(exc),
                }
            )
            await buying.show_purchase_feedback(context.bot, query.message, feedback)
            return
        except Exception as exc:  # noqa: BLE001 -- never replay any payment
            logger.warning(
                "Autobuy owner decision failed item=%s error=%s",
                identity,
                type(exc).__name__,
            )
            return
        # Resolving an earlier payment must not re-arm this blocked alert. The
        # owner may already have purchased its item manually; only a future tap
        # on a different alert can authorise another purchase.
        source = buying.result(str(row["item_id"]))
        if identity == str(row["item_id"]):
            feedback = resolved
        elif source and source.get("state") not in (
            "closed",
            "failed_before_payment",
            "preparing",
        ):
            # A different callback may have submitted this alert's payment
            # before our buyer lock was acquired. Never hide that durable state.
            feedback = dict(
                source, message=resolved["message"] + " " + source["message"]
            )
        else:
            feedback = {
                "state": "owner_resolution_recorded",
                "message": resolved["message"]
                + " This blocked alert is closed; use Autobuy on new items.",
            }
        pending = (
            source
            if source and source.get("state") in ("paying", "unknown", "needs_action")
            else pending_other(row["item_id"])
        )
        if pending:
            pending_id = str(pending.get("item_id", ""))
            pending_label = (
                "item " + pending_id
                if re.fullmatch(r"[0-9]{1,24}", pending_id)
                else "another saved item"
            )
            feedback = dict(
                feedback,
                reason="earlier_payment_uncertain",
                message=feedback["message"]
                + f" Another payment for {pending_label} remains unconfirmed.",
            )
        else:
            feedback = dict(
                feedback,
                message=feedback["message"] + " You can use Autobuy on new items now.",
            )
        await buying.show_purchase_feedback(context.bot, query.message, feedback)


async def refresh_existing(bot, chat_id):
    """Add the requested controls to existing results, without new messages."""
    try:
        await _refresh_existing(bot, chat_id)
    except Exception as exc:  # noqa: BLE001 -- independent of normal alert delivery
        logger.warning(
            "Autobuy owner controls refresh failed error=%s", type(exc).__name__
        )


async def _refresh_existing(bot, chat_id):
    import vinted_buying as buying

    chat_id = str(chat_id or "").strip()
    if not chat_id.isdigit() or int(chat_id) <= 0:
        return
    with closing(connection()) as conn:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT c.message_id FROM telegram_photo_cards c "
                "LEFT JOIN vinted_buy_attempts a ON a.item_id=c.item_id "
                "WHERE c.platform='vinted' AND (a.state IN ('unknown','needs_action') "
                "OR INSTR(c.details,'earlier_payment_uncertain')>0) "
                "ORDER BY c.message_id DESC LIMIT 20"
            )
        ]
    refreshed = 0
    for message_id in ids:
        saved = photo_cards.load("vinted", message_id)
        if not saved:
            continue
        row, details, _ = saved
        current = buying.result(str(row["item_id"]))
        cached = details.get("buy_feedback") or {}
        outcome = (
            cached
            if cached.get("reason") == "earlier_payment_uncertain"
            and (
                not current
                or current.get("state")
                in ("failed_before_payment", "preparing", "closed")
            )
            else current or cached
        )
        if (
            not descriptor(buying.result(str(row["item_id"])))
            and outcome.get("reason") != "earlier_payment_uncertain"
        ):
            continue
        if not with_offer(row, outcome).get("owner_resolution"):
            continue
        message = SimpleNamespace(
            message_id=message_id, chat=SimpleNamespace(id=chat_id)
        )
        await buying.show_purchase_feedback(bot, message, outcome)
        logger.info(
            "Autobuy owner controls refresh attempted alert_item=%s message_id=%s",
            row["item_id"],
            message_id,
        )
        refreshed += 1
        if refreshed >= 10:
            break
