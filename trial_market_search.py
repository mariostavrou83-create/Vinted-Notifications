"""Read ordinary UK marketplace pages for bounded, on-demand resale comparisons."""

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
from lxml import etree, html

from resale_pricing import garment, pence, styles
from trial_price_rules import field, grade, product_mismatch, words
from trial_style_evidence import assess, same_garment_family

SITES = {
    "vinted_uk": ("www.vinted.co.uk", r"/items/([0-9]+)(?:-[^/]*)?/?"),
    "depop_uk": ("www.depop.com", r"/(?:gb/)?products/([a-z0-9][a-z0-9-]{1,250})/?"),
    "ebay_uk_active": ("www.ebay.co.uk", r"/itm/(?:[^/]+/)?([0-9]{9,18})/?"),
    "ebay_uk_sold": ("www.ebay.co.uk", r"/itm/(?:[^/]+/)?([0-9]{9,18})/?"),
}
CACHE_AGE = 900
MAX_REQUESTS = 192
MAX_DETAILS = 3
MAX_BYTES = 2_000_000
UA = "FinderMarketResearch/1.0"
NEGATIVE = {"outofstock", "soldout", "discontinued", "reserved", "sold", "unavailable"}
UK = {"uk", "gb", "gbr", "great britain", "united kingdom"}
CONDITION_LABELS = {
    "likenewcondition": "like new",
    "newcondition": "new",
    "refurbishedcondition": "",
}
MONEY = r"£\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{2})?)"

# Human-visible query terms, ordered from cut/construction to decorative details.
QUERY_TRAITS = (
    "puffer",
    "bomber",
    "biker",
    "trench",
    "fur collar",
    "cable knit",
    "lace trim",
    "argyle",
    "pinstripe",
    "plaid",
    "beading",
    "sequins",
    "embroidery",
    "pleated",
    "ruched",
    "asymmetric",
    "strapless",
    "cropped",
    "oversized",
    "zip front",
    "hooded",
    "belted",
    "distressed",
    "fitted",
    "pullover",
)
QUERY_ALIASES = {
    "fur collared": "fur collar",
    "cable knitted": "cable knit",
    "beaded": "beading",
}


def _query(target):
    brand = field(target, "brand")
    kind = garment(target.get("title")) or field(target, "model")
    title = words(target.get("title"))
    if not kind:
        # Product titles carry critical variants such as storage or lens type.
        details = [t for t in title.split() if t not in brand.split()][:10]
        return " ".join([brand, *details]).strip()[:150]
    for alias, term in QUERY_ALIASES.items():
        title = title.replace(alias, term)
    photo_traits = target.get("visible_traits")
    photo = (
        {
            str(t).lower()
            for t in photo_traits
            if isinstance(t, str) and t in QUERY_TRAITS
        }
        if isinstance(photo_traits, list)
        else set()
    )
    terms = [t for t in QUERY_TRAITS if t in photo or f" {t} " in f" {title} "]
    if not terms:
        terms = list(styles(title))
    size = field(target, "size")
    if size.isdigit():
        # A number without an explicit UK size designation could be an EU size.
        size = ("UK " + size) if words(target.get("size")).startswith("uk ") else ""
    elif size not in {"xxs", "xs", "s", "m", "l", "xl", "xxl", "xxxl"}:
        size = ""
    terms = [brand, kind, *terms[:2], size.upper() if size else ""]
    return (
        " ".join(dict.fromkeys(t for t in terms if t))[:150]
        or words(target.get("title"))[:100]
    )


def search_links(target):
    query = _query(target)
    ebay = {"_nkw": query, "LH_PrefLoc": "1"}
    return {
        "vinted_uk": "https://www.vinted.co.uk/catalog?"
        + urlencode({"search_text": query}),
        "depop_uk": "https://www.depop.com/gb/search/?" + urlencode({"q": query}),
        "ebay_uk_active": "https://www.ebay.co.uk/sch/i.html?" + urlencode(ebay),
        "ebay_uk_sold": "https://www.ebay.co.uk/sch/i.html?"
        + urlencode(dict(ebay, LH_Sold="1", LH_Complete="1")),
    }


def _canonical(source, link, base):
    host, pattern = SITES[source]
    try:
        parsed = urlsplit(urljoin(base, str(link or "")))
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc.lower() not in {
        host,
        host.removeprefix("www."),
    }:
        return None
    match = re.fullmatch(pattern, parsed.path, re.IGNORECASE)
    if not match:
        return None
    key = match[1]
    prefix = {"vinted_uk": "items", "depop_uk": "products"}.get(source, "itm")
    # Keep Vinted's ordinary linked slug; where the site supplies it Depop needs its trailing slash.
    path = parsed.path if source in {"vinted_uk", "depop_uk"} else f"/{prefix}/{key}"
    return key, f"https://{host}{path}"


def _text(node):
    return " ".join(" ".join(node.itertext()).split())


def _sold_label(text):
    if re.search(
        r"best offer|offer accepted|or best offer|original price|was £",
        text,
        re.IGNORECASE,
    ):
        return None
    actual = re.search(
        r"(?:sold for|winning bid)\s*[:\-]?\s*" + MONEY, text, re.IGNORECASE
    )
    date = re.search(
        r"\bsold\s+(?:on\s+)?(\d{1,2}\s+[a-z]{3,9}\s+\d{4})", text, re.IGNORECASE
    )
    if not actual or not date:
        return None
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            when = datetime.strptime(date[1], fmt).replace(tzinfo=UTC).timestamp()
            break
        except ValueError:
            continue
    else:
        return None
    if not 0 <= time.time() - when <= 90 * 86400:
        return None
    amount = pence(actual[1].replace(",", ""))
    return (amount, when) if amount else None


def _search_items(doc, source, url, target):
    seen = {}
    target_tokens = set(words(target.get("title")).split())
    for a in doc.xpath("//a[@href]"):
        link = _canonical(source, a.get("href"), url)
        if not link:
            continue
        cards = a.xpath(
            "ancestor::*[self::li or self::article or @data-testid='grid-item'][1]"
        )
        node = cards[0] if cards else a
        text = _text(node)
        alt = " ".join(a.xpath(".//img/@alt"))
        labels = f"{text} {alt} {a.get('title', '')} {a.get('aria-label', '')}"
        strike = node.xpath(
            ".//s|.//del|.//strike|.//*[contains(@style,'line-through') or contains(@class,'STRIKETHROUGH') or contains(@class,'strike-through')]"
        )
        sold = _sold_label(text) if source == "ebay_uk_sold" and not strike else None
        if source == "ebay_uk_sold" and not sold:
            continue
        score = len(target_tokens & set(words(labels).split()))
        if link[0] not in seen or score > seen[link[0]][0]:
            seen[link[0]] = (score, link, sold)
    return sorted(seen.values(), key=lambda entry: -entry[0])[:MAX_DETAILS]


def _types(node):
    value = node.get("@type", [])
    return [
        v.split("/")[-1].lower()
        for v in (value if isinstance(value, list) else [value])
        if isinstance(v, str)
    ]


def _products(doc):
    for script in doc.xpath("//script[@type='application/ld+json']/text()"):
        try:
            parsed = json.loads(script)
        except (ValueError, RecursionError):
            continue
        roots = parsed if isinstance(parsed, list) else [parsed]
        for root in roots:
            if not isinstance(root, dict):
                continue
            graph = root.get("@graph", [])
            for record in [root, *(graph if isinstance(graph, list) else [])]:
                if isinstance(record, dict) and "product" in _types(record):
                    yield record


def _string(item):
    if isinstance(item, (list, dict)):
        return (
            str(item.get("name") or item.get("value") or "")
            if isinstance(item, dict)
            else ""
        )
    return str(item or "")


def _grade_label(raw):
    tail = _string(raw).rstrip("/").split("/")[-1]
    return (
        tail if grade({"condition": tail}) else CONDITION_LABELS.get(tail.lower(), "")
    )


def _fields(doc):
    labels = {}
    for name in doc.xpath(
        "//dt|//*[@data-testid='ux-labels-values-label']|//*[contains(concat(' ',normalize-space(@class),' '),' ux-labels-values__labels ')]"
    ):
        sib = name.xpath("following-sibling::*[1]")
        if sib:
            labels[words(_text(name))] = _text(sib[0])[:240]
    # Vinted product facts can be simple neighbouring label/value blocks.
    for name in doc.xpath(
        "//*[self::span or self::div or self::p][not(*) and normalize-space(text())='Brand' or not(*) and normalize-space(text())='Condition' or not(*) and normalize-space(text())='Size']"
    ):
        for sib in (
            name.xpath("following-sibling::*[1]"),
            name.xpath("../following-sibling::*[1]"),
        ):
            if sib and 0 < len(_text(sib[0])) < 85:
                labels.setdefault(words(_text(name)), _text(sib[0]))
                break
    return labels


def _country(entry):
    if not isinstance(entry, dict):
        return ""
    address = entry.get("address", entry)
    if isinstance(address, dict):
        return _string(address.get("addressCountry", "")).strip()
    return ""


def _identity_metadata(source, seller, product):
    fields = {}
    profile = seller.get("url")
    if isinstance(profile, str):
        try:
            parsed = urlsplit(profile)
        except ValueError:
            parsed = None
        pattern = (
            r"/usr/([a-z0-9_.-]+)/?"
            if source.startswith("ebay")
            else (
                r"/([a-z0-9_.-]+)/?"
                if source == "depop_uk"
                else r"/member/\d+-([a-z0-9_.-]+)/?"
            )
        )
        if (
            parsed
            and parsed.scheme == "https"
            and parsed.netloc.lower() in {SITES[source][0], SITES[source][0][4:]}
        ):
            match = re.fullmatch(pattern, parsed.path, re.IGNORECASE)
            if match:
                fields["public_seller_handle"] = match[1].casefold()
    image = product.get("image")
    if isinstance(image, list):
        image = next(iter(image), None)
    if isinstance(image, dict):
        image = image.get("contentUrl") or image.get("url")
    if isinstance(image, str):
        try:
            parsed = urlsplit(image)
            if (
                parsed.scheme == "https"
                and parsed.username is None
                and parsed.password is None
                and parsed.hostname
            ):
                fields["image_url"] = image
        except ValueError:
            pass
    return fields


def _seller(source, seller, doc, url):
    profile = seller.get("url") or seller.get("@id") or ""
    if source == "vinted_uk":
        found = set()
        host = SITES[source][0]
        for href in [profile, *doc.xpath("//a[contains(@href,'/member/')]/@href")]:
            try:
                u = urlsplit(urljoin(url, href))
            except ValueError:
                continue
            m = re.fullmatch(r"/member/(\d+)(?:[-/][^/]*)?/?", u.path)
            if (
                m
                and u.scheme == "https"
                and u.netloc.lower() in {host, host.removeprefix("www.")}
            ):
                found.add(m[1])
        if len(found) == 1:
            return found.pop()
        return ""
    name = _string(seller.get("name") or seller.get("identifier")).strip()
    return (
        (source.split("_")[0] + ":" + name.casefold()) if 0 < len(name) <= 100 else ""
    )


def _offer_price(offer, doc):
    raw = offer.get("price")
    currency = str(offer.get("priceCurrency") or "").upper()

    def meta(name):
        entries = doc.xpath(
            "//meta[@property=$name or @itemprop=$name]/@content", name=name
        )
        return entries[0] if len(set(entries)) == 1 else ""

    currency = currency or meta("product:price:currency").upper()
    raw = raw if raw is not None else meta("product:price:amount")
    return pence(raw) if currency == "GBP" else None


def _documented_facts(product, labels, title, brand, size):
    from trial_market_hour import product_facts

    raw = {
        "title": title,
        "brand": brand,
        "size": size,
        "category": product.get("category") or labels.get("category"),
        "description": product.get("description"),
        "condition_description": labels.get("condition description"),
        "seller_notes": labels.get("seller notes") or labels.get("seller s notes"),
        "audience": product.get("audience"),
        "condition": labels.get("condition"),
    }
    for key, label in (
        ("age_group", "age group"),
        ("gender", "gender"),
        ("department", "department"),
        ("suggestedGender", "suggested gender"),
        ("suggestedMinAge", "suggested min age"),
        ("suggestedMaxAge", "suggested max age"),
    ):
        raw[key] = product.get(key) or labels.get(label)
    return product_facts(raw)


def _detail(doc, source, link, sold):
    identity, url = link
    declared = doc.xpath(
        "//link[@rel='canonical']/@href|//meta[@property='og:url']/@content"
    )
    if any((_canonical(source, d, url) or ("",))[0] != identity for d in declared):
        return None
    products = list(_products(doc))
    scoped = [
        p
        for p in products
        if (_canonical(source, p.get("url") or p.get("@id"), url) or ("",))[0]
        == identity
        and (p.get("url") or p.get("@id"))
    ]
    if not scoped:
        if (
            not declared
            or len(products) > 1
            or any(p.get("url") or p.get("@id") for p in products)
        ):
            return None
        scoped = products or [{}]
    if len(scoped) != 1:
        return None
    product = scoped[0]
    offer = product.get("offers") or {}
    if isinstance(offer, list):
        if len(offer) != 1:
            return None
        offer = offer[0]
    if not isinstance(offer, dict) or "aggregateoffer" in _types(offer):
        return None
    title = (
        _string(product.get("name")) or " ".join(doc.xpath("//h1[1]//text()")).strip()
    )
    labels = _fields(doc)
    for prop in product.get("additionalProperty") or []:
        if isinstance(prop, dict):
            labels.setdefault(words(prop.get("name")), _string(prop.get("value")))
    brand = _string(product.get("brand")) or labels.get("brand", "")
    size = _string(product.get("size")) or labels.get("size", "")
    condition = _grade_label(
        labels.get("condition")
        or product.get("itemCondition")
        or offer.get("itemCondition")
    )
    seller = offer.get("seller") or product.get("seller") or {}
    if not isinstance(seller, dict):
        seller = {}
    seller_id = _seller(source, seller, doc, url)
    metadata = _identity_metadata(source, seller, product)
    metadata.update(_documented_facts(product, labels, title, brand, size))
    # Only an item-scoped category name supplied by the product, not surrounding navigation.
    category = (
        _string(product.get("category") or labels.get("product type"))
        .split(">")[-1]
        .strip()
    )
    if 1 < len(category) <= 100 and garment(category):
        metadata["leaf_title"] = category
    country = (
        _country(offer.get("availableAtOrFrom"))
        or _country(product.get("contentLocation"))
        or _country(seller)
    )
    if not country:
        location = (
            (labels.get("item location") or labels.get("location") or "")
            .split(",")[-1]
            .strip()
        )
        country = location or ""
    if (country and words(country) not in UK) or (
        not country and source != "vinted_uk"
    ):
        return None
    price = _offer_price(offer, doc)
    availability = _string(offer.get("availability")).rstrip("/").split("/")[-1].lower()
    controls = {words(_text(e)) for e in doc.xpath("//button|//a[@role='button']")}
    badges = {
        words(_text(e))
        for e in doc.xpath(
            "//main//*[self::strong or self::span or self::h2 or self::p][not(*)]"
        )
    }
    explicitly_ended = bool(
        badges
        & {
            "sold",
            "reserved",
            "listing ended",
            "this listing has ended",
            "this item has sold",
            "this item is no longer available",
        }
    )
    if not (title and brand and size and condition and seller_id):
        return None
    if source == "ebay_uk_sold":
        # Checked sold-result card supplies dated completed transaction and paid GBP. A relisted active offer is another observation.
        if (
            not sold
            or availability in {"instock", "limitedavailability"}
            or "buy it now" in controls
        ):
            return None
        return dict(
            id=identity,
            url=url,
            source=source,
            title=title,
            brand=brand,
            size=size,
            condition=condition,
            seller_id=seller_id,
            currency="GBP",
            country="UK",
            sold_pence=sold[0],
            sold_at=sold[1],
            active=False,
            price_kind="sold",
            sold_amount_confirmed=True,
            observed_at=time.time(),
            **metadata,
        )
    if (
        explicitly_ended
        or not price
        or availability in NEGATIVE
        or (
            availability != "instock"
            and not controls
            & {"buy now", "buy it now", "add to bag", "add to cart", "add to basket"}
        )
    ):
        return None
    return dict(
        id=identity,
        url=url,
        source=source,
        title=title,
        brand=brand,
        size=size,
        condition=condition,
        seller_id=seller_id,
        currency="GBP",
        country="UK",
        ask_pence=price,
        active=True,
        price_kind="ask",
        observed_at=time.time(),
        **metadata,
    )


def _match(target, row):
    if not field(target, "brand") or field(target, "brand") != field(row, "brand"):
        return False
    if (
        grade(target) != grade(row)
        or not field(target, "size")
        or field(target, "size") != field(row, "size")
    ):
        return False
    typ = garment(target.get("title"))
    if (not typ and not field(target, "model")) or (
        typ and not same_garment_family(target, row)
    ):
        return False
    if row["source"] == "vinted_uk" and (
        str(target.get("seller_id") or "") == row["seller_id"]
        or str(target.get("id")) == row["id"]
    ):
        return False
    return (
        not product_mismatch(target, row, rank_style=True)
        and assess(target, row, grade(target), grade(row))[0] == "close"
    )


def _ledger(path, source, query, action, *, payload=None):
    now = time.time()
    key = hashlib.sha256(query.encode()).hexdigest()
    with closing(sqlite3.connect(path, timeout=1)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS page_cache (site TEXT,query TEXT,until REAL,status TEXT,data TEXT,PRIMARY KEY(site,query))"
        )
        db.execute("CREATE TABLE IF NOT EXISTS page_budget (at REAL NOT NULL)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS page_cooldown (site TEXT PRIMARY KEY,until REAL)"
        )
        db.execute("BEGIN IMMEDIATE")
        if action == "save":
            status, rows = payload
            if status == "blocked":
                db.execute(
                    "INSERT OR REPLACE INTO page_cooldown VALUES(?,?)",
                    (source.split("_")[0], now + CACHE_AGE),
                )
            db.execute(
                "INSERT OR REPLACE INTO page_cache VALUES(?,?,?,?,?)",
                (source, key, now + CACHE_AGE, status, json.dumps(rows)),
            )
            return
        if action == "claim":
            db.execute("DELETE FROM page_cache WHERE until<=?", (now,))
            prior = db.execute(
                "SELECT status,data FROM page_cache WHERE site=? AND query=?",
                (source, key),
            ).fetchone()
            if prior:
                return prior[0], json.loads(prior[1])
            if db.execute(
                "SELECT 1 FROM page_cooldown WHERE site=? AND until>?",
                (source.split("_")[0], now),
            ).fetchone():
                return "blocked", []
            db.execute(
                "INSERT INTO page_cache VALUES(?,?,?,?,?)",
                (source, key, now + 20, "busy", "[]"),
            )
            return None
        db.execute("DELETE FROM page_budget WHERE at<=?", (now - 3600,))
        if db.execute("SELECT COUNT(*) FROM page_budget").fetchone()[0] >= MAX_REQUESTS:
            return False
        db.execute("INSERT INTO page_budget VALUES(?)", (now,))
        return True


async def _fetch(client, path, source, url, stats):
    if not await asyncio.to_thread(_ledger, path, source, url, "request"):
        return None, "hour_limit"
    stats["requests"] += 1
    # Crawls never carry an account cookie, authorization, or API credentials.
    request = httpx.Request(
        "GET",
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html",
            "Accept-Language": "en-GB,en;q=0.8",
        },
    )
    request.extensions["timeout"] = httpx.Timeout(3.5, connect=1.5).as_dict()
    response = await client.send(
        request, stream=True, auth=None, follow_redirects=False
    )
    try:
        if (
            response.status_code in {401, 403, 407, 429}
            or 300 <= response.status_code < 400
        ):
            return None, "blocked"
        if (
            response.status_code != 200
            or "html" not in response.headers.get("content-type", "").lower()
        ):
            return None, "unavailable"
        data = bytearray()
        async for part in response.aiter_bytes(16384):
            if len(data) + len(part) > MAX_BYTES:
                return None, "unavailable"
            data.extend(part)
        root = html.fromstring(
            bytes(data),
            parser=html.HTMLParser(
                encoding=response.charset_encoding or "UTF-8", no_network=True
            ),
        )
        heading = " ".join(root.xpath("//title/text()|//h1/text()")).casefold()
        if any(
            key in heading
            for key in (
                "access denied",
                "verify you are human",
                "security check",
                "pardon our interruption",
                "just a moment",
                "robot check",
            )
        ):
            return None, "blocked"
        return root, "ok"
    finally:
        await response.aclose()


async def search_marketplaces(target, database, client):
    links = search_links(target or {})
    stats = {"requests": 0}
    path = Path(database).with_name("trial-public-market-search.sqlite3")

    async def lookup(source, url):
        status, rows = "insufficient_target", []
        if (
            target
            and target.get("active") is not False
            and target.get("currency", "GBP") == "GBP"
            and target.get("seller_id")
            and field(target, "brand")
            and grade(target)
            and field(target, "size")
            and (garment(target.get("title")) or field(target, "model"))
        ):
            status = "unavailable"
            prior = False
            try:
                prior = await asyncio.to_thread(_ledger, path, source, url, "claim")
                if prior is None:
                    async with asyncio.timeout(6):
                        doc, status = await _fetch(client, path, source, url, stats)
                        if doc is not None:
                            status = "no_matches"
                            items = _search_items(doc, source, url, target)
                            if not items and not re.search(
                                r"no (?:matching |search )?results|couldn.t find any|no items found|0 results",
                                _text(doc)[:25000],
                                re.IGNORECASE,
                            ):
                                status = "unavailable"
                            for _score, link, sold in items:
                                detail, outcome = await _fetch(
                                    client, path, source, link[1], stats
                                )
                                if detail is None:
                                    if outcome in {"blocked", "hour_limit"}:
                                        status = outcome
                                        break
                                    continue
                                row = _detail(detail, source, link, sold)
                                if row:
                                    rows.append(row)
                    await asyncio.to_thread(
                        _ledger, path, source, url, "save", payload=(status, rows)
                    )
                else:
                    status, rows = prior
            except (
                httpx.HTTPError,
                TimeoutError,
                sqlite3.Error,
                etree.LxmlError,
                ValueError,
                IndexError,
                TypeError,
                RecursionError,
            ):
                if prior is None:
                    try:
                        await asyncio.to_thread(
                            _ledger,
                            path,
                            source,
                            url,
                            "save",
                            payload=("unavailable", rows),
                        )
                    except sqlite3.Error:
                        pass
                status = "unavailable"
        selected = {}
        for row in rows:
            if _match(target, row):
                key = row["seller_id"]
                price = row.get("ask_pence") or row.get("sold_pence")
                if key not in selected or price < (
                    selected[key].get("ask_pence") or selected[key].get("sold_pence")
                ):
                    selected[key] = row
        if selected and status == "no_matches":
            status = "checked"
        return source, {"status": status, "url": url, "rows": list(selected.values())}

    data = await asyncio.gather(*(lookup(source, url) for source, url in links.items()))
    return {"sources": dict(data), "fresh_requests": stats["requests"]}
