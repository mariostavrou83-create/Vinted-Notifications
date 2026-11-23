"""Private owner controls and fixed, credential-free discovery status."""

import json
import math
import time
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

from search_settings import connection

CODES = {
    "disabled": "Paused",
    "waiting": "Waiting for check",
    "checked": "Checked",
    "partial": "Checking candidates",
    "listing_deferred": "Waiting to retry individual listing checks",
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


def seller_status(conn):
    row = conn.execute("SELECT value FROM parameters WHERE key='home_monitor_status_seller'").fetchone()
    try:
        raw = json.loads(row[0]) if row else {}
    except (ValueError, TypeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    code = raw.get("code", "waiting")
    value = {"role": "seller", "label": "MSJ Vinted",
             "state": CODES.get(code, "Check incomplete") if isinstance(code, str) else "Check incomplete"}
    for key in (
        "candidates", "checked", "fresh", "matched", "alerts", "listing_unavailable",
        "details_unverified", "global_blocked", "scope_miss", "budget_or_exclusions",
        "style_miss", "photo_checked", "photo_matches", "card_screened",
        "duplicates", "age_expired", "age_unverified",
    ):
        number = raw.get(key, 0)
        value[key] = number if type(number) is int and 0 <= number <= 1000000 else 0
    stamp = raw.get("checked_at")
    value["time"] = (
        datetime.fromtimestamp(stamp, ZoneInfo("Europe/London")).strftime("%d %b %H:%M:%S %Z")
        if type(stamp) in (float, int) and math.isfinite(stamp) and 0 < stamp < 4102444800
        else "Not checked"
    )
    return value


def summary():
    with closing(connection()) as conn:
        account = seller_status(conn)
        enabled = conn.execute("SELECT value FROM parameters WHERE key='home_monitor_enabled'").fetchone()
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
        "enabled": bool(enabled and enabled[0] == "1"),
        "accounts": [account],
    }
