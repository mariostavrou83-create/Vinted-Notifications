"""One explicitly authorised recovery and manual BUY activation; no purchases."""

import json
import logging
import math
import os
import re
import time
from contextlib import closing

import vinted_buyer as buyer
from search_settings import connection

logger = logging.getLogger(__name__)
MARKER = "buyer_session_recovery_release"
RESULT = "buyer_session_recovery_result"


def recovery_release():
    value = os.environ.get("MSJ_BUYER_RECOVERY_ON_LINK", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value) or value.lower() in (
        "off",
        "false",
        "0",
    ):
        return None
    with closing(connection()) as conn:
        previous = conn.execute(
            "SELECT value FROM parameters WHERE key=?", (MARKER,)
        ).fetchone()
    return None if previous and previous[0] == value else value


def _reserve(release):
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            "SELECT value FROM parameters WHERE key=?", (MARKER,)
        ).fetchone()
        if previous and previous[0] == release:
            return False
        # A restart cannot repeat a rejected renewal or re-enable buying after
        # the owner later turns it off. Ordinary maintenance never uses this.
        conn.execute(
            "INSERT OR REPLACE INTO parameters(key,value) VALUES (?,?)",
            (MARKER, release),
        )
    return True


def recover_linked_client(client):
    """Caller holds exclusive() and has just verified the expected buyer."""
    release = recovery_release()
    if not release or not _reserve(release):
        return None
    result = {
        "outcome": "unverified",
        "stage": "configuration",
        "checked": time.time(),
        "normal_renewal_verified": False,
        "same_buyer_account": False,
        "autobuy_on": False,
        "checkout_created": False,
        "payment_submitted": False,
    }
    with closing(connection()) as conn, conn:
        conn.execute("UPDATE vinted_buyer SET enabled=0 WHERE id=1")
    try:
        import vinted_buying as buying
        import vinted_telegram_review as telegram

        current = buyer.settings()
        if telegram.restricted() or not current["connected"] or not current["user_id"]:
            return result
        expected_id = current["user_id"]
        signature = telegram.network_signature()
        preferences = buying.choice_preferences(current)
        browser_info = buying.saved_browser_info()
        if client.network != buyer.network_configuration():
            return result
        result["stage"] = "normal_renewal"
        buyer.renew_saved_client(client)
        result["stage"] = "identity"
        user_id, username = client.identity()
        if user_id != expected_id:
            raise buyer.BuyerError(
                buyer.AUTH_REASONS["account_changed"],
                reason="account_changed",
                stage="identity",
            )
        saved = client.exported()
        sealed = buyer.encrypt(saved)
        with closing(connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?,verified_at=?,username=? WHERE id=1 AND user_id=?",
                (sealed, time.time(), username, expected_id),
            )
        client.bind_verified_session(user_id, sealed, saved)
        buyer.record_auth("connected", "identity", 200)
        result.update(normal_renewal_verified=True, same_buyer_account=True)
        current = buyer.settings()
        if (
            telegram.restricted()
            or current["user_id"] != expected_id
            or telegram.network_signature() != signature
            or buying.choice_preferences(current) != preferences
            or buying.saved_browser_info() != browser_info
        ):
            result["stage"] = "settings_changed"
            return result
        with closing(connection()) as conn, conn:
            pending = conn.execute(
                "SELECT item_id FROM vinted_buy_attempts WHERE state IN ('paying','unknown','needs_action')"
            ).fetchall()
            result["pending_payment_items"] = [
                row[0]
                for row in pending
                if isinstance(row[0], str) and re.fullmatch(r"[0-9]{1,24}", row[0])
            ]
            conn.execute("UPDATE vinted_buyer SET enabled=1 WHERE id=1")
            enabled = conn.execute(
                "SELECT enabled FROM vinted_buyer WHERE id=1"
            ).fetchone()[0]
        if enabled == 1:
            result.update(outcome="enabled", stage="complete", autobuy_on=True)
        return result
    except buyer.BuyerError as exc:
        buyer.record_auth(exc.reason, exc.stage, exc.status)
        result.update(
            stage=exc.stage if exc.stage in buyer.AUTH_STAGES else result["stage"],
            reason=exc.reason if exc.reason in buyer.AUTH_REASONS else "not_confirmed",
        )
        if type(exc.status) is int and 100 <= exc.status <= 599:
            result["http_status"] = exc.status
        return result
    except (
        Exception
    ) as exc:  # noqa: BLE001 -- never record credentials or provider text
        logger.warning("Vinted session recovery unavailable: %s", type(exc).__name__)
        return result
    finally:
        result["checked"] = time.time()
        with closing(connection()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO parameters(key,value) VALUES (?,?)",
                (RESULT, json.dumps(result, sort_keys=True)),
            )
        logger.info("Vinted session recovery: %s", json.dumps(result, sort_keys=True))


def summary(result):
    if result.get("outcome") == "enabled":
        return "Vinted verified the same buyer and accepted normal session renewal. Autobuy is on for your manual Telegram test. No checkout or payment was created."
    status = result.get("http_status")
    suffix = f" (HTTP {status})" if type(status) is int else ""
    return f"Your fresh session was linked, but recovery stopped at {result['stage'].replace('_', ' ')}{suffix}. Autobuy stays off. No checkout or payment was created."


def run_if_ready():
    """Check private-form evidence, stored verification and the current account."""
    client = None
    try:
        with buyer.exclusive(wait_seconds=45):
            if not recovery_release():
                return None
            try:
                after = float(os.environ.get("MSJ_BUYER_RECOVERY_AFTER", ""))
                if not math.isfinite(after) or not 0 <= after <= 4102444800:
                    return None
            except ValueError:
                return None
            saved = buyer.settings()
            events = buyer.public_auth_results()
            owner_link = events.get("buyer_session")
            verified_at = saved.get("verified_at")
            evidence = {
                "saved_verified_at": (
                    verified_at if type(verified_at) in (int, float) else None
                ),
                "fresh_saved_verification": bool(
                    type(verified_at) in (int, float)
                    and after <= verified_at <= time.time()
                ),
                "private_form_result": (
                    {
                        key: owner_link[key]
                        for key in ("reason", "stage_code", "http_status", "checked_at")
                    }
                    if owner_link
                    else None
                ),
                "fresh_private_form_verified": bool(
                    owner_link
                    and owner_link["reason"] == "connected"
                    and after <= owner_link["checked_at"] <= time.time()
                ),
            }
            logger.info(
                "Vinted session recovery evidence: %s",
                json.dumps(evidence, sort_keys=True),
            )
            # Missing logs or an older form result are not an authentication
            # failure. Ask Vinted now, without renewing or buying, before deciding.
            client = buyer.connected_client(allow_refresh=False)
            logger.info(
                "Vinted session recovery current account: same_buyer_verified=True; no checkout or payment"
            )
            return recover_linked_client(client)
    except buyer.BuyerError as exc:
        logger.info(
            "Vinted session recovery waiting: stage=%s reason=%s http=%s",
            exc.stage if exc.stage in buyer.AUTH_STAGES else "request",
            exc.reason if exc.reason in buyer.AUTH_REASONS else "not_confirmed",
            exc.status if type(exc.status) is int else None,
        )
        return None
    finally:
        if client:
            client.session.close()
