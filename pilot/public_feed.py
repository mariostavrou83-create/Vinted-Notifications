"""Private saved-search alerts sourced from Scavio's public Vinted UK search API."""

import hashlib
import json
import os
import re
import time
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlsplit

import requests
from flask import current_app

from . import flow, market
from .db import DEFAULT_SETTINGS, get_db, transaction
from .inputs import money, search_url

SCHEMA = """
CREATE TABLE IF NOT EXISTS public_search_checkpoint(
 search_id INTEGER PRIMARY KEY REFERENCES searches(id) ON DELETE CASCADE,
 fingerprint TEXT NOT NULL, last_success REAL NOT NULL, highest INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS public_query_schedule(digest TEXT PRIMARY KEY, attempted REAL NOT NULL);
CREATE TABLE IF NOT EXISTS public_checks(id INTEGER PRIMARY KEY, started REAL NOT NULL);
CREATE INDEX IF NOT EXISTS public_checks_time ON public_checks(started);
CREATE TABLE IF NOT EXISTS public_budget(id INTEGER PRIMARY KEY CHECK(id=1), used INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO public_budget(id) VALUES(1);
CREATE TRIGGER IF NOT EXISTS public_reset_search AFTER UPDATE OF url,enabled,include_keywords,exclude_keywords,budget_pence ON searches
WHEN OLD.url IS NOT NEW.url OR OLD.enabled IS NOT NEW.enabled OR OLD.include_keywords IS NOT NEW.include_keywords
 OR OLD.exclude_keywords IS NOT NEW.exclude_keywords OR OLD.budget_pence IS NOT NEW.budget_pence
BEGIN DELETE FROM public_search_checkpoint WHERE search_id=NEW.id; END;
"""
PARAMS = {"brand_ids", "size_ids", "status_ids", "color_ids", "material_ids"}
IGNORED = {"order", "sort", "page", "time", "search_id", "referrer", "disabled_personalization"}
PUBLIC_KEYS = (
    "PILOT_PUBLIC_SEARCH_ENABLED",
    "PILOT_PUBLIC_SEARCH_MAX_REQUESTS",
    "PILOT_PUBLIC_SEARCH_DAILY_LIMIT",
)


def init_app(app):
    for key in PUBLIC_KEYS:
        app.config.setdefault(key, os.environ.get(key, ""))
    get_db().executescript(SCHEMA)


def limits():
    config = current_app.config
    try:
        total = int(config.get(PUBLIC_KEYS[1]) or 0)
        daily = int(config.get(PUBLIC_KEYS[2]) or 0)
    except (TypeError, ValueError):
        total = daily = 0
    enabled = (
        str(config.get(PUBLIC_KEYS[0], "")).lower() in {"1", "true"}
        and bool(config.get("PILOT_SCAVIO_API_KEY"))
        and total > 0
        and daily > 0
    )
    return enabled, total, daily


def status(conn, user_id=None):
    enabled, total, daily = limits()
    used = conn.execute("SELECT used FROM public_budget WHERE id=1").fetchone()[0]
    last = conn.execute(
        "SELECT count(*) FROM public_checks WHERE started>?", (time.time() - 86400,)
    ).fetchone()[0]
    result = {
        "enabled": enabled,
        "mode": "public_search",
        "max_requests": total,
        "used_requests": used,
        "daily_limit": daily,
        "used_last_24h": last,
        "cadence_seconds": 5,
    }
    if user_id is not None:
        unsupported = {}
        for row in conn.execute("SELECT id,url FROM searches WHERE user_id=? AND enabled=1", (user_id,)):
            try:
                parameters(row["url"])
            except ValueError as exc:
                unsupported[str(row["id"])] = str(exc)
        result["unsupported"] = unsupported
    return result


def parameters(url):
    """Map only filters the public provider can honour. Never silently broaden a URL."""
    try:
        parts = urlsplit(search_url(url))
        args = parse_qs(parts.query, keep_blank_values=True, max_num_fields=150)
    except ValueError as exc:
        raise ValueError("Invalid UK Vinted search URL.") from exc
    body = {"country": "uk", "sort": "newest_first", "page": 1}
    category = re.fullmatch(r"/catalog/(\d+)(?:-[^/]*)?/?", parts.path)
    if category:
        body["category_id"] = int(category[1])
    elif parts.path.rstrip("/") != "/catalog":
        raise ValueError("Unsupported category path.")
    values = defaultdict(list)
    for key, vals in args.items():
        key = key.removesuffix("[]")
        values[key].extend(vals)
    for key, vals in values.items():
        if key in IGNORED or key.startswith("utm_"):
            continue
        if key == "currency" and vals == ["GBP"]:
            continue
        if (
            key in {"search_text", "query"}
            and len(vals) == 1
            and vals[0].strip()
            and len(vals[0]) <= 200
        ):
            if body.get("query", vals[0].strip()) != vals[0].strip():
                raise ValueError("Conflicting search terms.")
            body["query"] = vals[0].strip()
        elif key in PARAMS or key in {"catalog", "catalog_id", "category_id"}:
            items = [v for s in vals for v in s.split(",")]
            if not 1 <= len(items) <= 20 or not all(re.fullmatch(r"[1-9]\d{0,8}", v) for v in items):
                raise ValueError("Invalid catalogue filter.")
            ids = sorted(set(map(int, items)))
            if key in PARAMS:
                body[key] = ids
            elif len(ids) == 1 and body.get("category_id", ids[0]) == ids[0]:
                body["category_id"] = ids[0]
            else:
                raise ValueError("Use a single category for public search.")
        elif key in {"price_from", "price_to"} and len(vals) == 1:
            p = money(vals[0])
            if p is None:
                raise ValueError("Invalid price limit.")
            body[key] = p / 100
        else:
            raise ValueError("This saved filter requires the connected buyer feed.")
    if not any(k in body for k in ("category_id", "brand_ids", "query")):
        raise ValueError("Add a keyword, brand or category to the Vinted URL.")
    if body.get("price_from", 0) > body.get("price_to", float("inf")):
        raise ValueError("Invalid price range.")
    return body


def _confirmed(body, data):
    if (
        data.get("country") != "uk"
        or data.get("currency") != "GBP"
        or not isinstance(data.get("items"), list)
    ):
        return False
    filters = data.get("filters", {})
    if not isinstance(filters, dict):
        return False
    for field, expected in body.items():
        if field in {"country", "page", "sort", "query"}:
            continue
        applied = filters.get(field)
        if isinstance(expected, list):
            if not isinstance(applied, list) or {str(v) for v in applied} != {str(v) for v in expected}:
                return False
        elif field in ("price_to", "price_from"):
            if applied is None or Decimal(str(expected)) != Decimal(str(applied)):
                return False
        elif applied is None or str(applied) != str(expected):
            return False
    return not body.get("query") or data.get("query") == body["query"]


def _search(body):
    response = requests.post(
        market.SEARCH_URL,
        headers={"Authorization": "Bearer " + current_app.config["PILOT_SCAVIO_API_KEY"]},
        json=body,
        timeout=(3, 9),
        allow_redirects=False,
    )
    if response.status_code != 200:
        raise requests.HTTPError(response=response)
    data = response.json()["data"]
    if not isinstance(data, dict) or not _confirmed(body, data):
        raise ValueError("Search filters or market were not confirmed.")
    return data["items"][:96]


def _listing(raw):
    if not isinstance(raw, dict) or raw.get("currency") != "GBP" or market._unavailable(raw):
        return None
    ident = str(raw.get("id", ""))
    url = market._item_url(ident, raw.get("url"))
    title = market._text(raw.get("title"))
    price = market._ask(raw.get("price"))
    subtotal = market._ask(raw.get("total_price"))
    if not price or not title or not url:
        return None
    price = min(price, market._ask(raw.get("price_with_discount")) or price)
    fee = market._ask(raw.get("service_fee"))
    if subtotal is None and fee is not None:
        subtotal = price + fee
    if subtotal is None or subtotal < price:
        return None
    seller = raw.get("seller")
    info = {k: market._text(raw.get(k)) for k in market.DESCRIPTORS if market._text(raw.get(k))}
    return dict(
        info,
        id=ident,
        listing_url=url,
        title=title,
        seller_id=str((seller.get("id") if isinstance(seller, dict) else raw.get("seller_id")) or ""),
        description=market._text(raw.get("description")),
        currency="GBP",
        price_pence=price,
        subtotal_pence=subtotal,
        total_pence=subtotal,
        total_excludes_shipping=True,
        public_search=True,
    )


def _members(conn, now):
    groups = {}
    rows = conn.execute("""SELECT s.*,t.data FROM searches s LEFT JOIN settings t ON t.user_id=s.user_id
        WHERE s.enabled=1 AND NOT EXISTS(SELECT 1 FROM pilot_buyer_link b WHERE b.user_id=s.user_id AND b.revoke_pending=0)
        ORDER BY s.id""")
    for row in rows:
        prefs = {**DEFAULT_SETTINGS, **json.loads(row["data"] or "{}")}
        if flow.quiet(prefs, now):
            continue
        try:
            body = parameters(row["url"])
        except ValueError:
            continue
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        groups.setdefault(digest, (body, []))[1].append((dict(row), prefs))
    return groups


def _fingerprint(row):
    return hashlib.sha256(
        json.dumps(
            [row[key] for key in ("url", "include_keywords", "exclude_keywords", "budget_pence")]
        ).encode()
    ).hexdigest()


def _allowed(item, row, prefs):
    title = item["title"].casefold()
    text = title + " " + item["description"].casefold()
    include = json.loads(row["include_keywords"])
    exclude = json.loads(row["exclude_keywords"])
    if any(t.casefold() in title for t in exclude) or (
        include and not any(t.casefold() in text for t in include)
    ):
        return False
    if row["budget_pence"] is not None:
        shipping = money(prefs.get("max_shipping", "4.99"))
        if shipping is None or item["subtotal_pence"] + shipping > row["budget_pence"]:
            return False
    return True


def _record(conn, selected, rows, now):
    items = [item for raw in rows if (item := _listing(raw))]
    ids = [
        int(str(raw["id"]))
        for raw in rows
        if isinstance(raw, dict) and re.fullmatch(r"[1-9]\d{0,17}", str(raw.get("id", "")))
    ]
    top = max(ids, default=0)
    for old, _prefs in selected:
        s = conn.execute(
            """SELECT s.*,t.data FROM searches s LEFT JOIN settings t ON t.user_id=s.user_id
            WHERE s.id=? AND s.user_id=? AND enabled=1 AND NOT EXISTS
            (SELECT 1 FROM pilot_buyer_link b WHERE b.user_id=s.user_id AND b.revoke_pending=0)""",
            (old["id"], old["user_id"]),
        ).fetchone()
        if not s or _fingerprint(old) != _fingerprint(s):
            continue
        prefs = {**DEFAULT_SETTINGS, **json.loads(s["data"] or "{}")}
        if flow.quiet(prefs, now):
            continue
        prev = conn.execute(
            "SELECT * FROM public_search_checkpoint WHERE search_id=?", (s["id"],)
        ).fetchone()
        fresh = prev and prev["fingerprint"] == _fingerprint(s) and now - prev["last_success"] < 1200
        highest = prev["highest"] if fresh else 0
        # A first page at capacity with no overlap cannot prove freshness since the prior snapshot.
        if len(rows) == 96 and ids and min(ids) > highest and highest > 0:
            fresh = False
        if fresh:
            for item in reversed(items):
                if int(item["id"]) > highest and _allowed(item, s, prefs):
                    flow.record_public_listing(
                        conn, s["user_id"], dict(item, search_id=s["id"]), prefs, now
                    )
        conn.execute(
            "INSERT OR REPLACE INTO public_search_checkpoint VALUES(?,?,?,?)",
            (s["id"], _fingerprint(s), now, max(highest, top)),
        )


def public_discover(conn, now):
    enabled, total, daily = limits()
    if not enabled:
        return False
    # Bound work before iterating the user roster, even while quota is exhausted.
    with transaction(conn):
        if (
            conn.execute("SELECT used FROM public_budget WHERE id=1").fetchone()[0] >= total
            or conn.execute(
                "SELECT count(*) FROM public_checks WHERE started>?", (now - 86400,)
            ).fetchone()[0]
            >= daily
        ):
            return False
        lease = conn.execute("SELECT available_at FROM market_budget WHERE id=1").fetchone()[0]
        if lease > now:
            return False
        groups = _members(conn, now)
        last = dict(conn.execute("SELECT digest,attempted FROM public_query_schedule"))
        eligible = [d for d in groups if last.get(d, 0) <= now - 5]
        if not eligible:
            return False
        digest = min(eligible, key=lambda d: (last.get(d, 0), d))
        body, selected = groups[digest]
        conn.execute("UPDATE public_budget SET used=used+1 WHERE id=1")
        conn.execute("UPDATE market_budget SET available_at=? WHERE id=1", (now + 30,))
        conn.execute("DELETE FROM public_checks WHERE started<=?", (now - 86400,))
        conn.execute("INSERT INTO public_checks(started) VALUES(?)", (now,))
        conn.execute("DELETE FROM public_query_schedule WHERE attempted<?", (now - 86400,))
        conn.execute("INSERT OR REPLACE INTO public_query_schedule VALUES(?,?)", (digest, now))
    started = time.monotonic()
    delay = 1
    try:
        rows = _search(body)
        observed = now + time.monotonic() - started
        with transaction(conn):
            market.remember_public_results(conn, rows, observed)
            _record(conn, selected, rows, observed)
    except requests.HTTPError as exc:
        status_code = getattr(exc.response, "status_code", None)
        delay = 3600 if status_code in (401, 402, 403) else 60
        if status_code == 429:
            try:
                delay = max(60, min(86400, int(exc.response.headers.get("Retry-After", "60"))))
            except ValueError:
                pass
    except (requests.RequestException, ValueError, KeyError, TypeError, InvalidOperation):
        delay = 30
    finally:
        conn.execute(
            "UPDATE market_budget SET available_at=? WHERE id=1 AND available_at=?",
            (now + max(delay, time.monotonic() - started + 0.01), now + 30),
        )
    return True
