"""Identify explicit non-sale instructions on the public title without interpreting seller prose."""

import re
import unicodedata

# A bare notice must be its own label at the start/end or separated by punctuation.
# A named print, book or song (for example "Not for Sale T-shirt") can be a real item.
_BOUNDARY = r"[\s]*[()\[\]|:;!.,–—-][\s]*"
_NOTICE = r"(?:(?:please|pls)\s+)?(?:(?:do\s+not|don['’]?t)\s+(?:buy|purchase)(?:\s+(?:(?:this|the|my)\s+)?(?:item|listing|post))?|(?:this\s+(?:item|listing)\s+is\s+)?(?:not|no\s+longer)\s+for\s+sale)"
_SEPARATE_NOTICE = re.compile(rf"(?:^|{_BOUNDARY}){_NOTICE}(?:$|{_BOUNDARY})", re.IGNORECASE)
_EXPLICIT_END = re.compile(rf"\s+{_NOTICE}[!.)\]]*$", re.IGNORECASE)
_REQUEST = re.compile(r"^(?:in search of|looking to buy|wanted to buy|want to buy|would like to buy)\b", re.IGNORECASE)
_REQUEST_TAG = re.compile(r"^(?:iso|wanted|wtb)\s*[:|–—-]\s*\S|(?:^|[\[(])\s*(?:iso|wanted|wtb)\s*[\])]\s*$", re.IGNORECASE)
_REQUEST_GARMENT = re.compile(
    r"^(?:ISO|WTB|wanted)\s+(?:(?:a|an|some)\s+)?(?:jeans|skirt|dress|jacket|coat|shirt|hoodie|trainers|boots|handbag|jumper)\b",
    re.IGNORECASE,
)


def not_for_sale(item):
    title = item.get("title")
    if not isinstance(title, str):
        return False
    title = " ".join(unicodedata.normalize("NFKC", title).split())
    if _SEPARATE_NOTICE.search(title) or _EXPLICIT_END.search(title) or _REQUEST.search(title):
        return True
    # Protect commercial product titles beginning with their actual ISO/Wanted brand.
    brand = str(item.get("brand") or "").strip().casefold()
    if brand and brand in {"iso", "wanted", "wtb"} and title.casefold().startswith(brand + " "):
        return False
    return bool(_REQUEST_TAG.search(title) or _REQUEST_GARMENT.search(title))
