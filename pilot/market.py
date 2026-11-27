"""Shared public UK comparison evidence. No personal feeds or buyer accounts are stored here."""
import hashlib
import json
import logging
import math
import os
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import click
import requests
from flask import current_app

from .db import get_db, transaction

log = logging.getLogger(__name__)
SEARCH_URL = "https://api.scavio.dev/api/v1/vinted/search"
MAX_AGE = 48 * 3600
SEARCH_LIFETIME = 6 * 3600
DESCRIPTORS = ("title", "brand", "model", "subtype", "category", "material", "size", "condition",
               "edition", "capacity", "format", "variant")
STOP = frozenset("a an the of and by for with in is on size from very good condition new tags".split())

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_offers (
 id TEXT PRIMARY KEY, observed_at REAL NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS market_age ON market_offers(observed_at);
CREATE TABLE IF NOT EXISTS market_withdrawn (
 id TEXT PRIMARY KEY, observed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS market_terms (
 term TEXT NOT NULL, offer_id TEXT NOT NULL REFERENCES market_offers(id) ON DELETE CASCADE,
 PRIMARY KEY(term,offer_id)
);
CREATE INDEX IF NOT EXISTS market_offer_terms ON market_terms(offer_id);
CREATE TABLE IF NOT EXISTS market_queries (
 digest TEXT PRIMARY KEY, next_check REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS market_budget (
 id INTEGER PRIMARY KEY CHECK(id=1), used INTEGER NOT NULL DEFAULT 0,
 available_at REAL NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO market_budget(id) VALUES(1);
"""


def init_app(app):
    for key in ("PILOT_SCAVIO_ENABLED", "PILOT_SCAVIO_API_KEY", "PILOT_SCAVIO_MAX_SEARCHES"):
        app.config.setdefault(key, os.environ.get(key, ""))
    with app.app_context():
        get_db().executescript(SCHEMA)
    app.cli.add_command(import_command)


def _text(value):
    if isinstance(value, dict):
        value = value.get("title") or value.get("name") or value.get("label")
    return value.strip()[:180] if isinstance(value, str) else ""


def _tokens(value):
    return [t for t in re.findall(r"[^\W_]+", value.casefold()) if t not in STOP]


def _terms(item):
    return tuple(dict.fromkeys(_tokens(" ".join(_text(item.get(k)) for k in DESCRIPTORS))))[:40]


def _query(item):
    # The model or subtype is more discriminating than the category alone. Preserve
    # numbers and short model codes: they often differentiate price classes.
    words = []
    for field in ("brand", "model", "subtype", "title"):
        words.extend(_tokens(_text(item.get(field))))
    return " ".join(dict.fromkeys(words))[:180].strip()


def _ask(pounds):
    if isinstance(pounds, dict):
        if (pounds.get("currency_code") or pounds.get("currency") or "GBP") != "GBP":
            return None
        pounds = pounds.get("amount")
    try:
        p = Decimal(str(pounds)) * 100
        if p.is_finite() and 0 < p <= 100_000_000 and p == p.to_integral_value():
            return int(p)
    except (TypeError, ValueError, InvalidOperation):
        pass
    return None


def _timestamp(value):
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp() if dt.tzinfo else None
        except (ValueError, OverflowError, OSError):
            return None
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _item_url(item_id, raw_url):
    if not re.fullmatch(r"[1-9]\d{0,18}", item_id):
        return None
    canonical = f"https://www.vinted.co.uk/items/{item_id}"
    if not raw_url:
        return canonical
    try:
        url = urlsplit(raw_url)
        matching = (url.scheme == "https" and url.netloc.lower() in ("www.vinted.co.uk", "vinted.co.uk")
                    and re.fullmatch(r"/items/([1-9]\d*)(?:-[^/?#\\]*)?/?", url.path))
        if matching and matching.group(1) == item_id:
            return canonical
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def _unavailable(raw):
    return (raw.get("active") is False or raw.get("can_be_bought") is False or
            str(raw.get("status", "")).casefold() in {"sold", "deleted", "reserved", "hidden", "unavailable", "withdrawn", "unlisted"} or
            any(raw.get(flag) for flag in ("is_sold", "is_reserved", "sold", "reserved", "is_hidden", "is_deleted")))


def _normalize(raw, now, source, imported=False):
    if not isinstance(raw, dict) or raw.get("currency", "GBP" if not imported else None) != "GBP":
        return None
    if _unavailable(raw):
        return None
    if imported and (str(raw.get("country", "")).lower() != "uk" or raw.get("active") is not True):
        return None
    item_id = str(raw.get("id", ""))
    url = _item_url(item_id, raw.get("url"))
    seller = raw.get("seller")
    seller_id = seller.get("id") if isinstance(seller, dict) else raw.get("seller_id")
    if type(seller_id) not in (str, int) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(seller_id)):
        return None
    if imported:
        price = raw.get("ask_pence")
        if type(price) is not int or not 0 < price <= 100_000_000:
            return None
        observed = _timestamp(raw.get("observed_at"))
        if not raw.get("url") or observed is None or not now - MAX_AGE <= observed <= now:
            return None
    else:
        price = _ask(raw.get("price"))
        if price is None:
            return None
        price = min(price, _ask(raw.get("price_with_discount")) or price)
        observed = now
    if not url or not _text(raw.get("title")):
        return None
    result = {k: _text(raw.get(k)) for k in DESCRIPTORS if _text(raw.get(k))}
    # Carry product specifications for independently verified, structured imports.
    attrs = raw.get("attributes")
    if imported and isinstance(attrs, dict):
        result["attributes"] = {k: _text(v) for k, v in list(attrs.items())[:24]
                                if isinstance(k,str) and len(k)<=60 and _text(v)}
    return dict(result, id=item_id, url=url, ask_pence=price, observed_at=observed,
                seller_id=str(seller_id), country="UK", currency="GBP", active=True, source=source)


def _save(conn, items, now):
    conn.execute("DELETE FROM market_offers WHERE observed_at < ?", (now - MAX_AGE,))
    conn.execute("DELETE FROM market_queries WHERE next_check < ?", (now - MAX_AGE,))
    conn.execute("DELETE FROM market_withdrawn WHERE observed_at < ?", (now - MAX_AGE,))
    for item in items:
        previous = conn.execute("SELECT payload FROM market_offers WHERE id=?", (item["id"],)).fetchone()
        changed = conn.execute("""INSERT INTO market_offers(id,observed_at,payload)
            SELECT ?,?,? WHERE NOT EXISTS(SELECT 1 FROM market_withdrawn WHERE id=? AND observed_at>=?)
            ON CONFLICT(id) DO UPDATE SET observed_at=excluded.observed_at, payload=excluded.payload
            WHERE excluded.observed_at >= market_offers.observed_at""",
            (item["id"], item["observed_at"], json.dumps(item), item["id"], item["observed_at"]))
        if changed.rowcount:
            terms = _terms(item)
            if previous and terms == _terms(json.loads(previous[0])):
                continue
            conn.execute("DELETE FROM market_terms WHERE offer_id=?", (item["id"],))
            conn.executemany("INSERT INTO market_terms VALUES(?,?)", ((t, item["id"]) for t in terms))


def remember_public_results(conn, rows, observed_at):
    """Record only public product fields from a confirmed Scavio UK/GBP response, inside a transaction."""
    offers = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("id", ""))
        if _unavailable(row) and _item_url(item_id, row.get("url")):
            conn.execute(
                """INSERT INTO market_withdrawn VALUES(?,?)
                   ON CONFLICT(id) DO UPDATE SET observed_at=max(observed_at,excluded.observed_at)""",
                (item_id, observed_at),
            )
            conn.execute("DELETE FROM market_offers WHERE id=? AND observed_at<=?", (item_id, observed_at))
        elif offer := _normalize(row, observed_at, "Scavio UK search"):
            offers.append(offer)
    _save(conn, offers, observed_at)
    return len(offers)


def _cached(conn, target, now):
    terms = _terms(target)
    if not terms:
        return []
    placeholders = ",".join("?" for _ in terms)
    rows = conn.execute(f"""SELECT m.payload FROM market_terms t JOIN market_offers m ON m.id=t.offer_id
        WHERE t.term IN ({placeholders}) AND m.observed_at BETWEEN ? AND ? AND m.id != ?
        GROUP BY m.id ORDER BY count(*) DESC, m.observed_at DESC, m.id LIMIT 128""",
        (*terms, now - MAX_AGE, now, str(target.get("id", ""))))
    return [json.loads(row[0]) for row in rows]


def _reserve(conn, digest, now, limit):
    with transaction(conn):
        prior = conn.execute("SELECT next_check FROM market_queries WHERE digest=?", (digest,)).fetchone()
        if prior and prior[0] > now:
            return False
        # Reserve before the network call; ambiguous timeouts still count. A global
        # lease keeps all users/processes under one concurrent request and one start/s.
        updated = conn.execute("""UPDATE market_budget SET used=used+1,available_at=?
            WHERE id=1 AND used<? AND available_at<=?""", (now+30, limit, now))
        if not updated.rowcount:
            return False
        conn.execute("INSERT OR REPLACE INTO market_queries VALUES(?,?)", (digest, now+120))
        return True


def _search(key, query):
    response = requests.post(SEARCH_URL, headers={"Authorization": "Bearer " + key},
                             json={"country": "uk", "query": query, "sort": "relevance", "page": 1},
                             timeout=(3, 9), allow_redirects=False)
    if response.status_code != 200:
        raise requests.HTTPError(f"Market search response {response.status_code}", response=response)
    data = response.json()["data"]
    if data.get("country") != "uk" or data.get("currency") != "GBP" or not isinstance(data.get("items"), list):
        raise ValueError("Unexpected UK market search response")
    return data["items"][:96]


def comparisons_for(conn, target, *, config=None, now=None):
    """Find public asking prices for one item. Call outside database transactions."""
    now = time.time() if now is None else now
    config = current_app.config if config is None else config
    cached = _cached(conn, target, now)
    key = config.get("PILOT_SCAVIO_API_KEY")
    if str(config.get("PILOT_SCAVIO_ENABLED", "")).lower() not in ("1", "true") or not key:
        return cached
    try:
        limit = int(config.get("PILOT_SCAVIO_MAX_SEARCHES") or 0)
    except (ValueError, TypeError):
        return cached
    query = _query(target)
    if not query or limit < 1:
        return cached
    from .pricing import grade
    if not grade(target) or not target.get("seller_id") or not target.get("id") or target.get("currency", "GBP") != "GBP":
        return cached
    if cached:
        from .pricing import estimate_resale
        if estimate_resale(target, cached, now=now).get("resale_pence") is not None:
            return cached
    digest = hashlib.sha256(query.encode()).hexdigest()
    if not _reserve(conn, digest, now, limit):
        return cached

    delay, query_delay = 0, 120
    started = time.monotonic()
    try:
        raw = _search(key, query)
        with transaction(conn):
            count = remember_public_results(conn, raw, now)
        query_delay = SEARCH_LIFETIME if count else 15 * 60
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (401, 402, 403):
            delay = 3600
        elif status == 429:
            delay = 60
        log.info("Market comparison request unavailable (HTTP %s)", status)
    finally:
        with transaction(conn):
            conn.execute("UPDATE market_queries SET next_check=? WHERE digest=?", (now+query_delay, digest))
            # An expired lease may have been claimed by another process.
            conn.execute("UPDATE market_budget SET available_at=? WHERE id=1 AND available_at=?",
                         (now + max(1, time.monotonic() - started + delay), now+30))
    return _cached(conn, target, now)


@click.command("market-import")
@click.argument("jsonl", type=click.File("r", encoding="utf-8"))
def import_command(jsonl):
    """Import independently observed, active UK asking prices from JSON Lines."""
    conn = get_db()
    now = time.time()
    try:
        with transaction(conn):
            count = 0
            for line_number, line in enumerate(jsonl, 1):
                if not line.strip():
                    continue
                item = _normalize(json.loads(line), now, "Operator-verified UK listing", imported=True)
                if not item:
                    raise ValueError(f"Line {line_number} has no valid, active UK comparison.")
                _save(conn, [item], now)
                count += 1
    except (ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Imported {count} UK comparison listings.")
