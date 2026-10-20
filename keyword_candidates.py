"""Bounded, durable description proof before a shared alert becomes globally seen.

Only ambiguous positive-keyword candidates enter this queue. One item-scoped
read is shared by overlapping searches; normal Telegram delivery never awaits it.
Unknown descriptions are retained for bounded retry, never treated as a match.
"""

import asyncio
import json
import secrets
import time
from contextlib import closing
from types import SimpleNamespace

from logger import get_logger
from search_settings import connection, excluded_by
from shared_matching import keyword_evidence, required_keywords

logger = get_logger(__name__)
RETRY_WINDOW = 900
MAX_ATTEMPTS = 3
LEASE_SECONDS = 120


def pending_boundary(conn):
    """Report old queued shared positives without changing them or doing reads."""
    counts = {
        platform: {"pending": 0, "unverified": 0} for platform in ("vinted", "ebay")
    }
    for row in conn.execute(
        """SELECT o.platform,o.title,s.ebay_config,v.payload AS vinted_details,
        e.payload AS ebay_details FROM alert_outbox o JOIN search_platforms s
        ON s.query_id=o.query_id LEFT JOIN vinted_alert_details v ON v.item_id=o.item_id
        LEFT JOIN ebay_alert_details e ON e.item_id=o.item_id WHERE o.status='pending'"""
    ):
        platform = row["platform"]
        config = json.loads(row["ebay_config"])
        if platform not in counts or not required_keywords(config):
            continue
        details = json.loads(row[platform + "_details"] or "{}")
        evidence = keyword_evidence(
            config,
            row["title"],
            details.get("keyword_description", details.get("description", "")),
            description_complete=details.get("description_complete", False),
        )
        counts[platform]["pending"] += 1
        counts[platform]["unverified"] += int(evidence["state"] != "matched")
    return counts


def log_pending_boundary(conn):
    for platform, counts in pending_boundary(conn).items():
        logger.info(
            "Shared keyword rollout platform=%s pending=%s unverified=%s",
            platform,
            counts["pending"],
            counts["unverified"],
        )


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS keyword_candidates (
        platform TEXT NOT NULL, item_id TEXT NOT NULL,
        query_id INTEGER NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL, generation INTEGER NOT NULL,
        payload TEXT NOT NULL, found_at REAL NOT NULL, retry_until REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(platform,item_id,query_id))""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_keyword_candidates_pending
        ON keyword_candidates(platform,status,found_at)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_keyword_candidates_retention
        ON keyword_candidates(found_at)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS keyword_description_cache (
        platform TEXT NOT NULL, item_id TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
        lease_token TEXT, leased_until REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL, PRIMARY KEY(platform,item_id))""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_keyword_cache_retention
        ON keyword_description_cache(updated_at)""")


def stage(platform, item, search, alert, *, conn=None, timing=None, now=None):
    """Retain a fresh candidate without consuming global seen/outbox history."""
    if platform not in {"vinted", "ebay"} or not required_keywords(search):
        return False
    now = time.time() if now is None else now
    item = dict(item)
    item_id = str(item.get("item_id", item.get("id", "")))
    if not item_id:
        return False
    item["item_id"] = item_id
    payload = json.dumps({"item": item, "alert": alert, "timing": timing})
    if conn is None:
        with closing(connection()) as owned, owned:
            return stage(
                platform, item, search, alert, conn=owned, timing=timing, now=now
            )
    conn.execute(
        """INSERT INTO keyword_candidates VALUES (?,?,?,?,?,?,?,?, 'pending','')
        ON CONFLICT(platform,item_id,query_id) DO NOTHING""",
        (
            platform,
            item_id,
            search["id"],
            search.get("revision", 0),
            search.get("ebay_generation", 0),
            payload,
            now,
            now + RETRY_WINDOW,
        ),
    )
    conn.execute(
        """INSERT OR IGNORE INTO keyword_description_cache
        (platform,item_id,updated_at) VALUES (?,?,?)""",
        (platform, item_id, now),
    )
    cached = conn.execute(
        "SELECT payload FROM keyword_description_cache WHERE platform=? AND item_id=?",
        (platform, item_id),
    ).fetchone()
    data = json.loads(cached[0])
    evidence = keyword_evidence(
        search,
        item["title"],
        data.get("keyword_description", data.get("description", "")),
        description_complete=data.get("description_complete", False),
    )
    if evidence["state"] in {"matched", "absent"}:
        conn.execute(
            """UPDATE keyword_description_cache SET next_attempt=MIN(next_attempt,?)
            WHERE platform=? AND item_id=?""",
            (now, platform, item_id),
        )
    return True


def cached_description(platform, item_id):
    with closing(connection()) as conn:
        row = conn.execute(
            "SELECT payload FROM keyword_description_cache WHERE platform=? AND item_id=?",
            (platform, str(item_id)),
        ).fetchone()
    return json.loads(row[0]) if row else {}


def _search(conn, candidate):
    row = conn.execute(
        """SELECT q.*,COALESCE(d.revision,0) AS revision,
        COALESCE(d.paused,0) AS paused,COALESCE(d.archived,0) AS archived,
        COALESCE(p.exclusions,'[]') AS exclusions,
        COALESCE(s.vinted_enabled,1) AS vinted_enabled,
        COALESCE(s.ebay_enabled,0) AS ebay_enabled,
        COALESCE(s.ebay_generation,0) AS ebay_generation,
        COALESCE(s.ebay_config,'{}') AS ebay_config,
        b.max_total AS vinted_max_total,
        COALESCE(b.postage_estimate,220) AS vinted_postage_estimate
        FROM queries q LEFT JOIN search_dashboard d ON d.query_id=q.id
        LEFT JOIN search_preferences p ON p.query_id=q.id
        LEFT JOIN search_platforms s ON s.query_id=q.id
        LEFT JOIN vinted_search_budgets b ON b.query_id=q.id WHERE q.id=?""",
        (candidate["query_id"],),
    ).fetchone()
    if not row:
        return None
    search = dict(row)
    search["ebay"] = json.loads(search.pop("ebay_config"))
    search["shared_alert_version"] = search["ebay"].get("shared_alert_version", 0)
    search["shared_keywords"] = search["ebay"].get("shared_keywords", [])
    search["exclusions"] = json.loads(search["exclusions"])
    platform = candidate["platform"]
    if (
        search["paused"]
        or search["archived"]
        or not search[platform + "_enabled"]
        or search["revision"] != candidate["revision"]
        or search["shared_alert_version"] != 1
    ):
        return None
    if platform == "ebay":
        from ebay_store import live_ids

        if search["ebay_generation"] != candidate["generation"] or search[
            "id"
        ] not in live_ids(conn):
            return None
        owner = conn.execute(
            """SELECT o.seller_hash FROM ebay_item_owners o
            WHERE o.item_id=? AND NOT EXISTS
            (SELECT 1 FROM ebay_deleted_sellers d WHERE d.digest=o.seller_hash)""",
            (candidate["item_id"],),
        ).fetchone()
        if not owner:
            return None
    return search


def _budget(item, search, platform):
    if platform == "vinted":
        from vinted_budget import estimate

        budget = estimate(
            SimpleNamespace(
                price=item["price"],
                currency=item.get("currency", "GBP"),
                raw_data=item.get("raw_data", {}),
            ),
            search,
        )
        return budget is not None and budget["within_budget"]
    total = item.get("estimated_total")
    maximum = search["ebay"].get("max_price")
    minimum = search["ebay"].get("min_price")
    return (
        type(total) is int
        and (maximum is None or total <= maximum)
        and (minimum is None or total >= minimum)
    )


def _decision(conn, candidate, data, now):
    search = _search(conn, candidate)
    if search is None:
        return "cancelled", "Saved search changed", None
    item = json.loads(candidate["payload"])["item"]
    if excluded_by(item["title"], search["exclusions"]):
        return "rejected", "Blocked by current title exclusions", search
    if not _budget(item, search, candidate["platform"]):
        return "rejected", "Outside current estimated total budget", search
    if now >= candidate["retry_until"]:
        return "unverified", "Description verification timed out", search
    evidence = keyword_evidence(
        search,
        item["title"],
        data.get("keyword_description", data.get("description", "")),
        description_complete=data.get("description_complete", False),
    )
    if evidence["state"] in {"matched", "unrestricted"}:
        return "matched", "", search
    if evidence["state"] == "absent":
        return "rejected", "Saved keywords absent from title and description", search
    return "pending", "Description not yet verified", search


def _set_status(conn, candidate, status, reason):
    conn.execute(
        """UPDATE keyword_candidates SET status=?,reason=?
        WHERE platform=? AND item_id=? AND query_id=? AND status='pending'""",
        (
            status,
            reason,
            candidate["platform"],
            candidate["item_id"],
            candidate["query_id"],
        ),
    )


def _claim(platform, now):
    with closing(connection()) as conn, conn:
        # Most ticks have no ambiguous listing. Keep that path read-only so
        # ordinary discovery/delivery writes never contend with an idle worker.
        due = conn.execute(
            """SELECT 1 FROM keyword_candidates c JOIN keyword_description_cache d
            ON d.platform=c.platform AND d.item_id=c.item_id WHERE c.platform=?
            AND c.status='pending' AND d.next_attempt<=? AND d.leased_until<=? LIMIT 1""",
            (platform, now, now),
        ).fetchone()
        if not due:
            return None
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM keyword_candidates WHERE status!='pending' AND found_at<?",
            (now - 7 * 86400,),
        )
        conn.execute(
            """DELETE FROM keyword_description_cache WHERE updated_at<?
            AND NOT EXISTS(SELECT 1 FROM keyword_candidates c WHERE c.platform=
            keyword_description_cache.platform AND c.item_id=keyword_description_cache.item_id)""",
            (now - 7 * 86400,),
        )
        candidates = conn.execute(
            """SELECT c.* FROM keyword_candidates c JOIN keyword_description_cache d
            ON d.platform=c.platform AND d.item_id=c.item_id WHERE c.platform=?
            AND c.status='pending' AND d.next_attempt<=? AND d.leased_until<=?
            ORDER BY c.found_at,c.item_id LIMIT 30""",
            (platform, now, now),
        ).fetchall()
        for row in candidates:
            candidate = dict(row)
            saved = conn.execute(
                "SELECT * FROM keyword_description_cache WHERE platform=? AND item_id=?",
                (platform, candidate["item_id"]),
            ).fetchone()
            data = json.loads(saved["payload"])
            status, reason, _ = _decision(conn, candidate, data, now)
            if status not in {"pending", "matched"}:
                _set_status(conn, candidate, status, reason)
                continue
            token = secrets.token_hex(16)
            conn.execute(
                """UPDATE keyword_description_cache SET lease_token=?,leased_until=?
                WHERE platform=? AND item_id=?""",
                (token, now + LEASE_SECONDS, platform, candidate["item_id"]),
            )
            return dict(
                candidate,
                lease_token=token,
                cached=data,
                attempts=saved["attempts"],
                ready=status == "matched",
            )
    return None


def fetch_candidate_details(platform, row):
    if platform == "ebay":
        from ebay_images import fetch_candidate_details as fetch
    else:
        from vinted_gallery import fetch_candidate_details as fetch
    return fetch(row)


def _enqueue(conn, candidate, data, search):
    payload = json.loads(candidate["payload"])
    item, alert = payload["item"], payload["alert"]
    item_id, platform = candidate["item_id"], candidate["platform"]
    details_key = "ebay_details" if platform == "ebay" else "vinted_details"
    details = dict(alert.get(details_key) or {})
    details.update(
        description=data.get("description", ""),
        keyword_description=data.get(
            "keyword_description", data.get("description", "")
        ),
        description_checked=bool(data.get("available") or data.get("description")),
        description_complete=data.get("description_complete", False),
        keyword_verified=True,
        gallery_checked=bool(data.get("gallery_checked")),
        keyword_evidence=keyword_evidence(
            search,
            item["title"],
            data.get("keyword_description", data.get("description", "")),
            description_complete=data.get("description_complete", False),
        ),
    )
    if data.get("photos"):
        details["photos"] = list(
            dict.fromkeys(data["photos"] + details.get("photos", []))
        )[:4]
        details["gallery_checked"] = True
    if platform == "vinted":
        # items.item is indexed but not UNIQUE. The surrounding immediate
        # transaction serialises this explicit check with the discovery path.
        if conn.execute("SELECT 1 FROM items WHERE item=?", (item_id,)).fetchone():
            from search_match_audit import capture, record

            record(conn, item_id, candidate["query_id"], capture(search, details))
            return False
        conn.execute(
            """INSERT INTO items
            (item,title,price,currency,timestamp,photo_url,query_id) VALUES (?,?,?,?,?,?,?)""",
            (
                item_id,
                item["title"],
                item["price"],
                item.get("currency", "GBP"),
                item.get("timestamp", candidate["found_at"]),
                item.get("photo_url"),
                candidate["query_id"],
            ),
        )
        conn.execute(
            "UPDATE queries SET last_item=MAX(COALESCE(last_item,0),?) WHERE id=?",
            (item.get("timestamp", candidate["found_at"]), candidate["query_id"]),
        )
        price = str(item["price"])
    else:
        price = f"{item['price'] / 100:.2f}"
    inserted = conn.execute(
        """INSERT OR IGNORE INTO alert_outbox
        (item_id,query_id,search_name,content,url,title,price,currency,photo_url,
         reference_id,found_at,photo_status,platform) VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?)""",
        (
            item_id,
            candidate["query_id"],
            alert["search_name"],
            alert["content"],
            alert.get("url", item["url"]),
            item["title"],
            price,
            item.get("currency", "GBP"),
            item.get("photo_url"),
            alert.get("reference_id"),
            candidate["found_at"],
            platform,
        ),
    ).rowcount
    if inserted:
        table = "ebay_alert_details" if platform == "ebay" else "vinted_alert_details"
        conn.execute(
            f"INSERT INTO {table} VALUES (?,?)", (item_id, json.dumps(details))
        )
        if platform == "vinted":
            from search_match_audit import capture, record

            record(
                conn,
                item_id,
                candidate["query_id"],
                capture(search, details),
                canonical=True,
            )
        timing = payload.get("timing")
        if platform == "ebay" and timing:
            values = (
                timing["listed_at"],
                timing["date_source"],
                timing["request_started"],
                timing["response_received"],
            )
            conn.execute(
                "INSERT INTO ebay_alert_timing VALUES (?,?,?,?,?)", (item_id, *values)
            )
    return bool(inserted)


def process_one(platform, *, now=None):
    live_clock = now is None
    now = time.time() if now is None else now
    claimed = _claim(platform, now)
    if not claimed:
        return False
    data = claimed["cached"]
    fetched = not claimed["ready"]
    if fetched:
        item = json.loads(claimed["payload"])["item"]
        data = fetch_candidate_details(platform, item) or {}
    completed_at = time.time() if live_clock else now
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        cache = conn.execute(
            """SELECT * FROM keyword_description_cache
            WHERE platform=? AND item_id=? AND lease_token=?""",
            (platform, claimed["item_id"], claimed["lease_token"]),
        ).fetchone()
        candidates = conn.execute(
            """SELECT * FROM keyword_candidates
            WHERE platform=? AND item_id=? AND status='pending'""",
            (platform, claimed["item_id"]),
        ).fetchall()
        if not cache or cache["leased_until"] <= completed_at or not candidates:
            return True  # A deletion during the GET cannot restore its listing.
        valid = []
        for row in candidates:
            candidate = dict(row)
            if _search(conn, candidate) is None:
                _set_status(conn, candidate, "cancelled", "Saved search changed")
            else:
                valid.append(candidate)
        if not valid:
            conn.execute(
                """DELETE FROM keyword_description_cache
                WHERE platform=? AND item_id=? AND lease_token=?""",
                (platform, claimed["item_id"], claimed["lease_token"]),
            )
            return True
        attempts = cache["attempts"] + int(fetched and not data.get("retry_after"))
        for candidate in valid:
            status, reason, search = _decision(conn, candidate, data, completed_at)
            if status == "matched":
                _enqueue(conn, candidate, data, search)
                status = "accepted"
            elif status == "pending" and attempts >= MAX_ATTEMPTS:
                status, reason = (
                    "unverified",
                    "Description unavailable after bounded retries",
                )
            _set_status(conn, candidate, status, reason)
        retry_at = max(completed_at + 30, data.get("retry_after", 0))
        conn.execute(
            """UPDATE keyword_description_cache SET payload=?,attempts=?,next_attempt=?,
            lease_token=NULL,leased_until=0,updated_at=? WHERE platform=? AND item_id=? AND lease_token=?""",
            (
                json.dumps(data),
                attempts,
                retry_at,
                completed_at,
                platform,
                claimed["item_id"],
                claimed["lease_token"],
            ),
        )
    logger.info(
        "Shared keyword description checked platform=%s item=%s",
        platform,
        claimed["item_id"],
    )
    return True


async def run(platform):
    while True:
        try:
            worked = await asyncio.to_thread(process_one, platform)
            await asyncio.sleep(0.1 if worked else 0.5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Shared keyword candidate will retry after %s", type(exc).__name__
            )
            await asyncio.sleep(2)
