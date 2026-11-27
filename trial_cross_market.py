"""Guide based on close independently verified UK alternatives across different marketplaces."""
import re
from urllib.parse import urlsplit

from trial_price_rules import RECENT_SECONDS, field, grade, pence, product_mismatch, timestamp, words
from trial_style_evidence import assess


def _link(row, source):
    try:
        url = urlsplit(row.get("url") or "")
        host, path = url.netloc.lower(), url.path.rstrip("/")
        if url.scheme != "https":
            return None
        if source == "vinted_uk" and host in {"vinted.co.uk", "www.vinted.co.uk"}:
            match = re.fullmatch(r"/items/(\d+)(?:-[^/]*)?", path)
            if match:
                return "https://www.vinted.co.uk/items/" + match[1]
        if source.startswith("ebay_uk_") and host in {"ebay.co.uk", "www.ebay.co.uk"}:
            match = re.fullmatch(r"/itm/(?:[^/]+/)?(\d{9,15})", path)
            if match:
                return "https://www.ebay.co.uk/itm/" + match[1]
        if source == "depop_uk" and host in {"depop.com", "www.depop.com"} and re.fullmatch(r"/products/[\w-]+", path):
            return "https://www.depop.com" + path + "/"
    except (ValueError, TypeError):
        pass
    return None


def _duplicate(c, other):
    if c["url"] == other["url"] or c["seller"] == other["seller"]:
        return True
    if c["market"] == other["market"]:
        return False
    # Treat matching public seller handles and nearly identical titles across venues
    # conservatively as one cross-listed item, even where asking prices differ.
    if (c["handle"] and c["handle"] == other["handle"]) or (c["image"] and c["image"] == other["image"]):
        return True
    a, b = set(words(c["title"]).split()), set(words(other["title"]).split())
    return bool(a and b and len(a & b) / len(a | b) >= .8)


def guide(target, sources, offers, direct_ids, now):
    result = {"status": "insufficient", "low_pence": None, "high_pence": None, "evidence": []}
    if target.get("active") is False or not target.get("seller_id") or not grade(target) or not field(target, "size") or not field(target, "brand"):
        return result
    qualified = []
    candidates = [("vinted_uk", v) for v in offers if str(v.get("id")) in direct_ids]
    for source in ("ebay_uk_sold", "vinted_uk", "depop_uk", "ebay_uk_active"):
        group = sources.get(source) or {}
        if group.get("status") == "checked":
            candidates.extend((source, v) for v in group.get("rows", []) if isinstance(v, dict))
    for source, item in candidates:
        sold = source == "ebay_uk_sold"
        kind = "sold" if sold else "ask"
        price = item.get("sold_pence" if sold else "ask_pence")
        seller, url, observed = str(item.get("seller_id") or "").strip(), _link(item, source), timestamp(item.get("observed_at"))
        if (not seller or not url or not observed or not 0 <= now - observed <= RECENT_SECONDS or
                not pence(price) or not price or item.get("currency") != "GBP" or
                words(item.get("country")) not in {"uk", "gb", "united kingdom"} or
                item.get("price_kind", "ask") != kind or
                (sold and item.get("sold_amount_confirmed") is not True) or
                (not sold and item.get("active") is not True)):
            continue
        if source == "vinted_uk" and (str(item.get("id")) == str(target.get("id")) or
                                      seller.split(":")[-1] == str(target.get("seller_id"))):
            continue
        if (not grade(item) or grade(item) != grade(target) or
                field(item, "brand") != field(target, "brand") or field(item, "size") != field(target, "size") or
                product_mismatch(target, item, rank_style=True)):
            continue
        tier, reason = assess(target, item, grade(target), grade(item))
        if tier != "close":
            continue
        market = "ebay" if source.startswith("ebay") else source.split("_")[0]
        qualified.append({"source": source, "market": market, "price_kind": kind, "price_pence": price,
                          "title": item["title"], "reason": reason, "url": url,
                          "seller": (market, seller.split(":")[-1].casefold()),
                          "handle": str(item.get("public_seller_handle") or "").casefold(),
                          "image": str(item.get("image_url") or "").split("?")[0]})
    # Dedup before counting sources, preferring achieved sales over an older active copy.
    qualified.sort(key=lambda row: (row["price_kind"] != "sold", row["source"] != "vinted_uk", row["price_pence"], row["url"]))
    selected = []
    for candidate in qualified:
        if not any(_duplicate(candidate, kept) for kept in selected):
            selected.append(candidate)
    sold = [row for row in selected if row["price_kind"] == "sold"]
    basis_rows = sold if len(sold) >= 2 else selected
    bound_rows = [min(basis_rows, key=lambda r: r["price_pence"]), max(basis_rows, key=lambda r: r["price_pence"])] if basis_rows else []
    shown = list({r["url"]: r for r in [*bound_rows, *selected]}.values())[:8]
    result["evidence"] = [{k: row[k] for k in ("source", "url", "title", "price_kind", "price_pence", "reason")} for row in shown]
    if len(selected) < 2 or not any(i["source"] != "vinted_uk" for i in selected):
        return result
    sold_prices = [i["price_pence"] for i in selected if i["price_kind"] == "sold"]
    basis = sold_prices if len(sold_prices) >= 2 else [i["price_pence"] for i in selected]
    low, high = min(basis), max(basis)
    if high > low * 2:
        result["explanation"] = "Prices on the closest listings vary by more than twofold."
        return result
    result.update(status="estimated", low_pence=low, high_pence=high,
                  sold_count=len(sold_prices), sold_high_pence=max(sold_prices) if sold_prices else None,
                  explanation=("Range of confirmed eBay UK sale amounts for close alternatives." if len(sold_prices) >= 2
                               else "Range of comparable UK prices; each listing identifies an ask or a confirmed sale."))
    return result
