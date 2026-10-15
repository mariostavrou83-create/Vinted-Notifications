"""Current-session-bound Telegram warnings; no Vinted requests or buying actions."""

import asyncio
import hashlib
import logging
import math
import os
import re
import time
import uuid
from contextlib import closing
from urllib.parse import urlsplit

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter

import db
import vinted_buyer as buyer
from search_settings import connection

logger = logging.getLogger(__name__)
TRANSIENT_GRACE = 15 * 60
DELIVERY_RETRY = 5 * 60
DELIVERY_LEASE = 120
RECONNECT_REASONS = frozenset(
    {
        "refresh_rejected",
        "credentials",
        "signin_redirect",
        "account_changed",
        "saved_session",
    }
)


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_connection_notice (
        id INTEGER PRIMARY KEY CHECK(id=1), session_fingerprint TEXT,
        buyer_id TEXT, reason TEXT, stage TEXT, http_status INTEGER,
        checked REAL, incident_started REAL, sent_level INTEGER NOT NULL DEFAULT 0,
        lease_id TEXT, lease_until REAL NOT NULL DEFAULT 0,
        retry_at REAL NOT NULL DEFAULT 0, telegram_message_id INTEGER,
        test_checked REAL NOT NULL DEFAULT 0)""")
    conn.execute("INSERT OR IGNORE INTO vinted_connection_notice(id) VALUES (1)")
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(vinted_connection_notice)")
    }
    if "identity_rejected" not in columns:
        conn.execute(
            "ALTER TABLE vinted_connection_notice ADD COLUMN identity_rejected INTEGER NOT NULL DEFAULT 0"
        )
    from vinted_buyer_events import migrate as migrate_events

    migrate_events(conn)


def record_failure(error, session_fingerprint, *, checked_at=None):
    """Record fixed failure metadata only if its encrypted session is still current.

    The caller supplies the fingerprint captured by the failed account request.
    Historical password login failures and superseded connections are excluded.
    Notification bookkeeping must never obscure the original account error.
    """
    if (
        not isinstance(error, buyer.BuyerError)
        or error.reason not in buyer.AUTH_REASONS
        or error.reason
        in ("connected", "renewed", "verification_code", "endpoint_reached")
        or error.stage not in buyer.AUTH_STAGES
        or error.stage == "sign_in"
        or not isinstance(session_fingerprint, str)
        or not re.fullmatch(r"[0-9a-f]{64}", session_fingerprint)
    ):
        return False
    try:
        now = time.time() if checked_at is None else checked_at
        if type(now) not in (int, float) or not math.isfinite(now):
            return False
        status = (
            error.status
            if type(error.status) is int and 100 <= error.status <= 599
            else None
        )
        with closing(connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT session,user_id,verified_at FROM vinted_buyer WHERE id=1"
            ).fetchone()
            if (
                not current
                or not current[0]
                or not current[1]
                or not current[2]
                or now < current[2]
                or hashlib.sha256(current[0]).hexdigest() != session_fingerprint
            ):
                return False
            previous = conn.execute(
                "SELECT * FROM vinted_connection_notice WHERE id=1"
            ).fetchone()
            same_incident = bool(
                previous["buyer_id"] == current[1]
                and previous["checked"] is not None
                and previous["checked"] >= current[2]
            )
            conn.execute(
                """UPDATE vinted_connection_notice SET session_fingerprint=?,buyer_id=?,
                reason=?,stage=?,http_status=?,checked=?,incident_started=?,sent_level=?,
                lease_id=?,lease_until=?,retry_at=?,telegram_message_id=?,identity_rejected=? WHERE id=1""",
                (
                    session_fingerprint,
                    current[1],
                    error.reason,
                    error.stage,
                    status,
                    now,
                    previous["incident_started"] if same_incident else now,
                    previous["sent_level"] if same_incident else 0,
                    previous["lease_id"] if same_incident else None,
                    previous["lease_until"] if same_incident else 0,
                    previous["retry_at"] if same_incident else 0,
                    previous["telegram_message_id"] if same_incident else None,
                    int(getattr(error, "identity_rejected", False) is True),
                ),
            )
        return True
    except Exception as exc:  # noqa: BLE001 -- optional bookkeeping cannot break buying
        logger.warning(
            "Vinted connection notice capture unavailable: %s", type(exc).__name__
        )
        return False


def dashboard_url():
    base = os.environ.get("DASHBOARD_URL", "").strip().rstrip("/")
    if not base:
        domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
        base = "https://" + domain if domain else ""
    try:
        parsed = urlsplit(base)
        if (
            len(base) > 2048
            or any(ord(char) <= 32 for char in base)
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
            or parsed.port not in (None, 443)
        ):
            return None
        return base.rstrip("/") + "/connections#vinted-buying"
    except ValueError:
        return None


def warning_level(row, now):
    reason, stage, status = row["reason"], row["stage"], row["http_status"]
    if row.get("identity_rejected"):
        return 2
    if reason in RECONNECT_REASONS:
        return 2
    if reason == "renewal_failed" and stage == "renewal" and status == 400:
        return 1
    if reason in ("network", "rate_limited") or (type(status) is int and status >= 500):
        return 1 if now - row["incident_started"] >= TRANSIENT_GRACE else 0
    return 1


def warning_text(row):
    reason, stage, status = row["reason"], row["stage"], row["http_status"]
    if row.get("identity_rejected"):
        detail = "Vinted rejected the saved account check (HTTP 401), and the connection could not be restored. Autobuy cannot proceed with this connection."
        action = (
            "Open Connections → Vinted & buying and reconnect the same buyer account."
        )
    elif reason == "renewal_failed" and stage == "renewal" and status == 400:
        detail = "The saved session renewal could not be confirmed. This does not prove that your login has expired. Automatic maintenance will retry after its cooldown."
        action = (
            "Open Connections → Vinted & buying and check the saved buyer connection."
        )
    else:
        detail = buyer.AUTH_REASONS.get(reason, buyer.AUTH_REASONS["not_confirmed"])
        if reason == "account_restricted":
            action = "Open your Vinted account and check its restriction notice."
        elif reason == "security_challenge":
            action = "Open Connections → Vinted & buying and check the security-check result."
        elif reason in RECONNECT_REASONS:
            action = "Open Connections → Vinted & buying and reconnect the same buyer account."
        elif reason in ("network", "rate_limited") or (
            type(status) is int and status >= 500
        ):
            action = "No later successful account check has been recorded for at least 15 minutes. Open Connections to check the saved buyer connection."
        else:
            action = "Open Connections → Vinted & buying and check the latest connection result."
    operation = buyer.AUTH_STAGES.get(stage, buyer.AUTH_STAGES["request"])
    evidence = operation + (f" · HTTP {status}" if status is not None else "")
    checked = buyer._display_auth_time(row["checked"])
    return (
        "⚠️ Vinted Autobuy connection needs attention\n\n"
        + detail
        + "\n\n"
        + evidence
        + ("\n" + checked if checked else "")
        + "\n\n"
        + action
    )


class ConnectionNotifier:
    def __init__(self, bot, chat_id):
        self.bot = bot
        self.chat_id = str(chat_id or "").strip()

    def recipient_valid(self):
        return bool(
            re.fullmatch(r"-?[0-9]{1,20}", self.chat_id)
            and self.chat_id != "0"
            and str(db.get_parameter("telegram_chat_id") or "").strip() == self.chat_id
        )

    def notice_text(self, row):
        return warning_text(row)

    def claim(self):
        if not self.recipient_valid():
            return None
        now = time.time()
        # Skip a tick when a purchase or connection check owns the buyer lock.
        # No Telegram request is made while holding it.
        with buyer.exclusive(), closing(connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT session,user_id,verified_at FROM vinted_buyer WHERE id=1"
            ).fetchone()
            row = dict(
                conn.execute(
                    "SELECT * FROM vinted_connection_notice WHERE id=1"
                ).fetchone()
            )
            if (
                not current
                or not current[0]
                or not current[2]
                or row["buyer_id"] != current[1]
                or row["checked"] is None
                or current[2] > row["checked"]
            ):
                # Fresh same-account verification or a removed connection closes
                # the old episode, including any queued delivery lease.
                conn.execute(
                    "UPDATE vinted_connection_notice SET session_fingerprint=NULL,checked=NULL,incident_started=NULL,sent_level=0,lease_id=NULL,lease_until=0,retry_at=0 WHERE id=1"
                )
                return None
            if (
                hashlib.sha256(current[0]).hexdigest() != row["session_fingerprint"]
                or row["reason"] not in buyer.AUTH_REASONS
                or row["stage"] not in buyer.AUTH_STAGES
                or row["stage"] == "sign_in"
                or row["incident_started"] is None
                or row["retry_at"] > now
                or row["lease_until"] > now
            ):
                return None
            level = warning_level(row, now)
            if level <= row["sent_level"]:
                return None
            lease = uuid.uuid4().hex
            conn.execute(
                "UPDATE vinted_connection_notice SET lease_id=?,lease_until=? WHERE id=1",
                (lease, now + DELIVERY_LEASE),
            )
            row.update(lease_id=lease, level=level)
            return row

    def still_current(self, row):
        if not self.recipient_valid():
            return False
        with closing(connection()) as conn:
            current = conn.execute(
                "SELECT session,user_id,verified_at FROM vinted_buyer WHERE id=1"
            ).fetchone()
            pending = conn.execute(
                "SELECT lease_id,checked FROM vinted_connection_notice WHERE id=1"
            ).fetchone()
        return bool(
            current
            and current[0]
            and current[1] == row["buyer_id"]
            and current[2]
            and current[2] <= row["checked"]
            and hashlib.sha256(current[0]).hexdigest() == row["session_fingerprint"]
            and pending
            and pending[0] == row["lease_id"]
            and pending[1] == row["checked"]
        )

    def finish(self, row, message_id=None, delay=DELIVERY_RETRY):
        with closing(connection()) as conn, conn:
            if message_id is not None:
                conn.execute(
                    "UPDATE vinted_connection_notice SET sent_level=MAX(sent_level,?),telegram_message_id=?,lease_id=NULL,lease_until=0,retry_at=0 WHERE id=1 AND lease_id=?",
                    (row["level"], message_id, row["lease_id"]),
                )
            else:
                conn.execute(
                    "UPDATE vinted_connection_notice SET lease_id=NULL,lease_until=0,retry_at=? WHERE id=1 AND lease_id=?",
                    (time.time() + delay, row["lease_id"]),
                )

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
            markup = (
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Open Vinted connection", url=url)]]
                )
                if url
                else None
            )
            sent = await self.bot.send_message(
                chat_id=self.chat_id,
                text=self.notice_text(row),
                reply_markup=markup,
                disable_web_page_preview=True,
                read_timeout=10,
                write_timeout=10,
                connect_timeout=10,
                pool_timeout=10,
            )
            await asyncio.to_thread(self.finish, row, sent.message_id)
            logger.info(
                "Vinted buyer notification accepted: channel=%s kind=%s level=%s stage=%s reason=%s http=%s",
                row.get("channel", "account"),
                row.get("kind", "warning"),
                row["level"],
                row["stage"],
                row["reason"],
                row["http_status"],
            )
            return "sent"
        except buyer.BuyerError as exc:
            if exc.reason == "busy":
                return "busy"
            logger.warning("Vinted connection notice unavailable: BuyerError")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Vinted connection notice unavailable: %s", type(exc).__name__
            )
            if row is not None:
                delay = DELIVERY_RETRY
                if isinstance(exc, RetryAfter):
                    seconds = exc.retry_after
                    seconds = (
                        seconds.total_seconds()
                        if hasattr(seconds, "total_seconds")
                        else seconds
                    )
                    if type(seconds) in (int, float) and math.isfinite(seconds):
                        delay = max(DELIVERY_RETRY, min(86400, seconds + 2))
                try:
                    await asyncio.to_thread(self.finish, row, delay=delay)
                except Exception:  # noqa: BLE001 -- retain the bounded lease for retry
                    logger.warning(
                        "Vinted connection notice retry bookkeeping unavailable"
                    )
        return "unavailable"


async def send_test():
    """One clearly labelled owner-requested delivery test; no account request."""
    chat_id = str(db.get_parameter("telegram_chat_id") or "").strip()
    if not re.fullmatch(r"-?[0-9]{1,20}", chat_id) or chat_id == "0":
        raise ValueError("Configure the Vinted Telegram chat before testing warnings.")
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            "SELECT test_checked FROM vinted_connection_notice WHERE id=1"
        ).fetchone()[0]
        if time.time() - previous < 300:
            raise ValueError(
                "Wait five minutes before sending another connection-warning test."
            )
        conn.execute(
            "UPDATE vinted_connection_notice SET test_checked=? WHERE id=1",
            (time.time(),),
        )
    try:
        url = dashboard_url()
        markup = (
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("Open Vinted connection", url=url)]]
            )
            if url
            else None
        )
        async with Bot(db.get_parameter("telegram_token")) as bot:
            await bot.send_message(
                chat_id=chat_id,
                text="TEST — Vinted connection warnings\n\nAutomatic Telegram warnings are enabled for detected saved-connection failures, with the reason and a dashboard link. Repeated warnings for the same unresolved problem are suppressed.\n\nThis is a delivery test, not an account failure or a successful Vinted renewal. No Vinted request, checkout or payment was made.",
                reply_markup=markup,
                disable_web_page_preview=True,
                read_timeout=10,
                write_timeout=10,
                connect_timeout=10,
                pool_timeout=10,
            )
        logger.info("Vinted connection warning test accepted by Telegram")
        return "Telegram accepted the connection-warning test. Your Vinted connection and buying settings were unchanged."
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Vinted connection warning test unavailable: %s", type(exc).__name__
        )
        raise ValueError(
            "Telegram did not confirm the connection-warning test. Wait five minutes before retrying."
        ) from None
