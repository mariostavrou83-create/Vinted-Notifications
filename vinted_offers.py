"""Persistent, paced seller offers. Independent of catalogue polling and buying.

Only favourite notifications with explicit item/recipient IDs are eligible.
No free-text messages, purchases, offer acceptance, challenge solving or replay
of uncertain writes is performed here. Network calls never hold a SQLite write
transaction. The existing account lock protects encrypted session rotations.
"""

import logging
import math
import re
import time
from contextlib import closing
from datetime import datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import vinted_buyer as buyer
from search_settings import connection

logger = logging.getLogger(__name__)
SEND_GAP = 180
SCAN_GAP = 60
PAGE_GAP = 5
PAGE_SIZE = 20
LEASE_SECONDS = 120
MODES = {"paused", "preview", "live"}
ROUNDINGS = {"half_pound", "penny"}
HISTORIES = {"today", "new", "all"}

MESSAGES = {
    "account": "Connect and verify your Vinted account in Connections.",
    "account_changed": "The connected account changed. Review Offers before restarting.",
    "auth": "Vinted needs account attention. Check Connections and Vinted before restarting.",
    "restricted": "Vinted refused this action. Offers are paused; check your account on Vinted.",
    "cooldown": "Vinted requested a cooldown. Offers are paused; no repeated sends were attempted.",
    "network": "The connection failed before an offer was sent. The queue will check again later.",
    "shape": "Vinted returned an unfamiliar response. Offers are paused for review.",
    "uncertain": "Vinted did not confirm this offer. Check the conversation on Vinted; it will not be retried automatically.",
    "unavailable": "The item is sold, reserved, hidden or unavailable.",
    "not_owned": "Vinted did not confirm that this item belongs to your connected account.",
    "price": "A valid GBP price could not be confirmed, or the discounted price is below £1.",
    "existing_offer": "This conversation already contains an offer or negotiation.",
    "recipient": "The conversation did not confirm the expected item, seller and recipient.",
    "cancelled": "Removed from the queue by you.",
    "stale": "Settings changed while this item was being checked. It will be checked again.",
    "inventory": "Checking your wardrobe for this item.",
    "crash": "The worker restarted during this send. Check Vinted; it will not be retried automatically.",
}


class OfferCheck(ValueError):
    def __init__(self, reason, *, pause=False):
        self.reason = reason
        self.pause = pause
        super().__init__(MESSAGES.get(reason, MESSAGES["shape"]))


class InventoryPending(ValueError):
    """One wardrobe page per tick keeps account work bounded."""


class SellerItems:
    """Use the current wardrobe API plus the existing verified listing parser.

    The former UK item-detail API returns 404. A wardrobe record confirms the
    item's active/closed state even when can_buy=False because we are its owner.
    The canonical listing page confirms current price, seller and restrictions.
    No refusal causes a route switch, anonymous read, or challenge retry.
    """

    def __init__(self, clock):
        self.clock = clock
        self.account = None
        self.rows = {}
        self.page = 1
        self.complete = False
        self.expires = 0

    def read(self, client, job):
        now = self.clock()
        if self.account != job["account_id"] or self.complete and self.expires < now:
            self.account, self.rows, self.page = job["account_id"], {}, 1
            self.complete = False
        if job["item_id"] not in self.rows and not self.complete:
            data = client.request(
                "GET",
                f"/api/v2/wardrobe/{self.account}/items?page={self.page}&per_page=96&order=relevance",
            )
            rows = data.get("items") if isinstance(data, dict) else None
            if not isinstance(rows, list) or len(rows) > 96:
                raise OfferCheck("shape", pause=True)
            for item in rows:
                if isinstance(item, dict) and identifier(item.get("id")):
                    self.rows[identifier(item["id"])] = (item, now)
            if len(self.rows) > 10000 or self.page > 200:
                raise OfferCheck("shape", pause=True)
            self.page += 1
            self.complete = len(rows) < 96
            self.expires = now + 60
        found = self.rows.get(job["item_id"])
        if not found:
            if self.complete:
                raise OfferCheck("unavailable")
            raise InventoryPending()
        record, checked = found
        if checked < now - 60:
            self.account = None
            raise InventoryPending()
        # Ownership must be explicit on both records. Do not infer it merely
        # because a recommendation appeared in the wardrobe response.
        checked_item({"item": record}, job, "penny")
        data = client.listing_page(
            buyer.BASE + "/items/" + job["item_id"], job["item_id"]
        )
        current = data.get("item") if isinstance(data, dict) else None
        if (
            not isinstance(current, dict)
            or identifier(current.get("id")) != job["item_id"]
            or identifier(current.get("user_id")) != job["account_id"]
        ):
            raise OfferCheck("not_owned")
        if any(
            type(current.get(flag)) is not bool for flag in ("is_reserved", "is_hidden")
        ):
            raise OfferCheck("shape", pause=True)
        return {
            "item": {
                **record,
                **current,
                "user": {"id": job["account_id"]},
                "title": record.get("title", "Vinted item"),
            }
        }


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_offer_settings (
        id INTEGER PRIMARY KEY CHECK(id=1), mode TEXT NOT NULL DEFAULT 'paused',
        rounding TEXT NOT NULL DEFAULT 'half_pound', history TEXT NOT NULL DEFAULT 'today',
        account_id TEXT, since REAL, revision INTEGER NOT NULL DEFAULT 0,
        next_send REAL NOT NULL DEFAULT 0, next_scan REAL NOT NULL DEFAULT 0,
        cooldown_until REAL NOT NULL DEFAULT 0,
        scan_page INTEGER NOT NULL DEFAULT 1, scan_latest REAL NOT NULL DEFAULT 0,
        scan_watermark REAL NOT NULL DEFAULT 0, last_scan REAL, last_sent REAL,
        heartbeat REAL, pause_reason TEXT NOT NULL DEFAULT '',
        unknown_notifications INTEGER NOT NULL DEFAULT 0)""")
    conn.execute("INSERT OR IGNORE INTO vinted_offer_settings(id) VALUES (1)")
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_offer_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
        item_id TEXT NOT NULL, recipient_id TEXT NOT NULL,
        notification_id TEXT NOT NULL, liked_at REAL NOT NULL, created REAL NOT NULL,
        title TEXT NOT NULL DEFAULT '', price INTEGER, offer_price INTEGER,
        status TEXT NOT NULL DEFAULT 'queued', reason TEXT NOT NULL DEFAULT '',
        conversation_id TEXT, transaction_id TEXT, offer_id TEXT,
        lease_until REAL NOT NULL DEFAULT 0, checked_at REAL, sent_at REAL,
        UNIQUE(account_id,item_id,recipient_id))""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_offer_queue
        ON vinted_offer_jobs(account_id,status,liked_at,id)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_offer_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, happened REAL NOT NULL,
        job_id INTEGER REFERENCES vinted_offer_jobs(id),
        action TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '')""")


def state():
    with closing(connection()) as conn:
        return dict(
            conn.execute("SELECT * FROM vinted_offer_settings WHERE id=1").fetchone()
        )


def event(conn, action, reason="", job_id=None, now=None):
    conn.execute(
        "INSERT INTO vinted_offer_events(happened,job_id,action,reason) VALUES (?,?,?,?)",
        (time.time() if now is None else now, job_id, action, reason),
    )


def configure(mode, rounding, history, *, now=None):
    now = time.time() if now is None else now
    if mode not in MODES or rounding not in ROUNDINGS or history not in HISTORIES:
        raise ValueError("Choose the displayed offer settings.")
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        old = dict(
            conn.execute("SELECT * FROM vinted_offer_settings WHERE id=1").fetchone()
        )
        account = conn.execute(
            "SELECT user_id,verified_at,session FROM vinted_buyer WHERE id=1"
        ).fetchone()
        if mode != "paused" and not (
            account and account[0] and account[1] and account[2]
        ):
            raise ValueError(MESSAGES["account"])
        account_id = str(account[0]) if account and account[0] else old["account_id"]
        restart_scan = (
            old["since"] is None
            or history != old["history"]
            or account_id != old["account_id"]
        )
        since = old["since"]
        if restart_scan:
            since = (
                now
                if history == "new"
                else (
                    0
                    if history == "all"
                    else datetime.fromtimestamp(now, ZoneInfo("Europe/London"))
                    .replace(hour=0, minute=0, second=0, microsecond=0)
                    .timestamp()
                )
            )
        conn.execute(
            """UPDATE vinted_offer_settings SET mode=?,rounding=?,history=?,
            account_id=?,since=?,revision=revision+1,pause_reason='',next_scan=0,
            scan_page=1,scan_latest=0,scan_watermark=CASE WHEN ? THEN 0 ELSE scan_watermark END
            WHERE id=1""",
            (mode, rounding, history, account_id, since, restart_scan),
        )
        event(conn, mode, now=now)


def pause(reason):
    with closing(connection()) as conn, conn:
        conn.execute(
            "UPDATE vinted_offer_settings SET mode='paused',pause_reason=?,revision=revision+1 WHERE id=1",
            (reason,),
        )
        event(conn, "paused", reason)


def cancel(job_id):
    with closing(connection()) as conn, conn:
        changed = conn.execute(
            "UPDATE vinted_offer_jobs SET status='skipped',reason='cancelled' WHERE id=? AND status IN ('queued','ready')",
            (job_id,),
        ).rowcount
        if changed:
            event(conn, "skipped", "cancelled", job_id)
        return bool(changed)


def identifier(value):
    text = str(value) if type(value) in (str, int) else ""
    return text if re.fullmatch(r"[1-9][0-9]{0,23}", text) else None


def timestamp(value):
    try:
        if type(value) in (float, int) or (isinstance(value, str) and value.isdigit()):
            result = float(value)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                return None
            result = parsed.timestamp()
        else:
            return None
        return result if math.isfinite(result) and 0 < result < 4102444800 else None
    except (ValueError, OverflowError, OSError):
        return None


def notification_time(notification):
    for field in ("created_at", "created_at_ts", "updated_at"):
        value = timestamp(notification.get(field))
        if value is not None:
            return value
    return None


def favourite(notification):
    """Use typed ItemFavourite metadata, or an explicit legacy action link.

    The current inbox gateway uses entry_type=20, subject_id for the item,
    user_id for the liker and updated_at for the event time. Message, price
    reduction and sold-item notifications must never become offer recipients.
    """
    if not isinstance(notification, dict):
        return None
    liked_at = notification_time(notification)
    if "entry_type" in notification:
        if (
            type(notification["entry_type"]) is not int
            or notification["entry_type"] != 20
        ):
            return None
        item_id = identifier(notification.get("subject_id"))
        recipient = identifier(notification.get("user_id"))
        # The current web client builds favourite actions from offering_id.
        # Some notification versions omit user_id, so accept this explicit
        # action recipient too. Never fetch the supplied link.
        link = notification.get("link")
        if isinstance(link, str) and "offering_id" in link:
            if len(link) > 2048:
                return None
            try:
                url = urlsplit(link)
                if not (
                    url.scheme in ("", "https")
                    and url.netloc in ("", "vinted.co.uk", "www.vinted.co.uk")
                    or url.scheme == "vinted"
                    and url.netloc == "messaging"
                ):
                    return None
                query = parse_qs(url.query)
                users = query.get("offering_id", [])
                action_recipient = identifier(users[0]) if len(users) == 1 else None
                items = query.get("item_id")
                if not action_recipient or (
                    recipient and recipient != action_recipient
                ):
                    return None
                if items is not None and (
                    len(items) != 1 or identifier(items[0]) != item_id
                ):
                    return None
                recipient = action_recipient
            except ValueError:
                return None
        if not (item_id and recipient and liked_at):
            return None
        return {
            "item_id": item_id,
            "recipient_id": recipient,
            "notification_id": identifier(notification.get("id"))
            or f"{item_id}:{recipient}",
            "liked_at": liked_at,
        }
    link = notification.get("link")
    if not isinstance(link, str) or len(link) > 2048:
        return None
    try:
        url = urlsplit(link)
        if (url.scheme and url.scheme != "https") or url.netloc not in (
            "",
            "www.vinted.co.uk",
            "vinted.co.uk",
        ):
            return None
        query = parse_qs(url.query)
        values = query.get("offering_id", [])
        recipient = identifier(values[0]) if len(values) == 1 else None
        match = re.fullmatch(r"/items/([1-9][0-9]{0,23})/want_it/new/?", url.path)
        if match:
            item_id = match[1]
        elif url.path.rstrip("/") == "/inbox/want_it":
            items = query.get("item_id", [])
            item_id = identifier(items[0]) if len(items) == 1 else None
        else:
            return None
        if not (recipient and item_id and liked_at):
            return None
        return {
            "item_id": item_id,
            "recipient_id": recipient,
            "notification_id": identifier(notification.get("id"))
            or f"{item_id}:{recipient}",
            "liked_at": liked_at,
        }
    except (ValueError, TypeError):
        return None


def notification_shape(rows, page):
    """Only structural counts, never recipient IDs, links, bodies or credentials."""
    types, formats, keys = set(), set(), set()
    updated = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        entry = row.get("entry_type")
        if type(entry) is int and 0 <= entry <= 1000:
            types.add(entry)
        updated += timestamp(row.get("updated_at")) is not None
        try:
            url = urlsplit(str(row.get("link") or "")[:2048])
            if url.scheme in ("", "https") and url.netloc in (
                "",
                "vinted.co.uk",
                "www.vinted.co.uk",
            ):
                formats.add("web")
            elif url.scheme == "vinted" and url.netloc in ("messaging", "item"):
                formats.add(url.netloc)
            else:
                formats.add("other")
            keys.update(
                set(parse_qs(url.query))
                & {
                    "item_id",
                    "offering_id",
                    "opposite_user_id",
                    "user_id",
                    "portal",
                    "initiator",
                }
            )
        except ValueError:
            formats.add("invalid")
    logger.info(
        "Vinted offers notification format: page=%s rows=%s entry_types=%s links=%s query_fields=%s updated_dates=%s supported_favourites=%s",
        page,
        len(rows),
        ",".join(map(str, sorted(types))) or "none",
        ",".join(sorted(formats)) or "none",
        ",".join(sorted(keys)) or "none",
        updated,
        sum(favourite(row) is not None for row in rows),
    )


def offer_amount(price, rounding):
    """Integer pennies; 5% off, then the owner's selected downward rounding."""
    if (
        type(price) is not int
        or not 100 <= price <= 100_000_000
        or rounding not in ROUNDINGS
    ):
        raise OfferCheck("price")
    pennies = price * 95 // 100
    if rounding == "half_pound":
        pennies = pennies // 50 * 50
    if pennies < 100 or pennies >= price:
        raise OfferCheck("price")
    return pennies


def price_pennies(value):
    try:
        amount = Decimal(str(value))
        if (
            not amount.is_finite()
            or amount <= 0
            or amount > 1_000_000
            or amount * 100 != (amount * 100).to_integral_value()
        ):
            raise OfferCheck("price")
        return int((amount * 100).to_integral_value(rounding=ROUND_DOWN))
    except (InvalidOperation, ValueError, TypeError):
        raise OfferCheck("price") from None


def checked_item(data, job, rounding):
    item = data.get("item") if isinstance(data, dict) else None
    if not isinstance(item, dict) or identifier(item.get("id")) != job["item_id"]:
        raise OfferCheck("shape", pause=True)
    owner = item.get("user") or {}
    if (
        not isinstance(owner, dict)
        or identifier(owner.get("id") or item.get("user_id")) != job["account_id"]
    ):
        raise OfferCheck("not_owned")
    if not any(type(item.get(key)) is bool for key in ("is_closed", "is_sold")):
        raise OfferCheck("shape", pause=True)
    if any(
        item.get(key)
        for key in (
            "is_closed",
            "is_sold",
            "is_reserved",
            "is_hidden",
            "is_draft",
            "is_deleted",
        )
    ):
        raise OfferCheck("unavailable")
    if (
        item.get("can_be_sold") is False
        or item.get("can_buy") is False
        and item.get("is_visible") is False
    ):
        raise OfferCheck("unavailable")
    price = item.get("price")
    currency = item.get("currency")
    if isinstance(price, dict):
        currency = price.get("currency_code") or price.get("currency") or currency
        price = price.get("amount")
    if currency != "GBP":
        raise OfferCheck("price")
    pennies = price_pennies(price)
    return {
        "title": str(item.get("title") or "Vinted item")[:300],
        "price": pennies,
        "offer_price": offer_amount(pennies, rounding),
    }


def checked_conversation(data, job):
    conv = data.get("conversation") if isinstance(data, dict) else None
    if not isinstance(conv, dict):
        raise OfferCheck("shape", pause=True)
    transaction = conv.get("transaction") or {}
    opposite = conv.get("opposite_user") or {}
    item = conv.get("item") or transaction.get("item") or {}
    if not all(isinstance(v, dict) for v in (transaction, opposite, item)):
        raise OfferCheck("recipient", pause=True)
    if not (
        identifier(conv.get("id"))
        and identifier(transaction.get("id"))
        and identifier(opposite.get("id") or conv.get("opposite_user_id"))
        == job["recipient_id"]
        and identifier(item.get("id") or transaction.get("item_id")) == job["item_id"]
        and identifier(transaction.get("seller_id")) == job["account_id"]
        and transaction.get("user_side") == "seller"
    ):
        raise OfferCheck("recipient", pause=True)
    if any(transaction.get(key) for key in ("is_completed", "is_cancelled", "is_paid")):
        raise OfferCheck("unavailable")
    messages = conv.get("messages")
    if not isinstance(messages, list):
        raise OfferCheck("shape", pause=True)
    if any(
        isinstance(m, dict)
        and (
            m.get("entity_type") in ("offer_message", "offer_request_message")
            or m.get("type") in ("offer", "offer_request")
        )
        for m in messages
    ):
        raise OfferCheck("existing_offer")
    return str(conv["id"]), str(transaction["id"])


def snapshot():
    config = state()
    with closing(connection()) as conn:
        jobs = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM vinted_offer_jobs WHERE account_id=? ORDER BY created DESC,id DESC LIMIT 150",
                (config["account_id"],),
            )
        ]
        counts = dict(
            conn.execute(
                "SELECT status,COUNT(*) FROM vinted_offer_jobs WHERE account_id=? GROUP BY status",
                (config["account_id"],),
            )
        )
        events = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM vinted_offer_events ORDER BY id DESC LIMIT 20"
            )
        ]
    for job in jobs:
        job["message"] = MESSAGES.get(job["reason"], "")
        job["item_url"] = buyer.BASE + "/items/" + job["item_id"]
        job["conversation_url"] = (
            buyer.BASE + "/inbox/" + job["conversation_id"]
            if job["conversation_id"]
            else None
        )
    return {
        "config": config,
        "jobs": jobs,
        "counts": counts,
        "events": events,
        "pause_message": MESSAGES.get(config["pause_reason"], ""),
        "send_gap": SEND_GAP,
        "worker_online": bool(
            config["heartbeat"] and config["heartbeat"] > time.time() - 60
        ),
    }


def _recover(now):
    with closing(connection()) as conn, conn:
        conn.execute(
            "UPDATE vinted_offer_settings SET heartbeat=? WHERE id=1 AND (heartbeat IS NULL OR heartbeat<?)",
            (now, now - 30),
        )
        conn.execute(
            "UPDATE vinted_offer_jobs SET status='queued',lease_until=0 WHERE status='checking' AND lease_until<=?",
            (now,),
        )
        rows = conn.execute(
            "SELECT id FROM vinted_offer_jobs WHERE status='sending' AND lease_until<=?",
            (now,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE vinted_offer_jobs SET status='uncertain',reason='crash',lease_until=0 WHERE id=?",
                (row[0],),
            )
            event(conn, "uncertain", "crash", row[0], now)
        if rows:
            conn.execute(
                "UPDATE vinted_offer_settings SET mode='paused',pause_reason='uncertain',revision=revision+1 WHERE id=1"
            )


def _finish(job_id, status, reason="", **fields):
    allowed = {
        "title",
        "price",
        "offer_price",
        "conversation_id",
        "transaction_id",
        "offer_id",
        "checked_at",
        "sent_at",
    }
    if not set(fields) <= allowed:
        raise ValueError("Unexpected offer update")
    assignments = ",".join(f"{key}=?" for key in fields)
    with closing(connection()) as conn, conn:
        conn.execute(
            f"UPDATE vinted_offer_jobs SET status=?,reason=?,lease_until=0{',' if fields else ''}{assignments} WHERE id=?",
            (status, reason, *fields.values(), job_id),
        )
        if status not in ("ready", "queued"):
            event(conn, status, reason, job_id)


def _error_reason(exc):
    if exc.reason == "account_changed":
        return "account_changed"
    if exc.status == 429 or exc.reason in ("rate_limited", "renewal_cooldown"):
        return "cooldown"
    if exc.status == 403 or exc.reason in (
        "security_challenge",
        "account_restricted",
        "renewal_blocked",
    ):
        return "restricted"
    if exc.reason == "network":
        return "network"
    return (
        "auth"
        if exc.status == 401 or exc.stage in ("identity", "saved_session", "renewal")
        else "shape"
    )


def _wait_after_failure(exc, now):
    delay = getattr(exc, "retry_after", 0)
    if type(delay) not in (int, float) or not math.isfinite(delay) or delay < 0:
        delay = 0
    if exc.status == 429 or exc.reason == "rate_limited":
        delay = max(delay, 900)
    elif exc.reason == "network":
        delay = max(delay, SCAN_GAP)
    if delay:
        with closing(connection()) as conn, conn:
            conn.execute(
                """UPDATE vinted_offer_settings SET
                cooldown_until=MAX(cooldown_until,?),next_send=MAX(next_send,?),
                next_scan=MAX(next_scan,?) WHERE id=1""",
                (now + delay,) * 3,
            )


class OfferWorker:
    def __init__(self, *, client_factory=None, clock=None, item_reader=None):
        self.client_factory = client_factory or (
            lambda: buyer.connected_client(solve_challenges=False)
        )
        self.clock = clock or time.time
        self.inventory = SellerItems(self.clock)
        self.item_reader = item_reader or self.inventory.read

    def scan(self, client, config):
        now = self.clock()
        page = config["scan_page"]
        data = client.request(
            "GET",
            buyer.NOTIFICATIONS_PATH,
            params={"page": page, "per_page": PAGE_SIZE},
        )
        rows = data.get("notifications") if isinstance(data, dict) else None
        if not isinstance(rows, list) or len(rows) > 100:
            raise OfferCheck("shape", pause=True)
        notification_shape(rows, page)
        cutoff = max(config["since"] or 0, config["scan_watermark"] - 300)
        times = [notification_time(n) for n in rows if isinstance(n, dict)]
        valid_times = [t for t in times if t is not None]
        latest = max([config["scan_latest"], *valid_times])
        pagination = data.get("pagination") or {}
        total_pages = (
            pagination.get("total_pages") if isinstance(pagination, dict) else None
        )
        more = type(total_pages) is int and page < total_pages
        finished = (len(rows) < PAGE_SIZE and not more) or bool(
            rows and len(valid_times) == len(rows) and max(valid_times) < cutoff
        )
        unknown = 0
        with closing(connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            fresh = conn.execute(
                "SELECT revision,mode FROM vinted_offer_settings WHERE id=1"
            ).fetchone()
            if fresh[0] != config["revision"] or fresh[1] == "paused":
                return
            for notification in rows:
                match = favourite(notification)
                if (
                    match
                    and config["since"] <= match["liked_at"] <= now + 60
                    and match["recipient_id"] != config["account_id"]
                ):
                    conn.execute(
                        """INSERT OR IGNORE INTO vinted_offer_jobs
                        (account_id,item_id,recipient_id,notification_id,liked_at,created)
                        VALUES (?,?,?,?,?,?)""",
                        (
                            config["account_id"],
                            match["item_id"],
                            match["recipient_id"],
                            match["notification_id"],
                            match["liked_at"],
                            now,
                        ),
                    )
                elif isinstance(notification, dict) and (
                    notification.get("entry_type") == 20
                    or "offering_id" in str(notification.get("link", ""))
                ):
                    unknown += 1
            conn.execute(
                """UPDATE vinted_offer_settings SET scan_page=?,scan_latest=?,
                scan_watermark=CASE WHEN ? THEN MAX(scan_watermark,?) ELSE scan_watermark END,
                next_scan=?,last_scan=?,unknown_notifications=unknown_notifications+? WHERE id=1""",
                (
                    1 if finished else page + 1,
                    0 if finished else latest,
                    finished,
                    latest,
                    now + (SCAN_GAP if finished else PAGE_GAP),
                    now,
                    unknown,
                ),
            )

    def claim(self, config):
        now = self.clock()
        with closing(connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            fresh = dict(
                conn.execute(
                    "SELECT * FROM vinted_offer_settings WHERE id=1"
                ).fetchone()
            )
            if fresh["revision"] != config["revision"] or fresh["mode"] == "paused":
                return None
            if fresh["next_send"] > now:
                return None
            statuses = ("queued", "ready") if fresh["mode"] == "live" else ("queued",)
            row = conn.execute(
                "SELECT * FROM vinted_offer_jobs WHERE account_id=? AND status IN ("
                + ",".join("?" for _ in statuses)
                + ") ORDER BY liked_at,id LIMIT 1",
                (fresh["account_id"], *statuses),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE vinted_offer_jobs SET status='checking',lease_until=? WHERE id=?",
                (now + LEASE_SECONDS, row["id"]),
            )
            return dict(row)

    def process(self, client, config, job):
        sending = False
        try:
            info = checked_item(self.item_reader(client, job), job, config["rounding"])
            if config["mode"] == "preview":
                _finish(job["id"], "ready", checked_at=self.clock(), **info)
                return
            data = client.request(
                "POST",
                "/api/v2/conversations",
                {
                    "initiator": "seller_enters_notification",
                    "item_id": int(job["item_id"]),
                    "opposite_user_id": int(job["recipient_id"]),
                },
            )
            conv_id = identifier((data.get("conversation") or {}).get("id"))
            if not conv_id:
                raise OfferCheck("shape", pause=True)
            conv_id, transaction_id = checked_conversation(
                client.request("GET", f"/api/v2/conversations/{conv_id}"), job
            )
            with closing(connection()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                current = dict(
                    conn.execute(
                        "SELECT * FROM vinted_offer_settings WHERE id=1"
                    ).fetchone()
                )
                saved = conn.execute(
                    "SELECT user_id FROM vinted_buyer WHERE id=1"
                ).fetchone()
                if (
                    current["mode"] != "live"
                    or current["revision"] != config["revision"]
                    or not saved
                    or str(saved[0]) != job["account_id"]
                ):
                    conn.execute(
                        "UPDATE vinted_offer_jobs SET status='queued',lease_until=0 WHERE id=?",
                        (job["id"],),
                    )
                    return
                now = self.clock()
                if current["next_send"] > now:
                    conn.execute(
                        "UPDATE vinted_offer_jobs SET status='queued',lease_until=0 WHERE id=?",
                        (job["id"],),
                    )
                    return
                # Reserve the spacing before the write; a restart cannot burst.
                conn.execute(
                    "UPDATE vinted_offer_settings SET next_send=? WHERE id=1",
                    (now + SEND_GAP,),
                )
                conn.execute(
                    """UPDATE vinted_offer_jobs SET status='sending',lease_until=?,
                    conversation_id=?,transaction_id=?,title=?,price=?,offer_price=?,checked_at=? WHERE id=?""",
                    (
                        now + LEASE_SECONDS,
                        conv_id,
                        transaction_id,
                        info["title"],
                        info["price"],
                        info["offer_price"],
                        now,
                        job["id"],
                    ),
                )
                event(conn, "sending", job_id=job["id"], now=now)
            sending = True
            response = client.request(
                "POST",
                f"/api/v2/transactions/{transaction_id}/offers",
                {
                    "offer": {
                        "price": f"{info['offer_price'] // 100}.{info['offer_price'] % 100:02d}",
                        "currency": "GBP",
                    }
                },
            )
            offer = response.get("offer") if isinstance(response, dict) else None
            if not isinstance(offer, dict) or not identifier(offer.get("id")):
                _finish(job["id"], "uncertain", "uncertain")
                pause("uncertain")
                return
            now = self.clock()
            _finish(job["id"], "sent", offer_id=str(offer["id"]), sent_at=now)
            with closing(connection()) as conn, conn:
                conn.execute(
                    "UPDATE vinted_offer_settings SET last_sent=?,next_send=MAX(next_send,?) WHERE id=1",
                    (now, now + SEND_GAP),
                )
            logger.info("Vinted offers: sent queue_id=%s", job["id"])
        except InventoryPending:
            _finish(job["id"], "queued", "inventory")
        except OfferCheck as exc:
            _finish(
                job["id"],
                "uncertain" if sending else "skipped",
                "uncertain" if sending else exc.reason,
            )
            if exc.pause or sending:
                pause("uncertain" if sending else exc.reason)
        except buyer.BuyerError as exc:
            _wait_after_failure(exc, self.clock())
            reason = _error_reason(exc)
            if sending:
                # Even a refused/errored write is never automatically replayed.
                _finish(job["id"], "uncertain", "uncertain")
                pause(reason if reason != "network" else "uncertain")
            elif reason == "network" or exc.reason == "busy":
                _finish(job["id"], "queued", "network")
                with closing(connection()) as conn, conn:
                    conn.execute(
                        "UPDATE vinted_offer_settings SET next_send=MAX(next_send,?) WHERE id=1",
                        (self.clock() + SCAN_GAP,),
                    )
            elif exc.status in (404, 410):
                _finish(job["id"], "skipped", "unavailable")
            else:
                _finish(job["id"], "queued", reason)
                pause(reason)

    def tick(self):
        now = self.clock()
        _recover(now)
        config = state()
        if config["mode"] == "paused" or config["cooldown_until"] > now:
            return
        needs_scan = config["next_scan"] <= now
        with closing(connection()) as conn:
            pending = conn.execute(
                "SELECT 1 FROM vinted_offer_jobs WHERE account_id=? AND (status='queued' OR (? AND status='ready')) LIMIT 1",
                (config["account_id"], config["mode"] == "live"),
            ).fetchone()
        if not needs_scan and (not pending or config["next_send"] > now):
            return
        client = None
        try:
            with buyer.exclusive():
                client = self.client_factory()
                if (
                    not client._verified_session
                    or client._verified_session[0] != config["account_id"]
                ):
                    raise OfferCheck("account_changed", pause=True)
                # Send first so scanning a long history cannot starve ready offers.
                job = self.claim(config)
                if job:
                    self.process(client, config, job)
                config = state()
                if config["mode"] != "paused" and config["next_scan"] <= self.clock():
                    self.scan(client, config)
        except OfferCheck as exc:
            pause(exc.reason)
        except buyer.BuyerError as exc:
            if exc.reason == "busy":
                return
            _wait_after_failure(exc, self.clock())
            reason = _error_reason(exc)
            if reason == "network":
                with closing(connection()) as conn, conn:
                    conn.execute(
                        "UPDATE vinted_offer_settings SET next_scan=? WHERE id=1",
                        (self.clock() + SCAN_GAP,),
                    )
            else:
                pause(reason)
        finally:
            if client:
                client.session.close()


def run():
    """A small dedicated worker; no sleeping or offer work in the fast poller."""
    worker = OfferWorker()
    logger.info("Vinted offers worker ready: minimum send spacing=%ss", SEND_GAP)
    while True:
        try:
            worker.tick()
        except Exception:  # noqa: BLE001 -- contain failures in this independent worker
            # Keep the listing poller running. Crash leases prevent send replays.
            logger.error("Vinted offers: unexpected worker failure; paused for review")
            pause("shape")
        time.sleep(5)
