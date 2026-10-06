"""User-tapped purchases with price limits and persistent payment idempotency."""

import asyncio
import json
import re
import time
from contextlib import closing
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

import db
import vinted_budget
import vinted_buyer as buyer
from search_settings import connection


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


def buy(row):
    item_id = str(row["item_id"])
    host = urlsplit(row["url"]).hostname
    if not item_id.isdigit() or host != "www.vinted.co.uk" or row["currency"] != "GBP":
        raise buyer.BuyerError("Autobuy currently supports UK Vinted listings in GBP.")
    with buyer.exclusive():
        config = buyer.settings()
        if not config["enabled"] or not config["connected"]:
            raise buyer.BuyerError(
                "Connect your Vinted buyer and enable Autobuy in Connections first."
            )
        maximum = vinted_budget.payment_limit(row)
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


async def callback(update, context):
    import photo_cards

    query = update.callback_query
    chat_id = str(db.get_parameter("telegram_chat_id") or "").strip()
    # Payments are private-owner only, even if notification routing later moves
    # into a group. A forwarded card cannot buy anything for another user.
    if (
        not query.message
        or str(query.message.chat.id) != chat_id
        or str(query.from_user.id) != chat_id
        or int(chat_id) <= 0
    ):
        await photo_cards.answer(
            query, "Only your private Telegram account can use Autobuy.", alert=True
        )
        return
    saved = photo_cards.recover("vinted", query.message)
    if not saved:
        await photo_cards.answer(
            query, "This saved alert is no longer available.", alert=True
        )
        return
    row, details, _card = saved
    await photo_cards.answer(query, "Preparing your Vinted checkout…")
    try:
        outcome = await asyncio.to_thread(buy, row)
    except buyer.BuyerError as exc:
        await photo_cards.answer(query, str(exc)[:190], alert=True)
        return
    if not outcome:
        return
    markup = photo_cards.markup(row, details).inline_keyboard
    state = outcome["state"]
    label = {
        "paid": "Paid ✓",
        "needs_action": "Finish payment in Vinted ↗",
        "unknown": "Check payment in Vinted ↗",
        "paying": "Check payment in Vinted ↗",
        "preparing": "Purchase is being prepared",
        "payment_failed": "Check failed payment in Vinted ↗",
    }.get(state, "Autobuy stopped · details")
    url = (
        buyer.BASE + "/checkout?purchase_id=" + outcome["checkout_id"]
        if outcome["checkout_id"]
        else row["url"]
    )
    buttons = [list(r) for r in markup]
    buttons[1] = [InlineKeyboardButton(label, url=url)]
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=query.message.message_id,
            reply_markup=InlineKeyboardMarkup(buttons),
            **photo_cards.TIMEOUTS,
        )
        await photo_cards.answer(query, outcome["message"][:190], alert=True)
    except TelegramError:
        pass  # A Telegram failure never triggers another payment attempt.
