"""One explicitly enabled, guarded release of the owner's audited searches.

Runs in the parent before pollers start. No HTTP, Telegram or checkout calls.
The entire settings change and comparison-run marker commit together.
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
from urllib.parse import parse_qsl, urlsplit

import db
import search_settings
import vinted_keywords
from ebay_search_link import parse_link, shared_keyword_query

RELEASE = "msj-search-audit-20261020-v1"


def _private_manifest():
    # The repository is public. Owner search definitions travel only through
    # the app's private deployment configuration, never in a source commit.
    encoded = "".join(
        os.environ.get(f"MSJ_SEARCH_AUDIT_CONFIG_{i}", "") for i in range(4)
    )
    if not encoded or len(encoded) > 100000:
        raise RuntimeError("Missing or oversized private search configuration")
    decompressor = zlib.decompressobj()
    raw = decompressor.decompress(base64.b64decode(encoded, validate=True), 500000)
    if not decompressor.eof or decompressor.unconsumed_tail or decompressor.unused_data:
        raise RuntimeError("Invalid bounded private search configuration")
    digest = os.environ.get("MSJ_SEARCH_AUDIT_SHA256", "")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise RuntimeError("Private search configuration checksum does not match")
    return raw


def _url_identity(url):
    parts = urlsplit(url)
    return parts.scheme, parts.netloc, parts.path, sorted(parse_qsl(parts.query))


def _backup(conn):
    directory = Path(db.DB_PATH).resolve().parent / "backups"
    directory.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"before-search-audit-{stamp}.sqlite3"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(path)) as destination:
        conn.backup(destination)
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Search audit backup failed integrity check")
    return str(path)


def apply_once(*, manifest_path=None, now=None):
    if os.environ.get("MSJ_SEARCH_AUDIT_RELEASE") != RELEASE:
        return None
    raw = (
        Path(manifest_path).read_bytes()
        if manifest_path is not None
        else _private_manifest()
    )
    manifest = json.loads(raw)
    if manifest["id"] != RELEASE:
        raise RuntimeError("Wrong search audit release")
    with db.connection_scope() as conn:
        conn.row_factory = sqlite3.Row
        if conn.execute(
            "SELECT 1 FROM search_audit_runs WHERE id=?", (RELEASE,)
        ).fetchone():
            return None
        backup = _backup(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            old_ids = set(manifest["old_ids"])
            new_ids = {r["id"] for r in manifest["rows"]}
            if (
                len(old_ids) != 40
                or new_ids != set(range(142, 270))
                or old_ids & new_ids
            ):
                raise RuntimeError("Unexpected search cohort membership")
            active = {r[0] for r in conn.execute("""SELECT q.id FROM queries q
                LEFT JOIN search_dashboard d ON d.query_id=q.id
                LEFT JOIN search_platforms s ON s.query_id=q.id
                WHERE COALESCE(d.paused,0)=0 AND COALESCE(d.archived,0)=0
                AND COALESCE(s.vinted_enabled,1)=1""")}
            if active != old_ids:
                raise RuntimeError(
                    "Active searches changed since the approved rollback; no settings applied"
                )
            changes = []
            for row in manifest["rows"]:
                search = search_settings.get_search(row["id"])
                expected = row["expected"]
                if (
                    not search
                    or search.get("shared_alert_version") != 1
                    or not search["paused"]
                    or search["archived"]
                ):
                    raise RuntimeError(
                        f"Search #{row['id']} state changed; no settings applied"
                    )
                checks = {
                    "name": search["query_name"],
                    "keywords": search["shared_keywords"],
                    "exclusions": search["exclusions"],
                    "maximum": search["vinted_max_total"],
                    "folder_id": search["folder_id"],
                    "reminder": search["reminder"],
                    "photo_count": conn.execute(
                        "SELECT COUNT(*) FROM search_reference_photos WHERE query_id=?",
                        (row["id"],),
                    ).fetchone()[0],
                }
                wrong = [key for key, value in checks.items() if value != expected[key]]
                if _url_identity(search["query"]) != _url_identity(expected["url"]):
                    wrong.append("url")
                if wrong:
                    raise RuntimeError(
                        f"Search #{row['id']} changed in {','.join(wrong)}; no settings applied"
                    )
                words = vinted_keywords.parse("\n".join(row["keywords"]))
                blocks = search_settings.parse_exclusions("\n".join(row["exclusions"]))
                if not words or len(row["reminder"]) > 800:
                    raise RuntimeError(f"Invalid audited search #{row['id']}")
                params = dict(parse_qsl(urlsplit(row["url"]).query))
                sizes = [
                    v
                    for k, v in parse_qsl(urlsplit(row["url"]).query)
                    if k == "size_ids[]"
                ]
                if (
                    sorted(sizes) != ["2", "3", "4"]
                    or not params.get("brand_ids[]")
                    or not params.get("catalog[]")
                ):
                    raise RuntimeError(
                        f"Missing size/brand/category filter for #{row['id']}"
                    )
                config = dict(search["ebay"])
                if row["ebay_url"]:
                    parsed = parse_link(
                        row["ebay_url"],
                        keywords=shared_keyword_query(words),
                        ignore_prices=True,
                    )
                    if not parsed["aspects"].get("Brand") or parsed["aspects"].get(
                        "Size"
                    ) != ["M", "S", "XS"]:
                        raise RuntimeError(f"Missing eBay brand/size for #{row['id']}")
                    config.update(parsed)
                config.update(
                    shared_alert_version=1,
                    shared_keywords=words,
                    min_price=None,
                    max_price=search["vinted_max_total"],
                    include_shipping=True,
                )
                changes.append((row, search, words, blocks, config))
            started = time.time() if now is None else now
            conn.execute(
                "INSERT INTO search_audit_runs(id,started_at,manifest_sha) VALUES (?,?,?)",
                (RELEASE, started, hashlib.sha256(raw).hexdigest()),
            )
            conn.executemany(
                "INSERT INTO search_audit_members VALUES (?,?,?)",
                [(RELEASE, i, "old") for i in sorted(old_ids)]
                + [(RELEASE, i, "new") for i in sorted(new_ids)],
            )
            for row, search, words, blocks, config in changes:
                identity = row["id"]
                conn.execute(
                    "UPDATE queries SET query=? WHERE id=?", (row["url"], identity)
                )
                conn.execute(
                    "UPDATE search_preferences SET exclusions=?,reminder=? WHERE query_id=?",
                    (json.dumps(blocks, ensure_ascii=False), row["reminder"], identity),
                )
                vinted_keywords.save(conn, identity, row["url"], words)
                # Fresh URL/keyword variants begin with a quiet baseline. Retain
                # global history, checkpoints, photos, folders and buyer state.
                conn.execute(
                    "UPDATE search_dashboard SET revision=revision+1,paused=0,rebaseline=1 WHERE query_id=?",
                    (identity,),
                )
                conn.execute(
                    "UPDATE search_platforms SET vinted_enabled=1,ebay_config=?,ebay_generation=ebay_generation+1 WHERE query_id=?",
                    (json.dumps(config, sort_keys=True), identity),
                )
                conn.execute(
                    "UPDATE alert_outbox SET status='cancelled',error='Audited search release; historical pending alert',lease_token=NULL,leased_until=0 WHERE query_id=? AND status='pending'",
                    (identity,),
                )
                conn.execute(
                    "UPDATE keyword_candidates SET status='cancelled',reason='Audited search release' WHERE query_id=? AND status='pending'",
                    (identity,),
                )
            # Keep configured eBay links visible, with no live eBay selection.
            conn.execute(
                "INSERT OR REPLACE INTO parameters VALUES ('ebay_live_ids','[]')"
            )
            summary = {
                "new_active": len(new_ids),
                "old_active": len(old_ids),
                "ebay_live": 0,
                "notification_policy": "one per listing; observed duplicate matches logged",
                "sizes": ["XS", "S", "M"],
                "ai_photo_screening": False,
                "backup": backup,
            }
            conn.execute(
                "UPDATE search_audit_runs SET applied_at=?,summary=? WHERE id=?",
                (started, json.dumps(summary), RELEASE),
            )
            conn.commit()
            return summary
        except Exception:
            conn.rollback()
            raise
