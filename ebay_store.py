"""Shared-search settings and private eBay connection configuration."""

import json
import math
import os
import re
import time
from contextlib import closing
from urllib.parse import urlencode

import db
from search_settings import connection

ALERT_TARGET_SECONDS = 15
DEFAULT_POLL_SECONDS = 5
MAX_DAILY_BUDGET = 10_000_000

DEFAULTS = {
    "keywords": "",
    "min_price": None,
    "max_price": None,
    "category": "",
    "condition": "any",
    "buying": "fixed",
    "uk_only": True,
    "include_shipping": False,
}
CONFIG_KEYS = (
    "source",
    "client_id",
    "client_secret",
    "telegram_token",
    "chat_id",
    "daily_budget",
    "target_interval",
)


def platform_details(query_id):
    with closing(connection()) as conn:
        row = conn.execute(
            "SELECT * FROM search_platforms WHERE query_id=?", (query_id,)
        ).fetchone()
        health = conn.execute(
            "SELECT * FROM ebay_state WHERE query_id=?", (query_id,)
        ).fetchone()
    result = (
        dict(row)
        if row
        else {"vinted_enabled": 1, "ebay_enabled": 0, "ebay_generation": 0}
    )
    result["ebay"] = dict(DEFAULTS, **json.loads(result.pop("ebay_config", "{}")))
    result["ebay_health"] = dict(health) if health else {}
    result["platform_mode"] = (
        "both"
        if result["vinted_enabled"] and result["ebay_enabled"]
        else "ebay" if result["ebay_enabled"] else "vinted"
    )
    result["ebay_url"] = search_url(result["ebay"])
    return result


def search_url(config):
    params = {"_nkw": config["keywords"], "_sop": "10"}
    if config["buying"] == "fixed":
        params["LH_BIN"] = "1"
    elif config["buying"] == "auction":
        params["LH_Auction"] = "1"
    if config["category"]:
        params["_sacat"] = config["category"]
    for field, key in [("min_price", "_udlo"), ("max_price", "_udhi")]:
        if config[field] is not None:
            params[key] = f"{config[field] / 100:.2f}"
    if config["uk_only"]:
        params["LH_PrefLoc"] = "1"
    return "https://www.ebay.co.uk/sch/i.html?" + urlencode(params)


def parse_form(form, previous=None):
    """Legacy forms retain platform settings; a new eBay-only search needs no Vinted URL."""
    previous = previous or {
        "vinted_enabled": 1,
        "ebay_enabled": 0,
        "ebay": dict(DEFAULTS),
    }
    if "platform_mode" not in form:
        return previous["vinted_enabled"], previous["ebay_enabled"], previous["ebay"]
    mode = form.get("platform_mode")
    if mode not in ("vinted", "ebay", "both"):
        raise ValueError("Choose Vinted only, eBay only, or both.")
    from dashboard_store import parse_money

    config = dict(DEFAULTS)
    config["keywords"] = form.get("ebay_keywords", "").strip()
    config["category"] = form.get("ebay_category", "").strip()
    config["buying"] = form.get("ebay_buying", "fixed")
    config["condition"] = form.get("ebay_condition", "any")
    config["uk_only"] = form.get("ebay_uk_only") == "yes"
    config["include_shipping"] = form.get("ebay_include_shipping") == "yes"
    for key in ("min_price", "max_price"):
        config[key] = parse_money(form.get("ebay_" + key, ""))
    if len(config["keywords"]) > 100 or (mode != "vinted" and not config["keywords"]):
        raise ValueError("Add eBay keywords (up to 100 characters).")
    if config["category"] and not re.fullmatch(r"[0-9]{1,10}", config["category"]):
        raise ValueError("Use a numeric eBay category ID, or leave it blank.")
    if config["buying"] not in ("fixed", "auction", "both") or config[
        "condition"
    ] not in ("any", "new", "used"):
        raise ValueError("Choose a valid eBay listing type and condition.")
    if (
        config["min_price"] is not None
        and config["max_price"] is not None
        and config["min_price"] > config["max_price"]
    ):
        raise ValueError("The eBay price range must start with the lower price.")
    return int(mode != "ebay"), int(mode != "vinted"), config


def save_platforms(conn, query_id, vinted, ebay, config):
    old = conn.execute(
        "SELECT * FROM search_platforms WHERE query_id=?", (query_id,)
    ).fetchone()
    old_vinted, old_ebay = (
        (old["vinted_enabled"], old["ebay_enabled"]) if old else (1, 0)
    )
    encoded = json.dumps(config, sort_keys=True)
    changed = not old or encoded != old["ebay_config"] or ebay != old_ebay
    conn.execute(
        """INSERT INTO search_platforms(query_id,vinted_enabled,ebay_enabled,ebay_config,ebay_generation)
        VALUES (?,?,?,?,?) ON CONFLICT(query_id) DO UPDATE SET
        vinted_enabled=excluded.vinted_enabled,ebay_enabled=excluded.ebay_enabled,
        ebay_config=excluded.ebay_config,ebay_generation=search_platforms.ebay_generation+?""",
        (query_id, vinted, ebay, encoded, int(changed), int(changed)),
    )
    if vinted and not old_vinted:
        conn.execute(
            "UPDATE search_dashboard SET rebaseline=1 WHERE query_id=?", (query_id,)
        )
    if changed:
        conn.execute("DELETE FROM ebay_state WHERE query_id=?", (query_id,))
    if changed or not ebay:
        conn.execute(
            "UPDATE alert_outbox SET status='cancelled',error='Search changed or disabled' WHERE query_id=? AND platform='ebay' AND status='pending'",
            (query_id,),
        )
    if not vinted:
        conn.execute(
            "UPDATE alert_outbox SET status='cancelled',error='Vinted disabled for search' WHERE query_id=? AND platform='vinted' AND status='pending'",
            (query_id,),
        )


def active_searches():
    with closing(connection()) as conn:
        ids = [
            r[0]
            for r in conn.execute(
                """SELECT q.id FROM queries q
            JOIN search_platforms s ON s.query_id=q.id
            LEFT JOIN search_dashboard d ON d.query_id=q.id
            WHERE s.ebay_enabled=1 AND COALESCE(d.paused,0)=0 AND COALESCE(d.archived,0)=0"""
            )
        ]
    from search_settings import get_search

    return [search for i in ids if (search := get_search(i)) is not None]


def configuration():
    with closing(connection()) as conn:
        values = dict(
            conn.execute("SELECT key,value FROM parameters WHERE key LIKE 'ebay_%'")
        )
    result = {
        k: os.environ.get("EBAY_" + k.upper(), values.get("ebay_" + k, ""))
        for k in CONFIG_KEYS
    }
    result["source"] = (
        result["source"] if result["source"] in ("browse", "public") else "browse"
    )
    try:
        result["daily_budget"] = max(
            100, min(MAX_DAILY_BUDGET, int(result["daily_budget"] or 5000))
        )
    except ValueError:
        result["daily_budget"] = 5000
    try:
        result["target_interval"] = max(
            1, min(3600, int(result["target_interval"] or DEFAULT_POLL_SECONDS))
        )
    except ValueError:
        result["target_interval"] = DEFAULT_POLL_SECONDS
    # Both bots can alert the same owner, but they must have distinct identities.
    result["chat_id"] = result["chat_id"] or db.get_parameter("telegram_chat_id") or ""
    return result


def missing_configuration(config=None):
    config = config or configuration()
    labels = {
        "client_id": "eBay App ID",
        "client_secret": "eBay Cert ID",
        "telegram_token": "new Telegram bot token",
        "chat_id": "Telegram chat ID",
    }
    if config.get("source") == "public":
        labels.pop("client_id")
        labels.pop("client_secret")
    missing = [label for key, label in labels.items() if not config[key]]
    current = db.get_parameter("telegram_token") or ""
    if (
        config["telegram_token"]
        and current
        and config["telegram_token"].split(":")[0] == current.split(":")[0]
    ):
        missing.append("a separate bot (the Vinted bot cannot be reused)")
    return missing


def save_configuration(form):
    values = {}
    for key in CONFIG_KEYS:
        value = form.get(key, "").strip()
        if not value:
            continue  # Blank password fields mean keep the saved value.
        if len(value) > 1000 or any(ord(c) < 32 for c in value):
            raise ValueError("That connection value is invalid.")
        if key == "source" and value not in ("browse", "public"):
            raise ValueError("Choose public search or Browse API.")
        if key == "daily_budget" and (
            not value.isdigit() or not 100 <= int(value) <= MAX_DAILY_BUDGET
        ):
            raise ValueError(
                "Use your approved eBay daily allowance, between 100 and 10,000,000."
            )
        if key == "target_interval" and (
            not value.isdigit() or not 1 <= int(value) <= 3600
        ):
            raise ValueError("Choose a target interval between 1 and 3,600 seconds.")
        if key == "telegram_token":
            if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", value):
                raise ValueError(
                    "Paste the token supplied by BotFather for the new eBay bot."
                )
            old = db.get_parameter("telegram_token") or ""
            if old and old.split(":")[0] == value.split(":")[0]:
                raise ValueError(
                    "Use the NEW eBay bot token. Your Vinted bot stays separate."
                )
        if key == "chat_id" and not re.fullmatch(r"-?\d+", value):
            raise ValueError("The Telegram chat ID must be a number.")
        values["ebay_" + key] = value
    with closing(connection()) as conn, conn:
        old_source = conn.execute(
            "SELECT value FROM parameters WHERE key='ebay_source'"
        ).fetchone()
        if "ebay_source" in values and values["ebay_source"] != (
            old_source[0] if old_source else "browse"
        ):
            conn.execute(
                "UPDATE search_platforms SET ebay_generation=ebay_generation+1"
            )
            conn.execute("DELETE FROM ebay_state")
            conn.execute(
                "UPDATE alert_outbox SET status='cancelled',error='eBay source changed' WHERE platform='ebay' AND status='pending'"
            )
        conn.executemany(
            "INSERT OR REPLACE INTO parameters(key,value) VALUES (?,?)", values.items()
        )


def connection_summary():
    config = configuration()
    from ebay_monitor import grouped_searches
    from ebay_quota import allowance_summary

    searches = active_searches()
    active = len(searches)
    groups = len(grouped_searches(searches))
    with closing(connection()) as conn:
        used = conn.execute(
            "SELECT COALESCE(SUM(calls),0) FROM ebay_call_buckets WHERE minute>=?",
            (int((time.time() - 86400) / 60),),
        ).fetchone()[0]
        public_paused = conn.execute(
            "SELECT value FROM delivery_runtime WHERE key='ebay_public_paused'"
        ).fetchone()
    capacity_interval = call_spacing(config) * max(1, groups)
    target = config["target_interval"]
    return {
        "source": config["source"],
        "public_paused": bool(public_paused and public_paused[0]),
        "alert_target": ALERT_TARGET_SECONDS,
        "timing": delivery_timing(),
        "target_interval": target,
        "required_budget": math.ceil(groups * 86400 / target / 0.9),
        "meets_target": capacity_interval <= target,
        "has_delivery_headroom": max(target, capacity_interval) < ALERT_TARGET_SECONDS,
        "missing": missing_configuration(config),
        "configured": {k: bool(config[k]) for k in CONFIG_KEYS},
        "daily_budget": config["daily_budget"],
        "allowance": allowance_summary(config, groups),
        "calls_used": used,
        "active": active,
        "groups": groups,
        "interval": math.ceil(max(target, capacity_interval)),
    }


def delivery_timing(now=None):
    """Measured server acceptance, never a claim about the owner's phone receipt."""
    now = time.time() if now is None else now
    with closing(connection()) as conn:
        rows = conn.execute(
            """SELECT t.*,a.found_at,a.sent_at,a.status
            FROM ebay_alert_timing t JOIN alert_outbox a ON a.item_id=t.item_id
            WHERE a.found_at>=? ORDER BY a.found_at DESC LIMIT 1000""",
            (now - 86400,),
        ).fetchall()
    totals, discovery, dispatch = [], [], []
    pending_late = invalid = fallback = failed = public = 0
    for row in rows:
        if row["status"] == "cancelled":
            continue
        failed += row["status"] == "failed"
        listed, seen, sent = row["listed_at"], row["response_received"], row["sent_at"]
        if row["date_source"] == "publicSearchMinute":
            public += 1
            if sent is not None and sent >= seen and row["status"] == "sent":
                dispatch.append(sent - seen)
            continue  # Minute timestamps and an unverified display timezone cannot prove a 15s deadline.
        fallback += row["date_source"] != "itemOriginDate"
        if listed > seen or (sent is not None and sent < seen):
            invalid += 1
            continue  # Clock/timestamp anomalies must not look artificially fast.
        if row["status"] == "pending" and now - listed > ALERT_TARGET_SECONDS:
            pending_late += 1
        if row["status"] == "sent" and sent is not None:
            totals.append(sent - listed)
            discovery.append(seen - listed)
            dispatch.append(sent - seen)

    def p95(values):
        return (
            round(sorted(values)[math.ceil(len(values) * 0.95) - 1], 2)
            if values
            else None
        )

    return {
        "sample_size": len(rows),
        "public_records": public,
        "sent": len(totals),
        "within_target": sum(t <= ALERT_TARGET_SECONDS for t in totals),
        "late": sum(t > ALERT_TARGET_SECONDS for t in totals),
        "pending_late": pending_late,
        "failed": failed,
        "invalid_timestamps": invalid,
        "fallback_timestamps": fallback,
        "p95_total": p95(totals),
        "p95_discovery": p95(discovery),
        "p95_dispatch": p95(dispatch),
    }


def call_spacing(config):
    if config.get("source") == "public":
        return 0.1  # A local ceiling of 10 public requests/second, not an eBay quota grant.
    # Ten percent headroom; rolling 24h accounting also survives restarts.
    return max(0.02, 86400 / (config["daily_budget"] * 0.9))


def reserve_call(config, now):
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if config.get("source") == "public":
            paused = conn.execute(
                "SELECT value FROM delivery_runtime WHERE key='ebay_public_paused'"
            ).fetchone()
            if paused and paused[0]:
                return now + 3600
            latest = conn.execute(
                "SELECT value FROM delivery_runtime WHERE key='ebay_public_last_call'"
            ).fetchone()
            cooldown = conn.execute(
                "SELECT value FROM delivery_runtime WHERE key='ebay_api_cooldown'"
            ).fetchone()
            due = max(
                (latest[0] if latest else 0) + call_spacing(config),
                cooldown[0] if cooldown else 0,
            )
            if now < due:
                return due
            conn.execute(
                "INSERT OR REPLACE INTO delivery_runtime VALUES ('ebay_public_last_call',?)",
                (now,),
            )
            return None
        cutoff = int((now - 86400) // 60)
        conn.execute("DELETE FROM ebay_call_buckets WHERE minute<?", (cutoff,))
        count, oldest = conn.execute(
            "SELECT COALESCE(SUM(calls),0),MIN(minute) FROM ebay_call_buckets"
        ).fetchone()
        latest_row = conn.execute(
            "SELECT value FROM delivery_runtime WHERE key='ebay_last_call'"
        ).fetchone()
        latest = latest_row[0] if latest_row else 0
        cooldown = conn.execute(
            "SELECT value FROM delivery_runtime WHERE key='ebay_api_cooldown'"
        ).fetchone()
        wait_until = max(
            (latest or 0) + call_spacing(config), cooldown[0] if cooldown else 0
        )
        if count >= math.floor(config["daily_budget"] * 0.9):
            wait_until = max(wait_until, (oldest + 1) * 60 + 86400)
        if now < wait_until:
            return wait_until
        conn.execute(
            """INSERT INTO ebay_call_buckets VALUES (?,1)
            ON CONFLICT(minute) DO UPDATE SET calls=calls+1""",
            (int(now // 60),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO delivery_runtime VALUES ('ebay_last_call',?)",
            (now,),
        )
        return None
