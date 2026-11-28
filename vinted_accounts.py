"""Explicit buyer/seller storage scopes; credentials remain inside this service."""

import sqlite3
from contextlib import closing, contextmanager
from contextvars import ContextVar
from functools import wraps

_ROLE = ContextVar("vinted_account_role", default="buyer")
TABLES = {
    "vinted_buyer": "vinted_seller",
    "vinted_buyer_access": "vinted_seller_access",
    "vinted_buyer_maintenance": "vinted_seller_maintenance",
    "vinted_buyer_event": "vinted_seller_event",
    "vinted_connection_notice": "vinted_seller_connection_notice",
}
MARKER = "vinted_separate_seller_v1"


def current_role():
    return _ROLE.get()


@contextmanager
def account_scope(role):
    if role not in ("buyer", "seller"):
        raise ValueError("Unknown Vinted account role.")
    token = _ROLE.set(role)
    try:
        yield
    finally:
        _ROLE.reset(token)


def account_table(name):
    if name not in TABLES:
        raise ValueError("Unknown Vinted account table.")
    return TABLES[name] if current_role() == "seller" else name


def account_key(key):
    return "seller_" + key if current_role() == "seller" and not key.startswith("seller_") else key


def separated(conn=None):
    from search_settings import connection

    if conn is None:
        with closing(connection()) as opened:
            return separated(opened)
    return bool(conn.execute("SELECT 1 FROM parameters WHERE key=?", (MARKER,)).fetchone())


def seller_role():
    return "seller" if separated() else "buyer"


def seller_table(name):
    with account_scope(seller_role()):
        return account_table(name)


def client_account(method):
    """A returned client retains its own role after its factory returns."""
    @wraps(method)
    def scoped(self, *args, **kwargs):
        role = getattr(self, "account_role", "buyer")
        with account_scope(role if isinstance(role, str) else current_role()):
            return method(self, *args, **kwargs)
    return scoped


def migrate(conn):
    """Add empty seller tables. Do not clone or change an active session here."""
    for source, destination in TABLES.items():
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (source,)).fetchone()
        if row is None:
            raise sqlite3.OperationalError("Vinted account schema is incomplete.")
        sql = row[0].replace(source, destination, 1)
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (destination,)).fetchone():
            conn.execute(sql)

    # Pause buying once while the owner connects the requested separate account.
    # A later owner settings change is never overwritten on restart.
    if not conn.execute("SELECT 1 FROM parameters WHERE key='vinted_separate_accounts_prepared_v1'").fetchone():
        conn.execute("UPDATE vinted_buyer SET enabled=0 WHERE id=1")
        conn.execute("INSERT INTO parameters(key,value) VALUES ('vinted_separate_accounts_prepared_v1','1')")
    from logger import get_logger
    from vinted_pickup_policy import required_point

    target = required_point()
    get_logger(__name__).info(
        "Vinted account storage ready: separate_accounts_supported=True required_inpost=%s point_complete=%s",
        target is not None,
        bool(target and all(target.get(field) for field in ("code", "name", "postcode"))),
    )


def preserve_seller(conn):
    """Pin the existing selling account atomically with the first buyer switch."""
    if separated(conn):
        return conn.execute("SELECT user_id FROM vinted_seller WHERE id=1").fetchone()[0]
    row = conn.execute("SELECT user_id,session,verified_at FROM vinted_buyer WHERE id=1").fetchone()
    if not row or not all(row):
        raise ValueError("Keep your existing selling account connected before adding a separate buyer.")
    migrate(conn)
    # Notice migrations create an empty singleton before the first account split.
    conn.execute("DELETE FROM vinted_seller_connection_notice WHERE id=1 AND buyer_id IS NULL")
    for source, destination in TABLES.items():
        conn.execute(f"INSERT INTO {destination} SELECT * FROM {source}")
    conn.execute("UPDATE vinted_seller SET enabled=0,pending=NULL WHERE id=1")
    for record in conn.execute("SELECT key,value FROM parameters WHERE key LIKE 'buyer_auth_result_%' OR key='buyer_connection_result'").fetchall():
        conn.execute("INSERT OR REPLACE INTO parameters(key,value) VALUES (?,?)", ("seller_" + record[0], record[1]))
    conn.execute("INSERT INTO parameters(key,value) VALUES (?,?)", (MARKER, row[0]))
    return row[0]
