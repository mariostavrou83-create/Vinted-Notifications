"""Review accepted listing alerts separately from delivery, retaining the first 100 as a sample."""

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
from resale_pricing import (
    cache_guide,
    forget_reference,
    market_key,
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
        forget_reference(identity)
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS resale_sample_time ON resale_sample(sampled)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS resale_trial_report (id INTEGER PRIMARY KEY, summary TEXT NOT NULL)"
    )
    conn.commit()


async def load_guides(conn, database, since):
    rows = conn.execute(
        "SELECT query,fetched_at,listings FROM market WHERE fetched_at>=?", (since,)
    ).fetchall()
    for group, observed, items in rows:
        try:
            independent = await asyncio.to_thread(
                independent_items, database, json.loads(items)
            )
            cache_guide(tuple(json.loads(group)), independent, observed)
        except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            logger.info(
                "Saved resale prices unavailable for one group: %s", type(exc).__name__
            )


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
    recent,
    refresh_needed,
    market_token=None,
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
            recent.append(target)
            key = market_key(target)
            entries = cached_listings(conn, key, time.time())
            older_snapshot = False
            if entries is None and key and market_token:
                refresh_needed.set()
            if entries is None and key:
                entries = cached_listings(conn, key, time.time(), max_age=7 * 86400)
                older_snapshot = bool(entries)
            rows = await asyncio.to_thread(independent_items, database, entries or [])
            candidates = shortlist(target, rows, time.time())
            baseline, review, path = await evaluate_one(
                target, candidates, conn, client, token, cover_bytes=cover
            )
            if older_snapshot and review.get("comparisons"):
                review = dict(review, verdict="Not confident", basis="saved")
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
                "Resale trial observation source=%s path=%s verdict=%s elapsed_ms=%d basis=%s priced=%s",
                target["source"],
                path,
                review["verdict"],
                elapsed,
                review.get("basis", "comparisons"),
                review.get("resale_pence") is not None,
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
                AND json_type(reviewed,'$.resale_pence')='integer' ORDER BY sampled DESC LIMIT ?""",
                (LIMIT,),
            )
            resale_telegram.restore_lines(
                (identity, valuation_text(json.loads(data)))
                for identity, data in recent_reviews
            )
            recent = deque(maxlen=32)
            async with httpx.AsyncClient(
                follow_redirects=False, trust_env=False, timeout=4
            ) as client:
                refresh_needed = asyncio.Event()

                # Keep up to 50 paid lookups for the first pilot. Persist the allowance across restarts.
                async def refresh():
                    await load_guides(conn, database, time.time() - 7 * 86400)
                    while True:
                        await refresh_needed.wait()
                        refresh_needed.clear()
                        used = conn.execute(
                            "SELECT COALESCE(MAX(used),0) FROM pilot_search_budget"
                        ).fetchone()[0]
                        if used >= 50:
                            return
                        # Spend a limited lookup only on a garment that just triggered an alert.
                        started = time.time()
                        outcome = await warm(
                            list(reversed(recent)),
                            conn,
                            client,
                            market_token,
                            1,
                            pilot_limit=50,
                        )
                        if outcome["market_groups_refreshed"]:
                            await load_guides(conn, database, started)
                        await asyncio.sleep(15)

                receipts = asyncio.Queue()
                _receipts = receipts
                refill = asyncio.create_task(refresh())
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
                                recent,
                                refresh_needed,
                                market_token,
                                continuous=True,
                            )
                        )
                        for _ in range(2)
                    ]
                    await asyncio.gather(*workers)
                finally:
                    _receipts = None
                    resale_telegram.finish_sample()
                    for task in [refill, *workers]:
                        task.cancel()
                    await asyncio.gather(refill, *workers, return_exceptions=True)
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
