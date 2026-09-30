"""Owner-requested eBay quota snapshots; never a quota grant or a live counter."""

import hashlib
import json
import math
import time
from contextlib import closing
from datetime import datetime, timezone

import requests

from search_settings import connection

OBSERVATION_KEY = "ebay_quota_observation"
# Analytics identifies resources by API or method name. Never treat the separate
# getItems/bulk bucket, another API version, or an unknown name as search quota.
SEARCH_RESOURCES = {
    "buy.browse",
    "browse",
    "search",
    "item_summary",
    "buy.browse.item_summary",
    "buy.browse.item_summary.search",
}
BULK_RESOURCES = {"buy.browse.item.bulk", "getitems"}


def identity(config):
    return hashlib.sha256(
        json.dumps([config["client_id"], config["client_secret"]]).encode()
    ).hexdigest()


def utc_label(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime(
        "%d %b %Y, %H:%M:%S UTC"
    )


def nonnegative_integer(value):
    return value if type(value) is int and 0 <= value <= 2**31 - 1 else None


def parse_limits(payload):
    if not isinstance(payload, dict) or payload.get("errors"):
        raise ValueError
    limits = payload.get("rateLimits")
    if not isinstance(limits, list):
        raise TypeError
    rows, incomplete = [], False
    for api in limits:
        if not isinstance(api, dict):
            raise TypeError
        if (
            str(api.get("apiContext", "")).lower() != "buy"
            or str(api.get("apiName", "")).lower() != "browse"
            or api.get("apiVersion") != "v1"
        ):
            continue
        resources = api.get("resources")
        if not isinstance(resources, list):
            raise TypeError
        for resource in resources:
            if not isinstance(resource, dict):
                raise TypeError
            name = str(resource.get("name", "")).lower()
            if name in BULK_RESOURCES:
                continue
            if name not in SEARCH_RESOURCES:
                incomplete = True
                continue
            rates = resource.get("rates")
            if not isinstance(rates, list) or not rates:
                raise ValueError
            for rate in rates:
                if not isinstance(rate, dict):
                    raise TypeError
                limit = nonnegative_integer(rate.get("limit"))
                window = nonnegative_integer(rate.get("timeWindow"))
                if limit is None or not window:
                    raise ValueError
                remaining = nonnegative_integer(rate.get("remaining"))
                if remaining is not None and remaining > limit:
                    raise ValueError
                reset = None
                if isinstance(rate.get("reset"), str):
                    try:
                        date = datetime.fromisoformat(
                            rate["reset"].replace("Z", "+00:00")
                        )
                        if date.tzinfo is not None:
                            reset = date.timestamp()
                    except (ValueError, OverflowError):
                        pass
                rows.append(
                    {
                        "limit": limit,
                        "window": window,
                        "remaining": remaining,
                        "reset": reset,
                    }
                )
    return rows, incomplete


def check_allowance(client, config):
    """Use the application token already authenticated by the Browse check."""
    observation = {"identity": identity(config), "checked_at": time.time(), "rows": []}
    message = "eBay allowance could not be read. Try Check eBay access again later."
    try:
        response = client.session.get(
            "https://api.ebay.com/developer/analytics/v1_beta/rate_limit/",
            params={"api_context": "buy", "api_name": "browse"},
            headers={
                "Authorization": "Bearer " + client.token,
                "Accept": "application/json",
            },
            timeout=(3, 7),
        )
        if response.status_code == 200:
            rows, incomplete = parse_limits(response.json())
            observation.update(rows=rows, incomplete=incomplete)
            message = (
                "eBay-reported search allowance saved; see the capacity check below."
                if rows
                else "eBay returned no identifiable Browse search allowance. Capacity remains unverified."
            )
        elif response.status_code == 204:
            message = "eBay returned no allowance data. Capacity remains unverified."
        elif response.status_code in (401, 403):
            message = "eBay search works, but allowance access was denied. Capacity remains unverified."
        elif response.status_code == 429:
            message = "eBay limited the allowance check. Wait before checking again; capacity remains unverified."
    except (requests.RequestException, ValueError, TypeError):
        pass  # Never persist response bodies, tokens, or network exception text.
    observation["message"] = message
    with closing(connection()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO parameters(key,value) VALUES (?,?)",
            (OBSERVATION_KEY, json.dumps(observation)),
        )
    return message


def allowance_summary(config, groups, now=None):
    now = time.time() if now is None else now
    result = {"checked": False}
    with closing(connection()) as conn:
        row = conn.execute(
            "SELECT value FROM parameters WHERE key=?", (OBSERVATION_KEY,)
        ).fetchone()
    if not row:
        return result
    try:
        observation = json.loads(row[0])
        if observation["identity"] != identity(config):
            return result
        checked_at = observation["checked_at"]
        rows = observation["rows"]
        stale = now - checked_at >= 86400 or checked_at > now
        daily = [r["limit"] for r in rows if r["window"] == 86400]
        complete = bool(daily) and not observation.get("incomplete")
        capacity = min(
            (
                math.floor(r["limit"] * 0.9 * config["target_interval"] / r["window"])
                for r in rows
            ),
            default=0,
        )
        for rate in rows:
            rate["reset_label"] = (
                utc_label(rate["reset"]) if rate["reset"] is not None else None
            )
            rate["window_label"] = (
                "24 hours" if rate["window"] == 86400 else f'{rate["window"]} seconds'
            )
        return {
            "checked": True,
            "checked_at": utc_label(checked_at),
            "message": observation["message"],
            "rows": rows,
            "stale": stale,
            "complete": complete,
            "capacity": capacity,
            "meets_target": bool(groups)
            and complete
            and not stale
            and capacity >= groups,
            "exceeds_reported": bool(daily) and config["daily_budget"] > min(daily),
        }
    except (ValueError, TypeError, KeyError, OverflowError):
        return result
