"""Boolean-only build/load evidence; never skip a request or authorize payment."""

import copy
import logging
import re

logger = logging.getLogger(__name__)
REQUIRED_COMPONENTS = (
    "additional_service",
    "payment_method",
    "shipping_address",
    "shipping_pickup_options",
    "shipping_pickup_details",
)
FIELDS = (
    "build_complete",
    "loaded_complete",
    "build_checksum_present",
    "loaded_checksum_present",
    "build_required_components_present",
    "loaded_required_components_present",
    "build_item_matches",
    "loaded_item_matches",
    "same_checkout",
    "same_shipping_order",
    "same_address",
    "selected_required_state_equal",
    "checksum_changed",
)


def _numeric_id(value):
    return type(value) in (str, int) and re.fullmatch(r"[0-9]{1,24}", str(value))


def _checksum(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 8192
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
    )


def _evidence(checkout, item_id, item_price, maximum):
    evidence = {
        "complete": False,
        "checksum_present": False,
        "required_components_present": False,
        "item_matches": False,
        "checkout_id": None,
        "shipping_order": None,
        "address_id": None,
        "checksum": None,
        "state": None,
    }
    try:
        # Validators receive no live response object, even if later changed.
        value = copy.deepcopy(checkout)
        if not isinstance(value, dict):
            return evidence
        identity = value.get("id")
        if isinstance(identity, str) and re.fullmatch(
            r"[A-Za-z0-9_-]{1,100}", identity
        ):
            evidence["checkout_id"] = identity
        checksum = value.get("checksum")
        evidence["checksum_present"] = bool(_checksum(checksum))
        if evidence["checksum_present"]:
            evidence["checksum"] = checksum
        components = value.get("components")
        if not isinstance(components, dict):
            return evidence
        evidence["required_components_present"] = all(
            isinstance(components.get(key), dict) for key in REQUIRED_COMPONENTS
        )

        import vinted_buying as buying

        summary = components.get("order_summary_v2")
        button = components.get("pay_button_v2")
        if summary is None and isinstance(button, dict):
            summary = button.get("order_summary_v2")
        if not isinstance(summary, dict) or not isinstance(button, dict):
            return evidence
        item = buying.checkout_item_details(components, summary, item_id=item_id)
        evidence["item_matches"] = item["id"] == str(item_id)
        if not evidence["required_components_present"]:
            return evidence

        address_component = components["shipping_address"]
        address = address_component.get("address")
        shipping = components["shipping_pickup_details"]
        order = shipping.get("shipping_order_id")
        if _numeric_id(order):
            evidence["shipping_order"] = str(order)
        if isinstance(address, dict) and _numeric_id(address.get("id")):
            evidence["address_id"] = str(address["id"])
        if (
            evidence["checkout_id"] is None
            or evidence["shipping_order"] is None
            or evidence["address_id"] is None
            or address.get("is_complete") is not True
            or address.get("country_code") != "GB"
            or address_component.get("address_is_missing")
            or (
                address_component.get("shipping_order_id") is not None
                and str(address_component["shipping_order_id"]) != str(order)
            )
        ):
            return evidence
        options = components["shipping_pickup_options"]
        choices = options.get("pickup_options")
        details = shipping.get("pickup_details")
        if not isinstance(choices, dict) or not isinstance(details, dict):
            return evidence
        selected = options.get("selected_pickup_option")
        kinds = [
            kind
            for kind in ("home", "pickup")
            if isinstance(choices.get(kind), dict)
            and choices[kind].get("pickup_option_type") == selected
        ]
        if len(kinds) != 1 or not buying.choice_id(details.get("selected_rate_uuid")):
            return evidence
        if kinds[0] == "pickup":
            point = details.get("shipping_point")
            if not isinstance(point, dict) or not all(
                buying.choice_id(point.get(key))
                for key in ("uuid", "code", "rate_uuid")
            ):
                return evidence
            if point["rate_uuid"] != details["selected_rate_uuid"]:
                return evidence
            buying.coordinates(address.get("coordinates"))

        payment = components["payment_method"]
        methods = payment.get("pay_in_methods")
        cards = payment.get("cards")
        selected_method = payment.get("selected_payment_method")
        method = (
            selected_method.get("pay_in_method")
            if isinstance(selected_method, dict)
            else None
        )
        code = method.get("payment_method") if isinstance(method, dict) else None
        if (
            not isinstance(methods, list)
            or not isinstance(cards, list)
            or not any(
                isinstance(entry, dict)
                and entry.get("payment_method") == code
                and entry.get("enabled") is True
                for entry in methods
            )
        ):
            return evidence
        if code in ("card", "credit_card"):
            card = selected_method.get("credit_card")
            if (
                not isinstance(card, dict)
                or card.get("expired") is not False
                or not isinstance(card.get("last4"), str)
                or not re.fullmatch(r"[0-9]{4}", card["last4"])
            ):
                return evidence
            matching = [
                entry
                for entry in cards
                if isinstance(entry, dict)
                and entry.get("last4") == card["last4"]
                and entry.get("expired") is False
                and _numeric_id(entry.get("id"))
            ]
            if len(matching) != 1 or (
                card.get("id") is not None and str(card["id"]) != str(matching[0]["id"])
            ):
                return evidence
        elif code != "balance":
            return evidence

        buying.checkout_prices(value, item_price, maximum, item_id=item_id)
        amounts = buying.checkout_amounts(value)
        if code == "balance" and amounts["payment_due"] != 0:
            return evidence
        evidence["complete"] = bool(evidence["item_matches"])
        evidence["state"] = (
            {key: components[key] for key in REQUIRED_COMPONENTS},
            summary,
            button,
            components.get("shipping_contact"),
            components.get("item_presentation_escrow_v2"),
            amounts,
        )
    except Exception:  # noqa: BLE001,S110 -- evidence cannot affect buying
        pass
    return evidence


def observe_build(build_checkout, loaded_checkout, *, item_id, item_price, maximum):
    """Observe existing responses, returning/logging only fixed booleans.

    Completeness means the diagnostic's stricter current shapes and the existing
    price gate pass. It does not establish that initial loading is redundant.
    Equality is conservative: complete required component DTOs must match, not
    just their displayed totals. False can mean missing/unreadable evidence.
    """
    report = dict.fromkeys(FIELDS, False)
    try:
        build = _evidence(build_checkout, item_id, item_price, maximum)
        loaded = _evidence(loaded_checkout, item_id, item_price, maximum)
        for prefix, evidence in (("build", build), ("loaded", loaded)):
            for name in (
                "complete",
                "checksum_present",
                "required_components_present",
                "item_matches",
            ):
                report[f"{prefix}_{name}"] = bool(evidence[name])
        for name, key in (
            ("same_checkout", "checkout_id"),
            ("same_shipping_order", "shipping_order"),
            ("same_address", "address_id"),
        ):
            report[name] = bool(build[key] is not None and build[key] == loaded[key])
        report["selected_required_state_equal"] = bool(
            report["build_complete"]
            and report["loaded_complete"]
            and report["same_checkout"]
            and build["state"] == loaded["state"]
        )
        report["checksum_changed"] = bool(
            report["build_checksum_present"]
            and report["loaded_checksum_present"]
            and build["checksum"] != loaded["checksum"]
        )
    except Exception:  # noqa: BLE001 -- optional evidence fails closed
        report = dict.fromkeys(FIELDS, False)
    try:
        logger.info(
            "Vinted checkout build evidence: %s",
            " ".join(f"{key}={str(report[key]).lower()}" for key in FIELDS),
        )
    except Exception:  # noqa: BLE001,S110 -- logging cannot affect buying
        pass
    return report
