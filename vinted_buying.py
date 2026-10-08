"""User-tapped purchases with price limits and persistent payment idempotency."""

import asyncio
import json
import math
import os
import re
import time
from contextlib import closing
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlsplit

from telegram import InlineKeyboardButton, LinkPreviewOptions
from telegram.error import BadRequest, TelegramError

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
        if (
            not amount.is_finite()
            or not 0 <= amount <= 1000000
            or amount.as_tuple().exponent < -2
        ):
            raise ValueError
        return int(amount * 100)
    except (InvalidOperation, ValueError, TypeError):
        raise buyer.BuyerError(
            "Autobuy stopped: the checkout price could not be verified."
        ) from None


def record(item_id, state, message, *, checkout_id=None, total=None, action_url=None):
    with closing(connection()) as conn, conn:
        conn.execute(
            "UPDATE vinted_buy_attempts SET state=?,message=?,checkout_id=COALESCE(?,checkout_id),total=COALESCE(?,total),action_url=?,updated=? WHERE item_id=?",
            (state, message, checkout_id, total, action_url, time.time(), item_id),
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


def claim(row, *, recover_preparing=False):
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        saved = conn.execute(
            "SELECT * FROM vinted_buy_attempts WHERE item_id=?", (row["item_id"],)
        ).fetchone()
        retryable = {"failed_before_payment"}
        if recover_preparing:
            # Only the caller holding buyer.exclusive() may recover this state:
            # no live checkout can hold the same process-wide lock. Payment is
            # never sent until the distinct durable 'paying' marker is saved.
            retryable.add("preparing")
        if saved and saved["state"] not in retryable:
            return False
        conn.execute(
            "INSERT INTO vinted_buy_attempts(item_id,state,message,updated) VALUES (?,'preparing','Preparing checkout',?) ON CONFLICT(item_id) DO UPDATE SET state='preparing',message='Preparing checkout',checkout_id=NULL,total=NULL,action_url=NULL,updated=excluded.updated",
            (row["item_id"], time.time()),
        )
    return True


def checkout_item_details(components, summary, *, item_id=None):
    """Read the current frontend's single order item, never an aggregate subtotal."""
    button = components.get("pay_button_v2")
    embedded = button.get("order_summary_v2") if isinstance(button, dict) else None
    found = []
    seen = set()
    for source in (
        summary,
        components.get("item_presentation_escrow_v2"),
        embedded,
    ):
        if source is None or id(source) in seen:
            continue
        seen.add(id(source))
        if not isinstance(source, dict):
            raise buyer.BuyerError("Vinted did not return readable checkout items.")
        items = source.get("order_items")
        if items is None:
            continue
        if (
            not isinstance(items, list)
            or len(items) != 1
            or not isinstance(items[0], dict)
        ):
            raise buyer.BuyerError(
                "Vinted did not confirm a single item for this checkout."
            )
        item = items[0]
        identity = str(item.get("id", ""))
        if not re.fullmatch(r"[0-9]{1,24}", identity) or (
            item_id is not None and identity != str(item_id)
        ):
            raise buyer.BuyerError(
                "Vinted did not confirm the selected item in this checkout."
            )
        price = cents(item.get("price"))
        pricing = item.get("pricing")
        if pricing is not None:
            if not isinstance(pricing, dict):
                raise buyer.BuyerError("Vinted did not return readable item pricing.")
            # The current item-presentation plugins render pricing.final_price.
            price = cents(pricing.get("final_price"))
        title = item.get("title")
        found.append(
            {
                "id": identity,
                "price": price,
                "title": (
                    "".join(char for char in title[:200] if ord(char) >= 32)
                    if isinstance(title, str)
                    else ""
                ),
            }
        )
    if found:
        if any(
            (item["id"], item["price"]) != (found[0]["id"], found[0]["price"])
            for item in found[1:]
        ):
            raise buyer.BuyerError(
                "Vinted's checkout item prices or identities disagree."
            )
        return found[0]
    if "pay_button_v2" in components:
        raise buyer.BuyerError(
            "Vinted did not confirm a single item price in this checkout."
        )
    # Older checkout DTOs expose the item subtotal directly and no current button.
    subtotal = summary.get("subtotal")
    return {
        "id": None,
        "title": "",
        "price": cents(subtotal.get("price") if isinstance(subtotal, dict) else None),
    }


def checkout_prices(checkout, item_price, maximum, *, item_id=None):
    if not isinstance(checkout, dict):
        raise buyer.BuyerError("Vinted did not return a valid checkout.")
    components = checkout.get("components") or {}
    if not isinstance(components, dict):
        raise buyer.BuyerError("Vinted did not return readable checkout details.")
    # Only an explicit checkout total is accepted. A subtotal or listing price
    # can omit delivery and buyer protection, so neither is a payment fallback.
    pay_button = components.get("pay_button_v2")
    if "pay_button_v2" in components and not isinstance(pay_button, dict):
        raise buyer.BuyerError("Vinted did not return a readable checkout total.")
    current = pay_button is not None
    summary = components.get("order_summary_v2")
    if summary is None:
        summary = components.get("order_summary")
    if summary is None and current:
        summary = pay_button.get("order_summary_v2")
    if not isinstance(summary, dict):
        raise buyer.BuyerError("Vinted did not return a readable checkout total.")
    # Current web checkout renders the all-in amount from pay_button_v2.total.
    # order_summary_v2 contains the item subtotal and fee lines, not that total.
    total_part = (pay_button if pay_button is not None else summary).get("total") or {}
    total = cents(total_part.get("price") if isinstance(total_part, dict) else None)
    if pay_button is not None and summary.get("total") is not None:
        old_total = summary["total"]
        if not isinstance(old_total, dict) or cents(old_total.get("price")) != total:
            raise buyer.BuyerError(
                "Vinted's checkout totals disagree. No payment was sent."
            )
    if total < item_price:
        raise buyer.BuyerError(
            "Autobuy stopped: Vinted's checkout total could not be verified."
        )
    current_item = checkout_item_details(components, summary, item_id=item_id)
    if current_item["price"] > item_price:
        raise buyer.BuyerError(
            f"The item price changed during checkout to £{current_item['price']/100:.2f}. No payment was sent.",
            reason="price_increased",
        )
    if maximum is not None and total > maximum:
        raise buyer.BuyerError(
            f"Over budget: £{total/100:.2f} including fees and delivery; this search's maximum total is £{maximum/100:.2f}. No payment was sent.",
            reason="total_over_budget",
        )
    checksum = checkout.get("checksum")
    if (
        checkout.get("errors")
        or summary.get("errors")
        or not isinstance(checksum, str)
        or not checksum
        or len(checksum) > 8192
        or any(ord(char) < 32 or ord(char) == 127 for char in checksum)
        or any(
            isinstance(component, dict) and component.get("errors")
            for component in components.values()
        )
    ):
        raise buyer.BuyerError(
            "Vinted needs checkout details before this item can be paid for."
        )
    for key in ("shipping_address", "payment_method"):
        if not isinstance(components.get(key), dict) or not components[key]:
            raise buyer.BuyerError(
                "Set your delivery address and payment method in Vinted first."
            )
    payment = components["payment_method"]
    if current or any(
        key in payment for key in ("selected_payment_method", "cards", "pay_in_methods")
    ):
        selected = payment.get("selected_payment_method")
        method = selected.get("pay_in_method") if isinstance(selected, dict) else None
        code = method.get("payment_method") if isinstance(method, dict) else None
        if not isinstance(code, str) or not code:
            raise buyer.BuyerError(
                "Choose and save your payment method in Vinted first."
            )
        if code.lower() in ("card", "credit_card"):
            card = selected.get("credit_card")
            if not isinstance(card, dict) or card.get("expired") is not False:
                raise buyer.BuyerError(
                    "Choose a valid saved payment card in Vinted first."
                )
    elif not payment.get("id"):
        raise buyer.BuyerError("Choose and save your payment method in Vinted first.")
    address = components["shipping_address"]
    if (
        current
        or any(
            key in address
            for key in ("address", "address_is_missing", "shipping_order_id")
        )
    ) and (
        not isinstance(address.get("address"), dict)
        or not address["address"].get("id")
        or address["address"].get("is_complete") is not True
        or address.get("address_is_missing")
    ):
        raise buyer.BuyerError(
            "Complete and save your delivery address in Vinted first."
        )
    shipping = components.get("shipping_pickup_details") or {}
    options = components.get("shipping_pickup_options") or {}
    # Choices must be confirmed by Vinted, whether saved or selected below.
    if (
        not isinstance(shipping, dict)
        or not isinstance(options, dict)
        or not shipping
        or shipping.get("errors")
        or options.get("errors")
        or options.get("address_is_missing")
        or options.get("selected_pickup_option") is None
        or type(options.get("selected_pickup_option")) not in (int, str)
        or (
            not options.get("selected_pickup_option")
            and "pickup_details" not in shipping
        )
    ):
        raise buyer.BuyerError("Choose and save your delivery option in Vinted first.")
    if current or "pickup_details" in shipping:
        details = shipping.get("pickup_details")
        choices = options.get("pickup_options")
        if not isinstance(details, dict) or not isinstance(choices, dict):
            raise buyer.BuyerError(
                "Choose and save your delivery option in Vinted first."
            )
        selected_types = [
            kind
            for kind in ("home", "pickup")
            if isinstance(choices.get(kind), dict)
            and choices[kind].get("pickup_option_type")
            == options["selected_pickup_option"]
        ]
        rate = details.get("selected_rate_uuid")
        if len(selected_types) != 1 or not isinstance(rate, str) or not rate:
            raise buyer.BuyerError(
                "Choose and save your delivery option in Vinted first."
            )
        if selected_types[0] == "pickup":
            point = details.get("shipping_point")
            if (
                not isinstance(point, dict)
                or not (point.get("uuid") or point.get("code"))
                or (point.get("rate_uuid") is not None and point["rate_uuid"] != rate)
            ):
                raise buyer.BuyerError(
                    "Choose and save your pickup point in Vinted first."
                )
        else:
            receiver = shipping.get("receiver_address")
            selected_address = address.get("address", address)
            if (
                not isinstance(receiver, dict)
                or receiver.get("id") != selected_address.get("id")
                or receiver.get("is_complete") is not True
            ):
                raise buyer.BuyerError(
                    "Save your home delivery address in Vinted first."
                )
    contact = components.get("shipping_contact")
    if contact is not None:
        if not isinstance(contact, dict) or contact.get("errors"):
            raise buyer.BuyerError("Check your saved delivery contact in Vinted first.")
        if contact.get("is_receiver_phone_number_required") is True and (
            not isinstance(contact.get("phone_number"), str)
            or not contact["phone_number"].strip()
        ):
            raise buyer.BuyerError("Save your delivery phone number in Vinted first.")
    return total


def choice_preferences(config):
    return {
        "pickup_mode": config["pickup_mode"],
        "preferred_card_last4": config["preferred_card_last4"],
    }


def coordinates(value):
    if not isinstance(value, dict):
        raise TypeError
    result = []
    for key, bound in (("latitude", 90), ("longitude", 180)):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            raise TypeError
        number = float(raw)
        if not math.isfinite(number) or not -bound <= number <= bound:
            raise ValueError
        result.append(number)
    return tuple(result)


def choice_id(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 200
        and all(32 <= ord(char) < 127 for char in value)
    )


def nearest_pickup(client, components):
    address_component = components.get("shipping_address") or {}
    address = address_component.get("address") or {}
    shipping = components.get("shipping_pickup_details") or {}
    option = (components.get("shipping_pickup_options") or {}).get("pickup_options")
    pickup = option.get("pickup") if isinstance(option, dict) else None
    order_id = str(shipping.get("shipping_order_id", ""))
    if (
        not address.get("id")
        or address.get("is_complete") is not True
        or address_component.get("address_is_missing")
        or address.get("country_code") != "GB"
        or not re.fullmatch(r"[0-9]{1,24}", order_id)
        or (
            address_component.get("shipping_order_id") is not None
            and str(address_component["shipping_order_id"]) != order_id
        )
        or not isinstance(pickup, dict)
        or type(pickup.get("pickup_option_type")) not in (int, str)
    ):
        raise buyer.BuyerError(
            "Vinted did not confirm pickup delivery for your saved UK address."
        )
    try:
        latitude, longitude = coordinates(address.get("coordinates"))
    except (ValueError, TypeError, OverflowError):
        raise buyer.BuyerError(
            "Vinted did not provide coordinates for your saved address. Save a complete address in Vinted first."
        ) from None
    data = client.request(
        "GET",
        f"/web/gateway/shipping-estimation/external/shipping_orders/{order_id}/nearby_pickup_points",
        params={"country_code": "GB", "latitude": latitude, "longitude": longitude},
    )
    points, rates = data.get("shipping_points"), data.get("shipping_rates")
    if (
        not isinstance(points, list)
        or not isinstance(rates, list)
        or len(points) > 1000
        or len(rates) > 100
    ):
        raise buyer.BuyerError(
            "Vinted did not return readable available pickup points."
        )
    eligible = {}
    for rate in rates:
        if (
            not isinstance(rate, dict)
            or rate.get("restriction")
            or rate.get("verification_service_type")
            or not choice_id(rate.get("rate_uuid"))
        ):
            continue
        try:
            price = cents(rate.get("price"))
        except buyer.BuyerError:
            continue
        if rate["rate_uuid"] in eligible:
            raise buyer.BuyerError("Vinted returned conflicting pickup rates.")
        eligible[rate["rate_uuid"]] = price
    candidates = []
    lat = math.radians(latitude)
    for entry in points:
        point = entry.get("point") if isinstance(entry, dict) else None
        if (
            not isinstance(point, dict)
            or point.get("rate_uuid") not in eligible
            or not choice_id(point.get("uuid"))
            or not choice_id(point.get("code"))
        ):
            continue
        try:
            point_lat, point_lon = coordinates(point)
        except (ValueError, TypeError, OverflowError):
            continue
        delta_lat = math.radians(point_lat - latitude)
        delta_lon = math.radians(point_lon - longitude)
        square = (
            math.sin(delta_lat / 2) ** 2
            + math.cos(lat)
            * math.cos(math.radians(point_lat))
            * math.sin(delta_lon / 2) ** 2
        )
        distance = 6371000 * 2 * math.asin(math.sqrt(min(1, max(0, square))))
        candidates.append(
            (
                distance,
                eligible[point["rate_uuid"]],
                point["code"],
                point["uuid"],
                point["rate_uuid"],
                point,
            )
        )
    if not candidates:
        raise buyer.BuyerError(
            "Vinted did not return an available pickup point for your saved address."
        )
    point = min(candidates, key=lambda candidate: candidate[:5])[-1]
    return address["id"], pickup["pickup_option_type"], point


def preferred_payment(payment, last4):
    selected = payment.get("selected_payment_method") or {}
    method = selected.get("pay_in_method") or {}
    code = method.get("payment_method")
    card = selected.get("credit_card") or {}
    methods = payment.get("pay_in_methods")
    if (
        code == "balance"
        and isinstance(methods, list)
        and any(
            isinstance(method, dict)
            and method.get("payment_method") == "balance"
            and method.get("enabled") is not True
            for method in methods
        )
    ):
        raise buyer.BuyerError(
            "Vinted did not confirm an available balance for this checkout."
        )
    if code == "balance" or (
        code in ("card", "credit_card")
        and card.get("last4") == last4
        and card.get("expired") is False
    ):
        return None
    cards = payment.get("cards")
    if not isinstance(methods, list) or not isinstance(cards, list):
        raise buyer.BuyerError(
            "Vinted did not confirm your preferred saved card or an available balance."
        )
    valid = [
        card
        for card in cards
        if isinstance(card, dict)
        and card.get("last4") == last4
        and card.get("expired") is False
        and re.fullmatch(r"[0-9]{1,24}", str(card.get("id", "")))
    ]
    card_methods = [
        method["payment_method"]
        for method in methods
        if isinstance(method, dict)
        and method.get("enabled") is True
        and method.get("payment_method") in ("card", "credit_card")
    ]
    if len(valid) == 1 and card_methods:
        return {"card_id": str(valid[0]["id"]), "payment_method": card_methods[0]}
    if any(
        isinstance(method, dict)
        and method.get("enabled") is True
        and method.get("payment_method") == "balance"
        for method in methods
    ):
        return {"payment_method": "balance"}
    raise buyer.BuyerError(
        "Your preferred saved card or an available Vinted balance could not be confirmed."
    )


def configure_checkout_choices(client, checkout, config):
    """Select only the owner's preferences through the current first-party API."""
    preferences = choice_preferences(config)
    if preferences == {"pickup_mode": "saved", "preferred_card_last4": ""}:
        return checkout
    purchase_id = str(checkout.get("id", ""))
    components = checkout.get("components")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", purchase_id) or not isinstance(
        components, dict
    ):
        raise buyer.BuyerError("Vinted did not confirm readable checkout choices.")
    changes = {}
    expected = None
    if config["pickup_mode"] == "nearest":
        address_id, pickup_type, point = nearest_pickup(client, components)
        expected = address_id, pickup_type, point
        details = (components.get("shipping_pickup_details") or {}).get(
            "pickup_details"
        ) or {}
        current_point = details.get("shipping_point") or {}
        if (
            details.get("selected_rate_uuid"),
            current_point.get("uuid"),
            current_point.get("code"),
            (components.get("shipping_pickup_options") or {}).get(
                "selected_pickup_option"
            ),
        ) != (point["rate_uuid"], point["uuid"], point["code"], pickup_type):
            changes["shipping_pickup_options"] = {"pickup_type": pickup_type}
            changes["shipping_pickup_details"] = {
                "rate_uuid": point["rate_uuid"],
                "point_code": point["code"],
                "point_uuid": point["uuid"],
            }
    last4 = config["preferred_card_last4"]
    if last4:
        payment = components.get("payment_method")
        if not isinstance(payment, dict):
            raise buyer.BuyerError("Vinted did not return readable payment choices.")
        change = preferred_payment(payment, last4)
        if change:
            changes["payment_method"] = change
    if changes:
        updated = client.request(
            "PUT", f"/api/v2/purchases/{purchase_id}/checkout", {"components": changes}
        )
        checkout = updated.get("checkout")
        if (
            not isinstance(checkout, dict)
            or str(checkout.get("id")) != purchase_id
            or not isinstance(checkout.get("components"), dict)
        ):
            raise buyer.BuyerError(
                "Vinted did not confirm your selected checkout choices."
            )
        components = checkout["components"]
    if expected:
        address_id, pickup_type, point = expected
        address = (components.get("shipping_address") or {}).get("address") or {}
        details = (components.get("shipping_pickup_details") or {}).get(
            "pickup_details"
        ) or {}
        current_point = details.get("shipping_point") or {}
        if address.get("id") != address_id or (
            details.get("selected_rate_uuid"),
            current_point.get("uuid"),
            current_point.get("code"),
            current_point.get("rate_uuid"),
            (components.get("shipping_pickup_options") or {}).get(
                "selected_pickup_option"
            ),
        ) != (
            point["rate_uuid"],
            point["uuid"],
            point["code"],
            point["rate_uuid"],
            pickup_type,
        ):
            raise buyer.BuyerError(
                "Vinted did not confirm the nearest available pickup point. No payment was sent."
            )
    if (
        last4
        and preferred_payment(components.get("payment_method") or {}, last4) is not None
    ):
        raise buyer.BuyerError(
            "Vinted did not confirm your preferred card or balance. No payment was sent."
        )
    return checkout


def checkout_choice_details(checkout):
    components = checkout["components"]
    selected = components["payment_method"].get("selected_payment_method") or {}
    code = (selected.get("pay_in_method") or {}).get("payment_method")
    card = selected.get("credit_card") or {}
    shipping = (components.get("shipping_pickup_details") or {}).get(
        "pickup_details"
    ) or {}
    point = shipping.get("shipping_point") or {}
    name = point.get("name")
    return {
        "signature": {
            "address_id": (components["shipping_address"].get("address") or {}).get(
                "id"
            ),
            "payment": code,
            "card_last4": (
                card.get("last4") if code in ("card", "credit_card") else None
            ),
            "point_uuid": point.get("uuid"),
            "point_code": point.get("code"),
            "rate_uuid": shipping.get("selected_rate_uuid"),
            "pickup_type": (components.get("shipping_pickup_options") or {}).get(
                "selected_pickup_option"
            ),
        },
        "pickup_name": (
            "".join(char for char in name[:150] if ord(char) >= 32)
            if isinstance(name, str)
            else ""
        ),
        "payment_label": (
            "Vinted balance"
            if code == "balance"
            else (
                f"Saved card ending {card.get('last4')}"
                if code in ("card", "credit_card")
                and re.fullmatch(r"[0-9]{4}", str(card.get("last4", "")))
                else "Saved Vinted payment method"
            )
        ),
    }


def payment_action_url(paid):
    """Read Vinted's bank-action link; never follow it or log its parameters."""
    action = paid.get("action") if isinstance(paid, dict) else None
    parameters = action.get("parameters") if isinstance(action, dict) else None
    value = parameters.get("url") if isinstance(parameters, dict) else None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
        ):
            return None
    except ValueError:
        return None
    return value


def ready(row):
    config = buyer.settings()
    if not config["connected"]:
        raise buyer.BuyerError(
            "Connect your Vinted buyer in Connections first.", reason="not_connected"
        )
    if not config["enabled"]:
        raise buyer.BuyerError(
            "Autobuy is off. Enable Autobuy in Connections. No purchase was started.",
            reason="disabled",
        )
    return config, vinted_budget.purchase_limits(row)


def check_item_limits(price, limits, query_id):
    if limits.item_maximum is not None and price > limits.item_maximum:
        raise buyer.BuyerError(
            f"The item is £{price/100:.2f}; search #{query_id}'s URL maximum item price is £{limits.item_maximum/100:.2f}. No payment was sent.",
            reason="item_over_url_limit",
        )
    if limits.total_maximum is not None and price > limits.total_maximum:
        raise buyer.BuyerError(
            f"Over budget: the item alone is £{price/100:.2f}; search #{query_id} has a £{limits.total_maximum/100:.2f} limit including fees and delivery. No payment was sent.",
            reason="item_over_budget",
        )


def verified_listing(client, row, config, limits):
    """Read the current item and apply the same listing gates for every caller."""
    item_id = str(row["item_id"])
    try:
        data = client.request("GET", f"/api/v2/items/{item_id}")
    except buyer.BuyerError as exc:
        if exc.status != 404 or exc.reason not in ("unreadable", "http_error"):
            raise
        # The former item-detail route returns 404 on the current UK site.
        # Read the same canonical listing's first-party server-rendered item;
        # access refusals and security challenges never use another route.
        data = client.listing_page(row["url"], item_id)
    item = data.get("item") or {}
    if not isinstance(item, dict) or str(item.get("id")) != item_id:
        raise buyer.BuyerError(
            "Vinted did not return this listing. Its availability could not be verified. No payment was sent.",
            reason="item_unavailable",
        )
    for flag, label, code in (
        ("is_sold", "already sold", "item_sold"),
        ("is_reserved", "reserved", "item_reserved"),
        ("is_closed", "closed or removed", "item_closed"),
        ("is_hidden", "hidden or removed", "item_closed"),
    ):
        if item.get(flag):
            raise buyer.BuyerError(
                f"This item is {label} on Vinted. No payment was sent.", reason=code
            )
    if "can_buy" in item and item["can_buy"] is not True:
        raise buyer.BuyerError(
            "Vinted does not currently allow this account to buy this item. No payment was sent.",
            reason="item_unavailable",
        )
    price = item.get("price")
    if not isinstance(price, dict):
        price = {"amount": price, "currency_code": item.get("currency")}
    current_price = cents(price)
    alert_price = cents({"amount": row["price"], "currency_code": row["currency"]})
    if current_price > alert_price:
        raise buyer.BuyerError(
            f"Item price increased from £{alert_price/100:.2f} to £{current_price/100:.2f} after your alert. No payment was sent.",
            reason="price_increased",
        )
    check_item_limits(current_price, limits, row["query_id"])
    user = item.get("user")
    seller = str(
        (user.get("id") if isinstance(user, dict) else None)
        or item.get("user_id")
        or ""
    )
    if not seller.isdigit() or seller == config["user_id"]:
        raise buyer.BuyerError("The seller could not be verified for this purchase.")
    return current_price, seller


def checkout_link_id(url):
    """Accept only an ordinary, owner-supplied UK transaction checkout link."""
    message = "Paste the existing Vinted UK checkout link, including its purchase and order details."
    if not isinstance(url, str) or len(url) > 2048:
        raise buyer.BuyerError(message)
    try:
        parts = urlsplit(url.strip())
        values = parse_qs(parts.query, keep_blank_values=True, max_num_fields=3)
        if (
            parts.scheme != "https"
            or parts.netloc != "www.vinted.co.uk"
            or parts.path != "/checkout"
            or parts.fragment
            or set(values) != {"purchase_id", "order_id", "order_type"}
            or any(len(value) != 1 for value in values.values())
            or values["order_type"] != ["transaction"]
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", values["purchase_id"][0])
            or not re.fullmatch(r"[0-9]{1,24}", values["order_id"][0])
        ):
            raise ValueError
    except ValueError:
        raise buyer.BuyerError(message) from None
    return values["purchase_id"][0]


def checkout_price_evidence(value):
    """Only fixed labels or verified GBP pennies may appear in diagnostics."""
    if value is None:
        return "absent"
    try:
        return f"GBP:{cents(value)}"
    except buyer.BuyerError:
        return "unreadable"


def checkout_item_evidence(items):
    if not isinstance(items, list):
        return "absent"
    if len(items) > 20:
        return "too_many"
    evidence = []
    for item in items:
        if not isinstance(item, dict):
            evidence.append("unreadable")
            continue
        pricing = item.get("pricing")
        evidence.append(
            {
                "price": checkout_price_evidence(item.get("price")),
                "final": checkout_price_evidence(
                    pricing.get("final_price") if isinstance(pricing, dict) else None
                ),
            }
        )
    return json.dumps(evidence, separators=(",", ":"))


def check_checkout(url, *, prepare_test=False):
    """Load a selected existing checkout; never claim or submit a payment."""
    purchase_id = checkout_link_id(url)
    with buyer.exclusive():
        with closing(connection()) as conn:
            submitted = conn.execute(
                "SELECT 1 FROM vinted_buy_attempts WHERE checkout_id=? AND state IN ('paying','unknown','needs_action','paid','payment_failed') LIMIT 1",
                (purchase_id,),
            ).fetchone()
        if submitted:
            raise buyer.BuyerError(
                "This checkout already has a recorded payment attempt. Use its payment-status button or check Vinted; the checkout was not reloaded."
            )
        client = None
        phase = "checking your buyer account"
        try:
            quoted_account = buyer.settings()
            client = buyer.connected_client()
            phase = "loading the existing checkout"
            # Current first-party fetchInitialSingleCheckoutData uses PUT with
            # no options to load a purchase already opened by the owner.
            data = client.request("PUT", f"/api/v2/purchases/{purchase_id}/checkout")
            checkout = data.get("checkout")
            if not isinstance(checkout, dict) or str(checkout.get("id")) != purchase_id:
                raise buyer.BuyerError("Vinted did not confirm this checkout.")
            phase = "selecting your delivery and payment preferences"
            checkout = configure_checkout_choices(client, checkout, quoted_account)
            components = checkout.get("components")
            if not isinstance(components, dict):
                raise buyer.BuyerError(
                    "Vinted did not return readable checkout details."
                )
            summary = components.get("order_summary_v2")
            pay_button = components.get("pay_button_v2")
            if summary is None:
                summary = components.get("order_summary")
            if summary is None and isinstance(pay_button, dict):
                summary = pay_button.get("order_summary_v2")
            total_part = pay_button if pay_button is not None else summary
            if not isinstance(summary, dict) or not isinstance(total_part, dict):
                raise buyer.BuyerError(
                    "Vinted did not return a readable checkout total."
                )
            subtotal_part = summary.get("subtotal")
            amount_part = total_part.get("total")
            presentation = components.get("item_presentation_escrow_v2")
            single = components.get("single_item_presentation")
            logger.info(
                "Autobuy checkout price layout: subtotal=%s total=%s summary_items=%s presentation_items=%s single_payable=%s",
                checkout_price_evidence(
                    subtotal_part.get("price")
                    if isinstance(subtotal_part, dict)
                    else None
                ),
                checkout_price_evidence(
                    amount_part.get("price") if isinstance(amount_part, dict) else None
                ),
                checkout_item_evidence(summary.get("order_items")),
                checkout_item_evidence(
                    presentation.get("order_items")
                    if isinstance(presentation, dict)
                    else None
                ),
                checkout_price_evidence(
                    single.get("payable_amount") if isinstance(single, dict) else None
                ),
            )
            phase = "checking the checkout prices"
            current_item = checkout_item_details(components, summary)
            item = current_item["price"]
            total = cents(
                amount_part.get("price") if isinstance(amount_part, dict) else None
            )
            phase = "checking saved delivery and payment choices"
            try:
                # This is a diagnostic validation only. Using its actual total
                # as the ceiling does not authorize any payment or change a budget.
                checkout_prices(checkout, item, total, item_id=current_item["id"])
            except buyer.BuyerError as exc:
                raise buyer.BuyerError(
                    f"Checkout total £{total/100:.2f} including fees and delivery; saved choices need attention: {exc}",
                    reason=exc.reason,
                    status=exc.status,
                ) from None
            logger.info("Autobuy checkout check: result=verified")
            target = (
                f" for {current_item['title'] or 'the selected item'} (item {current_item['id']})"
                if current_item["id"]
                else ""
            )
            message = (
                f"Checkout check passed{target}: item price £{item/100:.2f}; total £{total/100:.2f} including fees and delivery. "
                "Saved delivery and payment choices passed validation. This check does not authorize payment or change any search budget. No payment was submitted."
            )
            if not prepare_test:
                return message
            current_account = buyer.settings()
            if (
                not current_item["id"]
                or not quoted_account["connected"]
                or not current_account["connected"]
                or current_account["user_id"] != quoted_account["user_id"]
                or choice_preferences(current_account)
                != choice_preferences(quoted_account)
            ):
                raise buyer.BuyerError(
                    "Recheck this checkout with the same saved buyer account."
                )
            choices = checkout_choice_details(checkout)
            quote = {
                "purpose": "vinted_checkout_test_v1",
                "checkout_id": purchase_id,
                "item_id": current_item["id"],
                "buyer_id": current_account["user_id"],
                "item_price": item,
                "total": total,
                "created": time.time(),
                "preferences": choice_preferences(current_account),
                "choices": choices["signature"],
            }
            return {
                "message": message,
                "title": current_item["title"] or "Selected test item",
                "item_id": current_item["id"],
                "item_price": item,
                "total": total,
                "pickup_name": choices["pickup_name"],
                "payment_label": choices["payment_label"],
                "token": buyer.encrypt(quote).decode("ascii"),
            }
        except buyer.BuyerError as exc:
            logger.info(
                "Autobuy checkout check: phase=%s reason=%s http=%s",
                phase,
                exc.reason,
                exc.status,
            )
            http = f" (HTTP {exc.status})" if exc.status else ""
            raise buyer.BuyerError(
                f"Checkout check stopped while {phase}{http}: {exc} No payment was submitted.",
                reason=exc.reason,
                status=exc.status,
            ) from None
        finally:
            if client:
                client.session.close()


def saved_browser_info():
    with closing(connection()) as conn:
        info = json.loads(
            conn.execute("SELECT browser_info FROM vinted_buyer WHERE id=1").fetchone()[
                0
            ]
        )
    if not isinstance(info, dict) or not info:
        raise buyer.BuyerError(
            "Save the buyer settings from your browser before using Autobuy."
        )
    return info


def record_payment_result(item_id, paid, total):
    status = (paid.get("payment") or {}).get("status")
    if status in ("success", "completed"):
        record(item_id, "paid", f"Paid £{total/100:.2f}. Check your Vinted purchases.")
    elif status in ("pending", "preparing", "requires_action"):
        action_url = payment_action_url(paid)
        record(
            item_id,
            "needs_action",
            (
                "Vinted needs payment or bank confirmation. Use the confirmation button below; do not buy again."
                if action_url
                else "Vinted needs payment or bank confirmation. Open your Vinted checkout; do not buy again."
            ),
            action_url=action_url,
        )
    elif status in ("failure", "failed"):
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


def buy_checkout_quote(token):
    """Explicit one-off test using a checked item and its quoted all-in ceiling."""
    message = "Prepare a fresh test checkout before buying. No payment was sent."
    if not isinstance(token, str) or not 100 <= len(token) <= 8192:
        raise buyer.BuyerError(message)
    try:
        quote = buyer.decrypt(token.encode("ascii"))
    except (buyer.BuyerError, UnicodeError):
        raise buyer.BuyerError(message) from None
    if (
        not isinstance(quote, dict)
        or quote.get("purpose") != "vinted_checkout_test_v1"
        or not isinstance(quote.get("checkout_id"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", quote["checkout_id"])
        or not isinstance(quote.get("item_id"), str)
        or not re.fullmatch(r"[0-9]{1,24}", quote["item_id"])
        or not isinstance(quote.get("buyer_id"), str)
        or not re.fullmatch(r"[0-9]{1,24}", quote["buyer_id"])
        or type(quote.get("total")) is not int
        or type(quote.get("item_price")) is not int
        or not 0 < quote["item_price"] <= quote["total"] <= 100000000
        or type(quote.get("created")) not in (float, int)
        or not 0 <= time.time() - quote["created"] <= 1800
    ):
        raise buyer.BuyerError(message)
    item_id, purchase_id = quote["item_id"], quote["checkout_id"]
    with buyer.exclusive():
        config = buyer.settings()
        if (
            not config["connected"]
            or not config["enabled"]
            or config["user_id"] != quote["buyer_id"]
            or choice_preferences(config) != quote.get("preferences")
        ):
            raise buyer.BuyerError(
                "Prepare a fresh test with Autobuy enabled and your current buyer preferences. No payment was sent."
            )
        with closing(connection()) as conn:
            submitted = conn.execute(
                "SELECT item_id FROM vinted_buy_attempts WHERE checkout_id=? AND state IN ('paying','unknown','needs_action','paid','payment_failed') LIMIT 1",
                (purchase_id,),
            ).fetchone()
        if submitted:
            if submitted["item_id"] == item_id:
                return result(item_id)
            raise buyer.BuyerError(
                "This checkout already has a payment attempt. Check Vinted before doing anything else."
            )
        if not claim({"item_id": item_id}, recover_preparing=True):
            return result(item_id)
        client = None
        payment_started = False
        phase = "checking your buyer account"
        try:
            client = buyer.connected_client()
            record(
                item_id,
                "preparing",
                "Checking the quoted test checkout",
                checkout_id=purchase_id,
            )
            phase = "checking the quoted checkout"
            data = client.request("PUT", f"/api/v2/purchases/{purchase_id}/checkout")
            checkout = data.get("checkout")
            if not isinstance(checkout, dict) or str(checkout.get("id")) != purchase_id:
                raise buyer.BuyerError("Vinted did not confirm the selected checkout.")
            phase = "confirming your delivery and payment preferences"
            checkout = configure_checkout_choices(client, checkout, config)
            if checkout_choice_details(checkout)["signature"] != quote.get("choices"):
                raise buyer.BuyerError(
                    "The quoted delivery or payment choice changed. Prepare a fresh test before buying."
                )
            total = checkout_prices(
                checkout, quote["item_price"], quote["total"], item_id=item_id
            )
            current = buyer.settings()
            if (
                not current["connected"]
                or not current["enabled"]
                or current["user_id"] != quote["buyer_id"]
                or choice_preferences(current) != quote["preferences"]
            ):
                raise buyer.BuyerError(
                    "The buyer connection or Autobuy setting changed before payment."
                )
            info = saved_browser_info()
            record(
                item_id,
                "paying",
                "Payment submitted; awaiting Vinted confirmation",
                total=total,
            )
            payment_started = True
            phase = "submitting payment"
            paid = client.request(
                "POST",
                f"/api/v2/purchases/{purchase_id}/checkout/payment",
                {
                    "checksum": checkout["checksum"],
                    "payment_options": {"browser_info": info},
                },
            )
            record_payment_result(item_id, paid, total)
        except Exception as exc:  # noqa: BLE001 -- preserve uncertain payments
            logger.info(
                "Autobuy test stopped: phase=%s error_type=%s",
                phase,
                type(exc).__name__,
            )
            if payment_started:
                outcome = "Payment result is unconfirmed. Check Vinted before retrying."
            elif isinstance(exc, buyer.BuyerError):
                http = f" (HTTP {exc.status})" if exc.status else ""
                outcome = f"Test purchase stopped while {phase}{http}: {exc} No payment was sent."
            else:
                outcome = (
                    f"Unexpected checkout error while {phase}. No payment was sent."
                )
            record(
                item_id,
                "unknown" if payment_started else "failed_before_payment",
                outcome,
            )
        finally:
            if client:
                client.session.close()
        return result(item_id)


def check_listing(item_id):
    """Owner-triggered preflight; no claim, conversation, checkout or payment."""
    item_id = str(item_id)
    if not item_id.isascii() or not item_id.isdigit() or len(item_id) > 24:
        raise buyer.BuyerError("Choose an existing Vinted purchase attempt to check.")
    with closing(connection()) as conn:
        row = conn.execute(
            "SELECT o.* FROM alert_outbox o JOIN vinted_buy_attempts a ON a.item_id=o.item_id WHERE o.item_id=? AND o.platform='vinted'",
            (item_id,),
        ).fetchone()
    if row is None:
        raise buyer.BuyerError("This Vinted alert is no longer available to check.")
    row = dict(row)
    if urlsplit(row["url"]).hostname != "www.vinted.co.uk" or row["currency"] != "GBP":
        raise buyer.BuyerError("Autobuy currently supports UK Vinted listings in GBP.")
    with buyer.exclusive():
        client = None
        phase = "checking your buyer account"
        try:
            config, limits = ready(row)
            client = buyer.connected_client()
            phase = "checking the listing"
            price, _ = verified_listing(client, row, config, limits)
            logger.info("Autobuy listing check: item=%s result=verified", item_id)
            if limits.total_maximum is not None:
                limit_text = f"maximum total £{limits.total_maximum/100:.2f}"
            elif limits.item_maximum is not None:
                limit_text = f"URL maximum item price £{limits.item_maximum/100:.2f}; fees and delivery are added at checkout"
            else:
                limit_text = (
                    "alerted item price; fees and delivery are added at checkout"
                )
            return (
                f"Listing check passed: item £{price/100:.2f}; search #{row['query_id']} {limit_text}. "
                "Delivery, fees and saved payment choices still need verification at checkout. No checkout or payment was created."
            )
        except buyer.BuyerError as exc:
            logger.info(
                "Autobuy listing check: item=%s phase=%s reason=%s http=%s",
                item_id,
                phase,
                exc.reason,
                exc.status,
            )
            status = f" (HTTP {exc.status})" if exc.status else ""
            raise buyer.BuyerError(
                f"Listing check stopped while {phase}{status}: {exc} No checkout or payment was created.",
                reason=exc.reason,
                status=exc.status,
            ) from None
        finally:
            if client:
                client.session.close()


def buy(row):
    item_id = str(row["item_id"])
    host = urlsplit(row["url"]).hostname
    if not item_id.isdigit() or host != "www.vinted.co.uk" or row["currency"] != "GBP":
        raise buyer.BuyerError("Autobuy currently supports UK Vinted listings in GBP.")
    with buyer.exclusive():
        config, limits = ready(row)
        if not claim(row, recover_preparing=True):
            return result(item_id)
        client = None
        payment_started = False
        phase = "checking your buyer account"
        reason = None
        try:
            client = buyer.connected_client()
            phase = "checking the listing"
            current_price, seller = verified_listing(client, row, config, limits)
            phase = "preparing the purchase"
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
            phase = "building the checkout"
            built = client.request(
                "POST",
                "/api/v2/purchases/checkout/build",
                {"purchase_items": [{"id": transaction_id, "type": "transaction"}]},
            )
            checkout = built.get("checkout") or {}
            purchase_id = str(checkout.get("id") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", purchase_id):
                raise buyer.BuyerError("Vinted did not return a valid checkout.")
            record(
                item_id,
                "preparing",
                "Checking the final price and your saved delivery choice",
                checkout_id=purchase_id,
            )
            phase = "loading delivery and payment choices"
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
            phase = "checking the final total and delivery choice"
            # Read the current controls again immediately before payment.
            config = buyer.settings()
            if not config["enabled"] or not config["connected"]:
                raise buyer.BuyerError("Autobuy was disabled before payment.")
            checkout = configure_checkout_choices(client, checkout, config)
            current = buyer.settings()
            if (
                not current["enabled"]
                or not current["connected"]
                or current["user_id"] != config["user_id"]
                or choice_preferences(current) != choice_preferences(config)
            ):
                raise buyer.BuyerError(
                    "Your buyer account or preferences changed before payment."
                )
            limits = limits.restrict(vinted_budget.purchase_limits(row))
            check_item_limits(current_price, limits, row["query_id"])
            total = checkout_prices(
                checkout, current_price, limits.total_maximum, item_id=item_id
            )
            info = saved_browser_info()
            record(
                item_id,
                "paying",
                "Payment submitted; awaiting Vinted confirmation",
                total=total,
            )
            payment_started = True
            phase = "submitting payment"
            paid = client.request(
                "POST",
                f"/api/v2/purchases/{purchase_id}/checkout/payment",
                {
                    "checksum": checkout["checksum"],
                    "payment_options": {"browser_info": info},
                },
            )
            record_payment_result(item_id, paid, total)
        except buyer.BuyerError as exc:
            reason = exc.reason
            message = str(exc)
            if reason in buyer.AUTH_REASONS:
                http = f" (HTTP {exc.status})" if isinstance(exc.status, int) else ""
                message = f"Autobuy stopped while {phase}{http}: {message}"
            if not payment_started and "No payment" not in message:
                message += " No payment was sent."
            logger.info(
                "Autobuy stopped item=%s search=%s phase=%s reason=%s http=%s",
                item_id,
                row.get("query_id"),
                phase,
                reason,
                exc.status,
            )
            record(
                item_id,
                "unknown" if payment_started else "failed_before_payment",
                (
                    "Payment result is unconfirmed. Check Vinted before retrying."
                    if payment_started
                    else message
                ),
            )
        except Exception as exc:  # noqa: BLE001 -- preserve uncertain payments
            reason = "unexpected_error"
            logger.warning(
                "Autobuy error item=%s phase=%s error=%s",
                item_id,
                phase,
                type(exc).__name__,
            )
            record(
                item_id,
                "unknown" if payment_started else "failed_before_payment",
                (
                    "Payment result is unconfirmed. Check Vinted before retrying."
                    if payment_started
                    else f"Unexpected checkout error while {phase}. No payment was sent."
                ),
            )
        finally:
            if client:
                client.session.close()
        return dict(result(item_id), reason=reason)


def check_payment(item_id):
    """Read a submitted payment once; never create a checkout or submit payment."""
    with buyer.exclusive():
        saved = result(item_id)
        if not saved or saved["state"] not in ("paying", "unknown", "needs_action"):
            return saved
        purchase_id = saved.get("checkout_id")
        if not isinstance(purchase_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,100}", purchase_id
        ):
            return saved
        client = buyer.connected_client()
        try:
            data = client.request(
                "GET", f"/api/v2/purchases/{purchase_id}/checkout/payment"
            )
            payment = data.get("payment")
            if not isinstance(payment, dict):
                return saved
            status = payment.get("status")
            if status in ("success", "completed"):
                total = saved.get("total")
                record(
                    item_id,
                    "paid",
                    (
                        f"Paid £{total/100:.2f}. Check your Vinted purchases."
                        if isinstance(total, int)
                        else "Vinted confirmed payment. Check your purchases."
                    ),
                )
            elif status in ("failure", "failed"):
                record(
                    item_id,
                    "payment_failed",
                    "Vinted reports that payment failed. Check your Vinted purchases before buying again.",
                )
            elif status in ("pending", "preparing", "requires_action"):
                action = payment_action_url(data) or saved.get("action_url")
                record(
                    item_id,
                    "needs_action",
                    "Payment is awaiting Vinted or bank confirmation. Check again after completing any confirmation; do not buy again.",
                    action_url=action,
                )
            return result(item_id)
        finally:
            client.session.close()


def feedback_buttons(row, feedback=None):
    if not feedback:
        return [[InlineKeyboardButton("Autobuy", callback_data="buy:click")]]
    state = feedback.get("state")
    reason = feedback.get("reason")
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
    if state in ("setup_required", "failed_before_payment"):
        label = {
            "budget_missing": f"No total budget on search #{row.get('query_id')} · details",
            "disabled": "Autobuy is off · details",
            "not_connected": "Buyer not connected · details",
            "search_inactive": "Search is inactive · details",
            "item_sold": "Already sold · details",
            "item_reserved": "Item reserved · details",
            "item_closed": "Listing closed · details",
            "item_unavailable": "Listing unavailable · details",
            "price_increased": "Item price increased · details",
            "total_over_budget": "Over budget with fees & delivery · details",
            "item_over_budget": "Item exceeds total budget · details",
            "item_over_url_limit": "Item exceeds URL price limit · details",
            "url_limit_invalid": "Check search URL price limit · details",
            "budget_invalid": "Check saved total budget · details",
            "unreadable": "Vinted response unreadable · details",
            "network": "Vinted connection error · details",
            "security_challenge": "Vinted security check required · details",
            "rate_limited": "Vinted cooldown required · details",
            "credentials": "Vinted session expired · details",
            "unexpected_error": "Checkout error · details",
        }.get(reason, label)
    buttons = [[InlineKeyboardButton(label, callback_data="buy:status")]]
    if state == "preparing":
        buttons.append(
            [InlineKeyboardButton("Check / resume Autobuy", callback_data="buy:click")]
        )
    elif state in ("setup_required", "failed_before_payment") and reason not in (
        "item_sold",
        "item_closed",
        "security_challenge",
    ):
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
                        f"Search #{row['query_id']} budget ↗",
                        url=base + "/search/" + str(row["query_id"]),
                    )
                )
            setup.append(
                InlineKeyboardButton(
                    "Buyer settings ↗", url=base + "/connections#vinted-buying"
                )
            )
            buttons.append(setup)
    elif state == "needs_action" and payment_action_url(
        {"action": {"parameters": {"url": feedback.get("action_url")}}}
    ):
        buttons.append(
            [
                InlineKeyboardButton(
                    "Confirm payment / bank ↗", url=feedback["action_url"]
                )
            ]
        )
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

    saved = photo_cards.load("vinted", query.message.message_id)
    if not saved:
        return
    row, details, card = saved
    feedback = {
        key: outcome.get(key)
        for key in ("state", "message", "checkout_id", "total", "reason", "action_url")
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
        caption, note_pages = photo_cards.captions(row, details)
        if note_page is not None and note_pages:
            note_page = min(note_page, len(note_pages) - 1)
            view = f"notes:{note_page}"
            caption = photo_cards.notes_caption(details, note_page, note_pages)
        elif note_page is not None:
            note_page, view = None, "listing"
        kwargs = dict(
            chat_id=str(query.message.chat.id),
            message_id=query.message.message_id,
            parse_mode="HTML",
            reply_markup=photo_cards.markup(
                row, details, view=view, note_page=note_page
            ),
            **photo_cards.TIMEOUTS,
        )
        try:
            if card.get("listing_file_id") or card.get("example_file_id"):
                await bot.edit_message_caption(
                    **kwargs, caption=caption, show_caption_above_media=True
                )
            else:
                await bot.edit_message_text(
                    **kwargs,
                    text=caption,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
        photo_cards.after_edit("vinted", query.message.message_id, view=view)
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
        if previous and previous["state"] in ("paying", "unknown", "needs_action"):
            await photo_cards.answer(query, "Checking the payment already submitted…")
            try:
                refreshed = await asyncio.to_thread(check_payment, row["item_id"])
            except buyer.BuyerError as exc:
                refreshed = dict(
                    previous, message="Payment status could not be checked. " + str(exc)
                )
            except Exception:  # noqa: BLE001 -- keep the saved uncertain payment
                refreshed = dict(
                    previous,
                    message="Payment status could not be checked. Check your Vinted purchases before retrying.",
                )
            async with photo_cards.lock("vinted", query.message.message_id):
                await show_feedback(
                    context.bot, query, row, details, card, refreshed or previous
                )
            return
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
    if previous and previous["state"] not in ("failed_before_payment", "preparing"):
        await photo_cards.answer(query, previous["message"][:190], alert=True)
        async with photo_cards.lock("vinted", query.message.message_id):
            await show_feedback(context.bot, query, row, details, card, previous)
        return
    # Check local setup BEFORE answering the callback. Telegram must receive the
    # reason as its first answer, not a second popup after an acknowledgement.
    try:
        ready(row)
    except buyer.BuyerError as exc:
        await photo_cards.answer(query, str(exc)[:190], alert=True)
        async with photo_cards.lock("vinted", query.message.message_id):
            await show_feedback(
                context.bot,
                query,
                row,
                details,
                card,
                {"state": "setup_required", "message": str(exc), "reason": exc.reason},
            )
        return
    await photo_cards.answer(query, "Preparing your Vinted checkout…")
    async with photo_cards.lock("vinted", query.message.message_id):
        try:
            outcome = await asyncio.to_thread(buy, row)
        except buyer.BuyerError as exc:
            outcome = {
                "state": "setup_required",
                "message": str(exc),
                "reason": exc.reason,
            }
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
