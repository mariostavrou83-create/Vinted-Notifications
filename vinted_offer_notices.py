"""Durable offers health warnings; no marketplace requests or queue mutations."""

import asyncio
import logging
import math
import os
import time
import uuid
from contextlib import closing
from urllib.parse import urlsplit

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter

import db
from search_settings import connection
from vinted_connection_notices import ConnectionNotifier, DELIVERY_LEASE, DELIVERY_RETRY

logger = logging.getLogger(__name__)
COOLDOWN_GRACE = 15 * 60
STALE_HEARTBEAT = 3 * 60
PAUSE_REASONS = frozenset({
    "account", "account_changed", "auth", "restricted", "shape", "uncertain",
    "cooldown", "network", "recipient",
})


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_offer_notice (
        id INTEGER PRIMARY KEY CHECK(id=1), account_id TEXT, incident_id TEXT,
        incident_key TEXT, kind TEXT, reason TEXT, started REAL,
        active INTEGER NOT NULL DEFAULT 0, warned INTEGER NOT NULL DEFAULT 0,
        recovery_pending INTEGER NOT NULL DEFAULT 0,
        lease_id TEXT, lease_until REAL NOT NULL DEFAULT 0,
        retry_at REAL NOT NULL DEFAULT 0, telegram_message_id INTEGER,
        test_checked REAL NOT NULL DEFAULT 0)""")
    conn.execute("INSERT OR IGNORE INTO vinted_offer_notice(id) VALUES (1)")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(vinted_offer_notice)")}
    if "test_checked" not in columns:
        conn.execute(
            "ALTER TABLE vinted_offer_notice ADD COLUMN test_checked REAL NOT NULL DEFAULT 0"
        )


def dashboard_url():
    base = os.environ.get("DASHBOARD_URL", "").strip().rstrip("/")
    if not base:
        domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
        base = "https://" + domain if domain else ""
    try:
        parsed = urlsplit(base)
        if (len(base) > 2048 or any(ord(c) <= 32 for c in base)
                or parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")
                or parsed.port not in (None, 443)):
            return None
        return base.rstrip("/") + "/offers"
    except ValueError:
        return None


def _health(conn, now):
    row = conn.execute("SELECT * FROM vinted_offer_settings WHERE id=1").fetchone()
    if row is None:
        return None, None
    config = dict(row)
    activation = conn.execute(
        "SELECT id FROM vinted_offer_events WHERE action IN ('live','preview') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    active_id = activation[0] if activation else 0
    reason = config["pause_reason"]
    if config["mode"] == "paused":
        if not reason:
            return config, None  # An owner's explicit pause is not a failure.
        reason = reason if reason in PAUSE_REASONS else "shape"
        return config, (f"{active_id}:paused:{reason}", "paused", reason, now)
    if config["mode"] not in ("live", "preview"):
        return config, None
    heartbeat = config["heartbeat"]
    if heartbeat is None or heartbeat < now - STALE_HEARTBEAT:
        # The first observation also starts a grace period when no heartbeat
        # exists yet, avoiding a warning while a new worker starts.
        started = heartbeat if heartbeat is not None else now
        return config, (f"{active_id}:stalled", "stalled", "heartbeat", started)
    if config["cooldown_until"] > now:
        event = conn.execute(
            "SELECT happened FROM vinted_offer_events WHERE action='cooldown' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        started = min(now, event[0]) if event else now
        return config, (f"{active_id}:cooldown", "cooldown", "cooldown", started)
    return config, None


class OfferNotifier(ConnectionNotifier):
    """Observe stored offer state independently from buyer-session health."""

    def claim(self):
        if not self.recipient_valid():
            return None
        now = time.time()
        with closing(connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            config, candidate = _health(conn, now)
            if config is None:
                return None
            row = dict(conn.execute("SELECT * FROM vinted_offer_notice WHERE id=1").fetchone())
            if candidate:
                key, kind, reason, started = candidate
                same = (row["active"] and row["account_id"] == config["account_id"]
                        and row["incident_key"] == key)
                if not same:
                    conn.execute(
                        """UPDATE vinted_offer_notice SET account_id=?,incident_id=?,
                        incident_key=?,kind=?,reason=?,started=?,active=1,warned=0,
                        recovery_pending=0,lease_id=NULL,lease_until=0,retry_at=0,
                        telegram_message_id=NULL WHERE id=1""",
                        (config["account_id"], uuid.uuid4().hex, key, kind, reason, started),
                    )
                    row = dict(conn.execute("SELECT * FROM vinted_offer_notice WHERE id=1").fetchone())
                grace = COOLDOWN_GRACE if kind == "cooldown" else STALE_HEARTBEAT if kind == "stalled" else 0
                if row["warned"] or now - row["started"] < grace:
                    return None
            else:
                healthy = config["mode"] in ("live", "preview")
                if row["active"]:
                    recover = bool(healthy and row["warned"] and row["account_id"] == config["account_id"])
                    conn.execute(
                        """UPDATE vinted_offer_notice SET active=0,recovery_pending=?,
                        lease_id=NULL,lease_until=0,retry_at=0 WHERE id=1""",
                        (int(recover),),
                    )
                    row.update(active=0, recovery_pending=int(recover), lease_until=0, retry_at=0)
                if not healthy or row["account_id"] != config["account_id"]:
                    conn.execute(
                        "UPDATE vinted_offer_notice SET recovery_pending=0,lease_id=NULL,lease_until=0 WHERE id=1"
                    )
                    return None
                if not row["recovery_pending"]:
                    return None
            if row["lease_until"] > now or row["retry_at"] > now:
                return None
            lease = uuid.uuid4().hex
            conn.execute(
                "UPDATE vinted_offer_notice SET lease_id=?,lease_until=? WHERE id=1 AND incident_id=?",
                (lease, now + DELIVERY_LEASE, row["incident_id"]),
            )
            row.update(lease_id=lease, delivery_kind="warning" if candidate else "recovered", mode=config["mode"])
            return row

    def still_current(self, row):
        if not self.recipient_valid():
            return False
        with closing(connection()) as conn:
            config, candidate = _health(conn, time.time())
            current = conn.execute(
                "SELECT incident_id,lease_id,active,recovery_pending FROM vinted_offer_notice WHERE id=1"
            ).fetchone()
        if (not config or not current or current[0] != row["incident_id"]
                or current[1] != row["lease_id"] or config["account_id"] != row["account_id"]):
            return False
        if row["delivery_kind"] == "warning":
            return bool(current[2] and candidate and candidate[0] == row["incident_key"])
        return bool(current[3] and not candidate and config["mode"] in ("live", "preview"))

    def finish(self, row, message_id=None, delay=DELIVERY_RETRY):
        with closing(connection()) as conn, conn:
            if message_id is not None:
                conn.execute(
                    """UPDATE vinted_offer_notice SET warned=CASE WHEN ?='warning' THEN 1 ELSE warned END,
                    recovery_pending=0,telegram_message_id=?,lease_id=NULL,lease_until=0,retry_at=0
                    WHERE id=1 AND incident_id=? AND lease_id=?""",
                    (row["delivery_kind"], message_id, row["incident_id"], row["lease_id"]),
                )
            else:
                conn.execute(
                    """UPDATE vinted_offer_notice SET lease_id=NULL,lease_until=0,retry_at=?
                    WHERE id=1 AND incident_id=? AND lease_id=?""",
                    (time.time() + delay, row["incident_id"], row["lease_id"]),
                )

    def notice_text(self, row):
        if row["delivery_kind"] == "recovered":
            detail = ("Live offers are running again." if row["mode"] == "live"
                      else "Offer preview is running again; no offers are being sent in preview mode.")
            return "✅ Vinted offers recovered\n\n" + detail + " The queue keeps its existing send spacing and duplicate protection. Open Offers for the latest results."
        if row["kind"] == "stalled":
            detail = "The offers worker has not updated its heartbeat for at least three minutes. Offers may not be progressing."
        elif row["kind"] == "cooldown":
            detail = "Offers have been waiting through a cooldown or connection retry for at least 15 minutes. The queue is retained and will check again after the wait."
        else:
            detail = {
                "restricted": "Vinted refused an offers request. Check your Vinted account for any required security check or restriction, then review Offers.",
                "auth": "Offers need attention to the saved Vinted connection. Open Connections and review Offers.",
                "account": "Offers need a verified Vinted account. Open Connections and review Offers.",
                "account_changed": "The connected Vinted account changed. Review the account and Offers before restarting.",
                "uncertain": "Vinted did not confirm an attempted offer. Check its conversation before restarting; that uncertain offer will not be sent again automatically.",
                "recipient": "An offer check could not confirm the expected item and recipient. Review Offers before restarting.",
            }.get(row["reason"], "Offers stopped after an unexpected check result. Open Offers to review the reason before restarting.")
        return "⚠️ Vinted offers need attention\n\n" + detail + "\n\nThis is an offers warning; listing alerts use a separate worker."

    async def check(self):
        row = None
        try:
            row = await asyncio.to_thread(self.claim)
            if row is None:
                return "quiet"
            if not await asyncio.to_thread(self.still_current, row):
                await asyncio.to_thread(self.finish, row)
                return "superseded"
            url = dashboard_url()
            sent = await self.bot.send_message(
                chat_id=self.chat_id, text=self.notice_text(row),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open Offers", url=url)]]) if url else None,
                disable_web_page_preview=True,
                read_timeout=10, write_timeout=10, connect_timeout=10, pool_timeout=10,
            )
            await asyncio.to_thread(self.finish, row, sent.message_id)
            logger.info("Vinted offers notification accepted: kind=%s reason=%s", row["delivery_kind"], row["reason"])
            return "sent"
        except Exception as exc:  # noqa: BLE001 -- warning delivery must not stop account maintenance
            logger.warning("Vinted offers notice unavailable: %s", type(exc).__name__)
            if row is not None:
                delay = DELIVERY_RETRY
                if isinstance(exc, RetryAfter):
                    seconds = exc.retry_after
                    seconds = seconds.total_seconds() if hasattr(seconds, "total_seconds") else seconds
                    if type(seconds) in (int, float) and math.isfinite(seconds):
                        delay = max(DELIVERY_RETRY, min(86400, seconds + 2))
                try:
                    await asyncio.to_thread(self.finish, row, delay=delay)
                except Exception:  # noqa: BLE001 -- bounded durable lease allows later retry
                    logger.warning("Vinted offers notice retry bookkeeping unavailable")
            return "unavailable"


async def send_test():
    """One owner-requested delivery test, without a Vinted request or offer."""
    chat_id = str(db.get_parameter("telegram_chat_id") or "").strip()
    # Reuse the same recipient validation as automatic offer warnings.
    if not OfferNotifier(None, chat_id).recipient_valid():
        raise ValueError("Configure the Vinted Telegram chat before testing offer warnings.")
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        checked = conn.execute(
            "SELECT test_checked FROM vinted_offer_notice WHERE id=1"
        ).fetchone()[0]
        if time.time() - checked < 300:
            raise ValueError("Wait five minutes before sending another offer-warning test.")
        conn.execute(
            "UPDATE vinted_offer_notice SET test_checked=? WHERE id=1", (time.time(),)
        )
    try:
        url = dashboard_url()
        markup = (
            InlineKeyboardMarkup([[InlineKeyboardButton("Open Offers", url=url)]])
            if url else None
        )
        async with Bot(db.get_parameter("telegram_token")) as bot:
            await bot.send_message(
                chat_id=chat_id,
                text="TEST — Vinted offer warnings\n\nAutomatic Telegram warnings are enabled when offers pause unexpectedly, the offer worker stops reporting, or a cooldown or connection retry lasts at least 15 minutes. Failed warning deliveries are retried and repeat warnings for one unresolved incident are suppressed.\n\nThis is a delivery test, not an offer failure or a successful offer. No Vinted request or offer was made.",
                reply_markup=markup,
                disable_web_page_preview=True,
                read_timeout=10, write_timeout=10, connect_timeout=10, pool_timeout=10,
            )
        logger.info("Vinted offer warning test accepted by Telegram")
        return "Telegram accepted the offer-warning test. Your offer queue and settings were unchanged."
    except Exception as exc:  # noqa: BLE001 -- never expose a token or response body
        logger.warning("Vinted offer warning test unavailable: %s", type(exc).__name__)
        raise ValueError(
            "Telegram did not confirm the offer-warning test. Wait five minutes before retrying."
        ) from None
