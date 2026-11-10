"""Once-only recovery of an explicitly diagnosed listing-read pause."""

import hashlib
import json
import math
import os
import time
from contextlib import closing

from search_settings import connection


def recheck_price_reads_once(now=None):
    """After the owner-page repair, wake unattempted listing reads once.

    This clears only local read backoff. It neither enables offers nor changes
    send spacing, account cooldowns, recipients or any attempted send. Normal
    worker checks still verify the account, inventory and conversation afresh.
    """
    from vinted_offers import history_cutoff

    now = time.time() if now is None else now
    marker = "offer_owner_price_records_recheck_v1"
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM parameters WHERE key=?", (marker,)).fetchone():
            return 0
        config = conn.execute("SELECT * FROM vinted_offer_settings WHERE id=1").fetchone()
        account = conn.execute("SELECT user_id,session FROM vinted_buyer WHERE id=1").fetchone()
        if not (config and config["mode"] in ("live", "preview")
                and account and account["session"]
                and str(account["user_id"]) == config["account_id"]):
            return 0
        account_id = config["account_id"]
        if conn.execute("SELECT 1 FROM vinted_offer_jobs WHERE account_id=? "
                        "AND status IN ('sending','uncertain') LIMIT 1", (account_id,)).fetchone():
            return 0
        jobs = conn.execute(
            "SELECT id FROM vinted_offer_jobs WHERE account_id=? AND status='queued' "
            "AND reason='listing_check' AND lease_until>? AND liked_at>=? "
            "AND offer_id IS NULL AND sent_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM vinted_offer_events e "
            "WHERE e.job_id=vinted_offer_jobs.id AND e.action='sending')",
            (account_id, now, history_cutoff(dict(config), now)),
        ).fetchall()
        for job in jobs:
            conn.execute("UPDATE vinted_offer_jobs SET lease_until=0 WHERE id=?", (job["id"],))
            conn.execute("INSERT INTO vinted_offer_events(happened,action,reason,job_id) "
                         "VALUES (?,'listing_price_recheck','listing_check',?)", (now, job["id"]))
        conn.execute("INSERT INTO parameters(key,value) VALUES (?,?)", (marker, "applied"))
    return len(jobs)


def run_once(payload=None, now=None):
    now = time.time() if now is None else now
    if payload is None:
        raw = os.environ.get("MSJ_OFFERS_LISTING_RECOVERY", "")
        if not raw:
            return "not_requested"
        if len(raw) > 2048:
            return "invalid_request"
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError):
            return "invalid_request"
    if not isinstance(payload, dict) or set(payload) != {"account_id", "window", "jobs"}:
        return "invalid_request"
    account_id, window, jobs = payload["account_id"], payload["window"], payload["jobs"]
    if not (
        isinstance(account_id, str) and account_id.isascii() and account_id.isdigit()
        and 1 <= len(account_id) <= 24
        and isinstance(window, list) and len(window) == 2
        and all(type(n) in (int, float) and math.isfinite(n) for n in window)
        and 0 < window[0] < window[1] <= now and window[1] - window[0] <= 10
        and now - window[1] < 86400
        and isinstance(jobs, list) and 1 <= len(jobs) <= 100
        and all(type(n) is int and n > 0 for n in jobs) and len(set(jobs)) == len(jobs)
    ):
        return "invalid_request"
    marker = "offer_listing_recovery_" + hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("ascii")
    ).hexdigest()
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM parameters WHERE key=?", (marker,)).fetchone():
            return "already_applied"
        config = conn.execute("SELECT * FROM vinted_offer_settings WHERE id=1").fetchone()
        control = conn.execute(
            "SELECT * FROM vinted_offer_events WHERE action IN ('live','preview','paused') "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        active = conn.execute(
            "SELECT id,action FROM vinted_offer_events WHERE action IN ('live','preview') "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        account = conn.execute("SELECT user_id,session,verified_at FROM vinted_buyer WHERE id=1").fetchone()
        if not (
            config and config["account_id"] == account_id
            and config["mode"] == "paused" and config["pause_reason"] == "shape"
            and control and control["action"] == "paused" and control["reason"] == "shape"
            and window[0] <= control["happened"] <= window[1]
            and active and active["id"] < control["id"]
            and account and str(account["user_id"]) == account_id and account["session"]
            and account["verified_at"] and control["happened"] < account["verified_at"] <= now
            and now - account["verified_at"] < 300
        ):
            return "state_changed_or_unverified"
        if conn.execute("SELECT 1 FROM vinted_offer_jobs WHERE account_id=? "
                        "AND status IN ('sending','uncertain') LIMIT 1", (account_id,)).fetchone():
            return "uncertain_offer"
        queued = conn.execute("SELECT id,offer_id,sent_at FROM vinted_offer_jobs WHERE account_id=? "
                              "AND status='queued' AND reason='shape'", (account_id,)).fetchall()
        if {r["id"] for r in queued} != set(jobs) or any(r["offer_id"] or r["sent_at"] for r in queued):
            return "queue_changed"
        for job_id in jobs:
            if conn.execute("SELECT 1 FROM vinted_offer_events WHERE job_id=? AND action='sending'",
                            (job_id,)).fetchone():
                return "previous_send_attempt"
        conn.execute("UPDATE vinted_offer_settings SET mode=?,pause_reason='',revision=revision+1,next_scan=0 WHERE id=1",
                     (active["action"],))
        conn.execute("INSERT INTO vinted_offer_events(happened,action,reason) VALUES (?,'resumed','listing_read_repair')",
                     (now,))
        conn.execute("INSERT INTO parameters(key,value) VALUES (?,?)", (marker, "applied"))
    return "applied"
