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
REMINDER_PATTERN = r"^review:purchase:(got|miss):([0-9]{1,24}):([0-9a-f]{16})$"
REMINDER_LABEL = "⚠️ Confirm earlier purchase"


def private_owner(chat_id):
    identity = str(chat_id)
    return identity.isdigit() and int(identity) > 0 and identity == str(
        db.get_parameter("telegram_chat_id") or ""
    ).strip()


def pending_reminder(chat_id):
    """Read only the oldest attempt requiring an owner decision."""
    try:
        with db.connection_scope():
            if not private_owner(chat_id):
                return None
            with closing(connection()) as conn:
                saved = conn.execute(
                    """SELECT item_id,state,updated FROM vinted_buy_attempts
                    WHERE state IN ('unknown','needs_action') ORDER BY updated,item_id LIMIT 1"""
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
    match = re.fullmatch(REMINDER_PATTERN, query.data or "")
    if (
        not match or not message or message.chat.type != "private"
        or query.from_user.id != message.chat.id or not private_owner(message.chat.id)
    ):
        await photo_cards.answer(query, "Only your private Telegram chat can confirm purchases.", alert=True)
        return
    decision, item_id, token = match.groups()
    offer = descriptor(result(item_id))
    if not offer or offer["token"] != token:
        await photo_cards.answer(
            query, "This purchase reminder is out of date. Check Finder or your latest alert.", alert=True
        )
    else:
        try:
            await asyncio.to_thread(resolve_owned, item_id, offer["updated"], decision)
        except buyer.BuyerError as exc:
            await photo_cards.answer(query, str(exc), alert=True)
            return
        await photo_cards.answer(query, "Purchase confirmation saved.")
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


def pending_other(item_id):
    with closing(connection()) as conn:
        saved = conn.execute(
            "SELECT * FROM vinted_buy_attempts WHERE item_id<>? "
            "AND state IN ('paying','unknown','needs_action') "
            "ORDER BY updated,item_id LIMIT 1",
            (str(item_id),),
        ).fetchone()
    return dict(saved) if saved else None


def descriptor(saved):
    if not saved or saved.get("state") not in ("unknown", "needs_action"):
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
    token = hashlib.sha256(
        (identity + ":" + saved["state"] + ":" + version).encode("ascii")
    ).hexdigest()[:16]
    return {"item_id": identity, "updated": version, "token": token}


def with_offer(row, outcome):
    """Bind displayed decisions to an exact saved item and state version."""
    import vinted_buying as buying

    offer = None
    if outcome.get("state") in ("unknown", "needs_action"):
        offer = descriptor(buying.result(str(row["item_id"])))
    elif outcome.get("reason") == "earlier_payment_uncertain":
        offer = descriptor(pending_other(row["item_id"]))
    result = dict(outcome)
    result.pop("owner_resolution", None)
    if offer:
        message = (outcome.get("message") or "Payment remains unconfirmed.").split(
            "\n\nDid you get item "
        )[0]
        if outcome.get("reason") == "earlier_payment_uncertain" and outcome.get(
            "state"
        ) in ("setup_required", "failed_before_payment"):
            # Historical blocked alerts can name a payment already resolved.
            # Show the current exact blocker, rather than two different items.
            message = f"An earlier payment for item {offer['item_id']} needs your confirmation. No payment was started for this alert."
        result["owner_resolution"] = offer
        result["message"] = (
            message
            + f"\n\nDid you get item {offer['item_id']}? Check that exact item in "
            "Vinted Purchases and any pending bank payment, then choose below. "
            "Your choice clears this item's block on new purchases. "
            "This item will never be bought again by these buttons."
        )
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
                "I got it", callback_data=f"{prefix}:got:{identity}:{token}"
            ),
            InlineKeyboardButton(
                "I didn't get it", callback_data=f"{prefix}:miss:{identity}:{token}"
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
