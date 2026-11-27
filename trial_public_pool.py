"""Reuse verified public listings from completed UK catalogue pages for the one-hour trial."""

import json
import logging
import queue
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from trial_shared_hour import EARLIEST, LAST_START, DURATION, eligible_service

PAGE_LIMIT = 32
ROW_LIMIT = 8000
MAX_AGE = 15 * 60
_pages = queue.Queue(maxsize=4)
_writer = None
_next_offer = 0.0
_recent_urls = OrderedDict()
_next_inventory_log = 0.0
log = logging.getLogger(__name__)
_stop_at = LAST_START + DURATION


def _path(database):
    return Path(database).resolve().with_name("shared-public-market-20261127.sqlite3")


def activate(database, end_at):
    now = time.time()
    if not eligible_service() or not EARLIEST <= now < end_at <= LAST_START + DURATION:
        return
    with closing(sqlite3.connect(_path(database), timeout=1)) as conn, conn:
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS window(id INTEGER PRIMARY KEY, ends REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS offers(
                id TEXT PRIMARY KEY, observed REAL NOT NULL, brand TEXT NOT NULL COLLATE NOCASE,
                size TEXT NOT NULL COLLATE NOCASE, seller TEXT NOT NULL, payload TEXT);
            CREATE INDEX IF NOT EXISTS market_matching ON offers(size, brand, observed);
            CREATE INDEX IF NOT EXISTS market_age ON offers(observed);
        """)
        conn.execute("INSERT OR IGNORE INTO window VALUES (1,?)", (end_at,))


def offer(database, url, page):
    """Bounded in-memory handoff; the catalogue scheduler never waits for disk or price calculations."""
    global _writer, _next_offer
    now = time.time()
    if now < _next_offer or not EARLIEST <= now < _stop_at or not eligible_service():
        return
    try:
        last_seen, offset = _recent_urls.get(url, (0, 0))
        if now - last_seen < 12:
            return
        if urlsplit(url).hostname not in ("www.vinted.co.uk", "vinted.co.uk"):
            return
        observed = getattr(page, "observed_at", None)
        if type(observed) not in (int, float) or not now - 60 <= observed <= now:
            return
        catalogue = getattr(page, "catalogue_page", None)
        rows = catalogue._raw_items if catalogue is not None else page
        if not rows:
            return
        offset %= len(rows)
        # Spread the bounded sample across filters and catalogue positions.
        # The scheduler pays no cost to reparse the rest of a completed page.
        sample = rows[offset:offset + PAGE_LIMIT]
        _pages.put_nowait((database, observed, sample))
        _next_offer = now + .2
        _recent_urls[url] = (now, (offset + len(sample)) % len(rows))
        _recent_urls.move_to_end(url)
        if len(_recent_urls) > 768:
            _recent_urls.popitem(last=False)
        if _writer is None:
            _writer = threading.Thread(target=_consume, daemon=True, name="trial-public-market")
            _writer.start()
    except (ValueError, AttributeError, TypeError, RuntimeError, queue.Full):
        return


def _consume():
    global _stop_at
    while time.time() < _stop_at:
        try:
            database, observed, batch = _pages.get(timeout=6)
        except queue.Empty:
            continue
        try:
            path = _path(database)
            if path.is_file():
                with closing(sqlite3.connect(path, timeout=.15)) as conn:
                    ends = _store(conn, observed, batch, time.time())
                    if ends:
                        _stop_at = min(_stop_at, ends)
        except (sqlite3.Error, OSError, KeyError, ValueError, TypeError, AttributeError, OverflowError):
            continue


def _public_item(raw):
    # Accept only product fields from the already returned card. Missing fields
    # stay unknown; building a second marketplace client here would be wasteful.
    box = raw.get("item_box") or {}
    brand_line = box.get("first_line")
    second_line = box.get("second_line") or ""
    size = raw.get("size_title") or (second_line.split(" · ")[0] if " · " in second_line else "")
    url = raw["url"]
    if url.startswith("/items/"):
        url = "https://www.vinted.co.uk" + url
    return SimpleNamespace(id=raw["id"], title=raw["title"], url=url, raw_data=raw,
                           brand_title=raw.get("brand_title") or (brand_line if brand_line != raw["title"] else ""),
                           size_title=size, price=raw["price"]["amount"], currency=raw["price"]["currency_code"])


def _store(conn, observed, batch, now):
    window = conn.execute("SELECT ends FROM window WHERE id=1").fetchone()
    if not window:
        return None
    if not window[0] - DURATION <= observed <= now < window[0]:
        return window[0]
    from trial_market_hour import listing_fields, merge_product_facts
    from trial_price_rules import field, grade

    records = []
    for candidate in batch[:PAGE_LIMIT]:
        try:
            item = _public_item(candidate) if isinstance(candidate, dict) else candidate
            raw = item.raw_data
            values = listing_fields(item)
            identity = values["id"]
            if not identity or values["url"] != "https://www.vinted.co.uk/items/" + identity:
                continue
            user = raw.get("user")
            country = raw.get("seller_country_code") or raw.get("seller_country")
            if not country and isinstance(user, dict):
                country = user.get("country_iso_code") or user.get("country_code")
            is_uk = not country or str(country).casefold() in ("gb", "uk", "united kingdom")
            public = (values["currency"] == "GBP" and is_uk and values["active"]
                      and raw.get("can_buy") is not False and raw.get("is_visible") is not False
                      and raw.get("is_available") is not False and raw.get("available") is not False)
            values.update(observed_at=observed, source="observed_catalogue")
            complete = (public and values["title"] and grade(values) and values["seller_id"]
                        and values["size"] and (values["ask_pence"] or 0) > 0)
            payload = values if complete else None
            records.append((identity, observed, field(values, "brand"), field(values, "size"),
                            values["seller_id"], payload))
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            continue
    with conn:
        # Preserve item-specific evidence over successive summary cards. A new seller or a
        # withdrawal cannot inherit another seller's condition or audience description.
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        ids = list(dict.fromkeys(r[0] for r in records))
        previous = {}
        if ids:
            marks = ",".join("?" for _ in ids)
            previous = {row[0]: (row[1], row[2], json.loads(row[3]))
                        for row in conn.execute(
                            f"SELECT id,observed,seller,payload FROM offers WHERE id IN ({marks}) AND observed>=? AND payload IS NOT NULL",
                            (*ids, now - MAX_AGE))}
        stored = []
        for identity, when, brand, size, seller, item in records:
            prior = previous.get(identity)
            if item is not None and prior and prior[0] <= when and seller and prior[1] == seller:
                item = merge_product_facts(item, prior[2])
            if not prior or prior[0] <= when:
                if item is None:
                    previous.pop(identity, None)
                else:
                    previous[identity] = (when, seller, item)
            stored.append((identity, when, brand, size, seller, json.dumps(item) if item is not None else None))
        conn.executemany("""
            INSERT INTO offers VALUES(?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET observed=excluded.observed, brand=excluded.brand,
                size=excluded.size, seller=excluded.seller, payload=excluded.payload
            WHERE excluded.observed>=offers.observed
        """, stored)
        conn.execute("DELETE FROM offers WHERE observed<?", (now - MAX_AGE,))
        count = conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
        if count > ROW_LIMIT:
            conn.execute("DELETE FROM offers WHERE id IN (SELECT id FROM offers ORDER BY observed LIMIT ?)",
                         (count - ROW_LIMIT,))
    _log_inventory(conn, now)
    return window[0]


def _log_inventory(conn, now):
    global _next_inventory_log
    if now < _next_inventory_log:
        return
    _next_inventory_log = now + 60
    rows, complete, sellers, brands = conn.execute("""
        SELECT count(*),count(payload),count(DISTINCT CASE WHEN payload IS NOT NULL THEN seller END),
               count(DISTINCT CASE WHEN payload IS NOT NULL AND brand!='' THEN brand END)
        FROM offers WHERE observed BETWEEN ? AND ?""", (now - MAX_AGE, now)).fetchone()
    log.info("Finder hourly market inventory unique_listings=%d complete=%d sellers=%d brands=%d",
             rows, complete, sellers, brands)


def candidates(database, target, now=None):
    from trial_price_rules import field, size_lookup_keys
    path = _path(database)
    if not path.is_file():
        return []
    now = time.time() if now is None else now
    brand, sizes = field(target, "brand"), size_lookup_keys(target)
    if not sizes:
        return []
    placeholders = ",".join("?" for _ in sizes)
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.2)) as conn:
            offers = conn.execute(f"""
                SELECT payload FROM offers WHERE size IN ({placeholders}) AND (?='' OR brand IN (?, ''))
                  AND observed BETWEEN ? AND ? AND id<>? AND seller<>? AND payload IS NOT NULL
                ORDER BY (brand=?) DESC, observed DESC LIMIT 500""",
                (*sizes, brand, brand, now - MAX_AGE, now, str(target.get("id")), str(target.get("seller_id")), brand))
            return [json.loads(row[0]) for row in offers]
    except (OSError, ValueError, sqlite3.Error):
        return []


def latest_unavailable(database, ids, now=None):
    """A later withdrawal or incomplete observation supersedes an older price from any source."""
    path = _path(database)
    if not path.is_file() or not ids:
        return {}
    now = time.time() if now is None else now
    unique = list(dict.fromkeys(str(i) for i in ids))
    missing = {}
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.2)) as conn:
            for index in range(0, len(unique), 400):
                batch = unique[index:index + 400]
                placeholders = ",".join("?" for _ in batch)
                missing.update(conn.execute(
                    f"SELECT id,observed FROM offers WHERE id IN ({placeholders}) "
                    "AND payload IS NULL AND observed BETWEEN ? AND ?",
                    (*batch, now - MAX_AGE, now),
                ))
    except sqlite3.Error:
        return {}
    return missing
