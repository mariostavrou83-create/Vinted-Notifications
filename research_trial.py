"""Bounded 14-day research comparison, in its own Railway service and store.

No Telegram, offer, buyer-account or live-search writes. Shared outcome reads
are reused, but an arm only receives evidence for items it actually discovered.
"""

import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlsplit

import requests
from flask import Flask, abort, request
from lxml import html as lhtml

LOG = logging.getLogger("research_trial")
DURATION = 14 * 86400
CATALOG_LIMIT = 480
OUTCOME_LIMIT = 1200
HTTP_LIMIT = 1800
# Native HTML is about 1–2 MiB decoded; ingress is not Railway egress.
# Bound the decoded payload without exhausting the whole research day on a
# small number of legitimate pages. Stop known-slow outcomes after 24 hours.
BYTE_LIMIT = 512 * 1024 * 1024
ACTIVE_LIMIT = 175
ADMISSION_LIMIT = 25
DAY = 86400
QUICK_SECONDS = DAY
CATEGORIES = {
    "1037": "Jackets & coats",
    "12": "Tops",
    "13": "Knitwear",
    "196": "Hoodies",
    "9": "Trousers",
    "10": "Dresses",
    "11": "Skirts",
    "572": "Matching sets",
    "15": "Shorts",
    "183": "Denim",
    "576": "Sports zip tops",
}
ARMS = {
    "broad": "Broad new-brand discovery",
    "sellers": "Selected seller panels",
    "hybrid": "Adaptive hybrid",
}
STYLES = (
    "fur hood",
    "fur trim",
    "faux fur",
    "sherpa lined",
    "fur lined",
    "funnel neck",
    "mesh",
    "graphic",
    "tattoo print",
    "lace trim",
    "corset",
    "bustier",
    "babydoll",
    "henley",
    "foldover",
    "fold over",
    "yoga flare",
    "low rise",
    "wide leg",
    "bootcut",
    "asymmetric",
    "ruffled",
    "crochet",
    "cable knit",
    "off shoulder",
    "halter",
    "beaded",
    "embroidered",
    "contrast stitch",
    "leopard",
    "snakeskin",
    "suede",
    "leather",
    "cargo",
)
STYLES += (
    "cashmere",
    "merino",
    "linen",
    "silk",
    "cowl neck",
    "ribbed",
    "wrap",
    "tweed",
    "quilted",
    "down jacket",
    "puffer",
    "fleece",
    "waterproof",
    "workwear",
    "seamless",
    "compression",
    "sports bra",
    "leggings",
    "maternity",
    "tailored",
    "trench",
    "blazer",
)
STOP = frozenset(
    [
        "women",
        "womens",
        "woman",
        "ladies",
        "girls",
        "mens",
        "size",
        "small",
        "medium",
        "large",
        "xs",
        "xxs",
        "s",
        "m",
        "l",
        "xl",
        "uk",
        "us",
        "eu",
        "new",
        "tags",
        "bnwt",
        "vintage",
        "y2k",
        "retro",
        "rare",
        "beautiful",
        "gorgeous",
        "lovely",
        "excellent",
        "condition",
        "good",
        "brand",
        "genuine",
        "authentic",
        "sale",
        "sold",
        "bundle",
        "postage",
        "black",
        "white",
        "grey",
        "blue",
        "red",
        "pink",
        "brown",
        "cream",
        "beige",
        "navy",
        "cotton",
        "polyester",
        "viscose",
        "elastane",
        "top",
        "tops",
        "shirt",
        "blouse",
        "jumper",
        "jacket",
        "coat",
        "trousers",
        "skirt",
        "sleeves",
        "long",
        "short",
        "sleeve",
        "and",
        "with",
        "the",
        "for",
        "from",
        "a",
        "an",
        "of",
        "by",
        "in",
        "on",
    ]
)


def canonical(value):
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def money(value):
    try:
        number = Decimal(str(value).replace(",", ""))
        return (
            int(number * 100) if number.is_finite() and 0 <= number <= 10000 else None
        )
    except (InvalidOperation, ValueError, TypeError):
        return None


def item_url(value, identity=None):
    if not isinstance(value, str):
        return None
    if value.startswith("/"):
        value = "https://www.vinted.co.uk" + value
    try:
        p = urlsplit(value)
        match = re.fullmatch(r"/items/(\d{1,20})(?:-[^/?#]*)?", p.path)
        if (
            p.scheme == "https"
            and p.hostname in {"vinted.co.uk", "www.vinted.co.uk"}
            and p.port in (None, 443)
            and not p.username
            and not p.password
            and match
            and (identity is None or match[1] == str(identity))
        ):
            return "https://www.vinted.co.uk" + p.path
    except ValueError:
        pass
    return None


def age_bounds(value):
    value = " ".join(str(value).casefold().split())
    if value in {"just now", "a moment ago", "less than a minute ago"}:
        return 0, 60
    m = re.fullmatch(r"(a|an|\d+) (minute|hour|day|week|month|year)s? ago", value)
    if not m:
        return None
    n = 1 if m[1] in {"a", "an"} else int(m[1])
    unit = {
        "minute": 60,
        "hour": 3600,
        "day": DAY,
        "week": 7 * DAY,
        "month": 31 * DAY,
        "year": 366 * DAY,
    }[m[2]]
    return n * unit, (n + 1) * unit


def normalize(raw, category=None):
    if (
        not isinstance(raw, dict)
        or raw.get("is_closed") is True
        or raw.get("is_reserved") is True
    ):
        return None
    identity = str(raw.get("id", ""))
    if not re.fullmatch(r"\d{1,20}", identity):
        return None
    title = raw.get("title")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 500:
        return None
    price = raw.get("price") or {}
    if not isinstance(price, dict) or price.get("currency_code") != "GBP":
        return None
    amount = money(price.get("amount"))
    if amount is None or amount < 100:
        return None
    size = raw.get("size_title") or raw.get("size") or ""
    if size and str(size).split(" / ")[0].upper() not in {"XS", "S", "M"}:
        return None
    box = raw.get("item_box") if isinstance(raw.get("item_box"), dict) else {}
    brand = str(raw.get("brand_title") or box.get("first_line") or "Unbranded")[:100]
    if brand == title or brand in {"Not specified", "Unknown"}:
        brand = "Unbranded"
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    seller = str(raw.get("user_id") or user.get("id") or "")
    seller = seller if seller.isdigit() else None
    cat = str(category or raw.get("category") or raw.get("catalog_id") or "")
    photo = raw.get("photo") or {}
    photo = photo.get("url", "") if isinstance(photo, dict) else ""
    photo = photo if re.match(r"^https://images\d*\.vinted\.net/", photo) else ""
    return {
        "id": identity,
        "title": title.strip(),
        "price": amount,
        "brand": brand,
        "brand_id": str(raw.get("brand_id") or ""),
        "category": cat if cat in CATEGORIES else "",
        "size": str(size)[:60],
        "condition": str(raw.get("status") or raw.get("condition") or "")[:80],
        "seller": seller,
        "url": item_url(raw.get("url"), identity)
        or f"https://www.vinted.co.uk/items/{identity}",
        "photo": photo,
    }


def phrases(item):
    text = canonical(item["title"])
    found = [x for x in STYLES if re.search(r"\b" + re.escape(x) + r"\b", text)]
    brand = set(canonical(item["brand"]).split())
    for left, right in pairwise(text.split()):
        if (
            left not in STOP | brand
            and right not in STOP | brand
            and min(len(left), len(right)) >= 3
            and not left.isdigit()
            and not right.isdigit()
        ):
            found.append(left + " " + right)
    for token in text.split():
        if token not in STOP | brand and len(token) >= 4 and not token.isdigit():
            found.append(token)
    return list(dict.fromkeys(found))[:3]


def already_searched(item, exclusions):
    """Conservative novelty gate: existing brands and unbranded search types.

    Price caps are deliberately ignored: premium versions of an already-saved
    item type are still part of an existing search, not a new opportunity.
    """
    brand = canonical(item.get("brand"))
    names = exclusions.get("names", [])
    identity = str(item.get("brand_id") or "")
    if identity and identity in exclusions.get("brand_ids", []):
        return True
    if brand and brand not in {"unbranded", "no brand", "not specified"}:
        pattern = r"\b" + r"\s*".join(re.escape(w) for w in brand.split()) + r"\b"
        if any(re.search(pattern, name) for name in names):
            return True
    text = canonical(item.get("title"))
    if any(
        re.search(r"\b" + re.escape(name) + r"\b", text)
        for name in exclusions.get("brand_names", [])
        if name
    ):
        return True
    for scope in exclusions.get("unbranded", []):
        if (
            scope.get("category")
            and item.get("category")
            and scope["category"] != item["category"]
        ):
            continue
        if any(
            re.search(r"\b" + re.escape(word) + r"\b", text)
            for word in scope.get("keywords", [])
        ):
            return True
    return False


def parse_listing(body, identity):
    """Visible target sidebar + identity-scoped native record, never recommendations."""
    if not isinstance(body, str) or len(body) > 4 * 1024 * 1024:
        return {"state": "unknown"}
    try:
        tree = lhtml.fromstring(body)
    except (ValueError, lhtml.etree.ParserError):
        return {"state": "unknown"}
    canon = tree.xpath('//link[@rel="canonical"]/@href')
    if len(canon) != 1 or not item_url(canon[0], identity):
        return {"state": "identity_unverified"}
    summaries = tree.xpath('//*[@data-testid="item-page-summary-plugin"]')
    if len(summaries) != 1:
        return {"state": "unknown"}
    side = summaries[0].xpath(
        'ancestor::div[contains(@class,"item-page-sidebar-content")][1]'
    )
    if len(side) != 1:
        return {"state": "unknown"}
    side = side[0]
    from vinted_page_data import parse_purchase_item

    native = parse_purchase_item(body, str(identity)) or {}
    sold = any(
        " ".join(n.text_content().split()).casefold() == "sold"
        for n in side.xpath('.//div[contains(@class,"web_ui__Cell__success")]')
    )
    if sold and native.get("is_sold") is False:
        return {"state": "conflicting_status"}
    state = (
        "sold"
        if sold or native.get("is_sold") is True
        else (
            "reserved"
            if native.get("is_reserved") is True
            else "active" if native.get("can_buy") is True else "unknown"
        )
    )
    nodes = side.xpath('.//*[@data-testid="item-price"]')
    price = None
    if len(nodes) == 1:
        m = re.fullmatch(
            r"£\s*([\d,]+(?:\.\d{1,2})?)", " ".join(nodes[0].text_content().split())
        )
        price = money(m[1]) if m else None
    native_price = native.get("price") or {}
    if native_price.get("currency_code") == "GBP":
        n = money(native_price.get("amount"))
        if price is not None and n != price:
            return {"state": "conflicting_price"}
        price = n
    ages = side.xpath('.//*[@itemprop="upload_date"]')
    age = age_bounds(ages[0].text_content()) if len(ages) == 1 else None
    ids = {
        re.findall(r"^/member/(\d+)(?:-|$)", x)[0]
        for x in side.xpath(".//a/@href")
        if re.match(r"^/member/\d+(?:-|$)", x)
    }
    seller = str(native.get("user_id") or (next(iter(ids)) if len(ids) == 1 else ""))
    attrs = {}
    brand_nodes = side.xpath('.//a[starts-with(@href,"/brand/")]')
    brand_ids = {
        re.match(r"/brand/(\d+)", node.get("href", ""))[1]
        for node in brand_nodes
        if re.match(r"/brand/(\d+)", node.get("href", ""))
    }
    brand_label = (
        " ".join(brand_nodes[0].text_content().split()) if len(brand_ids) == 1 else ""
    )
    for key in ("size", "status", "condition"):
        values = side.xpath(f'.//*[@itemprop="{key}"]')
        if len(values) == 1:
            attrs[key] = " ".join(values[0].text_content().split())[:80]
    cats = [re.match(r"/catalog/(\d+)", p) for p in tree.xpath("//main//a/@href")]
    category = next((m[1] for m in cats if m and m[1] in CATEGORIES), "")
    return {
        "state": state,
        "price": price,
        "age_bounds": age,
        "seller": seller if seller.isdigit() else None,
        "size": attrs.get("size", ""),
        "condition": attrs.get("status") or attrs.get("condition", ""),
        "category": category,
        "brand": brand_label,
        "brand_id": next(iter(brand_ids)) if len(brand_ids) == 1 else "",
        "url": item_url(canon[0], identity),
    }


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as c, c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
            CREATE TABLE IF NOT EXISTS trial(id INTEGER PRIMARY KEY CHECK(id=1),started REAL,ends REAL,
                phase TEXT NOT NULL DEFAULT 'waiting',feed_at REAL,feed_cursor REAL NOT NULL DEFAULT 0,
                pause_until REAL NOT NULL DEFAULT 0,reason TEXT NOT NULL DEFAULT '',next_network REAL NOT NULL DEFAULT 0,
                baseline REAL,slow_windows INTEGER NOT NULL DEFAULT 0,last_heartbeat REAL,
                known_searches TEXT NOT NULL DEFAULT '[]');
            INSERT OR IGNORE INTO trial(id) VALUES(1);
            CREATE TABLE IF NOT EXISTS arms(id TEXT PRIMARY KEY,name TEXT,last_success REAL,error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS clocks(kind TEXT PRIMARY KEY,next_due REAL NOT NULL DEFAULT 0,cursor INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY,payload TEXT NOT NULL,first_seen REAL,last_seen REAL,
                entry_price INTEGER,state TEXT NOT NULL DEFAULT 'pending',checked REAL,next_check REAL NOT NULL DEFAULT 0,
                admitted REAL,admission_arm TEXT,published_lower REAL,published_upper REAL,ask_at_sold INTEGER,
                sold_at REAL,age_upper REAL,seller TEXT,live_at_entry INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS item_due ON items(admitted,state,next_check);
            CREATE TABLE IF NOT EXISTS seen(arm TEXT,item TEXT,observed REAL,source TEXT NOT NULL DEFAULT 'reuse',PRIMARY KEY(arm,item));
            CREATE TABLE IF NOT EXISTS groups(id TEXT PRIMARY KEY,brand TEXT,category TEXT,phrase TEXT,updated REAL);
            CREATE TABLE IF NOT EXISTS members(group_id TEXT,item TEXT,PRIMARY KEY(group_id,item));
            CREATE INDEX IF NOT EXISTS member_item ON members(item);
            CREATE TABLE IF NOT EXISTS sellers(id TEXT PRIMARY KEY,category TEXT,next_due REAL NOT NULL DEFAULT 0,
                verified INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS reads(id INTEGER PRIMARY KEY,started REAL,arm TEXT,kind TEXT,item TEXT,
                status TEXT NOT NULL DEFAULT 'running',body_bytes INTEGER NOT NULL DEFAULT 0,wire_bytes INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS reads_time ON reads(started);
            CREATE TABLE IF NOT EXISTS checks(id INTEGER PRIMARY KEY,item TEXT,checked REAL,state TEXT,price INTEGER,age_upper REAL);
            CREATE TABLE IF NOT EXISTS health(checked REAL PRIMARY KEY,p95 REAL,pending REAL,errors INTEGER);
            """)
            fields = {r[1] for r in c.execute("PRAGMA table_info(trial)")}
            for name, declaration in {
                "scope_version": "INTEGER NOT NULL DEFAULT 1",
                "exclusions": "TEXT NOT NULL DEFAULT '{}'",
                "network_cursor": "INTEGER NOT NULL DEFAULT 0",
                "reported_at": "REAL NOT NULL DEFAULT 0",
            }.items():
                if name not in fields:
                    c.execute(f"ALTER TABLE trial ADD COLUMN {name} {declaration}")
            c.executemany(
                "INSERT OR IGNORE INTO arms(id,name) VALUES (?,?)", ARMS.items()
            )
            c.executemany(
                "INSERT OR IGNORE INTO clocks(kind) VALUES (?)",
                [(x,) for x in ("broad", "sellers", "explore", "reference", "focus")],
            )

    def connect(self):
        c = sqlite3.connect(self.path, timeout=3)
        c.row_factory = sqlite3.Row
        return c

    def state(self, now=None):
        now = time.time() if now is None else now
        with closing(self.connect()) as c, c:
            c.execute(
                "UPDATE trial SET phase='completed',reason='14-day trial complete' WHERE ends<=? AND phase!='completed'",
                (now,),
            )
            return dict(c.execute("SELECT * FROM trial WHERE id=1").fetchone())

    def ingest(self, arm, items, now, *, observed=None, source=None):
        observed = now if observed is None else min(now, observed)
        with closing(self.connect()) as c, c:
            exclusions = json.loads(
                c.execute("SELECT exclusions FROM trial WHERE id=1").fetchone()[0]
            )
            items = [item for item in items if not already_searched(item, exclusions)]
            day = int(now // DAY) * DAY
            total = c.execute(
                "SELECT COUNT(*) FROM items WHERE admitted>=?", (day,)
            ).fetchone()[0]
            arm_new = c.execute(
                "SELECT COUNT(*) FROM items WHERE admitted>=? AND admission_arm=?",
                (day, arm),
            ).fetchone()[0]
            active = c.execute(
                "SELECT COUNT(*) FROM items WHERE admitted IS NOT NULL AND state IN ('pending','active','reserved','unknown')",
            ).fetchone()[0]
            for item in sorted(
                items, key=lambda x: hashlib.sha256(x["id"].encode()).digest()
            ):
                c.execute(
                    "INSERT INTO items(id,payload,first_seen,last_seen,entry_price,seller,live_at_entry) VALUES(?,?,?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,last_seen=excluded.last_seen,seller=COALESCE(items.seller,excluded.seller)",
                    (
                        item["id"],
                        json.dumps(item),
                        observed,
                        now,
                        item["price"],
                        item.get("seller"),
                    ),
                )
                c.execute(
                    "INSERT OR IGNORE INTO seen VALUES(?,?,?,?)",
                    (arm, item["id"], now, source or arm),
                )
                found = phrases(item)
                for phrase in found if item["category"] else []:
                    gid = hashlib.sha256(
                        (
                            canonical(item["brand"])
                            + "|"
                            + item["category"]
                            + "|"
                            + phrase
                        ).encode()
                    ).hexdigest()[:24]
                    c.execute(
                        "INSERT INTO groups VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated",
                        (gid, item["brand"], item["category"], phrase, now),
                    )
                    c.execute(
                        "INSERT OR IGNORE INTO members VALUES(?,?)", (gid, item["id"])
                    )
                if (
                    found
                    and active < ACTIVE_LIMIT
                    and total < ADMISSION_LIMIT
                    and arm_new < 9
                ):
                    added = c.execute(
                        "UPDATE items SET admitted=?,admission_arm=?,next_check=? WHERE id=? AND admitted IS NULL",
                        (now, arm, now, item["id"]),
                    ).rowcount
                    active += added
                    total += added
                    arm_new += added
                seller = item.get("seller")
                if (
                    seller
                    and found
                    and c.execute("SELECT COUNT(*) FROM sellers").fetchone()[0] < 8
                ):
                    c.execute(
                        "INSERT OR IGNORE INTO sellers(id,category) VALUES(?,?)",
                        (seller, item["category"]),
                    )
            c.execute("UPDATE arms SET last_success=?,error='' WHERE id=?", (now, arm))
            # Keep the pilot comfortably inside its small volume. Outcome
            # cohorts are protected; older untracked details are expendable.
            if c.execute("SELECT COUNT(*) FROM items").fetchone()[0] > 12000:
                c.execute(
                    "DELETE FROM items WHERE admitted IS NULL AND id IN (SELECT id FROM items WHERE admitted IS NULL ORDER BY last_seen DESC LIMIT -1 OFFSET 11600)"
                )
                c.execute(
                    "DELETE FROM members WHERE item NOT IN (SELECT id FROM items)"
                )
                c.execute(
                    "DELETE FROM groups WHERE id NOT IN (SELECT group_id FROM members)"
                )
            c.execute(
                "DELETE FROM seen WHERE rowid IN (SELECT rowid FROM seen ORDER BY observed DESC LIMIT -1 OFFSET 50000)"
            )
        return len(items)

    def feed(self, data, now):
        if (
            data.get("version") != 2
            or not isinstance(data.get("health"), dict)
            or not isinstance(data.get("exclusions"), dict)
        ):
            raise ValueError("Unverified alert feed")
        health = data["health"]
        p95 = health.get("interval_p95")
        count = health.get("samples", 0)
        with closing(self.connect()) as c, c:
            old = c.execute("SELECT scope_version FROM trial WHERE id=1").fetchone()[0]
            if old < 2:
                # Keep the short initial setup sample as an archive, never as
                # evidence in the corrected trial that explicitly excludes it.
                c.execute(
                    "CREATE TABLE IF NOT EXISTS initial_scope_archive AS SELECT * FROM items"
                )
                for table in (
                    "seen",
                    "members",
                    "groups",
                    "items",
                    "checks",
                    "sellers",
                ):
                    c.execute(f"DELETE FROM {table}")
                c.execute("UPDATE arms SET last_success=NULL,error=''")
                c.execute("UPDATE clocks SET next_due=0,cursor=0")
                c.execute("UPDATE clocks SET next_due=1 WHERE kind!='broad'")
                c.execute(
                    "UPDATE trial SET scope_version=2,started=NULL,ends=NULL,phase='waiting',baseline=NULL,slow_windows=0,reported_at=0,network_cursor=0,next_network=0 WHERE id=1"
                )
            encoded = json.dumps(data["exclusions"], sort_keys=True)
            previous = c.execute("SELECT exclusions FROM trial WHERE id=1").fetchone()[
                0
            ]
            if encoded != previous:
                for row in c.execute("SELECT id,payload FROM items").fetchall():
                    if already_searched(json.loads(row["payload"]), data["exclusions"]):
                        c.execute(
                            "UPDATE items SET state='excluded' WHERE id=?", (row["id"],)
                        )
                        c.execute("DELETE FROM seen WHERE item=?", (row["id"],))
                c.execute("UPDATE trial SET exclusions=? WHERE id=1", (encoded,))
            c.execute(
                "UPDATE trial SET started=COALESCE(started,?),ends=COALESCE(ends,?),phase=CASE WHEN phase='waiting' THEN 'running' ELSE phase END,feed_at=?,last_heartbeat=? WHERE id=1",
                (now, now + DURATION, now, now),
            )
            c.execute(
                "UPDATE trial SET known_searches=? WHERE id=1",
                (json.dumps(data.get("known_searches", [])[:1000]),),
            )
            state = dict(c.execute("SELECT * FROM trial WHERE id=1").fetchone())
            if type(p95) in (int, float) and count >= 20:
                baseline = state["baseline"] or p95
                # A hard 3-second ceiling only applies where the measured baseline
                # already meets it. Always protect the baseline against slowdown.
                threshold = (
                    min(3.0, baseline + 0.15) if baseline < 3 else baseline + 0.15
                )
                slow = (
                    p95 > threshold
                    or health.get("pending_age", 0) > 15
                    or health.get("errors", 0) > max(2, count * 0.05)
                )
                windows = state["slow_windows"] + 1 if slow else 0
                c.execute(
                    "UPDATE trial SET baseline=?,slow_windows=? WHERE id=1",
                    (baseline, windows),
                )
                if windows >= 2:
                    c.execute(
                        "UPDATE trial SET pause_until=?,reason='Alert performance guard' WHERE id=1",
                        (now + 600,),
                    )
                elif (
                    not slow
                    and state["reason"] == "Alert performance guard"
                    and state["pause_until"] <= now
                ):
                    c.execute("UPDATE trial SET reason='' WHERE id=1")
                c.execute(
                    "INSERT OR REPLACE INTO health VALUES(?,?,?,?)",
                    (now, p95, health.get("pending_age", 0), health.get("errors", 0)),
                )
            c.execute(
                "UPDATE trial SET feed_cursor=MAX(feed_cursor,?) WHERE id=1",
                (min(now, float(data.get("cursor", now))),),
            )
        # Existing alerts are exclusion evidence only. No existing alert items
        # are admitted or followed by any of the three collection approaches.

    def can_read(self, kind, now):
        with closing(self.connect()) as c:
            return self._allowed(c, kind, now)

    def _allowed(self, c, kind, now):
        state = c.execute("SELECT * FROM trial WHERE id=1").fetchone()
        if (
            state["phase"] != "running"
            or state["pause_until"] > now
            or not state["feed_at"]
            or now - state["feed_at"] > 180
        ):
            return False
        if state["ends"] is None or state["ends"] <= now:
            return False
        day = int(now // DAY) * DAY
        n, body = c.execute(
            "SELECT COUNT(*),COALESCE(SUM(body_bytes),0) FROM reads WHERE started>=?",
            (day,),
        ).fetchone()
        relevant = c.execute(
            "SELECT COUNT(*) FROM reads WHERE started>=? AND kind IN ('broad','sellers','explore','reference','focus')",
            (day,),
        ).fetchone()[0]
        details = c.execute(
            "SELECT COUNT(*) FROM reads WHERE started>=? AND kind='outcome'", (day,)
        ).fetchone()[0]
        return (
            n < HTTP_LIMIT
            and body < BYTE_LIMIT
            and (
                details < OUTCOME_LIMIT
                if kind == "outcome"
                else relevant < CATALOG_LIMIT
            )
        )

    def reserve_read(self, arm, kind, item=None, now=None):
        now = time.time() if now is None else now
        with closing(self.connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            if not self._allowed(c, kind, now):
                raise RuntimeError("Research budget or guard active")
            return c.execute(
                "INSERT INTO reads(started,arm,kind,item) VALUES(?,?,?,?)",
                (now, arm, kind, item),
            ).lastrowid

    def finish_read(self, identity, status, body, wire, now):
        with closing(self.connect()) as c, c:
            c.execute(
                "UPDATE reads SET status=?,body_bytes=?,wire_bytes=? WHERE id=?",
                (status, body, wire, identity),
            )
            if status in {"http_401", "http_403", "http_429", "challenge"}:
                c.execute(
                    "UPDATE trial SET pause_until=MAX(pause_until,?),reason=? WHERE id=1",
                    (now + 3600, "Marketplace access backoff"),
                )

    def record_outcome(self, identity, data, now):
        with closing(self.connect()) as c, c:
            row = c.execute("SELECT * FROM items WHERE id=?", (identity,)).fetchone()
            if row is None:
                return
            state = data.get("state", "unknown")
            state = (
                state
                if state in {"active", "reserved", "sold", "unavailable"}
                else "unknown"
            )
            lower, upper = row["published_lower"], row["published_upper"]
            age = data.get("age_bounds")
            if age:
                new_lower, new_upper = now - age[1], now - age[0]
                lower = max(lower, new_lower) if lower is not None else new_lower
                upper = min(upper, new_upper) if upper is not None else new_upper
                if lower > upper:
                    lower = upper = None
            if (
                state in {"active", "reserved"}
                and upper is not None
                and now - upper >= QUICK_SECONDS
            ):
                state = "unsold_24h"
            elif (
                state == "unknown"
                and row["admitted"] is not None
                and now - row["admitted"] >= QUICK_SECONDS
            ):
                state = "unverified"
            age_upper = now - lower if state == "sold" and lower is not None else None
            price = data.get("price")
            sold_price = price if state == "sold" else None
            payload = json.loads(row["payload"])
            for key in (
                "size",
                "condition",
                "category",
                "url",
                "seller",
                "brand",
                "brand_id",
            ):
                if data.get(key):
                    payload[key] = data[key]
            if price is not None:
                payload["price"] = price
            exclusions = json.loads(
                c.execute("SELECT exclusions FROM trial WHERE id=1").fetchone()[0]
            )
            if already_searched(payload, exclusions):
                c.execute(
                    "UPDATE items SET payload=?,state='excluded' WHERE id=?",
                    (json.dumps(payload), identity),
                )
                c.execute("DELETE FROM seen WHERE item=?", (identity,))
                return
            seller = data.get("seller") or row["seller"]
            c.execute(
                "UPDATE items SET payload=?,state=?,checked=?,next_check=?,published_lower=?,published_upper=?,ask_at_sold=?,sold_at=?,age_upper=?,seller=? WHERE id=?",
                (
                    json.dumps(payload),
                    state,
                    now,
                    now + (4 * 3600 if state in {"active", "reserved"} else 12 * 3600),
                    lower,
                    upper,
                    sold_price,
                    now if state == "sold" else None,
                    age_upper,
                    seller,
                    identity,
                ),
            )
            c.execute(
                "INSERT INTO checks(item,checked,state,price,age_upper) VALUES(?,?,?,?,?)",
                (identity, now, state, price, age_upper),
            )
            if seller and c.execute("SELECT COUNT(*) FROM sellers").fetchone()[0] < 8:
                c.execute(
                    "INSERT OR IGNORE INTO sellers(id,category) VALUES(?,?)",
                    (seller, payload.get("category", "")),
                )
            if payload.get("category") in CATEGORIES:
                for phrase in phrases(payload):
                    gid = hashlib.sha256(
                        (
                            canonical(payload["brand"])
                            + "|"
                            + payload["category"]
                            + "|"
                            + phrase
                        ).encode()
                    ).hexdigest()[:24]
                    c.execute(
                        "INSERT INTO groups VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated",
                        (gid, payload["brand"], payload["category"], phrase, now),
                    )
                    c.execute(
                        "INSERT OR IGNORE INTO members VALUES(?,?)", (gid, identity)
                    )

    def control(self, action, now):
        if action not in {"pause", "resume"}:
            raise ValueError("Unknown action")
        state = self.state(now)
        if state["phase"] == "completed":
            return
        with closing(self.connect()) as c, c:
            c.execute(
                "UPDATE trial SET phase=?,reason=? WHERE id=1",
                (
                    "paused" if action == "pause" else "running",
                    "Paused by owner" if action == "pause" else "",
                ),
            )

    def candidates(self, arm, now, limit=8):
        results = []
        with closing(self.connect()) as c:
            known = json.loads(
                c.execute("SELECT known_searches FROM trial WHERE id=1").fetchone()[0]
            )
            groups = c.execute(
                "SELECT g.*,COUNT(*) n FROM groups g JOIN members m ON m.group_id=g.id JOIN items i ON i.id=m.item JOIN seen s ON s.item=m.item AND s.arm=? GROUP BY g.id HAVING COUNT(*)>=3 ORDER BY COUNT(*) DESC LIMIT 80",
                (arm,),
            ).fetchall()
            for g in groups:
                rows = c.execute(
                    "SELECT i.*,s.observed FROM members m JOIN items i ON i.id=m.item JOIN seen s ON s.item=i.id AND s.arm=? WHERE m.group_id=?",
                    (arm, g["id"]),
                ).fetchall()
                prices = sorted(r["entry_price"] for r in rows)
                high_floor = prices[(len(prices) * 3) // 4]
                resale = high_floor * 90 // 100
                buy_limit = (
                    max(0, min(resale - 1800 - 300, resale * 70 // 100)) // 50 * 50
                )
                cheap = [
                    r
                    for r in rows
                    if r["entry_price"] + r["entry_price"] * 5 // 100 + 70 + 220
                    <= buy_limit
                ]
                premium = [
                    r for r in rows if r["entry_price"] >= high_floor and r not in cheap
                ]

                def quick(r):
                    return (
                        r["state"] == "sold"
                        and r["ask_at_sold"] is not None
                        and r["age_upper"] is not None
                        and 0 <= r["age_upper"] <= QUICK_SECONDS
                    )

                cs = [
                    r
                    for r in cheap
                    if quick(r)
                    and r["ask_at_sold"] + r["ask_at_sold"] * 5 // 100 + 290
                    <= buy_limit
                ]
                hs = [r for r in premium if quick(r) and r["ask_at_sold"] >= high_floor]
                sellers = lambda rs: len({r["seller"] for r in rs if r["seller"]})
                enrolled = [r for r in rows if r["admitted"] is not None]
                matured = [
                    r
                    for r in enrolled
                    if now - (r["published_upper"] or r["admitted"]) >= QUICK_SECONDS
                ]
                cheap_enrolled = [r for r in cheap if r in matured]
                high_enrolled = [r for r in premium if r in matured]
                resolved = sum(
                    bool(
                        r["state"] in {"sold", "unsold_24h"}
                        or r["checked"]
                        and r["published_upper"]
                        and r["checked"] - r["published_upper"] >= QUICK_SECONDS
                    )
                    for r in matured
                )
                mature_cs = [r for r in cs if r in matured]
                mature_hs = [r for r in hs if r in matured]
                arrivals = sum(r["observed"] >= now - 7 * DAY for r in cheap)
                enough = (
                    len(cs) >= 5
                    and len(hs) >= 5
                    and sellers(cs) >= 3
                    and sellers(hs) >= 3
                    and len(cheap_enrolled) >= 15
                    and len(high_enrolled) >= 15
                    and len(mature_cs) / len(cheap_enrolled) >= 0.3
                    and len(mature_hs) / len(high_enrolled) >= 0.3
                    and resolved >= len(matured) * 0.8
                    and arrivals >= 3
                )
                stage = (
                    "Ready for review"
                    if enough
                    else "Promising" if len(cs) >= 2 and len(hs) >= 2 else "Watching"
                )
                samples = [
                    json.loads(r["payload"])
                    | {
                        "state": r["state"],
                        "age_days": (
                            round(r["age_upper"] / DAY, 2)
                            if r["age_upper"] is not None
                            else None
                        ),
                    }
                    for r in sorted(
                        rows, key=lambda r: (not quick(r), -r["last_seen"])
                    )[:4]
                ]
                results.append(
                    {
                        "id": g["id"],
                        "name": f"{g['brand']} · {g['phrase']}",
                        "category": CATEGORIES.get(g["category"], "Category pending"),
                        "count": len(rows),
                        "tracked": len(enrolled),
                        "cheap_sold": len(cs),
                        "premium_sold": len(hs),
                        "cheap_tracked": len(cheap_enrolled),
                        "premium_tracked": len(high_enrolled),
                        "sellers": sellers(rows),
                        "cheap_arrivals": arrivals,
                        "stage": stage,
                        "buy_limit": buy_limit,
                        "resale_estimate": resale,
                        "samples": samples,
                        "covered": any(
                            canonical(g["brand"]) in name
                            and set(g["phrase"].split()) <= set(name.split())
                            for name in known
                        ),
                        "item_ids": frozenset(r["id"] for r in rows),
                    }
                )
        results.sort(
            key=lambda x: (
                {"Ready for review": 0, "Promising": 1, "Watching": 2}[x["stage"]],
                -(x["cheap_sold"] + x["premium_sold"]),
                -x["count"],
            )
        )
        selected = []
        results.sort(
            key=lambda x: (
                x["covered"],
                {"Ready for review": 0, "Promising": 1, "Watching": 2}[x["stage"]],
                -(x["cheap_sold"] + x["premium_sold"]),
                -x["count"],
                -len(x["name"]),
            )
        )
        for candidate in results:
            if any(
                len(candidate["item_ids"] & other["item_ids"])
                / min(len(candidate["item_ids"]), len(other["item_ids"]))
                >= 0.8
                for other in selected
            ):
                continue
            selected.append(candidate)
            if len(selected) >= limit:
                break
        for candidate in selected:
            candidate.pop("item_ids")
        return selected

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        state = self.state(now)
        day = int(now // DAY) * DAY
        with closing(self.connect()) as c:
            arms = []
            for key in ARMS:
                n = c.execute(
                    "SELECT COUNT(*) FROM seen WHERE arm=?", (key,)
                ).fetchone()[0]
                tracked = c.execute(
                    "SELECT COUNT(*) FROM seen s JOIN items i ON i.id=s.item WHERE s.arm=? AND i.admitted IS NOT NULL",
                    (key,),
                ).fetchone()[0]
                fast = c.execute(
                    "SELECT COUNT(*) FROM seen s JOIN items i ON i.id=s.item WHERE s.arm=? AND i.state='sold' AND i.age_upper BETWEEN 0 AND ?",
                    (key, QUICK_SECONDS),
                ).fetchone()[0]
                reads = c.execute(
                    "SELECT COUNT(*) FROM reads WHERE arm=? AND started>=?",
                    (key, state["started"] or now),
                ).fetchone()[0]
                armrow = dict(
                    c.execute("SELECT * FROM arms WHERE id=?", (key,)).fetchone()
                )
                cand = self.candidates(key, now)
                arms.append(
                    armrow
                    | {
                        "seen": n,
                        "tracked": tracked,
                        "fast_sold": fast,
                        "reads": reads,
                        "candidates": cand,
                        "promising": sum(x["stage"] != "Watching" for x in cand),
                    }
                )
            usage = dict(
                c.execute(
                    "SELECT COUNT(*) requests,COALESCE(SUM(body_bytes),0) body_bytes,COALESCE(SUM(wire_bytes),0) wire_bytes FROM reads WHERE started>=?",
                    (day,),
                ).fetchone()
            )
            outcome = c.execute(
                "SELECT COUNT(*) FROM reads WHERE started>=? AND kind='outcome'", (day,)
            ).fetchone()[0]
            shared = c.execute(
                "SELECT COUNT(*) FROM items WHERE (SELECT COUNT(*) FROM seen WHERE item=items.id)>1 AND admitted IS NOT NULL"
            ).fetchone()[0]
            sellers = c.execute(
                "SELECT COUNT(*) FROM sellers WHERE verified=1"
            ).fetchone()[0]
            reference = c.execute(
                "SELECT COUNT(*) FROM seen WHERE arm='hybrid' AND source='reference'"
            ).fetchone()[0]
            latest_health = c.execute(
                "SELECT * FROM health ORDER BY checked DESC LIMIT 1"
            ).fetchone()
            recent = [
                dict(r)
                for r in c.execute(
                    "SELECT started,arm,kind,status,body_bytes FROM reads ORDER BY id DESC LIMIT 8"
                )
            ]
        return {
            "version": 1,
            "checked_at": now,
            "trial": state,
            "arms": arms,
            "usage": usage
            | {
                "outcome_reads": outcome,
                "http_limit": HTTP_LIMIT,
                "byte_limit": BYTE_LIMIT,
            },
            "shared_tracked": shared,
            "reference_seen": reference,
            "seller_panels_verified": sellers,
            "health": dict(latest_health) if latest_health else None,
            "recent": recent,
            "method": "Shared collection and outcome checks; an arm only counts listings it discovered. This is an operational comparison, not a randomised experiment.",
            "limits": {
                "active": ACTIVE_LIMIT,
                "admissions_per_day": ADMISSION_LIMIT,
                "catalogue_reads_per_day": CATALOG_LIMIT,
                "outcome_reads_per_day": OUTCOME_LIMIT,
            },
        }

    def diagnostics(self, now):
        """Small private operational summary; no transport or account secrets."""
        state = self.state(now)
        with closing(self.connect()) as c:
            threshold = min(3.0, (state["baseline"] or 3) + 0.15)
            health = c.execute(
                "SELECT COUNT(*) samples,SUM(p95>?) slow,MIN(p95) minimum,AVG(p95) average,MAX(p95) maximum FROM health WHERE checked>=?",
                (threshold, now - DAY),
            ).fetchone()
            return {
                "phase": state["phase"],
                "reason": state["reason"],
                "pause_seconds": max(0, int(state["pause_until"] - now)),
                "feed_age": (
                    None if not state["feed_at"] else int(now - state["feed_at"])
                ),
                "baseline": state["baseline"],
                "guard_threshold": threshold,
                "health_24h": dict(health),
                "last_read": c.execute("SELECT MAX(started) FROM reads").fetchone()[0],
                "reads_24h": [
                    dict(r)
                    for r in c.execute(
                        "SELECT kind,status,COUNT(*) count,SUM(body_bytes) body_bytes FROM reads WHERE started>=? GROUP BY kind,status",
                        (now - DAY,),
                    )
                ],
                "cohorts": [
                    dict(r)
                    for r in c.execute(
                        "SELECT admission_arm arm,CASE WHEN entry_price<=2500 THEN 'low' WHEN entry_price>=3000 THEN 'high' ELSE 'middle' END band,state,COUNT(*) count FROM items WHERE admitted IS NOT NULL GROUP BY admission_arm,band,state"
                    )
                ],
                "due_outcomes": c.execute(
                    "SELECT COUNT(*) FROM items WHERE admitted IS NOT NULL AND state IN ('pending','active','reserved','unknown') AND next_check<=?",
                    (now,),
                ).fetchone()[0],
                "clocks": [dict(r) for r in c.execute("SELECT * FROM clocks")],
                "sold": [
                    dict(r)
                    for r in c.execute(
                        "SELECT id,entry_price,ask_at_sold,age_upper,admission_arm FROM items WHERE state='sold' LIMIT 12"
                    )
                ],
                "seller_panels": [
                    dict(r)
                    for r in c.execute(
                        "SELECT category,verified,next_due,error FROM sellers"
                    )
                ],
            }


class Transport:
    def __init__(self, store, proxy, user_agent=None):
        from vinted_http import BROWSER_USER_AGENT, BrowserSession

        self.store = store
        self.session = BrowserSession()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": user_agent or BROWSER_USER_AGENT,
                "Accept-Language": "en-GB",
                "Referer": "https://www.vinted.co.uk/",
            }
        )
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def read(self, url, arm, kind, *, params=None, item=None, method="GET"):
        identity = self.store.reserve_read(arm, kind, item)
        status = "read_unavailable"
        body = b""
        wire = 0
        size = 0
        started = time.monotonic()
        try:
            headers = (
                {"Accept": "application/json"}
                if "api.vinted.co.uk" in url or "/api/" in url
                else {"Accept": "text/html"}
            )
            token = self.session.cookies.get("access_token_web")
            if token and headers["Accept"] == "application/json":
                headers["Authorization"] = "Bearer " + token
                headers["Origin"] = "https://www.vinted.co.uk"
                anon = self.session.cookies.get("anon_id")
                if anon:
                    headers["x-anon-id"] = anon
            with self.session.request(
                method,
                url,
                params=params,
                headers=headers,
                timeout=(2, 5),
                allow_redirects=False,
                stream=True,
            ) as r:
                if r.status_code in (301, 302, 307, 308):
                    target = r.headers.get("Location", "")
                    safe = item_url(target, item) if item else None
                    if not safe:
                        raise ValueError("Unverified redirect")
                    status = "redirect"
                    return None, safe
                if r.status_code != 200:
                    status = "http_" + str(r.status_code)
                    if r.status_code in (404, 410):
                        return b"", None
                    raise ValueError(status)
                chunks = []
                size = 0
                for chunk in r.iter_content(65536):
                    size += len(chunk)
                    if size > 4 * 1024 * 1024 or time.monotonic() - started > 8:
                        raise ValueError("Response too large")
                    chunks.append(chunk)
                body = b"".join(chunks)
                length = r.headers.get("Content-Length", "")
                wire = int(length) if str(length).isdigit() else len(body)
                status = "ok"
                if (
                    b"captcha-delivery.com" in body[:120000]
                    and b"item-page-summary-plugin" not in body
                ):
                    status = "challenge"
                    raise ValueError("challenge")
                return body, None
        finally:
            self.store.finish_read(
                identity,
                status,
                max(len(body), size),
                wire or max(len(body), size),
                time.time(),
            )

    def auth(self, arm, kind):
        if not self.session.cookies.get("access_token_web"):
            self.read("https://www.vinted.co.uk/", arm, "auth", method="HEAD")
        if not self.session.cookies.get("access_token_web"):
            raise ValueError("Anonymous session unavailable")

    def catalogue(self, params, kind):
        arm = "broad" if kind == "broad" else "hybrid"
        self.auth(arm, kind)
        body, _ = self.read(
            "https://api.vinted.co.uk/svc-catalogue/items",
            arm,
            kind,
            params=params,
        )
        data = json.loads(body)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise TypeError("Catalogue format unavailable")
        return data["items"][:48]

    def wardrobe(self, seller):
        self.auth("sellers", "sellers")
        body, _ = self.read(
            f"https://www.vinted.co.uk/api/v2/wardrobe/{seller}/items",
            "sellers",
            "sellers",
            params={"page": 1, "per_page": 48, "order": "newest_first"},
        )
        data = json.loads(body)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise TypeError("Wardrobe format unavailable")
        results = []
        for raw in data["items"][:48]:
            owner = str(raw.get("user_id") or (raw.get("user") or {}).get("id") or "")
            if owner and owner != seller:
                raise ValueError("Seller identity mismatch")
            # The identity-scoped wardrobe endpoint supplies the owner even when
            # individual cards omit it. Never read recommendation collections.
            raw = dict(raw, user_id=seller)
            item = normalize(raw)
            if item:
                results.append(item)
        return results

    def outcome(self, item):
        url = item["url"]
        for _ in range(2):
            body, redirect = self.read(url, "shared", "outcome", item=item["id"])
            if redirect:
                url = redirect
                continue
            if not body:
                return {"state": "unavailable"}
            return parse_listing(body.decode("utf-8", errors="replace"), item["id"])
        return {"state": "unknown"}


class Worker:
    def __init__(self, store, feed_url, token):
        self.store = store
        self.feed_url = feed_url
        self.token = token
        self.transport = None
        self.stop = threading.Event()
        self.feed_due = 0
        self.diagnostics_due = 0

    def fetch_feed(self, now):
        state = self.store.state(now)
        with requests.get(
            self.feed_url,
            params={"since": max(0, state["feed_cursor"] - 60)},
            headers={"Authorization": "Bearer " + self.token},
            timeout=(2, 4),
        ) as r:
            r.raise_for_status()
            data = r.json()
        self.store.feed(data, now)
        if self.transport is None:
            network = data.get("network") or {}
            if not network.get("proxy"):
                raise ValueError("Configured research proxy unavailable")
            self.transport = Transport(
                self.store, network["proxy"], network.get("user_agent")
            )

    def step(self, now=None):
        now = time.time() if now is None else now
        state = self.store.state(now)
        if now >= self.diagnostics_due:
            self.diagnostics_due = now + 3600
            LOG.info(
                "Research trial diagnostics %s",
                json.dumps(self.store.diagnostics(now), separators=(",", ":")),
            )
        if state["started"] and (
            now - state["reported_at"] >= DAY
            or state["phase"] == "completed"
            and state["reported_at"] < state["ends"]
        ):
            self.report(now)
        if state["phase"] in {"paused", "completed"}:
            return
        if now >= self.feed_due:
            self.feed_due = now + 60
            try:
                self.fetch_feed(now)
            except (requests.RequestException, ValueError, KeyError):
                LOG.warning("Research feed unavailable; new marketplace work waits")
                return
        state = self.store.state(now)
        if (
            self.transport is None
            or not self.store.can_read("outcome", now)
            or state["next_network"] > now
        ):
            return
        with closing(self.store.connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            state = c.execute("SELECT * FROM trial WHERE id=1").fetchone()
            if state["next_network"] > now:
                return
            c.execute(
                "UPDATE trial SET next_network=?,last_heartbeat=?,network_cursor=network_cursor+1 WHERE id=1",
                (now + 45, now),
            )
            c.execute(
                "UPDATE items SET state='expired' WHERE admitted<? AND state IN ('pending','active','reserved','unknown')",
                (now - 8 * DAY,),
            )
            # One source discovery slot followed by several outcome slots, with
            # independent source clocks. No parallel marketplace requests.
            due = c.execute(
                "SELECT * FROM clocks WHERE next_due<=? ORDER BY next_due LIMIT 1",
                (now,),
            ).fetchone()
            item = c.execute(
                "SELECT * FROM items WHERE admitted IS NOT NULL AND state IN ('pending','active','reserved','unknown') AND next_check<=? ORDER BY next_check LIMIT 1",
                (now,),
            ).fetchone()
            source = (
                due if due and (not item or state["network_cursor"] % 3 == 0) else None
            )
            if source:
                interval = {
                    "broad": 600,
                    "sellers": 1800,
                    "explore": 900,
                    "reference": 1800,
                    "focus": 600,
                }[source["kind"]]
                c.execute(
                    "UPDATE clocks SET next_due=?,cursor=cursor+1 WHERE kind=?",
                    (now + interval, source["kind"]),
                )
        arm = (
            source["kind"]
            if source and source["kind"] in {"broad", "sellers"}
            else "hybrid"
        )
        try:
            if source:
                kind = source["kind"]
                cursor = source["cursor"]
                if not self.store.can_read(kind, now):
                    return
                if kind == "sellers":
                    with closing(self.store.connect()) as c:
                        seller = c.execute(
                            "SELECT * FROM sellers ORDER BY next_due LIMIT 1"
                        ).fetchone()
                    if not seller:
                        return
                    results = self.transport.wardrobe(seller["id"])
                    accepted = self.store.ingest("sellers", results, now)
                    with closing(self.store.connect()) as c, c:
                        c.execute(
                            "UPDATE sellers SET verified=1,error='',next_due=? WHERE id=?",
                            (now + 4 * 3600, seller["id"]),
                        )
                else:
                    category = list(CATEGORIES)[(cursor // 2) % len(CATEGORIES)]
                    band = cursor % 2
                    params = {
                        "attribute_ids[catalog]": category,
                        "attribute_ids[size]": "2,3,4",
                        "currency": "GBP",
                        "order": "newest_first",
                        "page": 1,
                        "per_page": 48,
                    }
                    if band:
                        params["price_from"] = 30
                    else:
                        params.update(price_from=1, price_to=25)
                    if kind == "focus":
                        candidates = self.store.candidates("hybrid", now, 2)
                        if candidates:
                            with closing(self.store.connect()) as c:
                                g = c.execute(
                                    "SELECT * FROM groups WHERE id=?",
                                    (
                                        candidates[(cursor // 2) % len(candidates)][
                                            "id"
                                        ],
                                    ),
                                ).fetchone()
                            category = g["category"] or category
                            params["attribute_ids[catalog]"] = category
                            params["search_text"] = (
                                g["brand"] + " " + g["phrase"]
                            ).replace("Unbranded ", "")
                    if kind == "reference":
                        params["order"] = "newest_first"
                    results = [
                        x
                        for raw in self.transport.catalogue(params, kind)
                        if (x := normalize(raw, category))
                    ]
                    accepted = self.store.ingest(arm, results, now, source=kind)
                LOG.info(
                    "Research trial sample arm=%s kind=%s accepted=%d",
                    arm,
                    kind,
                    accepted,
                )
            elif item:
                data = self.transport.outcome(json.loads(item["payload"]))
                self.store.record_outcome(item["id"], data, now)
                LOG.info(
                    "Research trial outcome state=%s", data.get("state", "unknown")
                )
        except (
            requests.RequestException,
            ValueError,
            KeyError,
            RuntimeError,
            TypeError,
        ):
            with closing(self.store.connect()) as c, c:
                c.execute(
                    "UPDATE arms SET error='Collection unavailable; bounded retry' WHERE id=?",
                    (arm,),
                )
                if item and not source:
                    c.execute(
                        "UPDATE items SET next_check=? WHERE id=?",
                        (now + 3600, item["id"]),
                    )
            LOG.warning(
                "Research trial read unavailable arm=%s; no immediate retry", arm
            )

    def report(self, now):
        data = self.store.snapshot(now)
        with closing(self.store.connect()) as c, c:
            usage = dict(
                c.execute(
                    "SELECT COUNT(*) requests,COALESCE(SUM(body_bytes),0) body_bytes FROM reads WHERE started>=?",
                    (data["trial"]["started"],),
                ).fetchone()
            )
            c.execute("UPDATE trial SET reported_at=? WHERE id=1", (now,))
        result = {
            "scope": "new_brands_only",
            "excluded_searches": len(
                json.loads(data["trial"]["exclusions"]).get("names", [])
            ),
            "phase": data["trial"]["phase"],
            "started": datetime.fromtimestamp(
                data["trial"]["started"], UTC
            ).isoformat(),
            "ends": datetime.fromtimestamp(data["trial"]["ends"], UTC).isoformat(),
            "usage": usage,
            "health": data["health"],
            "arms": [
                {
                    k: a[k]
                    for k in (
                        "id",
                        "seen",
                        "tracked",
                        "fast_sold",
                        "reads",
                        "promising",
                    )
                }
                | {
                    "candidates": [
                        {
                            k: x[k]
                            for k in (
                                "name",
                                "stage",
                                "cheap_sold",
                                "premium_sold",
                                "sellers",
                                "buy_limit",
                            )
                        }
                        for x in a["candidates"][:3]
                    ]
                }
                for a in data["arms"]
            ],
        }
        LOG.info("Research trial summary %s", json.dumps(result, separators=(",", ":")))

    def run(self):
        while not self.stop.is_set():
            try:
                self.step()
            except (sqlite3.Error, OSError, ValueError):
                LOG.warning("Research worker waiting after a local error")
            self.stop.wait(5)


def make_app(store, token):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1024

    @app.before_request
    def protect():
        if request.endpoint == "health":
            return
        expected = "Bearer " + token
        if len(token) < 32 or not secrets.compare_digest(
            request.headers.get("Authorization", ""), expected
        ):
            abort(401)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/snapshot.json")
    def snapshot():
        return store.snapshot(), 200, {"Cache-Control": "no-store"}

    @app.post("/control")
    def control():
        try:
            store.control(
                (request.get_json(silent=True) or {}).get("action"), time.time()
            )
        except ValueError:
            abort(400)
        return {"phase": store.state()["phase"]}

    return app


if __name__ == "__main__":
    from waitress import serve

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    token = os.environ.get("MSJ_RESEARCH_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("Research service key is missing")
    store = Store(os.environ.get("MSJ_RESEARCH_DB", "/app/data/research_trial.sqlite3"))
    with closing(store.connect()) as c, c:
        for seed in os.environ.get("MSJ_RESEARCH_SELLER_SEEDS", "").split(","):
            seller, _, category = seed.partition(":")
            if seller.isdigit() and category in CATEGORIES:
                c.execute(
                    "INSERT OR IGNORE INTO sellers(id,category) VALUES(?,?)",
                    (seller, category),
                )
    worker = Worker(store, os.environ["MSJ_RESEARCH_FEED"], token)
    threading.Thread(target=worker.run, name="research-trial", daemon=True).start()
    LOG.info(
        "Three-arm research service ready; 14-day clock starts after verified feed"
    )
    serve(
        make_app(store, token), listen="*:" + os.environ.get("PORT", "8000"), threads=2
    )
