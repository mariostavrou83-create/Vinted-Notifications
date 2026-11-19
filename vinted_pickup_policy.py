"""Require a specific InPost collection point when configured by the owner."""

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
    return f"InPost collection at {name} is unavailable or could not be confirmed for this item. No payment was sent."


def _words(value):
    return " ".join(re.findall(r"[a-z0-9]+", value.lower())) if isinstance(value, str) else ""


def _carriers(*records):
    names = []
    for record in records:
        if not isinstance(record, dict):
            continue
        for key in ("carrier", "carrier_code", "carrier_name", "shipping_provider", "provider_name"):
            value = record.get(key)
            if isinstance(value, dict):
                names.extend(value.get(k) for k in ("code", "name", "title"))
            else:
                names.append(value)
    return [_words(n).replace("in post", "inpost") for n in names if isinstance(n, str) and n.strip()]


def conflicting_carrier(*records):
    for name in _carriers(*records):
        if "inpost" not in name.split() or re.search(r"\b(home|relay|royal mail|dpd|evri|yodel)\b", name):
            return True
    return False


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
                values.extend(re.findall(r"\b[A-Z]{1,2}[0-9][A-Z0-9]?\s*[0-9][A-Z]{2}\b", value.upper()))
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


def conflicts_with_target(target, point, *details):
    postcodes = _postcodes(point)
    expected_postcode = "".join(target["postcode"].upper().split())
    return (
        unavailable(point, *details)
        or conflicting_carrier(point, *details)
        or bool(postcodes and postcodes != {expected_postcode})
    )


def matches(point, rate, target, *, entry=None):
    if not all(target.get(k) for k in ("code", "name", "postcode")):
        return False
    if conflicts_with_target(target, point, entry, rate):
        return False
    postcodes = _postcodes(point)
    # Some checkouts use the operator's ID, others use a local marketplace ID.
    for key in ("code", "external_code", "external_id"):
        code = point.get(key)
        if isinstance(code, str) and code.strip().casefold() == target["code"].casefold():
            return True
    # Allow operator labels beside the name, but never a similarly named branch.
    name = _words(point.get("name")).replace("in post", "inpost")
    name = re.sub(r"^inpost(?: parcel)?(?: shop| locker)? ", "", name)
    name = re.sub(r" inpost(?: parcel)?(?: shop| locker)?$", "", name)
    return bool(_carriers(entry, point, rate) and postcodes and name == _words(target["name"]))
