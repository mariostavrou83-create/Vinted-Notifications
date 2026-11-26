"""Prefer InPost and verify the configured physical collection shop for every courier."""

import os
import re


def required_point():
    """The owner's pickup location belongs in private service configuration."""
    target = {
        field: os.getenv("MSJ_INPOST_POINT_" + field.upper(), "").strip()
        for field in ("code", "name", "postcode")
    }
    for field in ("street_address", "street_context", "alternate_name"):
        value = os.getenv("MSJ_PICKUP_" + field.upper(), "").strip()
        if value:
            target[field] = value
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


_POSTCODE = re.compile(r"\b(?:GIR\s*0AA|[A-Z]{1,2}[0-9][A-Z0-9]?\s*[0-9][A-Z]{2})\b", re.I)
_ADDRESS_LINES = (
    "address", "address_line", "address_line1", "address_line2", "address_line_1",
    "address_line_2", "address_line_3", "line1", "line2", "line_1", "line_2",
    "full_address", "formatted_address", "street_address",
)
_ROAD_SUFFIXES = {
    "rd": "road", "st": "street", "ave": "avenue", "ln": "lane", "dr": "drive",
    "cl": "close", "cres": "crescent", "ct": "court", "pl": "place", "gdns": "gardens",
    "pde": "parade", "ter": "terrace", "sq": "square", "wy": "way",
}
_SUBUNIT = re.compile(r"\b(?:unit|suite|flat|apartment|stall|room|floor|shop)\s*(?:no\.?\s*)?(?:[0-9]+[a-z]?\b|[a-z]\b)", re.I)


def _address_records(point):
    address = point.get("address")
    return (point, address) if isinstance(address, dict) else (point,)


def _postcodes(point):
    values = []
    for record in _address_records(point):
        for key in ("postcode", "postal_code", "post_code", "zip_code"):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                values.append(value)
        for key in (*_ADDRESS_LINES, "street", "street_name", "name"):
            value = record.get(key)
            if isinstance(value, str):
                values.extend(_POSTCODE.findall(value))
    return {"".join(value.upper().split()) for value in values}


def _street_words(value):
    words = _words(value).split()
    if words:
        words[-1] = _ROAD_SUFFIXES.get(words[-1], words[-1])
    return " ".join(words)


def _street_status(target, point):
    """True for an exact numbered street, False on conflict, None for insufficient detail."""
    configured = target.get("street_address")
    if not configured:
        return None
    expected = _street_words(configured)
    house, _, street = expected.partition(" ")
    context = _street_words(target.get("street_context"))
    if not re.fullmatch(r"[0-9]+[a-z]?", house) or not street:
        return False
    numbers, streets, numbered_lines = set(), set(), set()
    context_as_street = False
    for record in _address_records(point):
        for key in ("house_number", "street_number", "building_number"):
            value = record.get(key)
            if type(value) in (str, int) and str(value).strip():
                numbers.add(str(value).strip().lower())
        if any(record.get(key) for key in ("unit", "unit_number", "flat", "flat_number", "suite",
                                         "apartment_number", "sub_building")):
            return False
        for key in (*_ADDRESS_LINES, "street", "street_name", "name"):
            value = record.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            for part in re.split(r"[,;\n]|\s+[-–—]\s+", _POSTCODE.sub("", value)):
                if _SUBUNIT.search(part):
                    return False
                line = _street_words(part)
                if context and line == context:
                    if key in ("street", "street_name"):
                        context_as_street = True
                    continue
                if context and line.endswith(" " + context):
                    line = _street_words(line[:-(len(context) + 1)])
                if key == "name" and line and not (line.endswith(" " + street) or
                                                   line.split()[-1] in _ROAD_SUFFIXES.values()):
                    continue  # A service label such as "24/7 Locker" is not a street.
                if re.match(r"^\d", line):
                    numbered_lines.add(line)
                elif line and (key in ("street", "street_name") or
                               (key != "name" and line.split()[-1] in _ROAD_SUFFIXES.values())):
                    streets.add(line)
    if numbers - {house} or streets - {street} or numbered_lines - {expected}:
        return False
    if context_as_street and numbers and not (street in streets or expected in numbered_lines):
        return False
    return True if expected in numbered_lines or (house in numbers and street in streets) else None


def _target_name(target, point, operator):
    name = _shop_name(point.get("name"), operator)
    return bool(name) and name in {
        _shop_name(target.get("name"), "inpost"),
        _shop_name(target.get("alternate_name"), operator),
    }


def unavailable(*records):
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("restriction") or any(
            record.get(key) is False for key in ("available", "is_available", "enabled")
        ):
            return True
    return False


def conflicts_with_target(target, point, *details, selected_carrier="", fallback=False, selected_point=None):
    postcodes = _postcodes(point)
    expected = "".join(target["postcode"].upper().split())
    current = carrier(point, *details)
    street = _street_status(target, point)
    if (
        unavailable(point, *details)
        or current is None
        or (current and selected_carrier and current != selected_carrier)
        or (postcodes and postcodes != {expected})
        or street is False
    ):
        return True
    operator = current or selected_carrier
    if not fallback or not point.get("name") or _target_name(target, point, operator):
        return False
    if street is True and postcodes == {expected}:
        return False
    # Checkout sometimes echoes the service's alias but omits its full address.
    # Only carry forward the evidence for exactly the point, code and rate selected.
    if selected_point and all(point.get(k) and point[k] == selected_point.get(k) for k in ("uuid", "code", "rate_uuid")):
        if _shop_name(point["name"], operator) == _shop_name(selected_point.get("name"), operator):
            return not (_street_status(target, selected_point) is True and _postcodes(selected_point) == {expected})
    return True


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
    postcode = "".join(target["postcode"].upper().split())
    location = bool(_POSTCODE.fullmatch(postcode)) and _postcodes(point) == {postcode} and (
        _target_name(target, point, operator) or _street_status(target, point) is True
    )
    if not location or (not operator and not rate.get("rate_uuid")):
        return None
    return (1 if operator == "inpost" else 2), operator


def matches(point, rate, target, *, entry=None):
    return choice(point, rate, target, entry=entry) is not None
