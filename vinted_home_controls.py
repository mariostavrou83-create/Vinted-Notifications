"""Private owner controls and fixed, credential-free discovery status."""

import json
import math
import time
from contextlib import closing
from datetime import UTC, datetime

import db
from search_settings import connection

CODES = {
    "disabled": "Paused",
    "waiting": "Waiting for check",
    "checked": "Checked",
    "partial": "Checking candidates",
    "no_searches": "No eligible active searches",
    "account_unavailable": "Account unavailable",
    "not_separated": "Separate accounts required",
    "key_missing": "Existing session key missing",
    "busy": "Giving priority to account activity",
    "security_challenge": "Paused by Vinted challenge",
    "credentials": "Account check refused",
    "rate_limited": "Vinted cooldown",
    "network": "Connection unavailable",
    "source_unavailable": "Personalised feed not verified",
    "age_unverified": "Listing age unverified",
    "error": "Check incomplete",
}


def configure(value):
    if value not in ("0", "1"):
        raise ValueError("Choose whether Home discovery is running or paused.")
    with closing(connection()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO parameters(key,value) VALUES('home_monitor_enabled',?)",
            (value,),
        )


def summary():
    rows = []
    for role, label in (("seller", "MSJ Vinted"),):
        try:
            raw = json.loads(db.get_parameter("home_monitor_status_" + role) or "{}")
        except (ValueError, TypeError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        value = {
            "role": role,
            "label": label,
            "state": CODES.get(raw.get("code", "waiting"), "Check incomplete"),
        }
        for key in (
            "candidates", "checked", "fresh", "matched", "alerts", "listing_unavailable",
            "details_unverified", "global_blocked", "scope_miss", "budget_or_exclusions",
            "style_miss", "photo_checked", "photo_matches", "card_screened",
        ):
            number = raw.get(key, 0)
            value[key] = number if type(number) is int and 0 <= number <= 1000000 else 0
        stamp = raw.get("checked_at")
        value["time"] = (
            datetime.fromtimestamp(stamp, UTC).strftime("%d %b %H:%M:%S UTC")
            if type(stamp) in (float, int)
            and math.isfinite(stamp)
            and 0 < stamp < 4102444800
            else "Not checked"
        )
        rows.append(value)
    with closing(connection()) as conn:
        # Home cards have had a stable house prefix since the feature launched.
        # Restrict the scan to recent finds using the existing found_at index.
        from vinted_home_activity import HOME_ALERT
        totals = dict(conn.execute(
            "SELECT status,COUNT(*) FROM alert_outbox WHERE found_at>=? AND " + HOME_ALERT + " GROUP BY status",
            (time.time() - 86400,),
        ))
    return {
        "sent_24h": totals.get("sent", 0),
        "pending_24h": totals.get("pending", 0),
        "enabled": db.get_parameter("home_monitor_enabled") == "1",
        "accounts": rows,
    }
