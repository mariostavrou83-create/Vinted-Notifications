"""Read buyer-bound native checkout quotes and purchase receipts."""

import json
from datetime import UTC, datetime

from .pricing import pence


def alert_rows(conn, user_id, alert_id=None):
    # Retain a receipted Home order after watching stops and its Telegram button is withdrawn.
    query = """SELECT a.*, o.quote AS native_quote, o.stage AS native_stage,
               o.order_ref AS native_receipt, o.expires AS task_expires, d.expires AS device_expires
        FROM pilot_alerts a LEFT JOIN native_orders o ON o.alert_id=a.id
          AND o.user_id=a.user_id AND o.listing_id=a.listing_id
          AND ((a.origin='public_search' AND a.account_ref='public' AND a.native_binding=o.binding)
            OR (((a.origin='browser_home' AND a.account_ref IN ('home:'||o.binding,'routed-home'))
                  OR (a.origin='browser_search' AND a.account_ref='catalog:'||o.binding))
                AND (a.native_binding=o.binding OR (a.native_binding IS NULL AND o.stage='purchased'))))
        LEFT JOIN native_devices d ON d.user_id=o.user_id AND d.binding=o.binding AND d.username=o.username
        WHERE a.user_id=?"""
    if alert_id is not None:
        return conn.execute(query + " AND a.id=?", (user_id, alert_id))
    return conn.execute(query + " ORDER BY a.id DESC LIMIT 100", (user_id,))


def _utc_label(instant):
    return datetime.fromtimestamp(instant, UTC).strftime("%d %b %Y at %H:%M:%S UTC")


def checkout_at(row, now):
    if row["origin"] == "connected" or not row["native_quote"]:
        return None
    quote = json.loads(row["native_quote"])
    if (
        quote.get("listing_id") != row["listing_id"]
        or quote.get("account_ref") != row["account_ref"]
        or quote.get("currency") != "GBP"
        or not quote.get("quote_id")
    ):
        return None
    amounts = [quote.get(k) for k in ("item_pence", "buyer_fee_pence", "shipping_pence", "total_pence")]
    if not all(pence(n) for n in amounts) or sum(amounts[:3]) != amounts[3]:
        return None

    stage = row["native_stage"]
    if stage == "purchased" and row["native_receipt"]:
        note = "Purchase confirmed on " + _utc_label(row["updated"])
        basis, total = "purchase", amounts[3]
    else:
        expiry = quote.get("expires_at")
        if type(expiry) not in (int, float):
            return None
        # Chrome may have a shorter instruction deadline than the quote itself.
        # After issuance its server reporting window is not purchase authority.
        if stage == "issued":
            deadline = quote.get("commit_deadline")
            if type(deadline) not in (int, float):
                return None
            expiry = min(expiry, deadline)
        elif stage in {"await_commit", "confirm"}:
            expiry = min(expiry, row["task_expires"])
        if row["device_expires"] is not None:
            expiry = min(expiry, row["device_expires"])
        if now >= expiry or stage == "expired":
            return {
                "basis": "expired",
                "total_pence": None,
                "note": "Checkout quote expired on " + _utc_label(expiry),
            }
        if stage not in {"issued", "await_commit", "confirm"} or row["device_expires"] is None:
            return None
        basis, total = "quote", amounts[3]
        note = "Checkout quote valid until " + _utc_label(expiry)

    return {"basis": basis, "total_pence": total, "note": note}


def for_alert(conn, user_id, alert_id, now):
    row = alert_rows(conn, user_id, alert_id).fetchone()
    return checkout_at(row, now) if row else None
