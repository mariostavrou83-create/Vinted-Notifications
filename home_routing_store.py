"""Owner-scoped, read-only Vinted Home sources and per-chat delivery receipts."""
import hashlib
import json
import math
import re
import secrets
import time
from contextlib import closing
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from search_settings import connection

ORIGINAL = "original"
LIFETIME = 600
AGE = 180
CODE = re.compile(r"[A-Za-z0-9_-]{24}")
NUMBER = re.compile(r"[1-9][0-9]{0,19}")


class Conflict(ValueError):
    pass


def migrate(conn):
    for statement in (
        """CREATE TABLE IF NOT EXISTS home_route_sources (
           owner INTEGER, id TEXT, label TEXT NOT NULL, account TEXT, token TEXT UNIQUE, last_seen REAL,
           PRIMARY KEY(owner,id), UNIQUE(owner,account))""",
        """CREATE TABLE IF NOT EXISTS home_route_destinations (
           owner INTEGER, id TEXT, label TEXT, chat INTEGER, partner INTEGER,
           PRIMARY KEY(owner,id), UNIQUE(owner,chat))""",
        """CREATE TABLE IF NOT EXISTS home_routes (
           owner INTEGER, source TEXT, dest TEXT, PRIMARY KEY(owner,source,dest))""",
        """CREATE TABLE IF NOT EXISTS home_route_codes (
           digest TEXT PRIMARY KEY, owner INTEGER, purpose TEXT, label TEXT, expires REAL)""",
        """CREATE TABLE IF NOT EXISTS home_route_events (
           owner INTEGER, source TEXT, item TEXT, expires REAL, observed REAL, PRIMARY KEY(owner,source,item))""",
        """CREATE TABLE IF NOT EXISTS home_route_jobs (
           owner INTEGER, chat INTEGER, item TEXT, body TEXT, expires REAL, state TEXT,
           next_try REAL DEFAULT 0, lease TEXT, receipt INTEGER, receipt_photo INTEGER DEFAULT 0, PRIMARY KEY(owner,chat,item))""",
        """CREATE INDEX IF NOT EXISTS home_route_due ON home_route_jobs(state,next_try,expires)""",
        """CREATE TABLE IF NOT EXISTS home_route_origins (
           owner INTEGER, chat INTEGER, item TEXT, source TEXT, dest TEXT, PRIMARY KEY(owner,chat,item,source,dest))""",
    ):
        conn.execute(statement)
    conn.execute("INSERT OR REPLACE INTO parameters(key,value) VALUES('home_route_schema','1')")


def install_schema(conn):
    marker = conn.execute("SELECT value FROM parameters WHERE key='home_route_schema'").fetchone()
    if not marker or marker[0] != "1":
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            migrate(conn)


def owner_id(conn):
    row = conn.execute("SELECT value FROM parameters WHERE key='telegram_chat_id'").fetchone()
    value = str(row[0]).strip() if row else ""
    return int(value) if re.fullmatch(r"[1-9][0-9]{0,17}", value) else 0


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _destinations(conn, owner):
    if not owner:
        return {}
    from telegram_shared_alerts import current

    dests = {"owner": ("Owner’s private Telegram", owner, None)}
    group = current(conn)
    if group:
        dests["shared-" + group.version] = ("Linked shared Telegram group", group.chat, group.partner)
    for key, label, chat, partner in conn.execute(
        "SELECT id,label,chat,partner FROM home_route_destinations WHERE owner=? ORDER BY id", (owner,)
    ):
        if not any(entry[1] == chat for entry in dests.values()):
            dests[key] = (label, chat, partner)
    return dests


def selected(conn, owner, source, dests):
    if source == ORIGINAL and not conn.execute(
        "SELECT 1 FROM home_route_sources WHERE owner=? AND id=?", (owner, ORIGINAL)
    ).fetchone():
        return [key for key in dests if key == "owner" or key.startswith("shared-")]
    return [r[0] for r in conn.execute(
        "SELECT dest FROM home_routes WHERE owner=? AND source=? ORDER BY dest", (owner, source)
    ) if r[0] in dests]


def snapshot(conn):
    owner = owner_id(conn)
    dests = _destinations(conn, owner)
    sources = [{"id": ORIGINAL, "label": "Current Vinted Home", "kind": "original",
                "connected": bool(owner), "destinations": selected(conn, owner, ORIGINAL, dests)}]
    for key, label, seen in conn.execute(
        "SELECT id,label,last_seen FROM home_route_sources WHERE owner=? AND id<>? ORDER BY label,id",
        (owner, ORIGINAL),
    ):
        sources.append({"id": key, "kind": "browser", "label": label, "connected": True,
                        "last_seen": seen, "state": "waiting" if not seen else "active" if time.time()-seen <= 600 else "stale",
                        "destinations": selected(conn, owner, key, dests)})
    public = [{"id": key, "label": label, "kind": "private" if chat > 0 else "group",
               "removable": key.startswith("chat-")} for key, (label, chat, partner) in dests.items()]
    stable = [(s["id"], s["label"], sorted(s["destinations"])) for s in sources]
    rev = _digest(json.dumps([owner, stable, public], sort_keys=True))
    return {"sources": sources, "destinations": public, "revision": rev}


def _prune(conn, now):
    row = conn.execute("SELECT value FROM parameters WHERE key='home_routes_pruned_at'").fetchone()
    if row and now - float(row[0]) < 3600:
        return
    conn.execute("DELETE FROM home_route_codes WHERE expires<=?", (now,))
    threshold = now - 86400
    conn.execute("DELETE FROM home_route_events WHERE expires<?", (threshold,))
    conn.execute("DELETE FROM home_route_jobs WHERE expires<?", (threshold,))
    conn.execute("""DELETE FROM home_route_origins AS o WHERE NOT EXISTS (SELECT 1 FROM home_route_jobs j
        WHERE (j.owner,j.chat,j.item)=(o.owner,o.chat,o.item))""")
    conn.execute("INSERT OR REPLACE INTO parameters(key,value) VALUES('home_routes_pruned_at',?)", (str(now),))


def configure(action, form):
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        owner = owner_id(conn)
        if not owner:
            raise ValueError("Connect the dashboard owner’s Telegram first.")
        state = snapshot(conn)
        now = time.time()
        _prune(conn, now)
        if action in {"pair", "pair_destination"}:
            label = str(form.get("label", "")).strip()
            if not 1 <= len(label) <= 64 or any(ord(c) < 32 for c in label):
                raise ValueError("Enter a name between 1 and 64 characters.")
            code = secrets.token_urlsafe(18)
            conn.execute("INSERT INTO home_route_codes VALUES(?,?,?,?,?)",
                         (_digest(code), owner, action, label, now + LIFETIME))
            return {"code": code, "expires_in": LIFETIME}
        if action not in {"routes", "remove", "remove_destination"}:
            raise ValueError("Unknown Home routing action.")
        if form.get("revision") != state["revision"]:
            raise Conflict("Routing has changed. Refresh and review the latest selections.")
        if action == "remove_destination":
            dest = form.get("destination")
            if dest not in {d["id"] for d in state["destinations"] if d["removable"]}:
                raise ValueError("Choose a paired Telegram destination.")
            conn.execute("DELETE FROM home_routes WHERE owner=? AND dest=?", (owner, dest))
            conn.execute("DELETE FROM home_route_origins WHERE owner=? AND dest=?", (owner, dest))
            conn.execute("DELETE FROM home_route_destinations WHERE owner=? AND id=?", (owner, dest))
            return {}
        source = form.get("source")
        sources = {s["id"]: s for s in state["sources"]}
        if source not in sources:
            raise ValueError("Choose an existing Home source.")
        if action == "remove":
            if source == ORIGINAL:
                raise ValueError("Only paired Chrome Home sources can be removed.")
            conn.execute("DELETE FROM home_route_sources WHERE owner=? AND id=?", (owner, source))
            choices = set()
        else:
            choices = set(form.getlist("destination"))
            if choices - {d["id"] for d in state["destinations"]}:
                raise ValueError("Choose verified destinations from this dashboard.")
            if source == ORIGINAL:
                conn.execute("INSERT OR IGNORE INTO home_route_sources(owner,id,label) VALUES(?,?,'Current Vinted Home')",
                             (owner, ORIGINAL))
        removed = set(sources[source]["destinations"]) - choices
        conn.executemany("DELETE FROM home_route_origins WHERE owner=? AND source=? AND dest=?",
                         ((owner, source, dest) for dest in removed))
        conn.execute("DELETE FROM home_routes WHERE owner=? AND source=?", (owner, source))
        conn.executemany("INSERT INTO home_routes VALUES(?,?,?)", ((owner, source, dest) for dest in choices))
        return {}


def _code(conn, code, purpose, now):
    if not isinstance(code, str) or not CODE.fullmatch(code):
        raise PermissionError
    row = conn.execute("""SELECT owner,label FROM home_route_codes WHERE digest=? AND purpose=? AND expires>?""",
                       (_digest(code), purpose, now)).fetchone()
    if not row or row[0] != owner_id(conn):
        raise PermissionError
    return tuple(row)


def pair_browser(code, account):
    if not isinstance(account, str) or not NUMBER.fullmatch(account):
        raise ValueError("A public Vinted account identifier is required.")
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        owner, label = _code(conn, code, "pair", time.time())
        if conn.execute("SELECT 1 FROM home_route_sources WHERE owner=? AND account=?", (owner, account)).fetchone():
            raise Conflict("This Home is already paired. Disconnect it before pairing it again.")
        key, token = "browser-" + secrets.token_urlsafe(12), secrets.token_urlsafe(32)
        conn.execute("INSERT INTO home_route_sources(owner,id,label,account,token) VALUES(?,?,?,?,?)",
                     (owner, key, label, account, _digest(token)))
        conn.execute("DELETE FROM home_route_codes WHERE digest=?", (_digest(code),))
        return {"source_id": key, "token": token}


def destination_challenge(code):
    with closing(connection()) as conn:
        return _code(conn, code, "pair_destination", time.time())


def pair_chat(code, chat, partner=None, *, display_name=""):
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        owner, label = _code(conn, code, "pair_destination", time.time())
        if not chat or (chat < 0 and not partner):
            raise ValueError("Private group membership could not be verified.")
        conn.execute("DELETE FROM home_route_codes WHERE digest=?", (_digest(code),))
        if not any(entry[1] == chat for entry in _destinations(conn, owner).values()):
            verified = " ".join(str(display_name).split())[:64]
            if verified and verified.casefold() != label.casefold():
                label = f"{label} · {verified}"
            conn.execute("INSERT INTO home_route_destinations VALUES(?,?,?,?,?)",
                         (owner, "chat-" + secrets.token_urlsafe(9), label, chat, partner))


def _listed(value):
    if isinstance(value, str):
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is None:
                raise ValueError
            value = date.timestamp()
        except (ValueError, OverflowError, OSError):
            raise ValueError("Listing time must include a timezone.") from None
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
        raise ValueError("Listing time is missing.")
    return value


def listing(item):
    if not isinstance(item, dict):
        raise ValueError("Invalid Home listing.")
    ident, title = str(item.get("id", "")), item.get("title")
    if not NUMBER.fullmatch(ident) or not isinstance(title, str) or not 1 <= len(title.strip()) <= 300:
        raise ValueError("Invalid listing title or identifier.")
    link = urlsplit(str(item.get("url", "")))
    if (link.scheme != "https" or link.netloc not in {"www.vinted.co.uk", "vinted.co.uk"}
            or not re.fullmatch(r"/items/" + ident + r"(?:-[\w%-]+)?/?", link.path)):
        raise ValueError("Expected the matching public Vinted UK listing.")
    try:
        price = Decimal(str(item.get("price_gbp")))
        if not price.is_finite() or price <= 0 or price > 1000000 or price != price.quantize(Decimal("0.01")):
            raise ValueError
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid GBP price.") from None
    photo = item.get("photo_url")
    if photo:
        photo_url = urlsplit(str(photo))
        if (photo_url.scheme != "https" or not re.fullmatch(r"(?:[a-z0-9-]+\.)*vinted\.(?:net|co\.uk)", photo_url.netloc)
                or not photo_url.path.startswith("/t/") or len(str(photo)) > 2048):
            photo = None
    return {"id": ident, "url": "https://www.vinted.co.uk/items/" + ident, "title": title.strip(),
            "price_gbp": format(price, ".2f"), "photo_url": photo, "listed_at": _listed(item.get("listed_at"))}


def _queue(conn, owner, source, item, expires, dests, now):
    fresh = conn.execute("INSERT OR IGNORE INTO home_route_events VALUES(?,?,?,?,?)",
                         (owner, source, item["id"], expires, now)).rowcount
    if not fresh:
        return False
    routes = selected(conn, owner, source, dests)
    for dest in routes:
        chat = dests[dest][1]
        native = source == ORIGINAL and ("owner" in routes and (dest == "owner" or dest.startswith("shared-")))
        state = "native" if native else "ready"
        # Any prior accepted native original/saved search wins over a browser rediscovery.
        if source != ORIGINAL and dest == "owner":
            prior = conn.execute("""SELECT 1 FROM alert_outbox WHERE item_id=? AND platform='vinted'
                AND (telegram_message_id IS NOT NULL OR status='pending')""", (item["id"],)).fetchone()
            if prior:
                state = "native"
        if source != ORIGINAL and dest.startswith("shared-"):
            prior = conn.execute("""SELECT 1 FROM telegram_shared_deliveries WHERE item_id=? AND version=?
                AND (message_id IS NOT NULL OR failed=0)""", (item["id"], dest.removeprefix("shared-"))).fetchone()
            if prior:
                state = "native"
        conn.execute("""INSERT OR IGNORE INTO home_route_jobs(owner,chat,item,body,expires,state)
                       VALUES(?,?,?,?,?,?)""", (owner, chat, item["id"], json.dumps(item), expires, state))
        # Upgrade an unsent browser observation to the full native controls when the original source also finds it.
        if native:
            conn.execute("UPDATE home_route_jobs SET state='native' WHERE owner=? AND chat=? AND item=? AND state IN ('ready','checking')",
                         (owner, chat, item["id"]))
        conn.execute("INSERT OR IGNORE INTO home_route_origins VALUES(?,?,?,?,?)", (owner, chat, item["id"], source, dest))
    return True


def ingest(token, account, items):
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise PermissionError
    if not isinstance(items, list) or len(items) > 50 or not isinstance(account, str):
        raise ValueError("Send up to 50 public Home listings and the paired account identifier.")
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        owner = owner_id(conn)
        row = conn.execute("SELECT id FROM home_route_sources WHERE owner=? AND account=? AND token=?",
                           (owner, account, _digest(token))).fetchone()
        if not row:
            raise PermissionError
        now = time.time()
        _prune(conn, now)
        batch = [listing(entry) for entry in items]
        conn.execute("UPDATE home_route_sources SET last_seen=? WHERE owner=? AND id=?", (now, owner, row[0]))
        dests = _destinations(conn, owner)
        return sum(_queue(conn, owner, row[0], entry, entry["listed_at"]+AGE, dests, now)
                   for entry in batch if now-AGE < entry["listed_at"] <= now)


def _enabled(conn):
    # A database can still be opening on its previous schema during a rolling update.
    return (conn.execute("SELECT 1 FROM sqlite_master WHERE name='home_route_sources'").fetchone()
            and conn.execute("SELECT 1 FROM home_route_sources LIMIT 1").fetchone())


def note_original(conn, ident, title, price, currency, photo, alert, now):
    """Return native owner routing permission and whether this Home observation was new."""
    if not _enabled(conn):
        return True, False
    owner = owner_id(conn)
    if not owner:
        return True, False
    recorded = False
    dests = _destinations(conn, owner)
    discovery = alert["vinted_details"]["home_discovery"]
    expires = discovery.get("expires_at", now)
    if currency == "GBP" and isinstance(expires, (float, int)) and expires > now:
        try:
            item = listing({"id": ident, "title": title, "price_gbp": price, "photo_url": photo,
                            "url": alert["url"], "listed_at": expires-AGE})
        except ValueError:
            pass
        else:
            _prune(conn, now)
            recorded = _queue(conn, owner, ORIGINAL, item, expires, dests, discovery.get("observed_at") or now)
    return native_allowed(conn, ident, "owner"), recorded


def native_allowed(conn, item, dest):
    if not _enabled(conn):
        return True
    owner = owner_id(conn)
    if not owner:
        return True
    dests = _destinations(conn, owner)
    if dest not in selected(conn, owner, ORIGINAL, dests):
        return False
    prior = conn.execute("SELECT state FROM home_route_jobs WHERE owner=? AND chat=? AND item=?",
                         (owner, dests[dest][1], str(item))).fetchone()
    return not prior or prior[0] in {"native", "native_hold", "sent", "sending", "unknown", "cancelled", "failed"}


def native_row_allowed(row, dest, conn=None):
    row = dict(row)
    if row.get("platform", "vinted") != "vinted" or not row.get("content", "").startswith("🏠"):
        return True
    if conn is None:
        with closing(connection()) as opened:
            return native_allowed(opened, row["item_id"], dest)
    return native_allowed(conn, row["item_id"], dest)
