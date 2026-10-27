"""Private, compare-and-swap keyword/exclusion edits before pollers start.

No owner rules belong in this public repository. No external requests or alerts
are made here. Existing budgets, URLs, sizes, photos and live selection are
protected transactionally; only new keyword variants receive a quiet baseline.
"""

import base64
import hashlib
import json
import os
import sqlite3
import time
import zlib
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import db
import vinted_keywords
from ebay_search_link import shared_keyword_query
from search_review_export import CONFIG, project
from search_settings import excluded_by, parse_exclusions

RELEASE = "msj-search-review-20261027-v1"
MARKER = "search_review_release_20261027_v1"
RULE_TABLES = (
    "queries", "search_preferences", "search_dashboard",
    "search_buying_guide", "vinted_search_budgets", "search_platforms",
)


def _private_manifest():
    encoded = "".join(os.environ.get(f"MSJ_SEARCH_REVIEW_CONFIG_{i}", "") for i in range(4))
    if not encoded or len(encoded) > 100000:
        raise RuntimeError("Missing or oversized private review configuration")
    decoder = zlib.decompressobj()
    raw = decoder.decompress(base64.b64decode(encoded, validate=True), 500000)
    if not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
        raise RuntimeError("Invalid bounded private review configuration")
    if hashlib.sha256(raw).hexdigest() != os.environ.get("MSJ_SEARCH_REVIEW_SHA256"):
        raise RuntimeError("Private review checksum does not match")
    return raw


def _state(conn, query_id):
    result = {}
    for table in RULE_TABLES:
        key = "id" if table == "queries" else "query_id"
        columns = [c for c in CONFIG[table].split() if c != "last_item"]
        row = conn.execute(f"SELECT {','.join(columns)} FROM {table} WHERE {key}=?", (query_id,)).fetchone()
        value = dict(row) if row else None
        if value and "ebay_config" in value:
            value["ebay_config"] = project(json.loads(value["ebay_config"]))
        result[table] = value
    result["keywords"] = [r["keyword"] for r in vinted_keywords.rows(query_id, conn)]
    return result


def _protected(conn, targets):
    """Full stored values, including fields intentionally absent from exports."""
    result = {}
    for table in (*RULE_TABLES, "search_folders", "search_reference_photos"):
        values = [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
        if table == "search_preferences":
            present = {r["query_id"] for r in values}
            values += [{"query_id": qid, "reminder": "", "exclusions": "[]"} for qid in targets - present]
            values.sort(key=lambda r: r["query_id"])
        for row in values:
            qid = row.get("query_id", row.get("id"))
            if qid not in targets:
                continue
            if table == "search_preferences":
                row.pop("exclusions", None)
            if table == "search_dashboard":
                row.pop("revision", None)
            if table == "search_platforms":
                row.pop("ebay_generation", None)
                config = json.loads(row["ebay_config"])
                if config.get("shared_alert_version") == 1:
                    config.pop("keywords", None)
                    config.pop("shared_keywords", None)
                    if config.get("search_url"):
                        parts = urlsplit(config["search_url"])
                        config["search_url"] = [parts.scheme, parts.netloc, parts.path,
                            [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "_nkw"], parts.fragment]
                row["ebay_config"] = config
        result[table] = values
    result["live_selection"] = list(conn.execute("SELECT value FROM parameters WHERE key='ebay_live_ids'").fetchone() or [])
    return result


def _backup(conn):
    directory = Path(db.DB_PATH).resolve().parent / "backups"
    directory.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"before-search-review-{stamp}.sqlite3"
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    with closing(sqlite3.connect(path)) as destination:
        conn.backup(destination)
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Review backup failed integrity check")
    return str(path)


def apply_once(*, manifest_path=None, now=None):
    if os.environ.get("MSJ_SEARCH_REVIEW_RELEASE") != RELEASE:
        return None
    with db.connection_scope() as conn:
        conn.row_factory = sqlite3.Row
        if conn.execute("SELECT 1 FROM parameters WHERE key=?", (MARKER,)).fetchone():
            return None
        raw = Path(manifest_path).read_bytes() if manifest_path else _private_manifest()
        manifest = json.loads(raw)
        edits = manifest["edits"]
        ids = [r["id"] for r in edits]
        if manifest.get("id") != RELEASE or not ids or len(ids) != len(set(ids)):
            raise RuntimeError("Invalid review membership")
        backup = _backup(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            if [r[0] for r in conn.execute("SELECT id FROM queries ORDER BY id")] != manifest["query_ids"]:
                raise RuntimeError("Saved search membership changed since review")
            prepared = []
            for edit in edits:
                qid = edit["id"]
                state = _state(conn, qid)
                if state != edit["expected"] or not state["queries"]:
                    raise RuntimeError(f"Search #{qid} changed since review; no edits applied")
                if not state["search_dashboard"] or state["search_dashboard"]["paused"] or state["search_dashboard"]["archived"]:
                    raise RuntimeError("Review targets must already be active")
                words = vinted_keywords.parse("\n".join(edit["keywords"]))
                blocks = parse_exclusions("\n".join(edit["exclusions"]))
                if words != edit["keywords"] or blocks != edit["exclusions"]:
                    raise RuntimeError("Non-canonical reviewed rules")
                if words == state["keywords"] and blocks == json.loads((state["search_preferences"] or {}).get("exclusions", "[]")):
                    raise RuntimeError("Unchanged search in review edit list")
                platform = conn.execute("SELECT * FROM search_platforms WHERE query_id=?", (qid,)).fetchone()
                config = json.loads(platform["ebay_config"]) if platform else {}
                shared = config.get("shared_alert_version") == 1
                keyword_query = shared_keyword_query(words) if shared else None
                if shared and (config.get("shared_keywords") != state["keywords"] or not config.get("uk_only")):
                    raise RuntimeError("Shared keywords or UK restriction changed")
                prepared.append((edit, state, words, blocks, config, keyword_query))
            protected = _protected(conn, set(ids))
            variants_before = conn.execute("SELECT COUNT(*) FROM vinted_keyword_variants").fetchone()[0]
            cancelled = 0
            keyword_edits = 0
            exclusion_edits = 0
            for edit, state, words, blocks, config, keyword_query in prepared:
                qid = edit["id"]
                word_change = words != state["keywords"]
                block_change = blocks != json.loads((state["search_preferences"] or {}).get("exclusions", "[]"))
                keyword_edits += int(word_change)
                exclusion_edits += int(block_change)
                if block_change:
                    conn.execute("INSERT INTO search_preferences(query_id,reminder,exclusions) VALUES(?,'',?) ON CONFLICT(query_id) DO UPDATE SET exclusions=excluded.exclusions", (qid, json.dumps(blocks, ensure_ascii=False)))
                if word_change:
                    vinted_keywords.save(conn, qid, state["queries"]["query"], words)
                    if keyword_query is not None:
                        config.update(shared_keywords=words, keywords=keyword_query)
                        if config.get("search_url"):
                            parts = urlsplit(config["search_url"])
                            params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "_nkw"]
                            params.append(("_nkw", keyword_query))
                            config["search_url"] = parts._replace(query=urlencode(params)).geturl()
                        conn.execute("UPDATE search_platforms SET ebay_config=?,ebay_generation=ebay_generation+1 WHERE query_id=?", (json.dumps(config, sort_keys=True), qid))
                        # The eBay poller quietly primes only changed criteria.
                        conn.execute("DELETE FROM ebay_state WHERE query_id=?", (qid,))
                conn.execute("UPDATE search_dashboard SET revision=revision+1 WHERE query_id=?", (qid,))
                # Never replay previous candidates. Future checks use the new revision.
                conn.execute("UPDATE keyword_candidates SET status='cancelled',reason='Saved search changed in reviewed edit' WHERE query_id=? AND status='pending'", (qid,))
                pending = conn.execute("SELECT item_id,platform,title FROM alert_outbox WHERE query_id=? AND status='pending'", (qid,)).fetchall()
                for pending_row in pending:
                    if (word_change and (pending_row["platform"] == "vinted" or keyword_query is not None)) or excluded_by(pending_row["title"], blocks):
                        cancelled += conn.execute("UPDATE alert_outbox SET status='cancelled',error='Saved search changed in reviewed edit',lease_token=NULL,leased_until=0 WHERE item_id=? AND platform=? AND status='pending'", (pending_row["item_id"], pending_row["platform"])).rowcount
            if _protected(conn, set(ids)) != protected:
                raise RuntimeError("A protected search setting changed; rolling back")
            summary = {"id": RELEASE, "applied_at": now or time.time(), "edits": len(edits),
                "keyword_edits": keyword_edits, "exclusion_edits": exclusion_edits,
                "variants_before": variants_before,
                "variants_after": conn.execute("SELECT COUNT(*) FROM vinted_keyword_variants").fetchone()[0],
                "pending_cancelled": cancelled, "protected_settings_unchanged": True,
                "backup": backup, "sha256": hashlib.sha256(raw).hexdigest()}
            conn.execute("INSERT INTO parameters(key,value) VALUES (?,?)", (MARKER, json.dumps(summary, sort_keys=True)))
            conn.commit()
            return summary
        except Exception:
            conn.rollback()
            raise
