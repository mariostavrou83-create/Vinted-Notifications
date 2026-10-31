"""Bounded, read-only recovery of recent uncertain payment results.

The immutable order identity owns its three-check budget across state updates
and restarts. Nothing here creates checkout, submits payment, or solves a
challenge. An unreadable result always leaves the original payment quarantine.
"""

import asyncio
import hashlib
import json
import re
import time
import uuid
from contextlib import closing

import vinted_buyer as buyer
import vinted_buying as buying
from logger import get_logger
from search_settings import connection
from vinted_buyer_priority import yield_to_purchase
from vinted_connection_notices import DELIVERY_LEASE, ConnectionNotifier

logger = get_logger(__name__)
MAX_AGE = 30 * 60
MAX_CHECKS = 3
CHECK_DELAYS = (5, 15, 40)
INTERVAL = 5
IDENTITY_FIELDS = ("item_id", "buyer_id", "transaction_id", "checkout_id")
PENDING = ("unknown", "needs_action")
NOTICE_STATES = ("paid", "payment_failed", "needs_action")


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_payment_recovery (
        identity TEXT PRIMARY KEY, item_id TEXT NOT NULL, buyer_id TEXT NOT NULL,
        transaction_id TEXT NOT NULL, checkout_id TEXT NOT NULL,
        observed_at REAL NOT NULL, observed_state TEXT NOT NULL,
        checks INTEGER NOT NULL DEFAULT 0,
        next_at REAL NOT NULL, lease_id TEXT, lease_until REAL NOT NULL DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_payment_recovery_notice (
        identity TEXT NOT NULL, state TEXT NOT NULL, checked REAL NOT NULL,
        sent INTEGER NOT NULL DEFAULT 0, lease_id TEXT,
        lease_until REAL NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
        PRIMARY KEY(identity,state))""")


def payment_identity(row):
    """Require the same complete provenance as the existing status checker."""
    if not row or any(
        not isinstance(row.get(key), str)
        or not re.fullmatch(r"[0-9]{1,24}", row[key])
        for key in IDENTITY_FIELDS[:3]
    ) or not isinstance(row.get("checkout_id"), str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,100}", row["checkout_id"]
    ):
        return None
    return hashlib.sha256(json.dumps(
        [row[key] for key in IDENTITY_FIELDS], separators=(",", ":"),
    ).encode("ascii")).hexdigest()


def _matches(row, job):
    return bool(row and all(row[key] == job[key] for key in IDENTITY_FIELDS))


def record_verified(conn, item_id, state):
    """Join the verified state write's transaction; never start network work.

    Owner resolutions and ordinary payment submissions never call this hook.
    Persisting this receipt with the result closes the process-crash gap before
    the worker can queue or deliver a notification itself.
    """
    if state not in NOTICE_STATES:
        return
    now = time.time()
    job = conn.execute(
        "SELECT r.identity FROM vinted_payment_recovery r "
        "JOIN vinted_buy_attempts a ON a.item_id=r.item_id AND a.buyer_id=r.buyer_id "
        "AND a.transaction_id=r.transaction_id AND a.checkout_id=r.checkout_id "
        "JOIN vinted_buyer b ON b.id=1 AND b.user_id=r.buyer_id "
        "WHERE a.item_id=? AND a.state=? AND r.observed_state<>? "
        "AND r.checks>0 AND r.observed_at BETWEEN ? AND ? LIMIT 1",
        (str(item_id), state, state, now - MAX_AGE, now),
    ).fetchone()
    if job:
        conn.execute(
            "INSERT OR IGNORE INTO vinted_payment_recovery_notice "
            "(identity,state,checked) VALUES (?,?,?)", (job[0], state, now),
        )


def claim():
    """Reserve one read budget before I/O; a crash cannot reset the cap."""
    yield_to_purchase()
    now = time.time()
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT session,user_id,verified_at FROM vinted_buyer WHERE id=1"
        ).fetchone()
        if not current or not current[0] or not current[2]:
            return None
        pending = conn.execute(
            "SELECT * FROM vinted_buy_attempts WHERE state IN ('unknown','needs_action') "
            "AND buyer_id=? AND updated BETWEEN ? AND ? ORDER BY updated LIMIT 20",
            (current[1], now - MAX_AGE, now),
        ).fetchall()
        for value in pending:
            attempt = dict(value)
            identity = payment_identity(attempt)
            if not identity:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO vinted_payment_recovery "
                "(identity,item_id,buyer_id,transaction_id,checkout_id,observed_at,observed_state,next_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (identity, *(attempt[key] for key in IDENTITY_FIELDS),
                 attempt["updated"], attempt["state"], attempt["updated"] + CHECK_DELAYS[0]),
            )
        selected = conn.execute(
            "SELECT r.*,a.state FROM vinted_payment_recovery r "
            "JOIN vinted_buy_attempts a ON a.item_id=r.item_id "
            "AND a.buyer_id=r.buyer_id AND a.transaction_id=r.transaction_id "
            "AND a.checkout_id=r.checkout_id "
            "WHERE a.state IN ('unknown','needs_action') AND r.buyer_id=? "
            "AND r.observed_at BETWEEN ? AND ? AND a.updated<=? "
            "AND r.checks<? AND r.next_at<=? AND r.lease_until<=? "
            "ORDER BY r.next_at,r.identity LIMIT 1",
            (current[1], now - MAX_AGE, now, now, MAX_CHECKS, now, now),
        ).fetchone()
        if selected is None:
            return None
        job = dict(selected)
        lease = uuid.uuid4().hex
        checks = job["checks"] + 1
        conn.execute(
            "UPDATE vinted_payment_recovery SET checks=?,next_at=?,lease_id=?,lease_until=? "
            "WHERE identity=?",
            (checks, now + (CHECK_DELAYS[checks] if checks < MAX_CHECKS else MAX_AGE),
             lease, now + DELIVERY_LEASE, job["identity"]),
        )
        job.update(checks=checks, lease_id=lease)
        return job


def finish(job, outcome=None):
    """Queue a verified changed result durably before Telegram delivery."""
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        reserved = conn.execute(
            "SELECT 1 FROM vinted_payment_recovery WHERE identity=? AND lease_id=?",
            (job["identity"], job["lease_id"]),
        ).fetchone()
        if not reserved:
            return
        saved = conn.execute(
            "SELECT * FROM vinted_buy_attempts WHERE item_id=?", (job["item_id"],),
        ).fetchone()
        if (
            outcome
            and outcome.get("reconciliation_outcome") == "verified"
            and outcome.get("state") in NOTICE_STATES
            and outcome["state"] != job["state"]
            and _matches(saved, job)
            and saved["state"] == outcome["state"]
        ):
            conn.execute(
                "INSERT OR IGNORE INTO vinted_payment_recovery_notice "
                "(identity,state,checked) VALUES (?,?,?)",
                (job["identity"], saved["state"], time.time()),
            )
        conn.execute(
            "UPDATE vinted_payment_recovery SET lease_id=NULL,lease_until=0 "
            "WHERE identity=? AND lease_id=?", (job["identity"], job["lease_id"]),
        )


def tick():
    job = None
    outcome = None
    try:
        job = claim()
        if job is None:
            return "quiet"
        outcome = buying.check_payment(
            job["item_id"], background=True,
            expected_payment=tuple(job[key] for key in IDENTITY_FIELDS[1:]),
        )
        return "checked"
    except buyer.BuyerError as exc:
        logger.info("Autobuy automatic payment check: outcome=unverified reason=%s", exc.reason)
        return "busy" if exc.reason == "busy" else "unverified"
    except Exception as exc:  # noqa: BLE001 -- retain the original payment state
        logger.warning("Autobuy automatic payment check unavailable: %s", type(exc).__name__)
        return "unavailable"
    finally:
        if job is not None:
            finish(job, outcome)


class PaymentRecoveryNotifier(ConnectionNotifier):
    def recipient_valid(self):
        # Purchase actions belong to the configured private owner, not a group.
        return bool(re.fullmatch(r"[1-9][0-9]{0,19}", self.chat_id)) and super().recipient_valid()

    def claim(self):
        if not self.recipient_valid():
            return None
        now = time.time()
        with closing(connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            selected = conn.execute(
                "SELECT n.*,r.item_id,r.buyer_id,r.transaction_id,r.checkout_id,a.total "
                "FROM vinted_payment_recovery_notice n "
                "JOIN vinted_payment_recovery r ON r.identity=n.identity "
                "JOIN vinted_buy_attempts a ON a.item_id=r.item_id AND a.buyer_id=r.buyer_id "
                "AND a.transaction_id=r.transaction_id AND a.checkout_id=r.checkout_id "
                "AND a.state=n.state JOIN vinted_buyer b ON b.id=1 AND b.user_id=r.buyer_id "
                "WHERE n.sent=0 AND n.retry_at<=? AND n.lease_until<=? "
                "AND n.checked>=? ORDER BY n.checked LIMIT 1",
                (now, now, now - 86400),
            ).fetchone()
            if selected is None:
                return None
            row = dict(selected)
            lease = uuid.uuid4().hex
            conn.execute(
                "UPDATE vinted_payment_recovery_notice SET lease_id=?,lease_until=? "
                "WHERE identity=? AND state=?",
                (lease, now + DELIVERY_LEASE, row["identity"], row["state"]),
            )
            row.update(lease_id=lease, channel="payment", kind="reconciled", level=1,
                       stage="payment_status", reason=row["state"], http_status=None)
            return row

    def still_current(self, row):
        if not self.recipient_valid():
            return False
        with closing(connection()) as conn:
            return conn.execute(
                "SELECT 1 FROM vinted_payment_recovery_notice n "
                "JOIN vinted_buy_attempts a ON a.item_id=? AND a.buyer_id=? "
                "AND a.transaction_id=? AND a.checkout_id=? AND a.state=n.state "
                "JOIN vinted_buyer b ON b.id=1 AND b.user_id=a.buyer_id "
                "WHERE n.identity=? AND n.state=? AND n.lease_id=? AND n.sent=0",
                (*(row[key] for key in IDENTITY_FIELDS), row["identity"], row["state"], row["lease_id"]),
            ).fetchone() is not None

    def finish(self, row, message_id=None, delay=60):
        with closing(connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_payment_recovery_notice SET sent=?,lease_id=NULL,lease_until=0,retry_at=? "
                "WHERE identity=? AND state=? AND lease_id=?",
                (int(message_id is not None), 0 if message_id is not None else time.time() + delay,
                 row["identity"], row["state"], row["lease_id"]),
            )

    def notice_text(self, row):
        title = {
            "paid": "Vinted confirmed your Autobuy payment",
            "payment_failed": "Vinted confirmed the Autobuy payment failed",
            "needs_action": "Your Autobuy payment needs confirmation",
        }[row["state"]]
        text = title + f"\n\nItem {row['item_id']}. "
        total = row.get("total")
        if row["state"] == "paid" and type(total) is int and 0 <= total <= 100000000:
            text += f"Order total £{total / 100:.2f}. "
        text += {
            "paid": "Check your Vinted Purchases. Do not buy this item again.",
            "payment_failed": "Check Vinted Purchases and any bank hold before buying again.",
            "needs_action": "Complete any required confirmation in Vinted, then use Check payment status. Do not buy again.",
        }[row["state"]]
        return text + "\n\nThe bot checked the existing payment only; it did not send another payment."


async def run(bot, chat_id):
    notifier = PaymentRecoveryNotifier(bot, chat_id)
    logger.info("Autobuy payment recovery started; read-only; maximum=3 checks per recent payment")
    while True:
        try:
            await asyncio.to_thread(tick)
            await notifier.check()
        except Exception as exc:  # noqa: BLE001 -- independent of fast alert delivery
            logger.warning("Autobuy payment recovery tick unavailable: %s", type(exc).__name__)
        await asyncio.sleep(INTERVAL)
