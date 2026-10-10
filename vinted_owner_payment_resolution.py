"""Consumed-once recording of an explicitly owner-confirmed failed purchase."""

import json
import os
import re
from contextlib import closing

import vinted_buying as buying
from logger import get_logger
from search_settings import connection

logger = get_logger(__name__)


def run_once():
    marker = os.getenv("MSJ_OWNER_FAILED_PAYMENT_ON_START", "").strip()
    match = re.fullmatch(r"([0-9]{1,24}):([A-Za-z0-9_-]{1,100})", marker)
    if not match:
        return None
    item_id = match.group(1)
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            "SELECT value FROM parameters WHERE key='owner_failed_payment_release'"
        ).fetchone()
        if previous and previous[0] == marker:
            return None
        conn.execute(
            "INSERT OR REPLACE INTO parameters(key,value) VALUES ('owner_failed_payment_release',?)",
            (marker,),
        )
    outcome = {
        "item_id": item_id,
        "outcome": "not_resolved",
        "payment_submitted": False,
    }
    saved = buying.result(item_id)
    try:
        if saved:
            resolved = buying.resolve_failed(item_id, str(saved["updated"]))
            outcome.update(outcome="owner_attested", state=resolved["state"])
            try:
                buying.guard_new_payment(item_id)
                outcome["other_payments_unconfirmed"] = False
            except ValueError:
                outcome["other_payments_unconfirmed"] = True
        else:
            outcome["reason"] = "attempt_missing"
    except ValueError:
        outcome["reason"] = "attempt_not_eligible_or_changed"
    logger.info(
        "Vinted owner failed-payment resolution: %s",
        json.dumps(outcome, sort_keys=True),
    )
    return outcome
