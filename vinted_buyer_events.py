"""Durable buyer confirmations and checkout failures, separate from login health."""

import logging
import re
import time
import uuid
from contextlib import closing

import vinted_buyer as buyer
from search_settings import connection
from vinted_connection_notices import DELIVERY_LEASE, DELIVERY_RETRY, ConnectionNotifier

logger = logging.getLogger(__name__)
PHASES = frozenset(
    {
        "checking the listing",
        "preparing the purchase",
        "building the checkout",
        "loading delivery and payment choices",
        "checking the final total and delivery choice",
        "submitting payment",
        "loading the existing checkout",
        "selecting your delivery and payment preferences",
        "checking the quoted checkout",
        "confirming your delivery and payment preferences",
    }
)
FAILURES = frozenset(
    {
        "security_challenge",
        "forbidden",
        "api_cookie_scope",
        "credentials",
        "signin_redirect",
        "account_restricted",
        "csrf",
        "network",
        "rate_limited",
        "http_error",
        "unreadable",
        "session_refresh",
    }
)


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_buyer_event (
        channel TEXT PRIMARY KEY CHECK(channel IN ('connection','checkout')),
        event_id TEXT NOT NULL, buyer_id TEXT NOT NULL, kind TEXT NOT NULL,
        checked REAL NOT NULL, reason TEXT, stage TEXT, http_status INTEGER,
        api_code INTEGER,
        phase TEXT, payment_started INTEGER NOT NULL DEFAULT 0,
        sent INTEGER NOT NULL DEFAULT 0, lease_id TEXT,
        lease_until REAL NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0)""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(vinted_buyer_event)")}
    if "api_code" not in columns:
        conn.execute("ALTER TABLE vinted_buyer_event ADD COLUMN api_code INTEGER")
    if "checkout_id" not in columns:
        conn.execute("ALTER TABLE vinted_buyer_event ADD COLUMN checkout_id TEXT")


def _current(conn, reference):
    if not isinstance(reference, tuple) or len(reference) != 3:
        return None
    current = conn.execute(
        "SELECT session,user_id,verified_at FROM vinted_buyer WHERE id=1"
    ).fetchone()
    return (
        current
        if (
            current
            and current[2]
            and current[0] == reference[1]
            and current[1] == reference[0]
        )
        else None
    )


def record_connected(reference, *, manual=False):
    """Only a durably saved, verified account can queue a confirmation."""
    try:
        with closing(connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            current = _current(conn, reference)
            if current is None:
                return False
            failure = conn.execute(
                "SELECT buyer_id,checked,sent_level FROM vinted_connection_notice WHERE id=1"
            ).fetchone()
            recovered = bool(
                failure
                and failure[0] == current[1]
                and failure[1]
                and failure[1] <= current[2]
                and failure[2]
            )
            if not manual and not recovered:
                return False
            conn.execute(
                """INSERT OR REPLACE INTO vinted_buyer_event
                (channel,event_id,buyer_id,kind,checked,reason,stage,http_status)
                VALUES ('connection',?,?,?,?, 'connected','identity',200)""",
                (
                    uuid.uuid4().hex,
                    current[1],
                    "reconnected" if manual else "recovered",
                    time.time(),
                ),
            )
            # Close the reported account incident atomically so repeated healthy
            # checks cannot enqueue another recovery. Checkout evidence is separate.
            conn.execute(
                "UPDATE vinted_connection_notice SET checked=NULL,incident_started=NULL,"
                "sent_level=0,lease_id=NULL,lease_until=0,retry_at=0 WHERE id=1"
            )
        return True
    except Exception as exc:  # noqa: BLE001 -- optional notices never undo a save
        logger.warning("Vinted buyer confirmation unavailable: %s", type(exc).__name__)
        return False


def record_checkout_failure(client, error, phase, *, payment_started=False, checkout_id=None):
    """Capture only failures from the exact verified client which made the request."""
    if (
        not isinstance(error, buyer.BuyerError)
        or error.reason not in FAILURES
        or phase not in PHASES
        or error.stage not in buyer.AUTH_STAGES
    ):
        return False
    reference = getattr(client, "_verified_session", None)
    checkout_id = (
        checkout_id
        if isinstance(checkout_id, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", checkout_id)
        else None
    )
    try:
        with closing(connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            current = _current(conn, reference)
            if current is None:
                return False
            previous = conn.execute(
                "SELECT * FROM vinted_buyer_event WHERE channel='checkout'"
            ).fetchone()
            # One warning per unresolved checkout episode, with one escalation
            # if a later attempt leaves a payment result uncertain.
            same = bool(
                previous
                and previous["buyer_id"] == current[1]
                and previous["kind"] == "blocked"
                and bool(previous["payment_started"]) == bool(payment_started)
            )
            status = (
                error.status
                if type(error.status) is int and 100 <= error.status <= 599
                else None
            )
            api_code = buyer.api_error_code(getattr(error, "api_code", None))
            if same:
                conn.execute(
                    "UPDATE vinted_buyer_event SET checked=?,reason=?,stage=?,http_status=?,phase=?,api_code=?,checkout_id=? "
                    "WHERE channel='checkout'",
                    (time.time(), error.reason, error.stage, status, phase, api_code, checkout_id),
                )
            else:
                conn.execute(
                    """INSERT OR REPLACE INTO vinted_buyer_event
                    (channel,event_id,buyer_id,kind,checked,reason,stage,http_status,phase,payment_started,api_code,checkout_id)
                    VALUES ('checkout',?,?,'blocked',?,?,?,?,?,?,?,?)""",
                    (
                        uuid.uuid4().hex,
                        current[1],
                        time.time(),
                        error.reason,
                        error.stage,
                        status,
                        phase,
                        int(bool(payment_started)),
                        api_code,
                        checkout_id,
                    ),
                )
        return True
    except Exception as exc:  # noqa: BLE001 -- preserve the actual purchase result
        logger.warning("Vinted checkout notice unavailable: %s", type(exc).__name__)
        return False


def clear_checkout_failure(client, *, checkout_id=None):
    """A confirmed purchase, never an identity GET, closes checkout evidence."""
    if checkout_id is not None and (
        not isinstance(checkout_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", checkout_id)
    ):
        return
    try:
        with closing(connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            current = _current(conn, getattr(client, "_verified_session", None))
            if current is not None:
                # A status read may resolve an older payment. It must not
                # erase a refusal from another checkout or an unbound legacy
                # warning. A newly completed purchase retains the existing
                # unscoped recovery behavior.
                sql = "DELETE FROM vinted_buyer_event WHERE channel='checkout' AND buyer_id=?"
                params = (current[1],)
                if checkout_id is not None:
                    sql += " AND checkout_id=?"
                    params += (checkout_id,)
                conn.execute(sql, params)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Vinted checkout recovery metadata unavailable: %s", type(exc).__name__
        )


def checkout_health(conn, user_id):
    row = conn.execute(
        "SELECT checked,reason,http_status,phase,payment_started,api_code FROM vinted_buyer_event "
        "WHERE channel='checkout' AND buyer_id=? AND kind='blocked'",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    value = dict(row)
    value["api_code"] = buyer.api_error_code(value["api_code"])
    value["checked_display"] = buyer._display_auth_time(value["checked"])
    return value


class BuyerEventNotifier(ConnectionNotifier):
    def claim(self):
        if not self.recipient_valid():
            return None
        now = time.time()
        with closing(connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT session,user_id,verified_at FROM vinted_buyer WHERE id=1"
            ).fetchone()
            if not current or not current[0] or not current[2]:
                conn.execute("DELETE FROM vinted_buyer_event")
                return None
            conn.execute(
                "DELETE FROM vinted_buyer_event WHERE buyer_id<>?", (current[1],)
            )
            selected = conn.execute(
                "SELECT * FROM vinted_buyer_event WHERE sent=0 AND retry_at<=? AND lease_until<=? "
                "ORDER BY checked LIMIT 1",
                (now, now),
            ).fetchone()
            if selected is None:
                return None
            row = dict(selected)
            lease = uuid.uuid4().hex
            conn.execute(
                "UPDATE vinted_buyer_event SET lease_id=?,lease_until=? WHERE channel=?",
                (lease, now + DELIVERY_LEASE, row["channel"]),
            )
            row.update(lease_id=lease, level=1)
            return row

    def still_current(self, row):
        if not self.recipient_valid():
            return False
        with closing(connection()) as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM vinted_buyer_event e JOIN vinted_buyer b ON b.id=1 "
                    "WHERE e.event_id=? AND e.lease_id=? AND e.buyer_id=b.user_id "
                    "AND b.session IS NOT NULL AND b.verified_at IS NOT NULL",
                    (row["event_id"], row["lease_id"]),
                ).fetchone()
                is not None
            )

    def finish(self, row, message_id=None, delay=DELIVERY_RETRY):
        with closing(connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer_event SET sent=?,lease_id=NULL,lease_until=0,retry_at=? "
                "WHERE event_id=? AND lease_id=?",
                (
                    int(message_id is not None),
                    0 if message_id is not None else time.time() + delay,
                    row["event_id"],
                    row["lease_id"],
                ),
            )

    def notice_text(self, row):
        checked = buyer._display_auth_time(row["checked"])
        if row["channel"] == "connection":
            title = (
                "✅ Vinted buyer reconnected"
                if row["kind"] == "reconnected"
                else "✅ Vinted account connection recovered"
            )
            with closing(connection()) as conn:
                config = conn.execute(
                    "SELECT enabled FROM vinted_buyer WHERE id=1"
                ).fetchone()
                checkout = checkout_health(conn, row["buyer_id"])
                failure = conn.execute(
                    "SELECT checked FROM vinted_connection_notice WHERE id=1"
                ).fetchone()
            text = (
                title
                + "\n\nVinted verified your saved buyer account. Autobuy permission is "
                + ("ON." if config[0] else "OFF.")
            )
            text += " Account verification does not test checkout or payment."
            if failure and failure[0] and failure[0] > row["checked"]:
                text += " A newer connection problem has since been recorded; check Connections."
            if checkout:
                text += " Your last checkout failure has not yet been cleared by a successful purchase."
        else:
            status = f" (HTTP {row['http_status']})" if row["http_status"] else ""
            api_code = buyer.api_error_code(row.get("api_code"))
            text = (
                "⚠️ Vinted Autobuy needs attention\n\nThe last attempt stopped while "
                + row["phase"]
                + status
                + "."
            )
            if api_code is not None:
                text += f" Vinted code {api_code}."
            if row["reason"] == "security_challenge":
                text += " Vinted requested a security check. Complete any required check in your Vinted account."
            elif row["reason"] == "forbidden":
                text += " Vinted refused the request. This does not mean your login disconnected."
            elif row["reason"] == "api_cookie_scope":
                text += " The saved access cookie does not apply to Vinted's API host. Reconnect using the original Vinted browser cookies. No API request was sent."
            elif row["reason"] == "rate_limited":
                text += " Vinted requested a cooldown."
            else:
                text += " Vinted did not confirm this request."
            text += (
                " Payment may have been submitted: check Vinted before retrying."
                if row["payment_started"]
                else " No payment was sent."
            )
            text += "\n\nOpen Connections to review the failure. A successful login check alone does not clear checkout failures."
        return text + ("\n\n" + checked if checked else "")
