"""Review accepted listing alerts separately from delivery, retaining the first 100 as a sample."""

import asyncio
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import httpx

from resale_pilot import (
    MARKET_SECONDS,
    TRIAL_SEARCH_LIMIT,
    cached_listings,
    encoded,
    evaluate_one,
    export_alerts,
    market_cache,
    market_pool,
    warm,
)
from resale_pricing import (
    search_key,
    shortlist,
    valuation_text,
)

logger = logging.getLogger(__name__)
LIMIT = 100
_receipts = None


def active():
    return _receipts is not None


def accepted(identity, cover=None, message=None):
    """No network, DB, image processing or awaits on the initial Telegram delivery path."""
    if _receipts is not None:
        # A burst can queue lightweight identities, but never an unbounded pile of photos.
        _receipts.put_nowait(
            (str(identity), cover if _receipts.qsize() < 16 else None, time.monotonic())
        )
        if message is not None:
            from resale_telegram import remember

            remember(identity, message)


def independent_items(path, rows):
    """Exclude other Finder finds too, even those outside this trial."""
    if not rows:
        return []
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=0.2)) as conn:
        alerted = set()
        for pos in range(0, len(rows), 400):
            block = [row["id"] for row in rows[pos : pos + 400]]
            parameters = ",".join("?" for _ in block)
            hits = conn.execute(
                f"SELECT item_id FROM alert_outbox WHERE item_id IN ({parameters})",
                block,
            )
            alerted.update(str(row[0]) for row in hits)
    return [row for row in rows if row["id"] not in alerted]


def sample_table(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS resale_sample(
        item_id TEXT PRIMARY KEY, sampled REAL NOT NULL, source TEXT, status TEXT NOT NULL,
        baseline TEXT, reviewed TEXT, display TEXT, elapsed_ms INTEGER, cover_present INTEGER)"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS resale_sample_time ON resale_sample(sampled)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS resale_trial_report (id INTEGER PRIMARY KEY, summary TEXT NOT NULL)"
    )
    conn.commit()


class MarketLookups:
    """Share a market request across similar garments, never a valuation or decision."""

    def __init__(self, conn, client, token):
        self.conn = conn
        self.client = client
        self.token = token
        self.running = {}

    async def get(self, target):
        key = search_key(target)
        if not key or not self.token:
            return "unavailable"
        if cached_listings(self.conn, key, time.time()) is not None:
            return "cached"
        task = self.running.get(key)
        if task is None:
            task = asyncio.create_task(self.fetch(target))
            self.running[key] = task
        try:
            return await task
        finally:
            if self.running.get(key) is task and task.done():
                del self.running[key]

    async def fetch(self, target):
        try:
            async with asyncio.timeout(MARKET_SECONDS):
                result = await warm(
                    [target],
                    self.conn,
                    self.client,
                    self.token,
                    1,
                    pilot_limit=TRIAL_SEARCH_LIMIT,
                )
        except TimeoutError:
            return "timeout"
        if result["market_groups_refreshed"]:
            return "refreshed"
        if result["search_requests"]:
            return "unavailable"
        used = self.conn.execute(
            "SELECT used FROM pilot_search_budget WHERE id=1"
        ).fetchone()
        return (
            "allowance_used" if used and used[0] >= TRIAL_SEARCH_LIMIT else "deferred"
        )


async def item_candidates(target, database, conn):
    now = time.time()
    pool = market_pool(conn, target, now)
    independent = await asyncio.to_thread(independent_items, database, pool)
    return shortlist(target, independent, now)


def reserve_item(conn, identity, source, *, continuous=False):
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        now = time.time()
        conn.execute(
            "DELETE FROM resale_sample WHERE status='running' AND sampled<?",
            (now - 90,),
        )
        if (
            not continuous
            and conn.execute("SELECT count(*) FROM resale_sample").fetchone()[0]
            >= LIMIT
        ):
            return False
        return (
            conn.execute(
                "INSERT OR IGNORE INTO resale_sample(item_id,sampled,source,status) VALUES(?,?,?,'running')",
                (identity, now, source),
            ).rowcount
            == 1
        )


async def sample_loop(
    receipts,
    database,
    conn,
    client,
    token,
    lookups=None,
    *,
    continuous=False,
):
    reporting = (
        continuous
        and not conn.execute("SELECT 1 FROM resale_trial_report WHERE id=1").fetchone()
    )
    next_cleanup = 0.0
    while continuous or (
        conn.execute(
            "SELECT COUNT(*) FROM resale_sample WHERE status!='running'"
        ).fetchone()[0]
        < LIMIT
    ):
        identity, cover, started = await receipts.get()
        try:
            if continuous and time.monotonic() >= next_cleanup:
                cutoff = time.time() - 31 * 86400
                with conn:
                    conn.execute(
                        """DELETE FROM resale_sample WHERE sampled<? AND item_id NOT IN
                        (SELECT item_id FROM resale_sample ORDER BY sampled LIMIT ?)""",
                        (cutoff, LIMIT),
                    )
                    conn.execute("DELETE FROM model_reviews WHERE started<?", (cutoff,))
                next_cleanup = time.monotonic() + 86400
            alerts = await asyncio.to_thread(export_alerts, database, 1, identity)
            if not alerts or not reserve_item(
                conn, identity, alerts[0]["source"], continuous=continuous
            ):
                continue
            target = alerts[0]
            candidates = await item_candidates(target, database, conn)
            close_sellers = {
                c["seller"]
                for c in candidates
                if c.get("seller") and c["strong"] and not c["long_listed"]
            }
            market_state = "cached" if candidates else "unavailable"
            if lookups and len(close_sellers) < 3:
                market_state = await lookups.get(target)
                if market_state == "refreshed":
                    # This alert gets to use the prices its own search just found.
                    candidates = await item_candidates(target, database, conn)
            baseline, review, path = await evaluate_one(
                target, candidates, conn, client, token, cover_bytes=cover
            )
            elapsed = round((time.monotonic() - started) * 1000)
            display = valuation_text(review)
            with conn:
                conn.execute(
                    """UPDATE resale_sample SET status=?,baseline=?,reviewed=?,display=?,
                    elapsed_ms=?,cover_present=? WHERE item_id=?""",
                    (
                        path,
                        encoded(baseline),
                        encoded(review),
                        display,
                        elapsed,
                        bool(cover),
                        identity,
                    ),
                )
            from resale_telegram import publish

            publish(identity, display)
            logger.info(
                "Resale trial observation source=%s path=%s verdict=%s elapsed_ms=%d basis=%s priced=%s market=%s candidates=%d comparisons=%d",
                target["source"],
                path,
                review["verdict"],
                elapsed,
                review.get("basis", "comparisons"),
                review.get("resale_pence") is not None,
                market_state,
                len(candidates),
                review.get("comparisons", 0),
            )
            if (
                reporting
                and conn.execute(
                    "SELECT COUNT(*) FROM resale_sample WHERE status!='running'"
                ).fetchone()[0]
                >= LIMIT
            ):
                report = encoded(sample_summary(conn))
                with conn:
                    first = conn.execute(
                        "INSERT OR IGNORE INTO resale_trial_report VALUES(1,?)",
                        (report,),
                    ).rowcount
                if first:
                    logger.info("Resale trial complete: %s", report)
                reporting = False
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            sqlite3.Error,
            httpx.HTTPError,
        ) as exc:
            logger.warning(
                "Resale trial observation unavailable: %s", type(exc).__name__
            )
        finally:
            receipts.task_done()


async def run(database):
    global _receipts
    if os.getenv("FINDER_RESALE_PILOT") != "1":
        return
    market_token = os.getenv("SCAVIO_API_KEY")
    model_token = os.getenv("MISTRAL_API_KEY")
    if not (market_token or model_token):
        logger.warning(
            "Resale trial awaits separate Mistral and Scavio service settings"
        )
        return
    cache_path = os.getenv("FINDER_RESALE_CACHE") or str(
        Path(database).with_name("resale-pilot.sqlite3")
    )
    try:
        with market_cache(cache_path) as conn:
            sample_table(conn)
            import resale_telegram

            resale_telegram.configure(os.getenv("FINDER_RESALE_SHADOW_ONLY") != "1")
            recent_reviews = conn.execute(
                """SELECT item_id,reviewed FROM resale_sample
                WHERE status!='running' AND json_valid(reviewed)
                AND COALESCE(json_extract(reviewed,'$.basis'),'comparisons') != 'category'
                AND json_type(reviewed,'$.resale_pence')='integer' ORDER BY sampled DESC LIMIT ?""",
                (LIMIT,),
            )
            resale_telegram.restore_lines(
                (identity, valuation_text(json.loads(data)))
                for identity, data in recent_reviews
            )
            async with httpx.AsyncClient(
                follow_redirects=False, trust_env=False, timeout=4
            ) as client:
                lookups = MarketLookups(conn, client, market_token)
                receipts = asyncio.Queue()
                _receipts = receipts
                workers = []
                try:
                    logger.info(
                        "Resale trial ready for accepted Home and saved-search alerts"
                    )
                    workers = [
                        asyncio.create_task(
                            sample_loop(
                                receipts,
                                database,
                                conn,
                                client,
                                model_token,
                                lookups,
                                continuous=True,
                            )
                        )
                        for _ in range(2)
                    ]
                    await asyncio.gather(*workers)
                finally:
                    _receipts = None
                    resale_telegram.finish_sample()
                    for task in workers:
                        task.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
    except (OSError, ValueError, sqlite3.Error, httpx.HTTPError) as exc:
        logger.warning("Resale trial unavailable: %s", type(exc).__name__)


def sample_summary(conn):
    rows = conn.execute(
        """SELECT * FROM (
        SELECT source,status,reviewed,elapsed_ms FROM resale_sample
        WHERE status!='running' ORDER BY sampled LIMIT ?) ORDER BY elapsed_ms""",
        (LIMIT,),
    ).fetchall()
    counted = {}
    for _, _, review, _ in rows:
        name = json.loads(review)["verdict"]
        counted[name] = counted.get(name, 0) + 1
    return {
        "unique_alerts": len(rows),
        "home": sum(r[0] == "home" for r in rows),
        "saved_search": sum(r[0] == "search" for r in rows),
        "verdicts": counted,
        "ai_reviewed": sum(r[1] in ("model", "cached_model") for r in rows),
        "within_five_seconds_of_receipt": sum(r[3] <= 5000 for r in rows),
        "p95_ms": rows[(len(rows) * 95 + 99) // 100 - 1][3] if rows else None,
    }
