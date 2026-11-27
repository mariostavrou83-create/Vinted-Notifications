"""Private saved-search alerts sourced from Scavio's public Vinted UK search API."""

import hashlib
import json
import os
import re
import time
from collections import defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlsplit

import requests
from flask import current_app, g

from . import flow, market
from .db import DEFAULT_SETTINGS, get_db, transaction
from .inputs import money, search_url
from .listing_metadata import vinted_photo_url
from .market_runtime import config_for

SCHEMA = """
CREATE TABLE IF NOT EXISTS public_search_checkpoint(
 search_id INTEGER PRIMARY KEY REFERENCES searches(id) ON DELETE CASCADE,
 fingerprint TEXT NOT NULL, last_success REAL NOT NULL, highest INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS public_round(id INTEGER PRIMARY KEY CHECK(id=1), tick INTEGER NOT NULL);
INSERT OR IGNORE INTO public_round VALUES (1,-1);
CREATE TABLE IF NOT EXISTS public_pause(id INTEGER PRIMARY KEY CHECK(id=1),until REAL NOT NULL);
INSERT OR IGNORE INTO public_pause VALUES(1,0);
CREATE TABLE IF NOT EXISTS public_query_health(digest TEXT PRIMARY KEY, tick INTEGER NOT NULL, state TEXT NOT NULL);
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
IGNORED = {
    "order",
    "sort",
    "page",
    "time",
    "search_id",
    "referrer",
    "disabled_personalization",
}
PUBLIC_KEYS = (
    "PILOT_PUBLIC_SEARCH_ENABLED",
    "PILOT_PUBLIC_SEARCH_MAX_REQUESTS",
    "PILOT_PUBLIC_SEARCH_DAILY_LIMIT",
)


INTERVAL = 5
RATE_KEYS = (
    "PILOT_PUBLIC_SEARCH_REQUESTS_PER_SECOND",
    "PILOT_PUBLIC_SEARCH_CONCURRENCY",
)
MESSAGES = {
    "upcoming": "Waiting for the first five-second check.",
    "checked": "Last check received a fresh response.",
    "inflight": "Checking this search.",
    "capacity": "Five-second checks paused. The configured provider capacity is full.",
    "budget": "Five-second checks paused. The configured market-data allowance is exhausted.",
    "stale": "Five-second check missed. Fresh results have not arrived.",
    "backoff": "Five-second check missed. The provider requested a pause or is unavailable.",
    "failed": "Five-second check failed. The provider returned no verified results.",
    "provider_busy": "Five-second check missed. Another request is using the provider.",
    "quiet": "Monitoring is paused during quiet hours.",
    "off": "Public saved-search monitoring is off.",
    "disabled": "This saved search is paused.",
}


def capacity(config):
    try:
        rate, concurrent = (int(config.get(k, "1")) for k in RATE_KEYS)
    except (ValueError, TypeError):
        return 0, 0
    if not 1 <= rate <= 100 or not 1 <= concurrent <= 100:
        return 0, 0
    return rate, concurrent


def init_app(app):
    for key in PUBLIC_KEYS:
        app.config.setdefault(key, os.environ.get(key, ""))
    for key in RATE_KEYS:
        app.config.setdefault(key, os.environ.get(key, "1"))
    get_db().executescript(SCHEMA)


def limits(config):
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


def digest_for(body):
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def status(conn, user_id=None, now=None):
    now = time.time() if now is None else now
    options = config_for(conn, current_app.config)
    enabled, total, daily = limits(options)
    used = conn.execute("SELECT used FROM public_budget WHERE id=1").fetchone()[0]
    last = conn.execute(
        "SELECT count(*) FROM public_checks WHERE started>?", (now - 86400,)
    ).fetchone()[0]
    rate, concurrency = capacity(options)
    result = {
        "enabled": enabled,
        "mode": "public_search",
        "max_requests": total,
        "used_requests": used,
        "daily_limit": daily,
        "used_last_24h": last,
        "cadence_seconds": INTERVAL,
        "requests_per_second": rate,
        "concurrency": concurrency,
    }
    if user_id is None:
        return result
    health = {}
    unsupported = {}
    rows = conn.execute(
        """SELECT s.id,s.url,s.enabled,c.last_success,t.data FROM searches s
        LEFT JOIN public_search_checkpoint c ON c.search_id=s.id LEFT JOIN settings t ON t.user_id=s.user_id
        WHERE s.user_id=?""",
        (user_id,),
    )
    current_tick = int(now // INTERVAL)
    for row in rows:
        checked = row["last_success"]
        prefs = {**DEFAULT_SETTINGS, **json.loads(row["data"] or "{}")}
        message = None
        try:
            digest = digest_for(parameters(row["url"]))
            entry = conn.execute(
                "SELECT tick,state FROM public_query_health WHERE digest=?", (digest,)
            ).fetchone()
            state = entry["state"] if entry else "upcoming"
            if not row["enabled"]:
                state = "disabled"
            elif not enabled:
                state = "off"
            elif flow.quiet(prefs, now):
                state = "quiet"
            elif used >= total or last >= daily:
                state = "budget"
            elif not rate or not concurrency:
                state = "capacity"
            elif entry and (
                current_tick - entry["tick"] > 1
                or (state == "inflight" and current_tick > entry["tick"])
            ):
                state = "stale"
            elif state == "checked" and (checked is None or now - checked > INTERVAL):
                state = "stale" if checked is not None else "upcoming"
        except ValueError as exc:
            state, message = "unsupported", str(exc)
            unsupported[str(row["id"])] = message
        health[str(row["id"])] = {
            "state": state,
            "message": message or MESSAGES[state],
            "last_checked_at": checked,
            "target_seconds": INTERVAL,
        }
    return dict(result, searches=health, unsupported=unsupported)


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
            if not 1 <= len(items) <= 20 or not all(
                re.fullmatch(r"[1-9]\d{0,8}", v) for v in items
            ):
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
            if not isinstance(applied, list) or {str(v) for v in applied} != {
                str(v) for v in expected
            }:
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
        headers={
            "Authorization": "Bearer " + g.public_market_config["PILOT_SCAVIO_API_KEY"]
        },
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
    if (
        not isinstance(raw, dict)
        or raw.get("currency") != "GBP"
        or market._unavailable(raw)
    ):
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
    info = {
        k: market._text(raw.get(k))
        for k in market.DESCRIPTORS
        if market._text(raw.get(k))
    }
    photos = raw.get("photos")
    candidates = [raw.get("photo")] + (photos[:3] if isinstance(photos, list) else [])
    photo_url = next(
        (
            photo
            for candidate in candidates
            if (
                photo := vinted_photo_url(
                    candidate.get("url") if isinstance(candidate, dict) else candidate
                )
            )
        ),
        None,
    )
    return dict(
        info,
        id=ident,
        listing_url=url,
        title=title,
        seller_id=str(
            (seller.get("id") if isinstance(seller, dict) else raw.get("seller_id"))
            or ""
        ),
        description=market._text(raw.get("description")),
        currency="GBP",
        price_pence=price,
        subtotal_pence=subtotal,
        total_pence=subtotal,
        total_excludes_shipping=True,
        photo_url=photo_url,
        posted_at_exact=False,
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
        digest = digest_for(body)
        groups.setdefault(digest, (body, []))[1].append((dict(row), prefs))
    return groups


def _fingerprint(row):
    return hashlib.sha256(
        json.dumps(
            [
                row[key]
                for key in (
                    "url",
                    "include_keywords",
                    "exclude_keywords",
                    "budget_pence",
                )
            ]
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
        if isinstance(raw, dict)
        and re.fullmatch(r"[1-9]\d{0,17}", str(raw.get("id", "")))
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
        fresh = (
            prev
            and prev["fingerprint"] == _fingerprint(s)
            and now - prev["last_success"] < 1200
        )
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


def _mark(conn, digests, tick, state):
    conn.executemany(
        """INSERT INTO public_query_health VALUES(?,?,?)
           ON CONFLICT(digest) DO UPDATE SET tick=excluded.tick,state=excluded.state
           WHERE excluded.tick>=public_query_health.tick""",
        ((digest, tick, state) for digest in digests),
    )


def _fetch(app, body, options):
    try:
        with app.app_context():
            g.public_market_config = options
            return _search(body), 0
    except requests.HTTPError as exc:
        status_code = getattr(exc.response, "status_code", None)
        if status_code == 429:
            try:
                return None, max(
                    60, min(86400, int(exc.response.headers.get("Retry-After", "60")))
                )
            except (TypeError, ValueError):
                return None, 60
        return None, 3600 if status_code in (401, 402, 403) else 60
    except (
        requests.RequestException,
        ValueError,
        KeyError,
        TypeError,
        InvalidOperation,
    ):
        return None, 30


def public_discover(conn, now):
    options = config_for(conn, current_app.config)
    enabled, total, daily = limits(options)
    if not enabled:
        return False
    tick = int(now // INTERVAL)
    started = time.monotonic()

    def wall_time():
        return now + (time.monotonic() - started)

    rate, concurrent = capacity(options)
    with transaction(conn):
        if (
            conn.execute("SELECT tick FROM public_round WHERE id=1").fetchone()[0]
            >= tick
        ):
            return False
        conn.execute("UPDATE public_round SET tick=? WHERE id=1", (tick,))
        groups = _members(conn, now)
        if not groups:
            return False
        conn.execute("DELETE FROM public_checks WHERE started<=?", (now - 86400,))
        conn.execute("DELETE FROM public_query_health WHERE tick<?", (tick - 17280,))
        used = conn.execute("SELECT used FROM public_budget WHERE id=1").fetchone()[0]
        today = conn.execute("SELECT count(*) FROM public_checks").fetchone()[0]
        remaining = max(0, min(total - used, daily - today))
        lease = conn.execute(
            "SELECT available_at FROM market_budget WHERE id=1"
        ).fetchone()[0]
        if lease > now:
            backoff = conn.execute(
                "SELECT until FROM public_pause WHERE id=1"
            ).fetchone()[0]
            _mark(conn, groups, tick, "backoff" if backoff > now else "provider_busy")
            return False
        # First-created sources keep their capacity from one round to the next.
        sources = list(groups)
        supported = rate * INTERVAL if concurrent else 0
        _mark(conn, sources[supported:], tick, "capacity")
        _mark(conn, sources[min(remaining, supported) : supported], tick, "budget")
        pending = deque(sources[: min(remaining, supported)])
        if not pending:
            return False
        _mark(conn, pending, tick, "upcoming")
        # The same lock covers paid price lookups and other worker processes.
        lease_until = now + 30
        conn.execute(
            "UPDATE market_budget SET available_at=? WHERE id=1", (lease_until,)
        )

    app = current_app._get_current_object()
    next_tick = (tick + 1) * INTERVAL
    backoff_until = 0
    launches = deque(
        row[0]
        for row in conn.execute(
            "SELECT started FROM public_checks WHERE started>? ORDER BY started",
            (now - 1,),
        )
    )
    sent_any = False

    def reserve(digest, when):
        nonlocal sent_any
        with transaction(conn):
            current = config_for(conn, current_app.config)
            enabled, lifetime, per_day = limits(current)
            if not enabled:
                return "off"
            if current.get("_market_generation") != options.get("_market_generation"):
                return "stale"
            counts = conn.execute(
                """SELECT used, (SELECT COUNT(*) FROM public_checks WHERE started>?)
                FROM public_budget WHERE id=1""",
                (when - 86400,),
            ).fetchone()
            if counts[0] >= lifetime or counts[1] >= per_day:
                return "budget"
            conn.execute("UPDATE public_budget SET used=used+1 WHERE id=1")
            conn.execute("INSERT INTO public_checks(started) VALUES(?)", (when,))
            _mark(conn, [digest], tick, "inflight")
        sent_any = True
        launches.append(when)

    def finish(digest, response):
        nonlocal backoff_until
        rows, delay = response
        observed = wall_time()
        with transaction(conn):
            current_lease = conn.execute(
                "SELECT available_at FROM market_budget WHERE id=1"
            ).fetchone()[0]
            if current_lease != lease_until:
                _mark(conn, [digest], tick, "stale")
                return
            if rows is None:
                backoff_until = max(backoff_until, observed + delay)
                _mark(conn, [digest], tick, "failed")
            else:
                _record(conn, groups[digest][1], rows, observed)
                _mark(
                    conn, [digest], tick, "checked" if observed < next_tick else "stale"
                )

    try:
        if len(pending) == 1 and len(launches) < rate:
            digest = pending.popleft()
            pause = reserve(digest, wall_time())
            if pause:
                _mark(conn, [digest], tick, pause)
            else:
                finish(digest, _fetch(app, groups[digest][0], options))
        else:
            with ThreadPoolExecutor(max_workers=min(concurrent, len(pending))) as pool:
                inflight = {}
                while pending or inflight:
                    when = wall_time()
                    while launches and launches[0] <= when - 1:
                        launches.popleft()
                    if pending and (backoff_until or when >= next_tick):
                        _mark(
                            conn, pending, tick, "backoff" if backoff_until else "stale"
                        )
                        pending.clear()
                    while (
                        pending and len(inflight) < concurrent and len(launches) < rate
                    ):
                        digest = pending.popleft()
                        when = wall_time()
                        if when >= next_tick:
                            _mark(conn, [digest, *pending], tick, "stale")
                            pending.clear()
                            break
                        pause = reserve(digest, when)
                        if pause:
                            _mark(conn, [digest, *pending], tick, pause)
                            pending.clear()
                            break
                        inflight[
                            pool.submit(_fetch, app, groups[digest][0], options)
                        ] = digest
                    delay = (
                        max(0, min(next_tick, launches[0] + 1) - wall_time())
                        if launches
                        else 0
                    )
                    if inflight:
                        # Wake early when a rate slot returns and a worker is free.
                        timeout = (
                            delay if pending and len(inflight) < concurrent else None
                        )
                        done, _ = wait(
                            inflight, timeout=timeout, return_when=FIRST_COMPLETED
                        )
                        for future in done:
                            finish(inflight.pop(future), future.result())
                    elif pending and delay:
                        time.sleep(delay)
    finally:
        with transaction(conn):
            released = conn.execute(
                "UPDATE market_budget SET available_at=? WHERE id=1 AND available_at=?",
                (max(backoff_until, wall_time()), lease_until),
            )
            if released.rowcount and backoff_until:
                conn.execute(
                    "UPDATE public_pause SET until=MAX(until,?) WHERE id=1",
                    (backoff_until,),
                )
    return sent_any
