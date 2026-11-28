"""Validation for dashboard form fields and uploaded reference pictures."""
import re
from datetime import time
from decimal import Decimal
from io import BytesIO
from urllib.parse import urlsplit

from PIL import Image, ImageOps, UnidentifiedImageError

from .db import DEFAULT_SETTINGS


class InputError(ValueError):
    pass


def text_field(value, limit, label):
    if not isinstance(value, str) or len(value) > limit:
        raise InputError(f"Enter a valid {label} (up to {limit} characters).")
    return value.strip()


def text_list(value, label, max_items=80):
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list) or len(value) > max_items:
        raise InputError(f"Enter up to {max_items} values for {label}.")
    words = [text_field(item, 150, label) for item in value]
    return list(dict.fromkeys(word for word in words if word))


def money(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,10}(?:\.[0-9]{1,2})?", str(value)):
        raise InputError("Enter a non-negative amount in pounds with up to two decimal places.")
    return int(Decimal(str(value)) * 100)


def boolean(value):
    if value is True or value in ("1", "on", "true"):
        return True
    if value is False or value in ("0", "off", "false"):
        return False
    raise InputError("Choose on or off.")


def search_url(value):
    value = text_field(value, 4096, "Vinted search link")
    try:
        p = urlsplit(value)
        valid = (
            p.scheme == "https" and p.hostname in {"vinted.co.uk", "www.vinted.co.uk"}
            and p.netloc.lower() in {"vinted.co.uk", "www.vinted.co.uk"}
            and re.fullmatch(r"/catalog(?:/[^?#\\]*)?", p.path)
            and "\\" not in value and not any(ord(c) < 32 for c in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise InputError("Paste a Vinted UK catalogue search link, including its filters.")
    return value


def search_fields(data, previous):
    def field(name, default=""):
        return data.get(name, previous.get(name, default))
    name = text_field(field("name"), 140, "search name")
    if not name:
        raise InputError("Give this search a name.")
    return {
        "name": name,
        "url": search_url(field("url")),
        "include_keywords": text_list(field("include_keywords", []), "keywords", 20),
        "exclude_keywords": text_list(field("exclude_keywords", []), "excluded words"),
        "budget_pence": money(field("budget")),
        "enabled": boolean(field("enabled", True)),
    }


def updated_settings(current, data, *, size_limit=80):
    if not isinstance(data, dict) or set(data) - DEFAULT_SETTINGS.keys():
        raise InputError("One or more preference fields are invalid.")
    result = current.copy()
    for key, value in data.items():
        if key in ("categories", "sizes", "allowed_couriers"):
            result[key] = text_list(value, key, max_items=size_limit if key == "sizes" else 80)
        elif key in ("max_total", "max_shipping", "max_item_price"):
            amount = money(value)
            result[key] = "" if amount is None else f"{amount // 100}.{amount % 100:02d}"
        elif key == "allow_same_shop_fallback":
            result[key] = boolean(value)
        elif key in ("quiet_start", "quiet_end"):
            if value == "":
                result[key] = ""
            elif isinstance(value, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                result[key] = time.fromisoformat(value).strftime("%H:%M")
            else:
                raise InputError("Enter a valid quiet-hours time.")
        elif key == "delivery_mode":
            if value not in ("pickup", "home"):
                raise InputError("Choose pickup or home delivery.")
            result[key] = value
        else:
            result[key] = text_field(value, 220, key.replace("_", " "))
    if bool(result["quiet_start"]) != bool(result["quiet_end"]):
        raise InputError("Enter both quiet-hour times or clear both.")
    return result


def reference_photo(upload):
    source = upload.stream.read(5 * 1024 * 1024 + 1)
    if len(source) > 5 * 1024 * 1024:
        raise InputError("Reference pictures must be 5 MB or smaller.")
    try:
        with Image.open(BytesIO(source), formats=["JPEG", "PNG", "WEBP"]) as im:
            if im.width * im.height > 18_000_000:
                raise InputError("Reference picture is too large.")
            im.thumbnail((1200, 1200))
            pic = ImageOps.exif_transpose(im).convert("RGB")
            output = BytesIO()
            pic.save(output, format="JPEG", quality=86)
            return output.getvalue()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise InputError("Use a valid JPEG, PNG or WebP picture.") from exc
