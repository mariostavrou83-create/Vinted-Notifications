"""Consumed-once, read-only verification of an exact owner purchase attempt."""

import json
import os
import re
from contextlib import closing

import vinted_buyer as buyer
import vinted_buying as buying
from logger import get_logger
from search_settings import connection

logger = get_logger(__name__)
STATES = {
    "preparing",
    "failed_before_payment",
    "paying",
    "unknown",
    "needs_action",
    "paid",
    "payment_failed",
}
STAGES = {
    "attempt_missing",
    "not_submitted",
    "bound_transaction",
    "bound_payment",
    "checkout_reference_missing",
    "attempt_binding_missing",
    "buyer_account_changed",
    "transaction_binding_unverified",
    "checkout_binding_unverified",
    "payment_response_unverified",
    "payment_order_disagree",
    "payment_status_unverified",
    "request_failed",
}


def run_once():
    marker = os.getenv("MSJ_PAYMENT_RECONCILE_ON_START", "").strip()
    match = re.fullmatch(r"([0-9]{1,24}):([A-Za-z0-9_-]{1,100})", marker)
    if not match:
        return None
    item_id = match.group(1)
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            "SELECT value FROM parameters WHERE key='payment_reconciled_release'"
        ).fetchone()
        if previous and previous[0] == marker:
            return None
        # Reserve before the read: a restart cannot repeat an uncertain check.
        conn.execute(
            "INSERT OR REPLACE INTO parameters(key,value) VALUES ('payment_reconciled_release',?)",
            (marker,),
        )
    result = {
        "outcome": "unverified",
        "stage": "request_failed",
        "payment_submitted_by_check": False,
        "checkout_created_by_check": False,
        "solver_attempted_by_check": False,
    }
    try:
        saved = buying.check_payment(item_id, verify_paid=True)
        if not saved:
            result["stage"] = "attempt_missing"
        else:
            state = saved.get("state")
            if state in STATES:
                result["state"] = state
            stage = saved.get("reconciliation_stage")
            result["stage"] = stage if stage in STAGES else "not_submitted"
            if saved.get("reconciliation_outcome") == "verified":
                result["outcome"] = "verified"
            total = saved.get("total")
            if type(total) is int and 0 <= total <= 100000000:
                result["total_gbp_pence"] = total
    except buyer.BuyerError as exc:
        if type(exc.status) is int and 100 <= exc.status <= 599:
            result["http_status"] = exc.status
        if exc.reason in buyer.AUTH_REASONS:
            result["reason"] = exc.reason
    except Exception:  # noqa: BLE001 -- never replay an uncertain submission
        pass
    logger.info("Vinted exact payment check: %s", json.dumps(result, sort_keys=True))
    return result
