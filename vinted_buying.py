"""User-tapped purchases with price limits and persistent payment idempotency."""

import asyncio
import json
import os
import re
import time
from contextlib import closing
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from telegram import InlineKeyboardButton
from telegram.error import TelegramError

import db
import vinted_budget
import vinted_buyer as buyer
from logger import get_logger
from search_settings import connection

logger = get_logger(__name__)


def cents(value):
    if (
        not isinstance(value, dict)
        or value.get("currency_code", value.get("currency")) != "GBP"
    ):
        raise buyer.BuyerError("Autobuy stopped: Vinted did not confirm a GBP price.")
    try:
        amount = Decimal(str(value.get("amount", value.get("value"))))
        if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
            raise ValueError
        return int(amount * 100)
    except (InvalidOperation, ValueError, TypeError):
        raise buyer.BuyerError(
            "Autobuy stopped: the checkout price could not be verified."
        ) from None


def record(item_id, state, message, *, checkout_id=None, total=None):
    with closing(connection()) as conn, conn:
        conn.execute(
            "UPDATE vinted_buy_attempts SET state=?,message=?,checkout_id=COALESCE(?,checkout_id),total=COALESCE(?,total),updated=? WHERE item_id=?",
            (state, message, checkout_id, total, time.time(), item_id),
        )


def result(item_id):
    with closing(connection()) as conn:
        row = conn.execute(
            "SELECT * FROM vinted_buy_attempts WHERE item_id=?", (item_id,)
        ).fetchone()
    return dict(row) if row else None


def history():
    with closing(connection()) as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT a.*,o.title,o.url FROM vinted_buy_attempts a LEFT JOIN alert_outbox o ON o.item_id=a.item_id ORDER BY a.updated DESC LIMIT 20"
            )
        ]


def claim(row):
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        saved = conn.execute(
            "SELECT * FROM vinted_buy_attempts WHERE item_id=?", (row["item_id"],)
        ).fetchone()
        if saved and saved["state"] != "failed_before_payment":
            return False
        conn.execute(
            "INSERT INTO vinted_buy_attempts(item_id,state,message,updated) VALUES (?,'preparing','Preparing checkout',?) ON CONFLICT(item_id) DO UPDATE SET state='preparing',message='Preparing checkout',checkout_id=NULL,total=NULL,updated=excluded.updated",
            (row["item_id"], time.time()),
        )
    return True


def checkout_prices(checkout, item_price, maximum):
    components = checkout.get("components") or {}
    summary = (
        components.get("order_summary_v2") or components.get("order_summary") or {}
    )
    # Only an explicit checkout total is accepted. A subtotal or listing price
    # can omit delivery and buyer protection, so neither is a payment fallback.
    total_part = summary.get("total") or {}
    total = cents(total_part.get("price") if isinstance(total_part, dict) else None)
    if total < item_price:
        raise buyer.BuyerError(
            "Autobuy stopped: Vinted's checkout total could not be verified."
        )
    if total > maximum:
        raise buyer.BuyerError(
            f"Autobuy stopped: checkout is £{total/100:.2f}; this search's maximum total is £{maximum/100:.2f}."
        )
    if checkout.get("errors") or not checkout.get("checksum"):
        raise buyer.BuyerError(
            "Vinted needs checkout details before this item can be paid for."
        )
    for key in ("shipping_address", "payment_method"):
        if not components.get(key) or components[key].get("errors"):
            raise buyer.BuyerError(
                "Set your delivery address and payment method in Vinted first."
            )
    shipping = components.get("shipping_pickup_details") or {}
    options = components.get("shipping_pickup_options") or {}
    # Use the account's existing delivery choice only. Never invent a pickup
    # point, pick a different address, or purchase an optional add-on.
    if not shipping or not options.get("selected_pickup_option"):
        raise buyer.BuyerError("Choose and save your delivery option in Vinted first.")
    return total


def ready(row):
    config = buyer.settings()
    if not config["connected"]:
        raise buyer.BuyerError("Connect your Vinted buyer in Connections first.")
    if not config["enabled"]:
        raise buyer.BuyerError(
            "Autobuy is off. Set a maximum total on this search, then enable Autobuy in Connections. No purchase was started."
        )
    return config, vinted_budget.payment_limit(row)


def buy(row):
    item_id = str(row["item_id"])
    host = urlsplit(row["url"]).hostname
    if not item_id.isdigit() or host != "www.vinted.co.uk" or row["currency"] != "GBP":
        raise buyer.BuyerError("Autobuy currently supports UK Vinted listings in GBP.")
    with buyer.exclusive():
        config, maximum = ready(row)
        if not claim(row):
            return result(item_id)
        client = None
        payment_started = False
        try:
            client = buyer.connected_client()
            data = client.request("GET", f"/api/v2/items/{item_id}")
            item = data.get("item") or {}
            if (
                str(item.get("id")) != item_id
                or item.get("is_closed")
                or item.get("is_sold")
                or item.get("is_reserved")
            ):
                raise buyer.BuyerError("This Vinted item is unavailable or reserved.")
            price = item.get("price")
            if not isinstance(price, dict):
                price = {"amount": price, "currency_code": item.get("currency")}
            current_price = cents(price)
            alert_price = cents(
                {"amount": row["price"], "currency_code": row["currency"]}
            )
            if current_price > alert_price:
                raise buyer.BuyerError(
                    "Autobuy stopped: the item price increased after your alert."
                )
            if current_price > maximum:
                raise buyer.BuyerError(
                    "Autobuy stopped: the item alone exceeds this search's maximum total."
                )
            seller = str(
                (item.get("user") or {}).get("id") or item.get("user_id") or ""
            )
            if not seller.isdigit() or seller == config["user_id"]:
                raise buyer.BuyerError(
                    "The seller could not be verified for this purchase."
                )
            conversation = client.request(
                "POST",
                "/api/v2/conversations",
                {"initiator": "buy", "item_id": item_id, "opposite_user_id": seller},
            )
            transaction = (conversation.get("conversation") or {}).get(
                "transaction"
            ) or {}
            transaction_id = transaction.get("id")
            if not str(transaction_id).isdigit():
                raise buyer.BuyerError(
                    "Vinted did not prepare a purchase for this item."
                )
            built = client.request(
                "POST",
                "/api/v2/purchases/checkout/build",
                {"purchase_items": [{"id": transaction_id, "type": "transaction"}]},
            )
            checkout = built.get("checkout") or {}
            purchase_id = str(checkout.get("id") or "")
            if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", purchase_id):
                raise buyer.BuyerError("Vinted did not return a valid checkout.")
            record(
                item_id,
                "preparing",
                "Checking the final price and your saved delivery choice",
                checkout_id=purchase_id,
            )
            updated = client.request(
                "PUT",
                f"/api/v2/purchases/{purchase_id}/checkout",
                {
                    "components": {
                        "additional_service": {},
                        "payment_method": {},
                        "shipping_address": {},
                        "shipping_pickup_options": {},
                        "shipping_pickup_details": {},
                    }
                },
            )
            checkout = updated.get("checkout") or {}
            if str(checkout.get("id")) != purchase_id:
                raise buyer.BuyerError(
                    "Vinted returned a different checkout. No payment was sent."
                )
            # Read the current controls again immediately before payment.
            config = buyer.settings()
            if not config["enabled"] or not config["connected"]:
                raise buyer.BuyerError("Autobuy was disabled before payment.")
            maximum = vinted_budget.payment_limit(row)
            total = checkout_prices(checkout, current_price, maximum)
            with closing(connection()) as conn:
                info = json.loads(
                    conn.execute(
                        "SELECT browser_info FROM vinted_buyer WHERE id=1"
                    ).fetchone()[0]
                )
            if not info:
                raise buyer.BuyerError(
                    "Save the buyer settings from your browser before using Autobuy."
                )
            record(
                item_id,
                "paying",
                "Payment submitted; awaiting Vinted confirmation",
                total=total,
            )
            payment_started = True
            paid = client.request(
                "POST",
                f"/api/v2/purchases/{purchase_id}/checkout/payment",
                {
                    "checksum": checkout["checksum"],
                    "payment_options": {"browser_info": info},
                },
            )
            status = (paid.get("payment") or {}).get("status")
            if status in ("success", "completed"):
                record(
                    item_id,
                    "paid",
                    f"Paid £{total/100:.2f}. Check your Vinted purchases.",
                )
            elif status in ("pending", "requires_action"):
                record(
                    item_id,
                    "needs_action",
                    "Vinted needs payment or bank confirmation. Open your Vinted checkout; do not buy again.",
                )
            elif status == "failed":
                record(
                    item_id,
                    "payment_failed",
                    "Vinted reported a failed payment. Check the purchase in Vinted before trying again there.",
                )
            else:
                record(
                    item_id,
                    "unknown",
                    "Payment result is unconfirmed. Check your Vinted purchases before doing anything else.",
                )
        except buyer.BuyerError as exc:
            record(
                item_id,
                "unknown" if payment_started else "failed_before_payment",
                (
                    "Payment result is unconfirmed. Check Vinted before retrying."
                    if payment_started
                    else str(exc)
                ),
            )
        except Exception:  # noqa: BLE001 -- unknown payment outcomes must never retry
            record(
                item_id,
                "unknown" if payment_started else "failed_before_payment",
                (
                    "Payment result is unconfirmed. Check Vinted before retrying."
                    if payment_started
                    else "Vinted returned an unsupported checkout. No payment was sent."
                ),
            )
        finally:
            if client:
                client.session.close()
        return result(item_id)


def feedback_buttons(row, feedback=None):
    if not feedback:
        return [[InlineKeyboardButton("Autobuy", callback_data="buy:click")]]
    state = feedback.get("state")
    label = {
        "setup_required": "Autobuy needs setup · see why",
        "failed_before_payment": "Autobuy stopped · see why",
        "paid": "Paid ✓ · details",
        "needs_action": "Bank / payment confirmation needed",
        "unknown": "Payment unconfirmed · check details",
        "paying": "Payment submitted · check details",
        "preparing": "Preparing purchase · details",
        "payment_failed": "Payment failed · check details",
    }.get(state, "Autobuy status · details")
    buttons = [[InlineKeyboardButton(label, callback_data="buy:status")]]
    if state in ("setup_required", "failed_before_payment"):
        buttons.append(
            [InlineKeyboardButton("Retry Autobuy", callback_data="buy:click")]
        )
        base = os.environ.get("DASHBOARD_URL", "").rstrip("/")
        if not base and os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
            base = "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"]
        parsed = urlsplit(base)
        if parsed.scheme == "https" and parsed.hostname and not parsed.username:
            setup = []
            if str(row.get("query_id", "")).isdigit():
                setup.append(
                    InlineKeyboardButton(
                        "Set search budget ↗",
                        url=base + "/search/" + str(row["query_id"]),
                    )
                )
            setup.append(
                InlineKeyboardButton(
                    "Buyer settings ↗", url=base + "/connections#vinted-buying"
                )
            )
            buttons.append(setup)
    elif feedback.get("checkout_id"):
        buttons.append(
            [
                InlineKeyboardButton(
                    "Open Vinted checkout ↗",
                    url=buyer.BASE + "/checkout?purchase_id=" + feedback["checkout_id"],
                )
            ]
        )
    return buttons


async def show_feedback(bot, query, row, details, card, outcome):
    """Keep the result on this same alert; callback popups are only transient."""
    import photo_cards

    feedback = {
        key: outcome.get(key) for key in ("state", "message", "checkout_id", "total")
    }
    details = dict(details, buy_feedback=feedback)
    try:
        with closing(connection()) as conn, conn:
            conn.execute(
                "UPDATE telegram_photo_cards SET details=? WHERE platform='vinted' AND message_id=? AND item_id=?",
                (json.dumps(details), query.message.message_id, row["item_id"]),
            )
        # Picture and notes controls re-use these details, preserving buy status.
        view = card.get("view", "listing")
        note_page = int(view.split(":")[1]) if view.startswith("notes:") else None
        await bot.edit_message_reply_markup(
            chat_id=str(query.message.chat.id),
            message_id=query.message.message_id,
            reply_markup=photo_cards.markup(
                row, details, view=view, note_page=note_page
            ),
            **photo_cards.TIMEOUTS,
        )
        photo_cards.control_health("vinted", "success")
    except TelegramError as exc:
        photo_cards.control_health(
            "vinted", "error", "Could not display Autobuy status"
        )
        logger.warning(
            "Autobuy result display failed item=%s error=%s",
            row["item_id"],
            type(exc).__name__,
        )
        # Never repeat a payment because its Telegram display failed.
    logger.info(
        "Autobuy status item=%s search=%s state=%s",
        row["item_id"],
        row.get("query_id"),
        feedback["state"],
    )


async def callback(update, context):
    import photo_cards

    query = update.callback_query
    chat_id = str(db.get_parameter("telegram_chat_id") or "").strip()
    if (
        not query.message
        or str(query.message.chat.id) != chat_id
        or str(query.from_user.id) != chat_id
        or not chat_id.isdigit()
        or int(chat_id) <= 0
    ):
        await photo_cards.answer(
            query, "Only your private Telegram account can use Autobuy.", alert=True
        )
        return
    photo_cards.control_health("vinted", "click")
    saved = photo_cards.recover("vinted", query.message)
    if not saved:
        await photo_cards.answer(
            query,
            "This saved alert is no longer available. Open Recent Finds in your dashboard.",
            alert=True,
        )
        return
    row, details, card = saved
    previous = result(row["item_id"])
    if getattr(query, "data", "") == "buy:status":
        feedback = (
            previous
            if previous and previous["state"] != "failed_before_payment"
            else details.get("buy_feedback") or previous
        )
        message = (
            feedback.get("message")
            if feedback
            else "Tap Autobuy to check this listing."
        )
        await photo_cards.answer(
            query, (message or "Check your Vinted purchases.")[:190], alert=True
        )
        return
    logger.info("Autobuy tap item=%s search=%s", row["item_id"], row.get("query_id"))
    if previous and previous["state"] != "failed_before_payment":
        await photo_cards.answer(query, previous["message"][:190], alert=True)
        await show_feedback(context.bot, query, row, details, card, previous)
        return
    # Check local setup BEFORE answering the callback. Telegram must receive the
    # reason as its first answer, not a second popup after an acknowledgement.
    try:
        ready(row)
    except buyer.BuyerError as exc:
        await photo_cards.answer(query, str(exc)[:190], alert=True)
        await show_feedback(
            context.bot,
            query,
            row,
            details,
            card,
            {"state": "setup_required", "message": str(exc)},
        )
        return
    await photo_cards.answer(query, "Preparing your Vinted checkout…")
    async with photo_cards.lock("vinted", query.message.message_id):
        try:
            outcome = await asyncio.to_thread(buy, row)
        except buyer.BuyerError as exc:
            outcome = {"state": "setup_required", "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 -- never retry an uncertain purchase
            logger.warning(
                "Autobuy request failed item=%s error=%s",
                row["item_id"],
                type(exc).__name__,
            )
            outcome = result(row["item_id"]) or {
                "state": "unknown",
                "message": "The purchase result could not be confirmed. Check your Vinted purchases before trying again.",
            }
        if outcome:
            await show_feedback(context.bot, query, row, details, card, outcome)
