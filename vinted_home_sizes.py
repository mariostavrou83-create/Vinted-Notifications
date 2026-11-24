"""Size exclusions for UK women's clothing in the Home feed."""

import re

_RANGE = r"\d{1,2}(?:\s*(?:[-–—/,&]|to|or)\s*\d{1,2})*"
_LABEL = r"(?:xs|[sml]|(?:extra\s+)?small|medium|large)"
_REGIONAL = re.compile(rf"\b(UK|GB|EU|US|AU)\s*[:=]?\s*({_RANGE})(?![\w.])", re.I)
_NUMERIC = re.compile(rf"^(?:{_LABEL}\s*[/,(]?\s*)?({_RANGE})(?:\s*\))?$", re.I)
_TITLE_SIZE = re.compile(
    rf"\bsize\s*[:=]?\s*(?:{_LABEL}\s*[/,(]?\s*)?({_RANGE})\b(?!\.\d)",
    re.I,
)
_XL = re.compile(
    r"\b(?:(?:[1-9]\s*)?x+l|[2-9]x|(?:double\s+|triple\s+)?extra[ -]*large|x+[ -]*large)\b",
    re.I,
)
_MAX = {"UK": 14, "GB": 14, "EU": 42, "US": 10, "AU": 14}


def _largest(sizes):
    return max(map(int, re.findall(r"\d+", sizes)))


def over_limit(size_title, title=""):
    """Leave missing sizes and one-size listings eligible."""
    label = size_title.strip() if isinstance(size_title, str) else ""
    title = title if isinstance(title, str) else ""
    if _XL.search(label) or _XL.search(title):
        return True
    regions = _REGIONAL.findall(label)
    # An explicit UK size is the most precise evidence for a mixed label.
    british = [(region, sizes) for region, sizes in regions if region.upper() in ("UK", "GB")]
    if any(_largest(sizes) > _MAX[region.upper()] for region, sizes in british or regions):
        return True
    if not regions:
        if label.casefold() in ("l", "large"):  # Vinted UK's Large is UK 16–18.
            return True
        numeric = _NUMERIC.fullmatch(label)
        if numeric and _largest(numeric[1]) > 14:
            return True
    return any(
        _largest(sizes) > _MAX[region.upper()] for region, sizes in _REGIONAL.findall(title)
    ) or any(_largest(sizes) > 14 for sizes in _TITLE_SIZE.findall(title))
