"""One authorised, one-hour valuation test on the November 27 shared group's alerts."""

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)
SERVICE = "83da7f26-68a6-4f1e-b107-1d6dadcf755e"
ENVIRONMENT = "4b1e4100-2a9f-47ab-a0fd-02fc1907dd8b"
EARLIEST = datetime(2026, 11, 27, 19, 48, tzinfo=UTC).timestamp()
LAST_START = datetime(2026, 11, 27, 20, 10, tzinfo=UTC).timestamp()
DURATION = 3600
_window_end = 0.0
_pending = None
_runner = None
_accepted_at = 0.0
_publishers = set()
_publish_lock = None
_notice_pending = False
_notice_done = False
_pool_end = 0.0
_last_window_read = 0.0


def audit_path():
    import db

    return Path(db.DB_PATH).with_name("shared-valuation-20261127.sqlite3")


def eligible_service():
    return (
        os.getenv("RAILWAY_SERVICE_ID") == SERVICE and os.getenv("RAILWAY_ENVIRONMENT_ID") == ENVIRONMENT
    )


def capture_active():
    now = time.time()
    if not eligible_service() or not EARLIEST <= now < LAST_START + DURATION:
        return False
    if _window_end:
        return now < _window_end
    if now <= LAST_START:
        return True
    # Discovery can run separately from the Telegram worker.
    return now < _read_end()


def _read_end():
    global _window_end, _last_window_read
    now = time.time()
    if now < _last_window_read + 5:
        return _window_end
    _last_window_read = now
    try:
        with closing(
            sqlite3.connect(audit_path().resolve().as_uri() + "?mode=ro", uri=True, timeout=0.15)
        ) as conn:
            row = conn.execute("SELECT ends FROM hour_window WHERE id=1").fetchone()
            _window_end = row[0] if row else -1
    except sqlite3.Error:
        return 0
    return _window_end


class Counters:
    def __init__(self, path, loop=None):
        self.path = path
        self.loop = loop

    def rough(self, identity, rough):
        import trial_resale_button

        partial = {"status": "evaluating", "rough": rough}
        if not trial_resale_button.photo_range(partial):
            return
        # Contains only the parsed pricing answer, never an image, URL or request payload.
        if _write(
            self.path,
            "UPDATE hour_alerts SET result=? WHERE item_id=? AND status='evaluating' "
            "AND EXISTS (SELECT 1 FROM hour_window WHERE ends>?)",
            (json.dumps(partial, ensure_ascii=False), str(identity), time.time()),
        ):
            log.info(
                "Finder hourly valuation photo ready item=%s low=%s high=%s ask=%s provider_ms=%s",
                identity,
                rough["low_pence"],
                rough["high_pence"],
                rough.get("quick_pence"),
                rough.get("provider_ms"),
            )
            if self.loop:
                self.loop.call_soon_threadsafe(trial_resale_button.remember, str(identity), partial)

    def reserve(self, kind, limit):
        hard_limits = {"market": 2, "model": 3, "coverage_notice": 1}
        if kind not in hard_limits:
            return False
        limit = min(limit, hard_limits[kind])
        with closing(sqlite3.connect(self.path, timeout=0.5)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM hour_window WHERE ends>?", (time.time(),)).fetchone():
                return False
            conn.execute("INSERT OR IGNORE INTO hour_counters VALUES(?,0)", (kind,))
            return bool(
                conn.execute(
                    "UPDATE hour_counters SET used=used+1 WHERE kind=? AND used<?", (kind, limit)
                ).rowcount
            )


def _start(path, accepted, version):
    with closing(sqlite3.connect(path, timeout=1)) as conn, conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS hour_window (
              id INTEGER PRIMARY KEY, started REAL NOT NULL, ends REAL NOT NULL, group_version TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS hour_counters (kind TEXT PRIMARY KEY, used INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS hour_alerts (
              item_id TEXT PRIMARY KEY, accepted REAL NOT NULL, source TEXT NOT NULL,
              parent_message INTEGER NOT NULL, status TEXT NOT NULL, result TEXT, ready REAL,
              reply_message INTEGER, sent REAL, publish_state TEXT);
            CREATE TABLE IF NOT EXISTS hour_numeric_cards (item_id TEXT PRIMARY KEY, shown REAL NOT NULL, label TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS hour_cooldown (id INTEGER PRIMARY KEY, next_send REAL NOT NULL);
        """)
        created = False
        if EARLIEST <= accepted <= LAST_START:
            created = conn.execute(
                "INSERT OR IGNORE INTO hour_window VALUES(1,?,?,?)",
                (accepted, accepted + DURATION, version),
            ).rowcount
        row = conn.execute("SELECT started,ends,group_version FROM hour_window WHERE id=1").fetchone()
        if created:
            log.info(
                "Finder hourly valuation start utc=%s ends=%s", _utc(accepted), _utc(accepted + DURATION)
            )
        return row


def _utc(stamp):
    return datetime.fromtimestamp(stamp, UTC).isoformat(timespec="seconds")


def _write(path, query, values):
    with closing(sqlite3.connect(path, timeout=1)) as conn, conn:
        return conn.execute(query, values).rowcount


def accepted(bot, group, identity, source, message_id):
    """Queue only a completed shared delivery, without I/O on Finder's alert path."""
    global _pending, _accepted_at
    if not eligible_service():
        return
    now = time.time()
    if not EARLIEST <= now < (_window_end or LAST_START + DURATION):
        return
    _accepted_at = now
    if _pending is None:
        _pending = asyncio.Queue(maxsize=128)
    try:
        _pending.put_nowait((bot, group, str(identity), source, message_id, now))
    except asyncio.QueueFull:
        log.warning("Finder hourly valuation skipped item=%s reason=queue_full", identity)
        return
    if _runner is None or _runner.done():
        _schedule_runner()


def _schedule_runner():
    global _runner
    _runner = asyncio.create_task(_run(_pending))
    _runner.add_done_callback(_runner_done)


def _runner_done(task):
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.warning("Finder hourly valuation worker stopped reason=%s", type(error).__name__)
    elif _runner is task and _pending is not None and not _pending.empty() and capture_active():
        _schedule_runner()


PUBLIC_ITEM = re.compile(r"https://(?:www\.)?vinted\.co\.uk/items/([0-9]+)(?:-[a-z0-9%-]+)?")


def _diagnostics(result):
    fields = (
        "had_target_seller",
        "had_target_condition",
        "observed_listing_count",
        "candidate_count",
        "market_calls",
        "model_calls",
    )
    return tuple(int(result[field]) if result.get(field) is not None else 0 for field in fields)


def _reason_codes(result):
    # The pricing rules own these exclusion labels; raw listings never enter log messages.
    common = sorted(result.get("exclusions", {}).items(), key=lambda entry: -entry[1])[:2]
    return ",".join(re.sub(r"[^a-z]+", "_", reason.lower()).strip("_") for reason, _ in common) or "none"


def _quote(result):
    price = result.get("resale_pence")
    if type(price) is not int or price < 100:
        return None
    examples = []
    for listing in result.get("evidence") or ():
        url = str(listing.get("url") or "")
        asking = listing.get("ask_pence")
        if (
            PUBLIC_ITEM.fullmatch(url)
            and type(asking) is int
            and asking > 0
            and url not in {u for _, u in examples}
        ):
            examples.append((asking, url))
        if len(examples) == 2:
            break
    if len(examples) < 2:
        return None
    lines = [f"Trial quick-sale estimate: £{price / 100:.2f}", "Comparable UK asking prices:"]
    lines.extend(f"£{p / 100:.2f} · {url}" for p, url in examples)
    lines.append("Reply here with high, fair, low, or the price you’d list it for (e.g. £20).")
    return "\n".join(lines)


def _evaluate_one(identity, database, counters, end):
    # All market cache and pricing operations run off the original alert event loop.
    import httpx

    from trial_market_hour import evaluate

    async def run():
        async with asyncio.timeout(min(35, max(0, end - time.time()))):
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                return await evaluate(identity, database, client, counters)

    return asyncio.run(run())


def _retained_photo(path, identity):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)) as conn:
        row = conn.execute(
            "SELECT result FROM hour_alerts WHERE item_id=? AND status='evaluating'", (identity,)
        ).fetchone()
    if not row or not row[0]:
        return None
    import trial_resale_button

    result = json.loads(row[0])
    if not trial_resale_button.photo_range(result):
        return None
    return dict(
        result,
        status="insufficient",
        resale_pence=None,
        evidence=[],
        market_status="check_failed",
        explanation="The photo estimate is available; the market check did not finish.",
    )


def _hold(path, until=None):
    with closing(sqlite3.connect(path, timeout=1)) as conn, conn:
        if until is not None:
            conn.execute(
                "INSERT INTO hour_cooldown VALUES(1,?) ON CONFLICT(id) "
                "DO UPDATE SET next_send=max(next_send,excluded.next_send)",
                (until,),
            )
        row = conn.execute("SELECT next_send FROM hour_cooldown WHERE id=1").fetchone()
        return row[0] if row else 0


async def card_slot():
    """Reserve update capacity without delaying an initial Telegram alert."""
    global _publish_lock
    if _publish_lock is None:
        _publish_lock = asyncio.Lock()
    async with _publish_lock:
        quiet = await asyncio.to_thread(_hold, audit_path())
        limit = min(time.time() + 12, _window_end)
        while time.time() < limit:
            now = time.time()
            send_after = max(quiet, _accepted_at + 3.1)
            if send_after <= now:
                await asyncio.to_thread(_hold, audit_path(), now + 3.2)
                return True
            await asyncio.sleep(max(0, min(send_after, limit) - now))
    return False


async def _mark(path, identity, state):
    await asyncio.to_thread(
        _write, path, "UPDATE hour_alerts SET publish_state=? WHERE item_id=?", (state, identity)
    )
    log.info("Finder hourly valuation reply item=%s state=%s", identity, state)


async def _publish(
    path,
    counters,
    bot,
    group,
    identity,
    parent,
    received,
    text,
    quote,
    *,
    is_photo=False,
    has_external=False,
):
    from telegram import LinkPreviewOptions
    from telegram.error import RetryAfter, TelegramError

    import telegram_shared_alerts as shared

    global _notice_done, _notice_pending
    try:
        if is_photo and not has_external:
            import trial_resale_button

            if await trial_resale_button.visible_on_card(identity):
                await _mark(path, identity, "numeric_card_accepted")
                return
        async with _publish_lock:
            now = time.time()
            quiet = await asyncio.to_thread(_hold, path)
            latest = min(_window_end, received + 90)
            while now < latest:
                wait_until = max(quiet, _accepted_at + 3.1)
                if now >= wait_until:
                    break
                await asyncio.sleep(max(0, min(wait_until, latest) - now))
                now = time.time()
            if now >= latest:
                await _mark(path, identity, "skipped_busy_or_expired")
                return
            if await asyncio.to_thread(shared.current) != group:
                await _mark(path, identity, "skipped_group_changed")
                return
            if quote is None and not (is_photo or has_external):
                if not await asyncio.to_thread(counters.reserve, "coverage_notice", 1):
                    _notice_done = True
                    return
                _notice_done = True
            await asyncio.to_thread(_hold, path, time.time() + 6)
            if time.time() >= _window_end:
                await _mark(path, identity, "skipped_expired")
                return
            attempted = time.time()
            try:
                response = await bot.send_message(
                    chat_id=group.chat,
                    text=text,
                    reply_to_message_id=parent,
                    allow_sending_without_reply=False,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    read_timeout=5,
                    write_timeout=4,
                    connect_timeout=3,
                    pool_timeout=2,
                )
            except RetryAfter as exc:
                retry = exc.retry_after
                seconds = retry.total_seconds() if hasattr(retry, "total_seconds") else retry
                await asyncio.to_thread(_hold, path, time.time() + seconds + 1)
                await _mark(path, identity, "throttled_unconfirmed")
                return
            except TelegramError:
                await _mark(path, identity, "telegram_unconfirmed")
                return
            sent = time.time()
            await asyncio.to_thread(
                _write,
                path,
                "UPDATE hour_alerts SET reply_message=?,sent=?,publish_state='accepted' WHERE item_id=?",
                (response.message_id, sent, identity),
            )
            log.info(
                "Finder hourly valuation shared reply accepted item=%s quote=%s elapsed_ms=%s telegram_ms=%s utc=%s photo_only=%s",
                identity,
                quote,
                round((sent - received) * 1000),
                round((sent - attempted) * 1000),
                _utc(sent),
                bool(is_photo),
            )
    except Exception as exc:  # noqa: BLE001 -- isolate the optional reply from live alerts.
        log.warning(
            "Finder hourly valuation reply failed item=%s reason=%s", identity, type(exc).__name__
        )
    finally:
        if quote is None and not (is_photo or has_external):
            _notice_pending = False


async def _run(queue):
    import db

    global _window_end, _pool_end, _publish_lock, _notice_pending
    path = audit_path()
    counters = Counters(path, asyncio.get_running_loop())
    window = None
    while not queue.empty():
        bot, group, identity, source, parent, accepted_at = queue.get_nowait()
        try:
            if window is None:
                window = await asyncio.to_thread(_start, path, accepted_at, group.version)
            if not window:
                _window_end = -1
                return
            start, _window_end, version = window
            if _pool_end != _window_end and time.time() < _window_end:
                from trial_public_pool import activate

                try:
                    await asyncio.to_thread(activate, db.DB_PATH, _window_end)
                    _pool_end = _window_end
                except (OSError, sqlite3.Error) as exc:
                    log.warning(
                        "Finder hourly valuation public cache unavailable reason=%s", type(exc).__name__
                    )
            if not start <= accepted_at < _window_end or group.version != version:
                continue
            import trial_wrong_price

            trial_wrong_price.schedule(bot, group, path)
            inserted = await asyncio.to_thread(
                _write,
                path,
                "INSERT OR IGNORE INTO hour_alerts(item_id,accepted,source,parent_message,status) "
                "VALUES(?,?,?,?,'evaluating')",
                (identity, accepted_at, source, parent),
            )
            if not inserted:
                continue
            if time.time() >= _window_end:
                result = {"status": "expired"}
            else:
                try:
                    result = await asyncio.to_thread(
                        _evaluate_one, identity, db.DB_PATH, counters, _window_end
                    )
                except Exception as exc:
                    result = await asyncio.to_thread(_retained_photo, path, identity)
                    if result is None:
                        raise
                    log.warning(
                        "Finder hourly valuation market unfinished item=%s reason=%s",
                        identity,
                        type(exc).__name__,
                    )
            ready = time.time()
            state = result.get("status", "insufficient")
            stage = result.get("rough") or {}
            research = result.get("cross_market") or {}
            outcomes = result.get("sources") or {}
            if outcomes or stage:
                sites = ("vinted_uk", "depop_uk", "ebay_uk_active", "ebay_uk_sold")
                counts = [
                    f"{site}:{outcomes[site].get('status')}:{outcomes[site].get('count', 0)}"
                    for site in sites if site in outcomes
                ]
                log.info(
                    "Finder hourly valuation research item=%s photo=%s provider_ms=%s "
                    "guide=%s low=%s high=%s lookups=%s sources=%s",
                    identity, stage.get("photo_status"), stage.get("provider_ms"),
                    research.get("status"), research.get("low_pence"), research.get("high_pence"),
                    result.get("public_market_requests", 0), ",".join(counts) or "none",
                )
            await asyncio.to_thread(
                _write,
                path,
                "UPDATE hour_alerts SET status=?,result=?,ready=? WHERE item_id=?",
                (state, json.dumps(result, ensure_ascii=False), ready, identity),
            )
            # A subsequent normal Telegram keyboard refresh can display the finished price.
            import trial_resale_button

            trial_resale_button.remember(identity, result)
            price = result.get("resale_pence")
            evidence_ids = ",".join(
                m.group(1)
                for entry in (result.get("evidence") or ())[:5]
                if (m := PUBLIC_ITEM.fullmatch(str(entry.get("url") or "")))
            )
            log.info(
                "Finder hourly valuation result item=%s source=%s status=%s ask_pence=%s "
                "baseline=%s evidence=%s evidence_ids=%s market=%s model=%s ready_ms=%s "
                "target_seller=%s target_condition=%s pool_rows=%s eligible=%s market_calls=%s model_calls=%s reasons=%s",
                identity,
                source,
                state,
                price,
                result.get("baseline_pence"),
                len(result.get("evidence") or []),
                evidence_ids or "none",
                result.get("market_status"),
                result.get("model_status"),
                round((ready - accepted_at) * 1000),
                *_diagnostics(result),
                _reason_codes(result),
            )
            if ready >= _window_end or state not in ("estimated", "insufficient"):
                continue
            text = _quote(result) if state == "estimated" else None
            rough = trial_resale_button.photo_range(result)
            external = trial_resale_button.external_range(result)
            external_lines = trial_resale_button.external_lines(result)
            is_photo = bool(rough and not text and not external)
            if rough:
                if text:
                    text = f"Photo estimate {rough}.\n" + text.replace(
                        "Trial quick-sale estimate:", "Market-checked quick-sale ask:", 1
                    )
                else:
                    text = (
                        trial_resale_button.brief(state, result)
                        + "\nReply here with high, fair, low, or the price you’d list it for."
                    )
            if external_lines:
                if not text:
                    text = "Finder has no confirmed Vinted price for this listing."
                if external and f"Cross-market guide {external}" in text:
                    external_lines = external_lines[1:]
                text += "\n" + "\n".join(external_lines)
            if not text and (_notice_done or _notice_pending):
                continue
            if len(_publishers) >= 24:
                await _mark(path, identity, "skipped_reply_backlog")
                continue
            quote = price if text and not is_photo else None
            if not text:
                text = (
                    "The one-hour resale test is running. Finder could not verify a price for this listing. "
                    "Reply here with what you’d list it for, e.g. “I’d list £20”."
                )
                _notice_pending = True
            if _publish_lock is None:
                _publish_lock = asyncio.Lock()
            task = asyncio.create_task(
                _publish(
                    path,
                    counters,
                    bot,
                    group,
                    identity,
                    parent,
                    accepted_at,
                    text,
                    quote,
                    is_photo=is_photo,
                    has_external=bool(external_lines),
                )
            )
            _publishers.add(task)
            task.add_done_callback(_publishers.discard)
        except Exception as exc:  # noqa: BLE001 -- preserve the next item if this valuation fails.
            log.warning("Finder hourly valuation failed item=%s reason=%s", identity, type(exc).__name__)
            try:
                await asyncio.to_thread(
                    _write,
                    path,
                    "UPDATE hour_alerts SET status='failed' WHERE item_id=? AND status='evaluating'",
                    (identity,),
                )
            except sqlite3.Error:
                pass
        finally:
            queue.task_done()
