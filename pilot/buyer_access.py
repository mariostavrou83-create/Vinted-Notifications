"""Access lifecycle for buyers on the hosted multiple-accounts service."""
import re

from flask import current_app

from .db import transaction
from .partner import PartnerError, partner_configured, partner_request


def support_email():
    address = current_app.config.get("FINDER_SUPPORT_EMAIL", "").strip()
    return address if re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", address) else ""


def paused_message():
    contact = support_email()
    if contact:
        return f"Your Finder access is paused. Email {contact} for more info."
    return "Your Finder access is paused. Contact your Finder administrator for more info."


def init(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS hosted_members (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT REFERENCES users(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
            paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1))
        );
        CREATE TRIGGER IF NOT EXISTS hosted_member_join AFTER INSERT ON users BEGIN
            INSERT OR IGNORE INTO hosted_members(user_id) VALUES(NEW.id);
        END;
        INSERT OR IGNORE INTO hosted_members(user_id) SELECT id FROM users;
        CREATE TABLE IF NOT EXISTS hosted_revocations (
            account_ref TEXT PRIMARY KEY, next_try REAL NOT NULL DEFAULT 0
        );
    """)


def active(db, uid):
    if not current_app.config.get("MULTIPLE_ACCOUNTS"):
        return True
    return db.execute("SELECT 1 FROM hosted_members WHERE user_id=? AND paused=0", (uid,)).fetchone() is not None


def set_paused(db, uid, paused):
    """Called in the admin transaction. Resume starts fresh with the saved filters and account."""
    changed = db.execute("UPDATE hosted_members SET paused=? WHERE user_id=? AND paused!=?", (paused, uid, paused))
    if not changed.rowcount:
        return
    db.execute("""UPDATE pilot_sources SET cursor=NULL,fingerprint=NULL,lease=NULL,next_at=0,status='waiting'
                  WHERE user_id=?""", (uid,))
    db.execute("DELETE FROM hosted_search_baseline WHERE search_id IN (SELECT id FROM searches WHERE user_id=?)", (uid,))
    if paused:
        db.execute("DELETE FROM pilot_link_attempt WHERE user_id=?", (uid,))
        db.execute("DELETE FROM pilot_link_codes WHERE user_id=?", (uid,))
        db.execute("UPDATE pilot_buy_jobs SET finished=1 WHERE user_id=? AND finished=0", (uid,))
        db.execute("UPDATE pilot_alerts SET delivery='expired' WHERE user_id=? AND delivery IN ('new','sending')", (uid,))
        db.execute("""UPDATE pilot_alerts SET order_status='blocked',check_token=NULL,confirmation=NULL
                      WHERE user_id=? AND order_status IN ('checking','confirm')""", (uid,))


def delete(db, uid):
    """Remove Finder data atomically. Only an opaque reference survives until its grant is revoked."""
    db.execute("""INSERT OR IGNORE INTO hosted_revocations(account_ref)
                  SELECT account_ref FROM pilot_buyer_link WHERE user_id=?""", (uid,))
    db.execute("DELETE FROM hosted_deliveries WHERE chat_id IN (SELECT chat_id FROM pilot_chats WHERE user_id=?)", (uid,))
    db.execute("DELETE FROM native_notifications WHERE alert_id IN (SELECT alert_id FROM native_orders WHERE user_id=?)", (uid,))
    # Earlier dashboard versions stored these records without cascading foreign keys.
    for table in ("native_orders", "native_devices", "native_pair_codes", "native_settings", "pilot_buy_jobs",
                  "pilot_sources", "pilot_link_codes", "pilot_chats", "pilot_alerts"):
        db.execute(f"DELETE FROM {table} WHERE user_id=?", (uid,))
    db.execute("DELETE FROM team_invites WHERE accepted_by=?", (uid,))
    db.execute("UPDATE team_owner SET user_id=NULL WHERE user_id=?", (uid,))
    db.execute("DELETE FROM users WHERE id=?", (uid,))


def revoke_next(db, now):
    if not partner_configured():
        return False
    if not db.execute("SELECT 1 FROM hosted_revocations WHERE next_try<=? LIMIT 1", (now,)).fetchone():
        return False
    with transaction(db):
        row = db.execute("SELECT account_ref FROM hosted_revocations WHERE next_try<=? ORDER BY next_try LIMIT 1", (now,)).fetchone()
        if not row:
            return False
        # A timeout leaves the local account deleted and the idempotent revoke pending.
        db.execute("UPDATE hosted_revocations SET next_try=? WHERE account_ref=?", (now + 60, row[0]))
    try:
        partner_request("POST", "/v1/connections/revoke", {"account_ref": row[0]})
    except PartnerError:
        return False
    db.execute("DELETE FROM hosted_revocations WHERE account_ref=? AND next_try=?", (row[0], now + 60))
    return True
