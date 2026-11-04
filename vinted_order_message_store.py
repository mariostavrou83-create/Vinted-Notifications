"""Durable one-message-per-order queue, independent of worker lifetimes."""

from contextlib import closing
import json

from search_settings import connection

DELAYS = {"purchase": 180, "sale": 180, "review": 172800}


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_message_discovery (
        account_id TEXT NOT NULL, transaction_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL, side TEXT NOT NULL,
        source_status TEXT NOT NULL, source_phase TEXT NOT NULL,
        created_at REAL NOT NULL, next_check REAL NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(account_id,transaction_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_message_orders (
        account_id TEXT NOT NULL, transaction_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL, side TEXT NOT NULL,
        recipient_id TEXT, paid INTEGER NOT NULL DEFAULT 0,
        delivered INTEGER NOT NULL DEFAULT 0, cancelled INTEGER NOT NULL DEFAULT 0,
        paid_at REAL, delivered_at REAL, first_seen REAL NOT NULL,
        checked_at REAL NOT NULL, detail TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY(account_id,transaction_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_message_jobs (
        id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, transaction_id TEXT NOT NULL,
        kind TEXT NOT NULL, conversation_id TEXT NOT NULL, recipient_id TEXT NOT NULL,
        event_at REAL NOT NULL, due_at REAL NOT NULL, body TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued', attempted_at REAL, sent_at REAL,
        message_id TEXT, reason TEXT NOT NULL DEFAULT '',
        UNIQUE(account_id,transaction_id,kind))""")
    conn.execute("CREATE INDEX IF NOT EXISTS vinted_message_due ON vinted_message_jobs(account_id,status,due_at)")


def discover(account, side, rows):
    from vinted_offers import identifier, timestamp
    import re
    with closing(connection()) as conn, conn:
        for row in rows:
            tid = identifier(row.get("transaction_id"))
            cid = row.get("conversation_id", "")
            at = timestamp(row.get("date"))
            if not tid or not isinstance(cid, str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", cid) or not at:
                raise ValueError("Vinted order identifiers or dates could not be verified.")
            conn.execute("""INSERT INTO vinted_message_discovery
                (account_id,transaction_id,conversation_id,side,source_status,source_phase,created_at)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(account_id,transaction_id) DO UPDATE SET
                next_check=CASE WHEN source_status<>excluded.source_status OR source_phase<>excluded.source_phase
                    THEN 0 ELSE next_check END,
                source_status=excluded.source_status,source_phase=excluded.source_phase""",
                (account, tid, cid, side, str(row.get("status", "")), str(row.get("transaction_user_status", "")), at))


def pending_order(account, now):
    with closing(connection()) as conn:
        row = conn.execute("SELECT * FROM vinted_message_discovery WHERE account_id=? AND next_check<=? ORDER BY created_at DESC LIMIT 1", (account, now)).fetchone()
    return dict(row) if row else None


def checked(account, tid, next_check, error=""):
    with closing(connection()) as conn, conn:
        conn.execute("UPDATE vinted_message_discovery SET next_check=?,last_error=? WHERE account_id=? AND transaction_id=?", (next_check, error, account, tid))


def uncertain(account):
    with closing(connection()) as conn:
        row = conn.execute("SELECT * FROM vinted_message_jobs WHERE account_id=? AND status IN ('sending','uncertain') ORDER BY attempted_at LIMIT 1", (account,)).fetchone()
    return dict(row) if row else None


def observe(config, order, now):
    """Queue only verified new events; unchanged observations never move timers."""
    account, tid = config["account_id"], order["transaction_id"]
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute("SELECT * FROM vinted_message_orders WHERE account_id=? AND transaction_id=?", (account, tid)).fetchone()
        if old and (old["conversation_id"] != order["conversation_id"] or old["side"] != order["side"] or (old["recipient_id"] and order["recipient_id"] and old["recipient_id"] != order["recipient_id"])):
            conn.execute("UPDATE vinted_message_jobs SET status='skipped',reason='Order recipient changed' WHERE account_id=? AND transaction_id=? AND status='queued'", (account, tid))
            raise ValueError("Order recipient changed; messages stopped.")
        paid_at = (old["paid_at"] if old else None) or order.get("paid_at")
        delivered_at = (old["delivered_at"] if old else None) or order.get("delivered_at")
        # Without an explicit event timestamp, require a previously observed
        # transition. An old completed order can never become a fresh delivery.
        if order["paid"] and not paid_at and old and not old["paid"]:
            paid_at = now
        if order["delivered"] and not delivered_at and old and not old["delivered"]:
            delivered_at = now
        conn.execute("""INSERT INTO vinted_message_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id,transaction_id) DO UPDATE SET
            recipient_id=excluded.recipient_id,paid=excluded.paid,
            delivered=excluded.delivered,cancelled=excluded.cancelled,
            paid_at=excluded.paid_at,delivered_at=excluded.delivered_at,
            checked_at=excluded.checked_at,detail=excluded.detail""",
            (account, tid, order["conversation_id"], order["side"], order["recipient_id"],
             bool(order["paid"]), bool(order["delivered"]), bool(order["cancelled"]),
             paid_at, delivered_at, old["first_seen"] if old else now, now,
             json.dumps(order.get("detail", {}))))
        if order["cancelled"]:
            conn.execute("UPDATE vinted_message_jobs SET status='skipped',reason='Order cancelled' WHERE account_id=? AND transaction_id=? AND status='queued'", (account, tid))
            return
        # Re-read the current controls inside this transaction to respect a
        # pause or account switch that happened while Vinted was being read.
        live = conn.execute("SELECT mode,account_id,enabled_at,templates FROM vinted_order_message_settings WHERE id=1").fetchone()
        if live["mode"] != "live" or live["account_id"] != account or not live["enabled_at"] or not order["recipient_id"]:
            return
        events = [("purchase" if order["side"] == "buyer" else "sale", paid_at if order["paid"] else None)]
        if order["side"] == "seller" and order["paid"] and order["delivered"]:
            events.append(("review", delivered_at))
        for kind, at in events:
            if at is not None and live["enabled_at"] <= at <= now:
                conn.execute("""INSERT OR IGNORE INTO vinted_message_jobs
                    (account_id,transaction_id,kind,conversation_id,recipient_id,event_at,due_at,body)
                    VALUES (?,?,?,?,?,?,?,?)""", (account, tid, kind, order["conversation_id"],
                    order["recipient_id"], at, at + DELAYS[kind], json.loads(live["templates"])[kind]))


def due(account, now):
    with closing(connection()) as conn:
        row = conn.execute("""SELECT * FROM vinted_message_jobs WHERE account_id=?
            AND status='queued' AND due_at<=? ORDER BY due_at,id LIMIT 1""", (account, now)).fetchone()
    return dict(row) if row else None


def begin_send(job, now):
    """Commit before POST. Any crash after this point can only reconcile."""
    with closing(connection()) as conn, conn:
        result = conn.execute("""UPDATE vinted_message_jobs SET status='sending',attempted_at=?
            WHERE id=? AND status='queued' AND due_at<=? AND EXISTS
            (SELECT 1 FROM vinted_order_message_settings WHERE id=1 AND mode='live' AND account_id=?)""",
            (now, job["id"], now, job["account_id"]))
        return bool(result.rowcount)


def finish(job_id, status, *, reason="", sent_at=None, message_id=None):
    with closing(connection()) as conn, conn:
        conn.execute("UPDATE vinted_message_jobs SET status=?,reason=?,sent_at=?,message_id=? WHERE id=?",
                     (status, reason, sent_at, message_id, job_id))


def snapshot(account):
    with closing(connection()) as conn:
        counts = dict(conn.execute("SELECT status,COUNT(*) FROM vinted_message_jobs WHERE account_id=? GROUP BY status", (account,)))
        jobs = [dict(row) for row in conn.execute("SELECT * FROM vinted_message_jobs WHERE account_id=? ORDER BY id DESC LIMIT 30", (account,))]
        orders = conn.execute("SELECT COUNT(*) FROM vinted_message_orders WHERE account_id=?", (account,)).fetchone()[0]
        checks = [dict(row) for row in conn.execute("SELECT transaction_id,last_error FROM vinted_message_discovery WHERE account_id=? AND last_error<>'' LIMIT 30", (account,))]
    return {"counts": counts, "jobs": jobs, "observed_orders": orders, "check_errors": checks}
