"""Owner decisions on existing payments; never start or repeat a purchase."""

import asyncio
import hashlib
import math
import re
from contextlib import closing
from types import SimpleNamespace

from telegram import InlineKeyboardButton

import photo_cards
import vinted_buyer as buyer
from logger import get_logger
from search_settings import connection

logger = get_logger(__name__)
CALLBACK_PATTERN = r"^buy:resolve:(got|miss):([0-9]{1,24}):([0-9a-f]{16})$"


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


def buttons(feedback):
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
                "I got it", callback_data=f"buy:resolve:got:{identity}:{token}"
            ),
            InlineKeyboardButton(
                "I didn't get it", callback_data=f"buy:resolve:miss:{identity}:{token}"
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
