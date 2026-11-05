"""Opt-in, bounded offer evidence. Reads SQLite only; never contacts Vinted."""

import json
import logging
import os
from contextlib import closing

from search_settings import connection

logger = logging.getLogger(__name__)
JOB_FIELDS = "id,item_id,title,status,reason,liked_at,created,checked_at,sent_at"
STATE_FIELDS = ("mode,history,rounding,since,priority_since,next_send,last_scan,"
                "last_sent,scan_page,scan_watermark,unknown_notifications,pause_reason")


def snapshot(title):
    """An owner-selected title match plus recent outcomes, without buyer IDs."""
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
        return None
    with closing(connection()) as conn:
        conn.execute("PRAGMA query_only=ON")
        account = conn.execute(
            "SELECT account_id FROM vinted_offer_settings WHERE id=1"
        ).fetchone()[0]
        state = dict(conn.execute(
            f"SELECT {STATE_FIELDS} FROM vinted_offer_settings WHERE id=1"
        ).fetchone())
        counts = [dict(row) for row in conn.execute(
            "SELECT status,reason,COUNT(*) AS count FROM vinted_offer_jobs "
            "WHERE account_id=? GROUP BY status,reason", (account,)
        )]
        matches = [dict(row) for row in conn.execute(
            f"SELECT {JOB_FIELDS} FROM vinted_offer_jobs WHERE account_id=? "
            "AND item_id IN (SELECT item_id FROM vinted_offer_jobs "
            "WHERE account_id=? AND instr(lower(title),lower(?))>0) "
            "ORDER BY liked_at DESC,id DESC LIMIT 200",
            (account, account, title.strip()),
        )]
        recent = [dict(row) for row in conn.execute(
            f"SELECT {JOB_FIELDS} FROM vinted_offer_jobs WHERE account_id=? "
            "ORDER BY liked_at DESC,id DESC LIMIT 60", (account,)
        )]
        items = [dict(row) for row in conn.execute(
            "SELECT item_id,MAX(title) AS title,status,reason,COUNT(*) AS count,"
            "MAX(liked_at) AS latest_like,MAX(sent_at) AS latest_sent "
            "FROM vinted_offer_jobs WHERE account_id=? "
            "GROUP BY item_id,status,reason ORDER BY latest_like DESC LIMIT 1000",
            (account,),
        )]
    for row in matches + recent + items:
        row["title"] = row["title"][:160]
    return {"state": state, "counts": counts, "matches": matches, "recent": recent,
            "items": items}


def log_on_start():
    title = os.environ.get("MSJ_OFFER_AUDIT_TITLE", "")
    if not title:
        return
    try:
        data = snapshot(title)
        if data is None:
            return
        logger.info("Vinted offer audit: %s", json.dumps({
            "state": data["state"], "counts": data["counts"],
            "matches": len(data["matches"]), "recent": len(data["recent"]),
            "items": len(data["items"]), "items_limit": 1000,
        }, separators=(",", ":")))
        for section in ("matches", "recent", "items"):
            for row in data[section]:
                logger.info("Vinted offer audit: %s", json.dumps(
                    {"section": section, **row}, separators=(",", ":")
                ))
    except Exception as exc:
        # Diagnostic failure must not pause or modify the operational worker.
        logger.warning("Vinted offer audit unavailable: %s", type(exc).__name__)
