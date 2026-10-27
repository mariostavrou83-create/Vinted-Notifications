"""Local, bounded eBay counters; no network reads or account secrets."""

import json
from collections import Counter
from contextlib import closing

from search_settings import connection

PREFIX = "ebay_flow:"


def refresh_native_allowance():
    """One Analytics read, separate from the Browse allowance; no search or send."""
    import ebay_quota
    import ebay_store
    from ebay_monitor import BrowseClient, grouped_searches, logger

    config = ebay_store.configuration()
    if config.get("source") != "browse" or not config.get("client_id") or not config.get("client_secret"):
        return
    try:
        client = BrowseClient(config)
        client.authenticate()
        ebay_quota.check_allowance(client, config)
        result = ebay_quota.allowance_summary(config, len(grouped_searches(ebay_store.active_searches())))
        logger.info("eBay native allowance summary %s", json.dumps(result, separators=(",", ":")))
    except Exception as exc:
        logger.warning("eBay native allowance unavailable: %s", type(exc).__name__)


def record_flow(conn, query_id, counts, now):
    # Called in the snapshot's existing short transaction, once per search
    # response, rather than writing a diagnostic row for every returned item.
    key = f"{PREFIX}{int(now // 3600)}:{query_id}"
    old = conn.execute("SELECT value FROM parameters WHERE key=?", (key,)).fetchone()
    saved = Counter(json.loads(old[0]) if old else {})
    saved.update(counts)
    saved["checks"] += 1
    conn.execute(
        "INSERT OR REPLACE INTO parameters(key,value) VALUES(?,?)",
        (key, json.dumps(saved, separators=(",", ":"))),
    )


def runtime_summary(config, groups, now):
    searches = {s["id"]: s for group in groups for s in group}
    day = int(now // 86400) * 86400
    flows = {}
    with closing(connection()) as conn, conn:
        calls = conn.execute(
            "SELECT COALESCE(SUM(calls),0),COALESCE(SUM(CASE WHEN minute>=? THEN calls ELSE 0 END),0) FROM ebay_call_buckets WHERE minute>=?",
            (day // 60, int((now - 86400) // 60)),
        ).fetchone()
        outbox = [dict(r) for r in conn.execute(
            "SELECT query_id,status,COUNT(*) count FROM alert_outbox WHERE platform='ebay' AND found_at>=? GROUP BY query_id,status",
            (now - 86400,),
        )]
        # Sent in the last day includes older queued alerts sent today.
        sent = conn.execute(
            "SELECT COUNT(*) FROM alert_outbox WHERE platform='ebay' AND status='sent' AND sent_at>=?",
            (now - 86400,),
        ).fetchone()[0]
        pending = [dict(r) for r in conn.execute(
            "SELECT status,reason,COUNT(*) count FROM keyword_candidates WHERE platform='ebay' AND found_at>=? GROUP BY status,reason",
            (now - 86400,),
        )]
        for row in conn.execute("SELECT key,value FROM parameters WHERE key GLOB 'ebay_flow:*'").fetchall():
            _, hour, query_id = row["key"].split(":")
            if int(hour) < int(now // 3600) - 47:
                conn.execute("DELETE FROM parameters WHERE key=?", (row["key"],))
            elif int(hour) >= int(now // 3600) - 23:
                flows.setdefault(query_id, Counter()).update(json.loads(row["value"]))
        health = []
        for identity, search in searches.items():
            state = search.get("ebay_health", {})
            filters = search["ebay"]
            health.append({
                "id": identity,
                "name": search["query_name"],
                "last_success_age": None if not state.get("last_success") else round(now - state["last_success"], 1),
                "actual_interval": state.get("actual_interval"),
                "failures": state.get("failures", 0),
                "error": state.get("error", ""),
                "warning": state.get("warning", ""),
                "keywords": filters.get("keywords"),
                "category": filters.get("category"),
                "aspects": filters.get("aspects"),
                "max_price": filters.get("max_price"),
                "condition_ids": filters.get("condition_ids"),
                "buying": filters.get("buying"),
                "item_location_country": "GB",
                "uk_only": filters.get("uk_only"),
                "seen_24h": conn.execute("SELECT COUNT(*) FROM ebay_seen WHERE query_id=? AND first_seen>=?", (identity, now - 86400)).fetchone()[0],
            })
    return {
        "checked_at": now,
        "source": config.get("source", "browse"),
        "configured_daily_limit": config["daily_budget"],
        "local_reserved_calls_rolling_24h": calls[0],
        "local_reserved_calls_today_utc": calls[1],
        "counter_basis": "Local reserved Browse reads, including failed attempts; not eBay Analytics. Minute buckets.",
        "active": len(searches),
        "request_groups": len(groups),
        "sent_24h": sent,
        "outbox_24h": outbox,
        "keyword_candidates_24h": pending,
        "filter_counts": flows,
        "filter_window": "Complete/current hourly buckets, up to 24 hours; starts at deployment.",
        "searches": health,
    }

