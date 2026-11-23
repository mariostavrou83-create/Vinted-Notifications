"""Owner notifications from existing Finder evidence. Makes no marketplace requests."""

import asyncio
import hashlib
import json
import logging
import math
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TelegramError

from search_settings import connection
from vinted_connection_notices import ConnectionNotifier, dashboard_url

logger = logging.getLogger(__name__)
STATE = "finder_operations_health_v1"
PREFIX = "finder_operations_event_"
_wake = None
HOME_ERRORS = {
    "credentials": "Vinted rejected the saved sign-in",
    "account_changed": "the account identity changed",
    "security_challenge": "Vinted requires account verification",
    "rate_limited": "Vinted requested a cooldown",
    "network": "the connection to Vinted failed",
    "source_unavailable": "the personalised page could not be read",
    "account_unavailable": "the account is disconnected",
    "key_missing": "Finder cannot read the saved connection",
    "not_separated": "the buying and original account connections are unavailable",
    "saved_session": "the saved connection is unavailable",
    "session_refresh": "the account needs a session refresh",
    "error": "Finder encountered a Home processing error",
    "response_too_large": "the response exceeded the read limit",
    "response_time_unverified": "the response time could not be verified",
}
FAILURE_STATES = {
    "setup_required",
    "failed_before_payment",
    "payment_failed",
    "unknown",
    "needs_action",
}


def uk(stamp):
    return datetime.fromtimestamp(stamp, ZoneInfo("Europe/London")).strftime(
        "%H:%M:%S UK"
    )


def read(conn, key, default):
    row = conn.execute("SELECT value FROM parameters WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def write(conn, key, data):
    conn.execute(
        "INSERT INTO parameters(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(data, separators=(",", ":"), ensure_ascii=False)),
    )


def enqueue(conn, key, kind, message, now, *, target="alerts"):
    value = {"kind": kind, "text": message, "at": now, "due": now, "target": target}
    conn.execute(
        "INSERT OR IGNORE INTO parameters(key,value) VALUES(?,?)",
        (PREFIX + key, json.dumps(value, ensure_ascii=False)),
    )


def buy_failure(row, outcome, callback_id):
    """A separate push for each failed button action, including before checkout exists."""
    if outcome.get("state") not in FAILURE_STATES or not callback_id:
        return
    reason = outcome.get("reason") or ""
    key = hashlib.sha256(str(callback_id).encode()).hexdigest()
    status = {
        "failed_before_payment": "Autobuy stopped before payment",
        "setup_required": "Autobuy needs attention",
        "payment_failed": "Autobuy payment failed",
        "unknown": "Autobuy payment status is unconfirmed",
        "needs_action": "Autobuy needs your payment confirmation",
    }[outcome["state"]]
    message = f"{status}\n{str(row.get('title') or 'Vinted listing')[:160]} · item {row['item_id']}\n{str(outcome.get('message') or 'Open the original alert for details.')[:650]}"
    target = (
        "reconnect"
        if reason
        in {
            "credentials",
            "not_connected",
            "renewal_blocked",
            "renewal_cooldown",
            "refresh_rejected",
        }
        else "buyer"
    )
    try:
        with closing(connection()) as conn, conn:
            enqueue(conn, key, "autobuy", message, time.time(), target=target)
    except (sqlite3.Error, OSError, ValueError) as exc:
        logger.warning("Autobuy warning could not be queued: %s", type(exc).__name__)


async def queue_buy_failure(row, outcome, callback_id):
    if outcome.get("state") in FAILURE_STATES:
        await asyncio.to_thread(buy_failure, row, outcome, callback_id)
        if _wake is not None:
            _wake.set()


def observe(conn, now, started):
    """None means there is not enough evidence to open or resolve that incident."""
    signals = {}
    home = conn.execute(
        "SELECT value FROM parameters WHERE key='home_monitor_enabled'"
    ).fetchone()
    if home and home[0] == "1":
        page = conn.execute(
            "SELECT fetched_at FROM vinted_home_pages WHERE role='seller'"
        ).fetchone()
        last_page = page[0] if page else None
        row = read(conn, "home_monitor_status_seller", {})
        code, checked = row.get("code"), row.get("checked_at") or 0
        if code in HOME_ERRORS and checked > 0 and checked >= (last_page or 0):
            status = row.get("http_status")
            detail = HOME_ERRORS[code] + (f" (HTTP {status})" if status else "")
            signals["MSJ Vinted Home"] = (code, f"{detail} at {uk(checked)}.", "alerts")
        elif last_page and now - last_page < 300 and code == "checked":
            signals["MSJ Vinted Home"] = (
                "",
                f"Vinted returned the personalised page at {uk(last_page)}.",
                "alerts",
            )
        elif now - (last_page or started) >= 300 and now - started >= 60:
            detail = "No successful page refresh for five minutes." + (
                f" Last page at {uk(last_page)}." if last_page else ""
            )
            signals["MSJ Vinted Home"] = ("stalled", detail, "alerts")
        # A readable Home feed can still produce verified-card alerts while
        # individual listing pages are unavailable. Track their recovery separately.
        retry = row.get("detail_retry_at")
        last_ok = row.get("last_detail_ok")
        failed_at = row.get("last_detail_failure")
        known_ok = type(last_ok) in (int, float) and 0 < last_ok <= now
        if (type(retry) in (int, float) and type(failed_at) in (int, float)
                and 0 < failed_at <= now and 120 <= retry - failed_at <= 300):
            detail = f"Individual listing checks repeatedly failed, most recently at {uk(failed_at)}."
            if known_ok:
                detail += f" Last successful listing check at {uk(last_ok)}."
            signals["MSJ Home listing checks"] = ("unavailable", detail, "alerts")
        elif retry == 0 and known_ok:
            signals["MSJ Home listing checks"] = (
                "", f"An individual listing check succeeded at {uk(last_ok)}.", "alerts",
            )
    account = conn.execute("""SELECT b.enabled, b.user_id IS NOT NULL AND b.session IS NOT NULL AS connected,
        b.verified_at,a.checked,a.reason,a.stage,a.http_status,n.checked AS rejected_at,n.identity_rejected
        FROM vinted_buyer b LEFT JOIN vinted_buyer_access a ON a.id=b.id
        LEFT JOIN vinted_connection_notice n ON n.id=b.id AND n.buyer_id=b.user_id WHERE b.id=1""").fetchone()
    if account and account["enabled"]:
        verified = account["verified_at"] or 0
        if not account["connected"]:
            signals["Autobuy account"] = (
                "disconnected",
                "Reconnect the buying account to use Autobuy.",
                "reconnect",
            )
        elif (
            account["identity_rejected"] and (account["rejected_at"] or 0) >= verified
        ) or (
            (account["checked"] or 0) >= verified
            and account["stage"] in {"identity", "renewal", "saved_session"}
            and (
                account["reason"]
                in {
                    "credentials",
                    "refresh_rejected",
                    "signin_redirect",
                    "account_changed",
                }
                or account["http_status"] == 401
            )
        ):
            signals["Autobuy account"] = (
                "credentials",
                "Vinted rejected the buying account sign-in. Reconnect before using Autobuy.",
                "reconnect",
            )
        elif verified and now - verified <= 300:
            signals["Autobuy account"] = (
                "",
                f"Buying account identity verified at {uk(verified)}.",
                "buyer",
            )
    active = conn.execute("""SELECT q.id,h.last_success,h.failures,h.last_attempt FROM queries q
        LEFT JOIN search_dashboard d ON d.query_id=q.id LEFT JOIN search_platforms p ON p.query_id=q.id
        LEFT JOIN search_health h ON h.query_id=q.id WHERE COALESCE(d.paused,0)=0 AND COALESCE(d.archived,0)=0
        AND COALESCE(p.vinted_enabled,1)=1 AND NOT EXISTS(SELECT 1 FROM vinted_home_guides g WHERE g.query_id=q.id)""").fetchall()
    latest = max((r[1] or 0 for r in active), default=0)
    if active:
        if latest and now - latest < 300:
            signals["Saved searches"] = (
                "",
                f"Successful search check at {uk(latest)}.",
                "alerts",
            )
            failing = [
                r[0]
                for r in active
                if (r[2] or 0) >= 3
                and r[3]
                and now - r[3] < 600
                and now - (r[1] or started) >= 300
            ]
            signals["Individual saved searches"] = (
                (
                    "errors:" + ",".join(map(str, sorted(failing))),
                    "Repeated check failures on "
                    + ", ".join("#" + str(x) for x in failing[:12])
                    + (f" and {len(failing) - 12} more." if len(failing) > 12 else "."),
                    "alerts",
                )
                if failing
                else (
                    "",
                    "Saved-search checks are running without a persistent failure.",
                    "alerts",
                )
            )
        elif now - started >= 300:
            signals["Saved searches"] = (
                "stalled",
                "No successful search check for five minutes."
                + (f" Last success at {uk(latest)}." if latest else ""),
                "alerts",
            )
    # Read only the recent listing window using its index, avoiding scans of long-term history.
    sent, failed = conn.execute(
        """SELECT MAX(CASE WHEN status='sent' THEN sent_at END),
        MAX(CASE WHEN status='failed' AND found_at>=? THEN found_at END) FROM alert_outbox INDEXED BY idx_outbox_found
        WHERE found_at>=? AND platform='vinted' """,
        (now - 1800, now - 86400),
    ).fetchone()
    pending = conn.execute(
        "SELECT COUNT(*),MIN(found_at) FROM alert_outbox WHERE platform='vinted' AND status='pending' AND (attempts>0 OR found_at<=?)",
        (now - 90,),
    ).fetchone()
    if pending[0] or failed:
        parts = []
        if pending[0]:
            noun = "alert has" if pending[0] == 1 else "alerts have"
            parts.append(
                f"{pending[0]} listing {noun} been delayed. The oldest was queued at {uk(pending[1])}."
            )
        if failed:
            record = conn.execute(
                "SELECT item_id,error FROM alert_outbox WHERE platform='vinted' AND status='failed' AND found_at=? LIMIT 1",
                (failed,),
            ).fetchone()
            if record:
                known = {
                    "BadRequest": "Telegram rejected the message",
                    "Forbidden": "Telegram denied delivery",
                    "NetworkError": "a network request failed",
                    "TimedOut": "a Telegram request timed out",
                }
                cause = known.get(record[1], "Telegram delivery failed")
                parts.append(f"Item {record[0]} queued at {uk(failed)}: {cause}.")
        signals["Telegram delivery"] = (
            "failed:" + str(failed) if failed else "delayed",
            " ".join(parts),
            "alerts",
        )
    elif sent and now - sent < 300:
        signals["Telegram delivery"] = (
            "",
            f"Telegram accepted a listing at {uk(sent)}.",
            "alerts",
        )
    if sent and now - sent < 1800:
        signals["30-minute listing update"] = (
            "",
            f"Telegram accepted a listing at {uk(sent)}.",
            "alerts",
        )
    elif (active or (home and home[0] == "1")) and now - (sent or started) >= 1800:
        recently_checked = any(
            not s[0]
            for key, s in signals.items()
            if key in ("Saved searches", "MSJ Vinted Home")
        )
        detail = (
            " Finder is still checking listings."
            if recently_checked
            else " Finder has no recent successful source check."
        )
        signals["30-minute listing update"] = (
            "quiet",
            "No confirmed Telegram listing delivery for 30 minutes."
            + (f" Last accepted at {uk(sent)}." if sent else "")
            + detail,
            "alerts",
        )
    return signals


def check_health(now):
    with closing(connection()) as conn:
        initial = read(conn, STATE, {})
        signals = observe(conn, now, initial.get("started", now))
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            state = read(conn, STATE, {"started": now, "signals": {}})
            last = state.get("checked")
            if last and last > now:
                return
            if last and now - last >= 300:
                enqueue(
                    conn,
                    uuid.uuid4().hex,
                    "service",
                    f"Finder checks resumed at {uk(now)}. Its warning worker had recorded no heartbeat since {uk(last)} ({int((now - last) // 60)} minutes).",
                    now,
                )
            for label, (code, detail, target) in signals.items():
                before = state["signals"].get(label, "")
                if before == code:
                    continue
                state["signals"][label] = code
                heading = label + (" · update" if code else " · recovered")
                enqueue(
                    conn,
                    uuid.uuid4().hex,
                    label,
                    f"{heading}\n{detail}",
                    now,
                    target=target,
                )
            state["checked"] = now
            write(conn, STATE, state)


def claim(now):
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        cooldown = conn.execute(
            "SELECT value FROM delivery_runtime WHERE key='cooldown_until'"
        ).fetchone()
        if cooldown and cooldown[0] > now:
            return None
        due = []
        for key, raw in conn.execute(
            "SELECT key,value FROM parameters WHERE key GLOB ?", (PREFIX + "*",)
        ):
            event = json.loads(raw)
            if event.get("kind") == "Buying account Home" and not event.get("sent"):
                conn.execute("DELETE FROM parameters WHERE key=?", (key,))
                continue
            if event.get("sent"):
                if now - event["sent"] > 86400:
                    conn.execute("DELETE FROM parameters WHERE key=?", (key,))
            elif event["due"] <= now:
                due.append((event["at"], key, event))
        if not due:
            return None
        _, key, event = min(due, key=lambda v: (v[0], v[1]))
        event.update(lease=uuid.uuid4().hex, due=now + 60)
        write(conn, key, event)
        return key, event


def finish(key, lease, now, *, delay=None, rate_limited=False):
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        event = read(conn, key, {})
        if event.get("lease") != lease:
            return
        if rate_limited and delay is not None:
            conn.execute(
                "INSERT INTO delivery_runtime VALUES('cooldown_until',?) ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)",
                (now + delay,),
            )
        if delay is None:
            event["sent"] = now
        else:
            event["due"] = now + delay
        event.pop("lease", None)
        write(conn, key, event)


def recent(conn):
    rows = (
        json.loads(r[0])
        for r in conn.execute(
            "SELECT value FROM parameters WHERE key GLOB ?", (PREFIX + "*",)
        )
    )
    return [
        {
            "text": event["text"],
            "time": uk(event["at"]),
            "sent": bool(event.get("sent")),
        }
        for event in sorted(rows, key=lambda e: e["at"], reverse=True)[:8]
    ]


def target_url(target):
    base = dashboard_url(reconnect=target == "reconnect")
    return (
        base.split("/connections#", 1)[0] + "/vinted-home"
        if base and target == "alerts"
        else base
    )


async def deliver(notifier):
    for _ in range(10):
        if not await asyncio.to_thread(notifier.recipient_valid):
            return
        claimed = await asyncio.to_thread(claim, time.time())
        if not claimed:
            return
        key, event = claimed
        try:
            url = target_url(event["target"])
            label = {
                "reconnect": "Reconnect buying account",
                "buyer": "Open buying account",
                "alerts": "Open Finder",
            }[event["target"]]
            markup = (
                InlineKeyboardMarkup([[InlineKeyboardButton(label, url=url)]])
                if url
                else None
            )
            sent = await notifier.bot.send_message(
                chat_id=notifier.chat_id,
                text=event["text"],
                reply_markup=markup,
                disable_web_page_preview=True,
                read_timeout=10,
                write_timeout=10,
                connect_timeout=10,
                pool_timeout=10,
            )
            await asyncio.to_thread(finish, key, event["lease"], time.time())
            logger.info(
                "Finder warning accepted: kind=%s message_id=%s",
                event["kind"],
                sent.message_id,
            )
        except (TelegramError, OSError, sqlite3.Error) as exc:
            delay = 30
            if isinstance(exc, RetryAfter):
                seconds = exc.retry_after
                seconds = (
                    seconds.total_seconds()
                    if hasattr(seconds, "total_seconds")
                    else float(seconds)
                )
                delay = max(delay, math.ceil(seconds) + 1)
            await asyncio.to_thread(
                finish,
                key,
                event["lease"],
                time.time(),
                delay=delay,
                rate_limited=isinstance(exc, RetryAfter),
            )
            logger.warning("Finder warning delivery deferred: %s", type(exc).__name__)
            return
        await asyncio.sleep(1.1)


async def run(bot, chat_id):
    global _wake
    notifier = ConnectionNotifier(bot, chat_id)
    wake = asyncio.Event()
    _wake = wake
    next_check = 0
    logger.info(
        "Finder incident notifications started: health_interval=45s quiet_interval=30m"
    )
    try:
        while True:
            wake.clear()
            try:
                if await asyncio.to_thread(notifier.recipient_valid):
                    if time.monotonic() >= next_check:
                        next_check = time.monotonic() + 45
                        try:
                            await asyncio.to_thread(check_health, time.time())
                        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
                            logger.warning(
                                "Finder health observation deferred: %s",
                                type(exc).__name__,
                            )
                    await deliver(notifier)
                else:
                    next_check = time.monotonic() + 45
            except Exception as exc:  # noqa: BLE001 -- keep the owner notification worker running
                logger.warning("Finder incident check deferred: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(
                    wake.wait(),
                    timeout=max(1, next_check - time.monotonic()) if next_check else 45,
                )
            except TimeoutError:
                pass
    finally:
        if _wake is wake:
            _wake = None
