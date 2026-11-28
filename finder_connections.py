"""Connections and listing delivery for the personal, two-account Finder."""

import time
from contextlib import closing

from search_settings import connection
from vinted_accounts import MARKER, separated

SERVICES = {
    "offers": ("Offers & messages", "seller"),
    "alerts": ("Home & saved-search alerts", "seller"),
    "autobuy": ("Autobuy", "buyer"),
}
SERVICE_TARGETS = {**SERVICES, "alerts-buyer": ("Buying account Home alerts", "buyer")}
AUTH_FAILURES = {"credentials", "refresh_rejected", "signin_redirect", "account_changed", "saved_session"}


def is_disconnect(reason, stage, status, identity_rejected=False):
    return identity_rejected or (
        stage in {"identity", "saved_session", "renewal"}
        and (reason in AUTH_FAILURES or status == 401)
    )


def mark_disconnected(conn, role, checked):
    # The caller has already compared the failed session against the saved account.
    if separated(conn):
        conn.execute(
            "UPDATE finder_listing_accounts SET blocked_at=COALESCE(blocked_at,?) WHERE role=?",
            (checked, role),
        )


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS finder_listing_accounts (
        role TEXT PRIMARY KEY, blocked_at REAL, resumed_at REAL NOT NULL DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS finder_group_notices (
        version TEXT, event_key TEXT, body TEXT NOT NULL, created REAL NOT NULL,
        due REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(version,event_key))""")
    conn.execute("CREATE INDEX IF NOT EXISTS finder_group_notices_due ON finder_group_notices(version,sent,due)")
    for role in ("seller", "buyer"):
        table = "vinted_" + role
        notice = "vinted_seller_connection_notice" if role == "seller" else "vinted_connection_notice"
        conn.execute("INSERT OR IGNORE INTO finder_listing_accounts(role) VALUES(?)", (role,))
        # All successful sign-ins and identity checks update verified_at in the same
        # transaction as the accepted session. Ordinary cookie rotations do not.
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS finder_{role}_verified
            AFTER UPDATE OF verified_at ON {table}
            WHEN NEW.session IS NOT NULL AND NEW.user_id IS NOT NULL AND NEW.verified_at>0
            BEGIN UPDATE finder_listing_accounts SET blocked_at=NULL, resumed_at=NEW.verified_at
            WHERE role='{role}' AND blocked_at IS NOT NULL AND NEW.verified_at>=blocked_at; END""")
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS finder_{role}_disconnected
            AFTER UPDATE OF session,user_id,verified_at ON {table}
            WHEN (NEW.session IS NULL OR NEW.user_id IS NULL OR NEW.verified_at IS NULL)
            AND EXISTS(SELECT 1 FROM parameters WHERE key='{MARKER}')
            BEGIN UPDATE finder_listing_accounts SET blocked_at=COALESCE(blocked_at,unixepoch())
            WHERE role='{role}'; END""")
        if not separated(conn):
            continue
        account = conn.execute(f"""SELECT b.user_id, b.session IS NOT NULL, b.verified_at,
            n.checked,n.reason,n.stage,n.http_status,n.identity_rejected
            FROM {table} b LEFT JOIN {notice} n ON n.id=b.id AND n.buyer_id=b.user_id WHERE b.id=1""").fetchone()
        if not account or not all(account[:3]):
            mark_disconnected(conn, role, time.time())
            continue
        if account[3] and account[3] >= account[2] and is_disconnect(*account[4:]):
            mark_disconnected(conn, role, account[3])
        access = conn.execute(f"SELECT checked,reason,stage,http_status FROM {table}_access WHERE id=1").fetchone()
        if access and access[0] and access[0] >= account[2] and is_disconnect(*access[1:]):
            mark_disconnected(conn, role, access[0])


def gate(conn=None):
    """Return accounts requiring sign-in, and the earliest eligible listing time."""
    if conn is None:
        with closing(connection()) as opened:
            return gate(opened)
    if not separated(conn):
        return (), 0
    rows = conn.execute("""SELECT g.role,g.blocked_at,g.resumed_at,
        CASE g.role WHEN 'seller' THEN s.session IS NOT NULL AND s.user_id IS NOT NULL AND s.verified_at>0
                    ELSE b.session IS NOT NULL AND b.user_id IS NOT NULL AND b.verified_at>0 END AS saved
        FROM finder_listing_accounts g LEFT JOIN vinted_seller s ON s.id=1 LEFT JOIN vinted_buyer b ON b.id=1
        ORDER BY g.role DESC""").fetchall()
    return tuple(role for role, blocked, _, saved in rows if blocked is not None or not saved), max(
        (resumed for _, _, resumed, _ in rows), default=0
    )


def allows_listing(observed, conn=None):
    blocked, resumed = gate(conn)
    return not blocked and observed >= resumed


def service_url(service):
    from vinted_connection_notices import dashboard_url

    if service not in SERVICE_TARGETS:
        return None
    base = dashboard_url()
    return base.split("#", 1)[0] + "?reconnect=" + service if base else None
