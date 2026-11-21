"""Prefer InPost and verify the configured physical collection shop for every courier."""

import os
import re


def required_point():
    """The owner's pickup location belongs in private service configuration."""
    target = {
        field: os.getenv("MSJ_INPOST_POINT_" + field.upper(), "").strip()
        for field in ("code", "name", "postcode")
    }
    return target if any(target.values()) else None


def required_message(target):
    name = target.get("name") or "your selected shop"
    return f"Collection at {name} is unavailable or could not be confirmed for this item. No payment was sent."


def _words(value):
    return (
        " ".join(re.findall(r"[a-z0-9]+", value.lower()))
        if isinstance(value, str)
        else ""
    )


def _carriers(*records):
    names = []
    for record in records:
        if not isinstance(record, dict):
            continue
        for key in (
            "carrier",
            "carrier_code",
            "carrier_name",
            "shipping_provider",
            "provider_name",
        ):
            value = record.get(key)
            if isinstance(value, dict):
                names.extend(value.get(k) for k in ("code", "name", "title"))
            else:
                names.append(value)
    return [
        _words(n).replace("in post", "inpost")
        for n in names
        if isinstance(n, str) and n.strip()
    ]


# Names and service labels used by UK pickup providers. Other named couriers
# can qualify as well through the exact shop-name and postcode comparison.
PROVIDERS = {
    "inpost": r"\binpost\b",
    "royal mail": r"\b(?:royal mail|royalmail)\b",
    "dpd": r"\bdpd\b",
    "evri": r"\b(?:evri|hermes)\b",
    "yodel": r"\byodel\b",
    "dhl": r"\bdhl\b",
    "ups": r"\bups\b",
    "relay": r"\brelay\b",
}
SERVICE_WORDS = {
    "uk",
    "gb",
    "pickup",
    "parcel",
    "parcels",
    "shop",
    "parcelshop",
    "locker",
    "pudo",
    "collection",
    "delivery",
}


def carrier(*records):
    """Return one consistent pickup operator, an empty unknown, or None on conflict."""
    operators = set()
    names = _carriers(*records)
    options = [
        _words(record.get(k))
        for record in records
        if isinstance(record, dict)
        for k in (
            "delivery_type",
            "shipping_type",
            "service_type",
            "delivery_method",
            "service_name",
        )
    ]
    if any(
        re.search(r"\bhome\b|\bdoor to door\b|\bdoor delivery\b", value)
        for value in names + options
    ):
        return None
    for value in names:
        matched = {
            name for name, pattern in PROVIDERS.items() if re.search(pattern, value)
        }
        if not matched:
            label = " ".join(
                word for word in value.split() if word not in SERVICE_WORDS
            )
            if label:
                matched.add(label)
        operators.update(matched)
    return next(iter(operators), "") if len(operators) <= 1 else None


def _shop_name(value, operator):
    name = _words(value).replace("in post", "inpost")
    if not name or not operator:
        return name
    operators = {operator}
    if operator == "evri":
        operators.add("hermes")
    service = r"(?: (?:parcelshop|parcel shop|pickup point|pickup|access point|collection point|shop|locker|parcel locker))?"
    for label in operators:
        prefix = re.escape(label) + service
        name = re.sub("^" + prefix + " ", "", name)
        name = re.sub(" " + prefix + "$", "", name)
    return name


def _postcodes(point):
    address = point.get("address")
    records = (point, address) if isinstance(address, dict) else (point,)
    values = []
    for record in records:
        for key in ("postcode", "postal_code", "post_code", "zip_code"):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                values.append(value)
        for key in ("address", "address_line", "full_address"):
            value = record.get(key)
            if isinstance(value, str):
                values.extend(
                    re.findall(
                        r"\b[A-Z]{1,2}[0-9][A-Z0-9]?\s*[0-9][A-Z]{2}\b", value.upper()
                    )
                )
    return {"".join(value.upper().split()) for value in values}


def unavailable(*records):
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("restriction") or any(
            record.get(key) is False for key in ("available", "is_available", "enabled")
        ):
            return True
    return False


def conflicts_with_target(target, point, *details, selected_carrier="", fallback=False):
    postcodes = _postcodes(point)
    expected = "".join(target["postcode"].upper().split())
    current = carrier(point, *details)
    if (
        unavailable(point, *details)
        or current is None
        or (current and selected_carrier and current != selected_carrier)
        or (postcodes and postcodes != {expected})
    ):
        return True
    return bool(
        fallback
        and point.get("name")
        and _shop_name(point["name"], current or selected_carrier)
        != _shop_name(target["name"], "inpost")
    )


def choice(point, rate, target, *, entry=None):
    """Return preference and operator only for a validated collection point."""
    if not all(
        target.get(k) for k in ("code", "name", "postcode")
    ) or conflicts_with_target(target, point, entry, rate):
        return None
    operator = carrier(point, entry, rate)
    code_matches = any(
        isinstance(value, str) and value.strip().casefold() == target["code"].casefold()
        for value in (
            point.get("code"),
            point.get("external_code"),
            point.get("external_id"),
        )
    )
    if code_matches and operator in ("", "inpost"):
        return 0, "inpost"
    location = _shop_name(point.get("name"), operator) == _shop_name(
        target["name"], "inpost"
    ) and bool(_postcodes(point))
    if not location or (not operator and not rate.get("rate_uuid")):
        return None
    return (1 if operator == "inpost" else 2), operator


def matches(point, rate, target, *, entry=None):
    return choice(point, rate, target, entry=entry) is not None
