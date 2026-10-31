"""One owner-authorized storage-incident recovery, selected by private config.

No general response-shape pause recovery, marketplace request or offer replay.
The exact historical pause window stays in service configuration, not source.
"""

import hashlib
import json
import math
import os
import time
from contextlib import closing

from search_settings import connection

VARIABLE = "MSJ_OFFERS_STORAGE_RECOVERY_WINDOW"


def run_once(*, window=None, now=None):
    now = time.time() if now is None else now
    if window is None:
        raw = os.environ.get(VARIABLE, "")
        if not raw:
            return "not_requested"
        if len(raw) > 128:
            return "invalid_window"
        try:
            window = json.loads(raw)
        except (ValueError, RecursionError):
            return "invalid_window"
    if (
        not isinstance(window, list) or len(window) != 2
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in window)
        or not 0 < window[0] < window[1] <= now
        or window[1] - window[0] > 60 or now - window[1] > 86400
    ):
        return "invalid_window"
    marker = "offer_storage_recovery_" + hashlib.sha256(
        json.dumps(window, separators=(",", ":")).encode("ascii")
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
            config and config["mode"] == "paused" and config["pause_reason"] == "shape"
            and control and control["action"] == "paused" and control["reason"] == "shape"
            and window[0] <= control["happened"] <= window[1]
            and active and active["id"] < control["id"]
            and account and account["session"] and account["user_id"] == config["account_id"]
            and account["verified_at"] and control["happened"] < account["verified_at"] <= now
        ):
            return "state_changed_or_unverified"
        # Match the ordinary recovery rule: an explicit earlier owner resume
        # can leave older quarantined jobs untouched, but any current or
        # unrecorded uncertainty and every in-flight send blocks this repair.
        uncertain = conn.execute(
            "SELECT 1 FROM vinted_offer_jobs j WHERE account_id=? AND (status='sending' "
            "OR (status='uncertain' AND COALESCE((SELECT MAX(e.id) FROM vinted_offer_events e "
            "WHERE e.job_id=j.id AND e.action='uncertain'),?)>=?)) LIMIT 1",
            (config["account_id"], active["id"], active["id"]),
        ).fetchone()
        if uncertain:
            return "uncertain_offer"
        conn.execute(
            "UPDATE vinted_offer_settings SET mode=?,pause_reason='',revision=revision+1,next_scan=0 WHERE id=1",
            (active["action"],),
        )
        conn.execute(
            "INSERT INTO vinted_offer_events(happened,action,reason) VALUES (?,'resumed','storage_incident')",
            (now,),
        )
        conn.execute("INSERT INTO parameters(key,value) VALUES (?,?)", (marker, "applied"))
    return "applied"
