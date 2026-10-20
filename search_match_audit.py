"""Record observed old/new matches without a second alert or purchase identity."""

import json
import time
from contextlib import closing

from search_settings import connection


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS search_audit_runs (
        id TEXT PRIMARY KEY, started_at REAL NOT NULL, applied_at REAL,
        manifest_sha TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '{}')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS search_audit_members (
        run_id TEXT NOT NULL REFERENCES search_audit_runs(id),
        query_id INTEGER NOT NULL, cohort TEXT NOT NULL CHECK(cohort IN ('old','new')),
        PRIMARY KEY(run_id,query_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS search_match_log (
        run_id TEXT NOT NULL REFERENCES search_audit_runs(id),
        item_id TEXT NOT NULL REFERENCES alert_outbox(item_id) ON DELETE CASCADE,
        query_id INTEGER NOT NULL, revision INTEGER NOT NULL,
        cohort TEXT NOT NULL, matched_at REAL NOT NULL, canonical INTEGER NOT NULL,
        rule_snapshot TEXT NOT NULL, evidence TEXT NOT NULL,
        PRIMARY KEY(run_id,item_id,query_id,revision))""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_match_audit_item
        ON search_match_log(item_id,run_id)""")


def eligible_seen(query_id, item_ids):
    """Only current-run alerts may be observed again; never replay history."""
    if not item_ids:
        return set()
    with closing(connection()) as conn:
        marks = ",".join("?" for _ in item_ids)
        return {
            str(row[0])
            for row in conn.execute(
                f"""SELECT o.item_id FROM alert_outbox o
            JOIN search_audit_members m ON m.query_id=?
            JOIN search_audit_runs r ON r.id=m.run_id
            WHERE o.platform='vinted' AND o.found_at>=r.started_at
            AND o.item_id IN ({marks})""",
                (query_id, *item_ids),
            )
        }


def capture(search, details):
    keys = (
        "id",
        "revision",
        "query_name",
        "query",
        "exclusions",
        "vinted_keywords",
        "shared_keywords",
        "vinted_max_total",
        "vinted_postage_estimate",
        "max_buy",
        "resale_low",
        "resale_high",
        "reminder",
        "must_have",
        "folder_id",
    )
    return {
        "rules": {key: search.get(key) for key in keys},
        "evidence": {
            key: details.get(key)
            for key in (
                "keyword_evidence",
                "keyword_description",
                "description_complete",
                "budget",
            )
        },
    }


def record(conn, item_id, query_id, snapshot, *, canonical=False, now=None):
    if not snapshot:
        return
    # Bind to the rules actually checked, rather than a subsequently edited row.
    revision = snapshot["rules"].get("revision", 0) or 0
    current = conn.execute(
        "SELECT COALESCE(revision,0) FROM search_dashboard WHERE query_id=?",
        (query_id,),
    ).fetchone()
    if current and current[0] != revision:
        return
    conn.execute(
        """INSERT OR IGNORE INTO search_match_log
        SELECT r.id,o.item_id,m.query_id,?,m.cohort,?,?,?,?
        FROM search_audit_members m JOIN search_audit_runs r ON r.id=m.run_id
        JOIN alert_outbox o ON o.item_id=? AND o.platform='vinted'
        WHERE m.query_id=? AND o.found_at>=r.started_at""",
        (
            revision,
            time.time() if now is None else now,
            int(canonical),
            json.dumps(snapshot["rules"], ensure_ascii=False),
            json.dumps(snapshot["evidence"], ensure_ascii=False),
            str(item_id),
            query_id,
        ),
    )


def summary():
    with closing(connection()) as conn:
        run = conn.execute(
            "SELECT * FROM search_audit_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if not run:
            return None
        counts = conn.execute(
            """SELECT COUNT(*) AS matches, COUNT(DISTINCT item_id) AS listings,
            SUM(NOT canonical) AS suppressed FROM search_match_log WHERE run_id=?""",
            (run["id"],),
        ).fetchone()
        overlap = conn.execute(
            """SELECT COUNT(*) FROM (SELECT item_id FROM search_match_log
            WHERE run_id=? GROUP BY item_id HAVING COUNT(DISTINCT cohort)=2)""",
            (run["id"],),
        ).fetchone()[0]
        ready = conn.execute(
            """SELECT COUNT(*) AS total, SUM(NOT EXISTS (
            SELECT 1 FROM vinted_keyword_variants v WHERE v.query_id=m.query_id
            AND v.primed=0)) AS ready FROM search_audit_members m
            WHERE m.run_id=? AND m.cohort='new'""",
            (run["id"],),
        ).fetchone()
        return {
            "run": dict(run),
            "counts": dict(counts),
            "overlap": overlap,
            "new_searches_ready": ready["ready"] or 0,
            "new_searches_total": ready["total"],
        }


def export_rows():
    with closing(connection()) as conn:
        return [dict(row) for row in conn.execute("""SELECT l.*,o.title,o.url,o.price,
            o.query_id AS notified_query_id,o.status AS alert_status,o.found_at,o.sent_at
            FROM search_match_log l JOIN alert_outbox o ON o.item_id=l.item_id
            ORDER BY l.matched_at,l.item_id,l.query_id""")]
