"""Opt-in Browse shadow plan: shared discovery with unchanged per-search rules.

Not loaded by the live poller. Planning has no external effects; the optional
shadow read uses the existing approved API client and caller's quota reservation.
No search settings, database state, baselines or deliveries are written here.
Only new shared searches with a category can pool their keyword discovery.
Every other remotely applied filter is kept identical. Legacy queries stay exact.
"""

import copy
import json
import math
from dataclasses import dataclass


@dataclass
class DiscoveryPool:
    config: dict
    members: list
    params: dict
    key: str


def plan(searches):
    from ebay_monitor import search_params

    pools = {}
    for original in searches:
        search = copy.deepcopy(original)
        config = copy.deepcopy(search["ebay"])
        eligible = config.get("shared_alert_version") == 1 and bool(
            config.get("category")
        )
        if eligible:
            config["keywords"] = ""
            config["shared_keywords"] = []
        params = search_params(config)
        # Keep legacy discovery isolated even when its parameters coincide.
        key = json.dumps({"shared_pool": eligible, "request": params}, sort_keys=True)
        if key not in pools:
            pools[key] = DiscoveryPool(config, [], params, key)
        pools[key].members.append(search)
    return list(pools.values())


def capacity(pool_count, *, interval_seconds=30, approved_daily=5000, reserve=0.1):
    if type(pool_count) is not int or pool_count < 0:
        raise ValueError("Pool count must be a nonnegative integer.")
    if interval_seconds <= 0 or approved_daily <= 0 or not 0 <= reserve < 1:
        raise ValueError("Use a positive interval and allowance and a valid reserve.")
    calls = pool_count * 86400 / interval_seconds
    usable = math.floor(approved_daily * (1 - reserve))
    return {
        "pools": pool_count,
        "target_interval_seconds": interval_seconds,
        "search_calls_per_day": math.ceil(calls),
        "required_approved_daily": math.ceil(calls / (1 - reserve)),
        "approved_daily_assumption": approved_daily,
        "reserved_fraction": reserve,
        "fits_allowance": calls <= usable,
        "minimum_interval_seconds": (
            math.ceil(pool_count * 86400 / usable) if pool_count and usable else 0
        ),
        "assumptions": "One first page per pool/check. Extra pages, details, diagnostics and retries consume additional calls; remote indexing delay is separate.",
    }


def coverage(items, previous_ids, *, page_size=200, seeded=False):
    """Flag a full page without overlap; do not hide saturation behind savings."""
    if type(page_size) is not int or page_size <= 0 or len(items) > page_size:
        return {"complete": False, "reason": "Invalid or oversized result page."}
    if any(not isinstance(item, dict) for item in items):
        return {"complete": False, "reason": "Malformed result record."}
    ids = {item.get("itemId") for item in items if item.get("itemId")}
    if len(ids) != len(items):
        return {"complete": False, "reason": "Missing or duplicate item identifiers."}
    if not seeded:
        return {
            "complete": True,
            "quiet_baseline": True,
            "reason": "Initial catalogue is baseline only.",
        }
    if len(items) < page_size or ids.intersection(previous_ids):
        return {
            "complete": True,
            "quiet_baseline": False,
            "reason": "Observed page has room or overlaps the previous page.",
        }
    return {
        "complete": False,
        "quiet_baseline": False,
        "reason": "Full page has no overlap. Fetch additional pages within approved quota or split the pool before claiming coverage.",
    }


def shadow_check(pool, client, reserve, *, previous_ids=(), seeded=False, now=None):
    """One approved-quota Browse read, local matching, no database/delivery writes.

    The caller supplies its existing quota reservation function. Description
    candidates remain unverified here; do not treat their titles as a mismatch.
    This is a reviewable shadow adapter, not a replacement for the live poller.
    """
    import time

    from ebay_monitor import BrowseClient, parse_item
    from search_settings import excluded_by
    from shared_matching import keyword_evidence

    if not isinstance(client, BrowseClient):
        raise TypeError("Shared discovery requires a Browse API client.")
    expected = plan(pool.members)
    if (
        len(expected) != 1
        or expected[0].key != pool.key
        or expected[0].params != pool.params
    ):
        raise ValueError("Pool members and remote filters differ.")
    from ebay_monitor import search_params

    if search_params(pool.config) != pool.params:
        raise ValueError("Pool request and remote filters differ.")
    if not reserve():
        return {"status": "quota_deferred", "search_calls": 0, "results": {}}
    items, warning = client.search(pool.config)
    check = coverage(items, previous_ids, seeded=seeded)
    if not check["complete"]:
        return {
            "status": "coverage_gap",
            "search_calls": 1,
            "coverage": check,
            "warning": warning,
            "results": {},
        }
    if check.get("quiet_baseline"):
        return {
            "status": "quiet_baseline",
            "search_calls": 1,
            "coverage": check,
            "results": {},
            "item_ids": [raw["itemId"] for raw in items],
        }
    now = time.time() if now is None else now
    results = {}
    for search in pool.members:
        result = {"matches": [], "needs_description": [], "rejected": []}
        for raw in items:
            item = parse_item(raw, search["ebay"], now)
            if item is None or excluded_by(item["title"], search.get("exclusions", [])):
                result["rejected"].append(raw["itemId"])
                continue
            evidence = keyword_evidence(
                search,
                item["title"],
                item.get("keyword_description", ""),
                description_complete=item.get("description_complete", False),
            )
            if evidence["state"] == "unverified":
                result["needs_description"].append(raw["itemId"])
            elif evidence["state"] in {"matched", "unrestricted"}:
                result["matches"].append(raw["itemId"])
            else:
                result["rejected"].append(raw["itemId"])
        results[str(search["id"])] = result
    return {
        "status": "shadow_matches",
        "search_calls": 1,
        "coverage": check,
        "warning": warning,
        "results": results,
        "item_ids": [raw["itemId"] for raw in items],
    }
