"""Follow an already submitted payment and notify the owner from durable evidence."""

import asyncio
import time
from contextlib import closing

import finder_incidents as incidents
from logger import get_logger
from search_settings import connection

logger = get_logger(__name__)
_listener = None
WINDOW = 180
FOLLOW_UP = 3600


def wake():
    if _listener:
        loop, event = _listener
        if not loop.is_closed():
            loop.call_soon_threadsafe(event.set)


def register(conn, item_id, title, payment_due):
    now = time.time()
    conn.execute(
        """INSERT INTO vinted_payment_watch(item_id,checkout_id,title,payment_due,started,next_check)
           SELECT item_id,checkout_id,?,?,?,? FROM vinted_buy_attempts
           WHERE item_id=? AND checkout_id IS NOT NULL AND buyer_id IS NOT NULL AND transaction_id IS NOT NULL
           ON CONFLICT(item_id) DO NOTHING""",
        (str(title or f"Vinted item {item_id}")[:160], payment_due, now, now, item_id),
    )


def advance(now):
    """Lease one status read, and commit owner notifications alongside the watch state."""
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT w.*,a.state,a.action_url IS NOT NULL AS has_link FROM vinted_payment_watch w
               LEFT JOIN vinted_buy_attempts a ON a.item_id=w.item_id AND a.checkout_id=w.checkout_id
               ORDER BY w.next_check LIMIT 1"""
        ).fetchone()
        if not row:
            return 45, None
        if row["next_check"] > now:
            return row["next_check"] - now, None
        item, checkout, state = row["item_id"], row["checkout_id"], row["state"]
        age = now - row["started"]
        finished = state not in ("paying", "needs_action", "unknown") or age >= FOLLOW_UP
        kind = text = None
        if state == "paid":
            kind, text = "paid", "Vinted confirmed your order is paid."
        elif state == "payment_failed":
            kind, text = "failed", "Vinted reported that this payment failed."
        elif state in ("paying", "needs_action", "unknown"):
            if age >= FOLLOW_UP:
                kind, text = "unconfirmed", "The automatic status checks ended without a final result. Check this order in Vinted Purchases before buying again."
                if state == "paying":
                    conn.execute(
                        "UPDATE vinted_buy_attempts SET state='unknown',message=?,updated=? WHERE item_id=? AND checkout_id=? AND state='paying'",
                        (text, now, item, checkout),
                    )
            elif state == "needs_action" or (row["has_link"] and age >= 5):
                kind = "action" if state == "needs_action" else "window"
                amount = f"£{row['payment_due'] / 100:.2f}"
                text = (f"Open the payment check for {amount}. If Nationwide asks, approve that amount in its app."
                        if kind == "action" else
                        f"Vinted still has a payment window for {amount}. Open it so the check can run. It may finish without an app approval.")
        if kind and kind != row["notice"]:
            key = f"payment_{item}_{checkout}_{kind}"
            incidents.enqueue(conn, key, "autobuy", f"{row['title']}\n{text}", now, target="buyer")
            event = incidents.read(conn, incidents.PREFIX + key, {})
            event["payment_watch"] = [item, checkout, kind]
            incidents.write(conn, incidents.PREFIX + key, event)
        if finished:
            conn.execute("DELETE FROM vinted_payment_watch WHERE item_id=?", (item,))
            return 0, None
        delay = 2 if age < 10 else (6 if age < 40 else 15)
        if age >= WINDOW:
            delay = 120
        conn.execute(
            "UPDATE vinted_payment_watch SET next_check=?,notice=? WHERE item_id=?",
            (min(now + delay, row["started"] + FOLLOW_UP), kind or row["notice"], item),
        )
        return 0, item


def browser_checkout(now):
    """Return only a validated Vinted checkout ID for the current buyer's pending window."""
    import re

    with closing(connection()) as conn:
        row = conn.execute(
            """SELECT w.checkout_id FROM vinted_payment_watch w
               JOIN vinted_buy_attempts a ON a.item_id=w.item_id AND a.checkout_id=w.checkout_id
               JOIN vinted_buyer b ON b.id=1 AND b.user_id=a.buyer_id AND b.enabled=1
               WHERE w.started BETWEEN ? AND ? AND (a.state='needs_action'
                   OR (a.state='paying' AND a.action_url IS NOT NULL AND w.started<=?))
               ORDER BY w.started DESC LIMIT 1""", (now - WINDOW, now, now - 2),
        ).fetchone()
    return row[0] if row and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", row[0]) else None


def delivery_button(item, checkout, kind):
    """Resolve the current private link at send time and discard obsolete prompts."""
    import vinted_buying as buying
    from vinted_buyer import BASE
    from urllib.parse import urlencode

    saved = buying.result(item)
    if not saved or saved["checkout_id"] != checkout:
        return None
    states = {"action": ("needs_action",), "window": ("paying",), "paid": ("paid",),
              "failed": ("payment_failed",), "unconfirmed": ("unknown", "needs_action")}
    if saved["state"] not in states[kind]:
        return None
    url = BASE + "/checkout?" + urlencode({"purchase_id": checkout})
    if kind in ("window", "action"):
        action = buying.payment_action_url({"action": {"parameters": {"url": saved["action_url"]}}})
        if kind == "window" and not action:
            return None
        return "Open original payment check", action or url
    return "View original checkout", url


async def run():
    global _listener
    import vinted_buying as buying

    event = asyncio.Event()
    _listener = asyncio.get_running_loop(), event
    first_check = True
    try:
        while True:
            event.clear()
            try:
                delay, item = await asyncio.to_thread(advance, time.time())
                if first_check:
                    logger.info("Autobuy payment follow-up ready: database verified")
                    first_check = False
                if delay == 0:
                    if incidents._wake is not None:
                        incidents._wake.set()
                    if item:
                        await asyncio.to_thread(buying.check_payment, item)
                    continue
            except Exception as exc:
                logger.info("Existing payment status check deferred: %s", type(exc).__name__)
                delay = 2
            try:
                await asyncio.wait_for(event.wait(), timeout=delay)
            except TimeoutError:
                pass
    finally:
        _listener = None
