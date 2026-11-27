"""Public presentation fields for the Finder sourcing feed."""

import math
import re
from datetime import datetime
from urllib.parse import urlsplit


def vinted_photo_url(value):
    """Allow only HTTPS image files on Vinted's listing image hosts."""
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\x00-\x20\x7f\\]", value):
        return None
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    if not (
        url.scheme == "https"
        and re.fullmatch(r"images\d{0,3}\.vinted\.net", url.netloc, re.IGNORECASE)
        and re.fullmatch(
            r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\.(?:jpe?g|png|webp|avif)", url.path, re.IGNORECASE
        )
        and not url.fragment
    ):
        return None
    return value


def display_text(value, max_length=80):
    if not isinstance(value, str) or len(value) > max_length or any(ord(ch) < 32 for ch in value):
        return None
    return value.strip() or None


def published_time(item, origin, discovered):
    """Visible relative labels give an age bound, never an exact posting time."""
    raw = item.get("listed_at")
    age = item.get("age_text")
    try:
        if origin in {"browser", "browser_home", "browser_search"}:
            dt = datetime.fromisoformat(age) if isinstance(age, str) else None
            if (
                not dt
                or not dt.tzinfo
                or type(raw) not in (int, float)
                or int(dt.timestamp()) != int(raw)
            ):
                return None
        elif item.get("posted_at_exact") is False:
            return None
        if isinstance(raw, str):
            dt = datetime.fromisoformat(raw)
            raw = dt.timestamp() if dt.tzinfo else None
        if type(raw) in (int, float) and math.isfinite(raw) and 0 < raw <= discovered:
            return int(raw)
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return None


def display_fields(item, origin, discovered):
    posted = published_time(item, origin, discovered)
    age = display_text(item.get("age_text"), 64) if posted is None else None
    if not age or not re.fullmatch(
        r"(?:just now|(?:a|one) minute ago|\d{1,5} (?:second|minute)s? ago)", age, re.I
    ):
        age = None
    return {
        "photo_url": vinted_photo_url(item.get("photo_url")),
        "brand": display_text(item.get("brand")),
        "condition": display_text(item.get("condition")),
        "size": display_text(item.get("size")),
        "search_name": display_text(item.get("search_name"), 140),
        "listed_at": posted,
        "listed_age_text": age,
        "discovered_at": discovered,
    }
