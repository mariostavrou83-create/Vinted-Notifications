"""Private owner controls and fixed, credential-free discovery status."""

import json
import math
import time
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

import alert_controls
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

TARGET_KEY = "home_poll_seconds"


def parse_target(value):
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 2:
        seconds = int(value)
        if 0 <= seconds <= 60:
            return seconds
    raise ValueError("Choose adaptive Home refresh (0) or an interval from 1 to 60 seconds.")


def target_seconds(conn=None):
    if conn is None:
        with closing(connection()) as opened:
            return target_seconds(opened)
    row = conn.execute("SELECT value FROM parameters WHERE key=?", (TARGET_KEY,)).fetchone()
    try:
        return parse_target(row[0]) if row else 0
    except ValueError:
        return 0


def save_target(value):
    seconds = parse_target(value)
    with closing(connection()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO parameters(key,value) VALUES(?,?)", (TARGET_KEY, str(seconds)))


def configure(value):
    if value not in ("0", "1"):
        raise ValueError("Choose whether Home discovery is running or paused.")
    alert_controls.set_paused("home", value == "0")


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
    target = target_seconds(conn)
    value["target_seconds"] = target
    for key in ("scheduled_seconds", "measured_seconds"):
        number = raw.get(key)
        value[key] = (
            round(number, 1)
            if raw.get("target_seconds") == target and type(number) in (float, int)
            and math.isfinite(number) and 0 < number <= 300
            else None
        )
    last_home = raw.get("last_home_at")
    value["last_home"] = (
        datetime.fromtimestamp(last_home, ZoneInfo("Europe/London")).strftime("%d %b %H:%M:%S %Z")
        if type(last_home) in (float, int) and math.isfinite(last_home) and 0 < last_home < 4102444800
        else None
    )
    for key in (
        "candidates", "checked", "fresh", "matched", "alerts", "listing_unavailable",
        "details_unverified", "global_blocked", "scope_miss", "size_miss", "budget_or_exclusions",
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
