"""Bounded valuation trial driven by receipts for the original Telegram alert."""

import asyncio
import json
import logging
import os
import sqlite3
import time
from collections import deque
from contextlib import closing
from pathlib import Path

import httpx

from resale_pilot import (
    cached_listings,
    encoded,
    evaluate_one,
    export_alerts,
    market_cache,
    warm,
)
from resale_pricing import alert_line, market_key, shortlist

logger = logging.getLogger(__name__)
LIMIT = 100
_receipts = None


def active():
    return _receipts is not None


def accepted(identity, cover=None, message=None):
    """No network, DB, image processing or awaits on the initial Telegram delivery path."""
    if _receipts is not None:
        try:
            _receipts.put_nowait((str(identity), cover, time.monotonic()))
        except asyncio.QueueFull:
            logger.info(
                "Resale trial skipped one arrival while the sample queue was full"
            )
        else:
            if message is not None:
                from resale_telegram import remember

                remember(identity, message)


def independent_items(path, rows):
    """Exclude other Finder finds too, even those outside this trial."""
    if not rows:
        return []
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=0.2)) as conn:
        parameters = ",".join("?" for _ in rows)
        seen = conn.execute(
            f"SELECT item_id FROM alert_outbox WHERE item_id IN ({parameters})",
            [row["id"] for row in rows],
        )
        alerted = {str(row[0]) for row in seen}
    return [row for row in rows if row["id"] not in alerted]


def sample_table(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS resale_sample(
        item_id TEXT PRIMARY KEY, sampled REAL NOT NULL, source TEXT, status TEXT NOT NULL,
        baseline TEXT, reviewed TEXT, display TEXT, elapsed_ms INTEGER, cover_present INTEGER)"""
    )
    conn.commit()


def reserve_item(conn, identity, source):
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        now = time.time()
        conn.execute(
            "DELETE FROM resale_sample WHERE status='running' AND sampled<?",
            (now - 90,),
        )
        if conn.execute("SELECT count(*) FROM resale_sample").fetchone()[0] >= LIMIT:
            return False
        return (
            conn.execute(
                "INSERT OR IGNORE INTO resale_sample(item_id,sampled,source,status) VALUES(?,?,?,'running')",
                (identity, now, source),
            ).rowcount
            == 1
        )


async def sample_loop(
    receipts, database, conn, client, token, recent, refresh_needed, market_token=None
):
    while (
        conn.execute(
            "SELECT COUNT(*) FROM resale_sample WHERE status!='running'"
        ).fetchone()[0]
        < LIMIT
    ):
        identity, cover, started = await receipts.get()
        try:
            alerts = await asyncio.to_thread(export_alerts, database, 1, identity)
            if not alerts or not reserve_item(conn, identity, alerts[0]["source"]):
                continue
            target = alerts[0]
            recent.append(target)
            key = market_key(target)
            entries = cached_listings(conn, key, time.time())
            if entries is None and key:
                if market_token:
                    await warm([target], conn, client, market_token, 1, pilot_limit=50)
                    entries = cached_listings(conn, key, time.time())
                refresh_needed.set()
            rows = await asyncio.to_thread(independent_items, database, entries or [])
            candidates = shortlist(target, rows, time.time())
            baseline, review, path = await evaluate_one(
                target, candidates, conn, client, token, cover_bytes=cover
            )
            if key and entries is None:
                path = "cold_market"
                review = dict(review, verdict="Market unavailable")
            elapsed = round((time.monotonic() - started) * 1000)
            with conn:
                conn.execute(
                    """UPDATE resale_sample SET status=?,baseline=?,reviewed=?,display=?,
                    elapsed_ms=?,cover_present=? WHERE item_id=?""",
                    (
                        path,
                        encoded(baseline),
                        encoded(review),
                        alert_line(review),
                        elapsed,
                        bool(cover),
                        identity,
                    ),
                )
            from resale_telegram import publish

            publish(identity, alert_line(review))
            logger.info(
                "Resale trial observation source=%s path=%s verdict=%s elapsed_ms=%d",
                target["source"],
                path,
                review["verdict"],
                elapsed,
            )
        except (OSError, ValueError, sqlite3.Error) as exc:
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
    if not (market_token and model_token):
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
            resale_telegram.restore_lines(
                conn.execute(
                    "SELECT item_id,display FROM resale_sample WHERE status!='running' ORDER BY sampled DESC LIMIT ?",
                    (LIMIT,),
                )
            )
            if (
                conn.execute(
                    "SELECT COUNT(*) FROM resale_sample WHERE status!='running'"
                ).fetchone()[0]
                >= LIMIT
            ):
                resale_telegram.finish_sample()
                return
            recent = deque(
                await asyncio.to_thread(export_alerts, database, LIMIT), maxlen=LIMIT
            )
            async with httpx.AsyncClient(
                follow_redirects=False, trust_env=False, timeout=4
            ) as client:
                refresh_needed = asyncio.Event()

                # Keep up to 50 paid lookups for the first pilot. Persist the allowance across restarts.
                async def refresh():
                    while True:
                        await refresh_needed.wait()
                        refresh_needed.clear()
                        used = conn.execute(
                            "SELECT COALESCE(MAX(used),0) FROM pilot_search_budget"
                        ).fetchone()[0]
                        if used >= 50:
                            return
                        await warm(
                            list(recent),
                            conn,
                            client,
                            market_token,
                            min(5, 50 - used),
                            pilot_limit=50,
                        )
                        await asyncio.sleep(15)

                receipts = asyncio.Queue(maxsize=16)
                _receipts = receipts
                refill = asyncio.create_task(refresh())
                try:
                    logger.info(
                        "Resale trial ready for accepted Home and saved-search alerts"
                    )
                    await sample_loop(
                        receipts,
                        database,
                        conn,
                        client,
                        model_token,
                        recent,
                        refresh_needed,
                        market_token,
                    )
                    logger.info(
                        "Resale trial complete: %s",
                        json.dumps(sample_summary(conn), sort_keys=True),
                    )
                finally:
                    _receipts = None
                    resale_telegram.finish_sample()
                    refill.cancel()
                    await asyncio.gather(refill, return_exceptions=True)
    except (OSError, ValueError, sqlite3.Error, httpx.HTTPError) as exc:
        logger.warning("Resale trial unavailable: %s", type(exc).__name__)


def sample_summary(conn):
    rows = conn.execute("""SELECT source,status,reviewed,elapsed_ms FROM resale_sample
        WHERE status!='running' ORDER BY elapsed_ms""").fetchall()
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
