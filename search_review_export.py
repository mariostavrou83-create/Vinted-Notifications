"""Opt-in, read-only search evidence for an owner's private operational review.

Only the explicit search/listing allowlist below is exported. No credentials,
sessions, payments, buyer records, message contents or account tables are read.
The dashboard's existing owner authentication protects this download.
"""

import hashlib
import json
from pathlib import Path
import sqlite3
import time
from contextlib import closing


CONFIG = {
    "queries": "id query query_name last_item",
    "search_preferences": "query_id reminder exclusions",
    "search_dashboard": "query_id paused archived revision rebaseline",
    "search_buying_guide": "query_id max_buy resale_low resale_high must_have folder_id",
    "search_folders": "id name",
    "vinted_search_budgets": "query_id max_total postage_estimate",
    "search_platforms": "query_id vinted_enabled ebay_enabled ebay_generation ebay_config",
    "vinted_keyword_variants": "id query_id position keyword url primed last_item max_item_id last_check last_attempt last_success actual_interval failures",
}
EVIDENCE = {
    "search_health": "query_id last_attempt last_success duration actual_interval failures",
    "ebay_state": "query_id generation baseline_at last_attempt last_success next_poll failures actual_interval source",
    "search_audit_runs": "id started_at applied_at manifest_sha summary",
    "search_audit_members": "run_id query_id cohort",
    "alert_outbox": "item_id query_id search_name url title price currency photo_url found_at status attempts sent_at user_status platform",
    "items": "item title price currency timestamp photo_url query_id",
    "search_match_log": "run_id item_id query_id revision cohort matched_at canonical rule_snapshot evidence",
    "keyword_candidates": "platform item_id query_id revision generation payload found_at retry_until status reason",
    "vinted_alert_details": "item_id payload",
    "ebay_alert_details": "item_id payload",
    "ebay_alert_timing": "item_id listed_at date_source request_started response_received",
}
JSON_FIELDS = {"ebay_config", "rule_snapshot", "evidence", "payload", "summary"}
# Recursive projection also removes seller/account metadata from listing payloads.
SAFE_KEYS = set((
    "id item item_id raw_id title url price currency photo_url brand size condition "
    "description keyword_description description_complete keyword_evidence state keyword field budget "
    "item_location_country shipping total max_total maximum postage_estimate "
    "query_id revision generation query_name query exclusions vinted_keywords shared_keywords "
    "vinted_max_total vinted_postage_estimate max_buy resale_low resale_high reminder must_have folder_id "
    "filter_mode search_url aspects condition_ids free_shipping keywords min_price max_price category "
    "buying uk_only include_shipping source shared_alert_version rules alert vinted_details ebay_details "
    "Brand Size Colour Material Style Neckline Pattern Sleeve Length Department Type "
    "shipping_cost shipping_known buyer_protection total_cost max_item_price"
).split())
SAFE_KEYS.update({"Sleeve Length", "UK Shoe Size", "US Shoe Size", "EU Shoe Size"})


def project(value):
    if isinstance(value, dict):
        return {k: project(v) for k, v in value.items() if k in SAFE_KEYS}
    if isinstance(value, list):
        return [project(v) for v in value[:100]]
    if isinstance(value, str):
        return value[:12000]
    return value


def read_table(conn, name, allowed, limit=50000):
    columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')}
    chosen = [c for c in allowed.split() if c in columns]
    if not chosen:
        return {"available": False}
    total = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    rows = []
    for row in conn.execute(f'SELECT {",".join(chosen)} FROM "{name}" LIMIT ?', (limit,)):
        result = dict(zip(chosen, row))
        for key in JSON_FIELDS & result.keys():
            try:
                result[key] = project(json.loads(result[key]))
            except (ValueError, TypeError):
                result[key] = {"unreadable": True}
        rows.append(result)
    return {"available": True, "count": total, "truncated": total > len(rows), "rows": rows}


def read_database(path, *, historical=False):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        conn.execute("PRAGMA query_only=ON")
        # A consistent snapshot without acquiring a write lock.
        conn.execute("BEGIN")
        selected = CONFIG if historical else dict(CONFIG, **EVIDENCE)
        data = {name: read_table(conn, name, columns) for name, columns in selected.items()}
        # Counts explain seen/filter suppression without copying millions of IDs.
        if not historical:
            data["suppression_counts"] = {}
            for table in ("search_filtered_items", "ebay_seen"):
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (table,)).fetchone():
                    data["suppression_counts"][table] = [list(row) for row in conn.execute(
                        f'SELECT query_id,COUNT(*) FROM "{table}" GROUP BY query_id')]
        return data


def collect(path):
    result = {"captured_at": time.time(), "live": read_database(path), "backups": []}
    directory = path.resolve().parent / "backups"
    backups = sorted(directory.glob("*.sqlite3"), key=lambda p: p.stat().st_mtime)
    result["backup_count"] = len(backups)
    # Earliest snapshots establish originals; latest snapshots cover recent edits.
    selected = backups if len(backups) <= 80 else backups[:40] + backups[-40:]
    result["backup_inventory_truncated"] = len(selected) != len(backups)
    hashes = {}
    for backup in selected:
        entry = {"name": backup.name, "modified_at": backup.stat().st_mtime}
        try:
            data = read_database(backup, historical=True)
            encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            entry["sha256"] = digest
            if digest not in hashes:
                entry["tables"] = data
                hashes[digest] = backup.name
            else:
                entry["same_as"] = hashes[digest]
        except (sqlite3.Error, OSError):
            entry["unavailable"] = True
        result["backups"].append(entry)
    return result

