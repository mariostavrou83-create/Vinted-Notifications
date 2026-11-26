"""Expiry-driven maintenance adapted from Blim's MIT-licensed refresh worker.

Source: masolupo/vinted-live-feed at b4ee651da44c7fe5e387f064d46f77a19541d194.
Copyright (c) 2026 masolupo; see third_party/blim/LICENSE.
Uses MSJ's current UK transport, encryption and cross-process buyer lock.
"""

import asyncio
import hashlib
import logging
import math
import sqlite3
import time
from contextlib import closing

import vinted_buyer as buyer
from search_settings import connection
from vinted_accounts import account_scope, account_table, current_role, separated
from vinted_buyer_priority import yield_to_purchase

logger = logging.getLogger(__name__)
# Start early enough for the longest normal cooldown and next 60-second tick
# to leave additional headroom for account requests before access expires.
ACCESS_MARGIN = 20 * 60
# The dedicated buyer has room for two 15-minute renewal deferrals before
# access expires. The selling account keeps its existing refresh schedule.
BUYER_ACCESS_MARGIN = 35 * 60
REFRESH_MARGIN = 2 * 86400
RETRY_DELAY = 300
AMBIGUOUS_RENEWAL_DELAY = 900
FIRST_RECOVERY_DELAY = 60
HEALTH_INTERVAL = 300
PROBE_RESERVATION_KEY = "dedicated_buyer_identity_probe"
COOKIE_REQUEST_PATHS = {
    "access_token_web": "/api/v2/users/current",
    "refresh_token_web": "/web/api/auth/refresh",
}


def _session_cookie_records(saved, name):
    """Select cookie values applicable to the canonical UK account request.

    Full records are authoritative when present. The legacy cookie dictionary
    has no metadata and can supply only JWT scheduling information.
    """
    if not isinstance(saved, dict) or name not in COOKIE_REQUEST_PATHS:
        return []
    records = saved.get("cookie_records")
    if records is None:
        cookies = saved.get("cookies", {})
        value = cookies.get(name) if isinstance(cookies, dict) else None
        return [{"value": value}] if isinstance(value, str) and value else []
    if not isinstance(records, list) or len(records) > 200:
        return []
    selected = []
    request_path = COOKIE_REQUEST_PATHS[name]
    for record in records:
        if not isinstance(record, dict) or record.get("name") != name:
            continue
        value = record.get("value")
        domain, path = record.get("domain"), record.get("path", "/")
        expiry = record.get("expires")
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 16384
            or any(ord(char) < 32 or ord(char) > 126 for char in value)
            or not isinstance(domain, str)
            or domain not in buyer.COOKIE_DOMAINS
            or not isinstance(path, str)
            or not path.startswith("/")
            or len(path) > 2048
            or any(ord(char) <= 32 or ord(char) > 126 for char in path)
            or type(record.get("secure", True)) is not bool
            or any(
                flag in record and type(record[flag]) is not bool
                for flag in (
                    "domain_specified",
                    "domain_initial_dot",
                    "path_specified",
                    "discard",
                )
            )
            or (
                expiry is not None
                and (type(expiry) is not int or not 0 <= expiry <= 253402300799)
            )
        ):
            continue
        # Host-only cookies must name this host; parent-domain cookies require
        # domain scope. All accepted domains otherwise match www.vinted.co.uk.
        if (
            record.get("domain_specified", True) is False
            and domain != "www.vinted.co.uk"
        ):
            continue
        if not (
            request_path == path
            or (
                request_path.startswith(path)
                and (path.endswith("/") or request_path[len(path) :].startswith("/"))
            )
        ):
            continue
        selected.append(record)
    # Ambiguous duplicate credentials are not grounds for a background request.
    if len({record["value"] for record in selected}) > 1:
        return []
    return selected


def session_cookie_expiry(saved, name):
    """Scheduling hint only: use the earliest valid claim or cookie expiry.

    Expired metadata remains a due hint so maintenance can recover after a
    restart. It does not authenticate a cookie or permit any buying action.
    """
    records = _session_cookie_records(saved, name)
    claims = [buyer.token_expiry_timestamp(record["value"]) for record in records]
    known_claims = [expiry for expiry in claims if expiry is not None]
    expiries = [
        record["expires"] for record in records if record.get("expires") is not None
    ]
    known_expiries = known_claims + expiries
    return min(known_expiries) if known_expiries else None


async def run(notifier=None):
    """Owned by Telegram's lifecycle; no buyer lock in the forking parent."""
    logger.info(
        "Vinted session maintenance: worker started; interval=60s "
        "access_margin_seconds=%s buyer_access_margin_seconds=%s network_retry_seconds=%s renewal_retry_seconds=%s role_scoped_locks=True",
        ACCESS_MARGIN,
        BUYER_ACCESS_MARGIN,
        RETRY_DELAY,
        AMBIGUOUS_RENEWAL_DELAY,
    )
    if notifier is not None:
        logger.info(
            "Vinted connection warnings enabled; stored-failure checks every 60s"
        )
        logger.info(
            "Vinted offers warnings enabled; stored-state checks every 60s"
        )
    while True:
        try:
            await asyncio.to_thread(maintain_connection)
            await asyncio.to_thread(check_account_health)
            if notifier is not None:
                await notifier.check()
                from vinted_buyer_events import BuyerEventNotifier

                events = BuyerEventNotifier(notifier.bot, notifier.chat_id)
                # At most the two fixed event channels; no unbounded outbox drain.
                for _ in range(2):
                    if await events.check() != "sent":
                        break
        except Exception as exc:  # noqa: BLE001 -- keep the worker alive
            logger.warning(
                "Vinted buyer worker tick unavailable: %s", type(exc).__name__
            )
        # Selling health is independent: a buyer failure must not pause offers.
        try:
            if separated():
                await asyncio.to_thread(maintain_seller_connection)
                if notifier is not None:
                    with account_scope("seller"):
                        await notifier.check()
                        from vinted_buyer_events import BuyerEventNotifier
                        seller_events = BuyerEventNotifier(notifier.bot, notifier.chat_id)
                        for _ in range(2):
                            if await seller_events.check() != "sent":
                                break
        except Exception as exc:  # noqa: BLE001 -- keep both account workers alive
            logger.warning("Vinted selling worker tick unavailable: %s", type(exc).__name__)
        # Offers have separate failure states: a healthy buyer identity does
        # not prove that notifications or seller offers are still working.
        # Check their stored state even if this tick's account work failed.
        if notifier is not None:
            try:
                from vinted_offer_notices import OfferNotifier

                await OfferNotifier(notifier.bot, notifier.chat_id).check()
            except Exception as exc:  # noqa: BLE001 -- preserve maintenance
                logger.warning(
                    "Vinted offers warning tick unavailable: %s", type(exc).__name__
                )
        await asyncio.sleep(60)


def check_account_health():
    """Verify idle sessions every five minutes, respecting shared renewal backoff.

    Expiry claims cannot detect revocation. This read uses the same account,
    transport and normal renewal policy; it never creates a checkout or payment.
    """
    client = None
    try:
        with buyer.exclusive():
            yield_to_purchase()
            now = time.time()
            identity_only = False
            with closing(connection()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    f"SELECT session,user_id,verified_at FROM {account_table('vinted_buyer')} WHERE id=1"
                ).fetchone()
                state = conn.execute(
                    f"SELECT * FROM {account_table('vinted_buyer_maintenance')} WHERE id=1"
                ).fetchone()
                access = conn.execute(
                    f"SELECT last_probe,reason,stage,http_status FROM {account_table('vinted_buyer_access')} WHERE id=1"
                ).fetchone()
                if not row or not row[0] or not row[2]:
                    return "idle"
                if now - max(row[2], access["last_probe"]) < HEALTH_INTERVAL:
                    return "fresh"
                if state["session_fingerprint"] == hashlib.sha256(
                    row[0]
                ).hexdigest() and (state["blocked"] or state["retry_at"] > now):
                    if (
                        state["blocked"] or current_role() != "buyer" or not separated(conn)
                        or (access["reason"], access["stage"], access["http_status"]) != ("connected", "identity", 200)
                    ):
                        return "cooldown"
                    # One identity-only read after a verified identity and a generic
                    # proactive renewal refusal. Bind it to this exact cooldown.
                    reservation = f"{state['session_fingerprint']}:{state['retry_at']}"
                    identity_only = conn.execute(
                        "DELETE FROM parameters WHERE key=? AND value=?",
                        (PROBE_RESERVATION_KEY, reservation),
                    ).rowcount == 1
                    if not identity_only:
                        return "cooldown"
                conn.execute(
                    f"UPDATE {account_table('vinted_buyer_access')} SET last_probe=? WHERE id=1", (now,)
                )
            if identity_only:
                try:
                    client = buyer.connected_client(allow_refresh=False, solve_challenges=False)
                except buyer.BuyerError as exc:
                    if exc.reason == "rate_limited" or exc.retry_after:
                        _, delay = renewal_failure_policy(exc)
                        with closing(connection()) as conn, conn:
                            conn.execute(
                                "UPDATE vinted_buyer_maintenance SET retry_at=MAX(retry_at,?) WHERE id=1 AND session_fingerprint=?",
                                (time.time() + delay, state["session_fingerprint"]),
                            )
                    raise
            else:
                client = buyer.connected_client()
            logger.info(
                "Vinted account health: verified; interval=300s; no checkout or payment; role=%s", current_role()
            )
            return "verified"
    except buyer.BuyerError as exc:
        if exc.reason == "busy":
            return "busy"
        logger.info(
            "Vinted account health: stage=%s reason=%s http=%s role=%s; no checkout or payment",
            exc.stage if exc.stage in buyer.AUTH_STAGES else "request",
            exc.reason if exc.reason in buyer.AUTH_REASONS else "not_confirmed",
            exc.status if type(exc.status) is int else None,
            current_role(),
        )
        return "unavailable"
    except Exception as exc:  # noqa: BLE001 -- keep maintenance running
        logger.warning("Vinted account health unavailable: %s", type(exc).__name__)
        return "unavailable"
    finally:
        if client is not None:
            client.session.close()


def due(saved, now, *, access_margin=ACCESS_MARGIN):
    if not _session_cookie_records(saved, "refresh_token_web"):
        return False
    for name, margin in (
        ("access_token_web", access_margin),
        ("refresh_token_web", REFRESH_MARGIN),
    ):
        expiry = session_cookie_expiry(saved, name)
        if expiry is not None and expiry <= now + margin:
            return True
    return False


def _ambiguous_renewal_failure(error):
    """An unexplained 400 is not proof that the refresh credential is invalid."""
    return (
        isinstance(error, buyer.BuyerError)
        and error.reason == "renewal_failed"
        and error.stage == "renewal"
        and type(error.status) is int
        and error.status == 400
    )


def first_recovery_retry(conn, error, fingerprint, now):
    """Give a recently verified buyer one earlier normal renewal after a lost login."""
    if (
        current_role() != "buyer"
        or not getattr(error, "identity_rejected", False)
        or not _ambiguous_renewal_failure(error)
        or error.retry_after
        or not separated(conn)
    ):
        return False
    return bool(conn.execute(
        """SELECT 1 FROM vinted_buyer b WHERE b.id=1 AND b.verified_at BETWEEN ? AND ?
           AND NOT EXISTS (SELECT 1 FROM vinted_connection_notice n
               WHERE n.buyer_id=b.user_id AND n.session_fingerprint=?
               AND n.checked>=b.verified_at AND n.identity_rejected=1)""",
        (now - 2 * HEALTH_INTERVAL, now, fingerprint),
    ).fetchone())


def renewal_failure_policy(error):
    """Keep foreground and maintenance renewal failures on the same backoff."""
    transient = (
        error.reason in ("network", "rate_limited")
        or (isinstance(error.status, int) and error.status >= 500)
        or _ambiguous_renewal_failure(error)
    )
    delay = (
        AMBIGUOUS_RENEWAL_DELAY
        if error.reason == "rate_limited" or _ambiguous_renewal_failure(error)
        else RETRY_DELAY
    )
    if getattr(error, "first_recovery_retry", False):
        delay = FIRST_RECOVERY_DELAY
    if type(error.retry_after) in (float, int) and math.isfinite(error.retry_after):
        delay = max(delay, error.retry_after)
    return not transient, delay


def reserve_renewal(client, *, reservation=None):
    """Caller holds the buyer lock; admit one renewal of its exact saved seal.

    A successful identity read is still allowed during backoff. Only the
    renewal path consumes this reservation, before public bootstrap or a paid
    solver. Maintenance may consume its own exact prior reservation once;
    a different seal, deadline or terminal block cannot use that allowance.
    """
    reference = client._verified_session or client._loaded_session_reference
    if reference is None:
        raise buyer.BuyerError(
            buyer.AUTH_REASONS["saved_session"],
            reason="saved_session",
            stage="saved_session",
        )
    user_id, sealed, _ = reference
    fingerprint = hashlib.sha256(sealed).hexdigest()
    now = time.time()
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            f"SELECT session,user_id FROM {account_table('vinted_buyer')} WHERE id=1"
        ).fetchone()
        if not current or current[0] != sealed or current[1] != user_id:
            raise buyer.BuyerError(
                buyer.AUTH_REASONS["saved_session"],
                reason="saved_session",
                stage="saved_session",
            )
        state = conn.execute(
            f"SELECT * FROM {account_table('vinted_buyer_maintenance')} WHERE id=1"
        ).fetchone()
        if state["session_fingerprint"] == fingerprint:
            if state["blocked"]:
                logger.info("Vinted renewal admission: result=blocked")
                raise buyer.BuyerError(
                    buyer.AUTH_REASONS["renewal_blocked"],
                    reason="renewal_blocked",
                    stage="renewal",
                )
            if state["retry_at"] > now and reservation != (
                fingerprint,
                state["retry_at"],
            ):
                error = buyer.BuyerError(
                    buyer.AUTH_REASONS["renewal_cooldown"],
                    reason="renewal_cooldown",
                    stage="renewal",
                    retry_after=math.ceil(state["retry_at"] - now),
                )
                error.retry_after_seconds = math.ceil(state["retry_at"] - now)
                logger.info(
                    "Vinted renewal admission: result=cooldown retry_after_seconds=%s",
                    error.retry_after_seconds,
                )
                raise error
        conn.execute(
            f"UPDATE {account_table('vinted_buyer_maintenance')} SET session_fingerprint=?,retry_at=?,blocked=0 WHERE id=1",
            (fingerprint, now + RETRY_DELAY),
        )


def record_renewal_result(error, *, session_fingerprint):
    """Caller holds the buyer lock; None completes one accepted renewal cycle."""
    if (
        (error is not None and error.reason in ("renewal_cooldown", "renewal_blocked"))
        or not isinstance(session_fingerprint, str)
        or len(session_fingerprint) != 64
        or any(char not in "0123456789abcdef" for char in session_fingerprint)
    ):
        return False
    blocked, delay = (
        renewal_failure_policy(error) if error is not None else (False, RETRY_DELAY)
    )
    try:
        with closing(connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                f"SELECT session FROM {account_table('vinted_buyer')} WHERE id=1"
            ).fetchone()
            if (
                not current
                or not current[0]
                or hashlib.sha256(current[0]).hexdigest() != session_fingerprint
            ):
                return False
            conn.execute(
                f"UPDATE {account_table('vinted_buyer_maintenance')} SET session_fingerprint=?,retry_at=?,blocked=? WHERE id=1",
                (session_fingerprint, time.time() + delay, int(blocked)),
            )
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Vinted renewal backoff unavailable: %s", type(exc).__name__)
        return False
    return True


def note_ambiguous_failure(error, *, session_fingerprint):
    """Adopt one freshly observed failure without clearing a newer session.

    An explicit connection check captures the failed session's fingerprint
    while holding the buyer lock. Its caller may invoke this after that lock
    closes; re-acquiring it and comparing the seal prevents a concurrent new
    login from inheriting an old failure. This helper makes no network request
    and never clears legacy blocks merely because the process restarted.
    """
    if (
        not _ambiguous_renewal_failure(error)
        or not isinstance(session_fingerprint, str)
        or len(session_fingerprint) != 64
        or any(char not in "0123456789abcdef" for char in session_fingerprint)
    ):
        return False
    try:
        with buyer.exclusive(), closing(connection()) as conn, conn:
            row = conn.execute(
                f"SELECT session,verified_at FROM {account_table('vinted_buyer')} WHERE id=1"
            ).fetchone()
            if (
                not row
                or not row[0]
                or not row[1]
                or hashlib.sha256(row[0]).hexdigest() != session_fingerprint
            ):
                return False
            _, delay = renewal_failure_policy(error)
            conn.execute(
                f"UPDATE {account_table('vinted_buyer_maintenance')} SET session_fingerprint=?,retry_at=?,blocked=0 WHERE id=1",
                (session_fingerprint, time.time() + delay),
            )
        return True
    except buyer.BuyerError as exc:
        if exc.reason == "busy":
            return False
        raise


def maintain_connection():
    """No purchases: renew only when due, cool down ambiguous failures."""
    try:
        with buyer.exclusive():
            yield_to_purchase()
            now = time.time()
            with closing(connection()) as conn:
                row = conn.execute(
                    f"SELECT session,verified_at,user_id FROM {account_table('vinted_buyer')} WHERE id=1"
                ).fetchone()
                state = conn.execute(
                    f"SELECT * FROM {account_table('vinted_buyer_maintenance')} WHERE id=1"
                ).fetchone()
                pending = conn.execute(
                    f"SELECT session_fingerprint FROM {account_table('vinted_connection_notice')} "
                    "WHERE id=1 AND buyer_id=? AND checked>=? AND recovery_pending=1",
                    (row[2], row[1]),
                ).fetchone() if row and row[0] else None
                margin = (
                    BUYER_ACCESS_MARGIN
                    if current_role() == "buyer" and separated(conn)
                    else ACCESS_MARGIN
                )
            if not row[0] or not row[1]:
                return "idle"
            fingerprint = hashlib.sha256(row[0]).hexdigest()
            if state["session_fingerprint"] == fingerprint:
                if state["blocked"]:
                    return "blocked"
                if state["retry_at"] > now:
                    return "cooldown"
            try:
                saved = buyer.decrypt(row[0])
            except buyer.BuyerError as exc:
                from vinted_connection_notices import record_failure

                record_failure(exc, fingerprint)
                raise
            recovering = bool(pending and pending[0] == fingerprint)
            if not recovering and not due(saved, now, access_margin=margin):
                return "fresh"
            access_expiry = session_cookie_expiry(saved, "access_token_web")
            refresh_expiry = session_cookie_expiry(saved, "refresh_token_web")
            logger.info(
                "Vinted session maintenance: due access_remaining_seconds=%s "
                "refresh_due=%s access_margin_seconds=%s role=%s",
                (
                    math.floor(access_expiry - now)
                    if access_expiry is not None
                    else "null"
                ),
                refresh_expiry is not None and refresh_expiry <= now + REFRESH_MARGIN,
                margin,
                current_role(),
            )
            # Reserve before contacting Vinted so a restart cannot repeat a
            # network failure immediately. Only a hash of encrypted data is saved.
            with closing(connection()) as conn, conn:
                conn.execute(
                    f"UPDATE {account_table('vinted_buyer_maintenance')} SET session_fingerprint=?,retry_at=?,blocked=0 WHERE id=1",
                    (fingerprint, now + RETRY_DELAY),
                )
            client = None
            blocked = False
            outcome = "verified"
            delay = RETRY_DELAY
            result_fingerprint = None
            local_gate = False
            schedule_identity_check = False
            try:
                client = buyer.connected_client(
                    renew_before=0 if recovering else margin,
                    solve_challenges=not recovering,
                    _renewal_reservation=(fingerprint, now + RETRY_DELAY),
                )
                deferred = getattr(client, "deferred_renewal", None)
                if isinstance(deferred, buyer.BuyerError):
                    blocked, delay = renewal_failure_policy(deferred)
                    outcome = "cooldown"
                    schedule_identity_check = (
                        current_role() == "buyer" and _ambiguous_renewal_failure(deferred)
                        and not deferred.retry_after
                    )
                    logger.info(
                        "Vinted session maintenance: proactive renewal deferred; "
                        "identity_verified=True http=%s retry_seconds=%s role=%s",
                        deferred.status, delay, current_role(),
                    )
                # A successful connection can advance the saved seal more than
                # once. Attribute maintenance only to its final verified seal.
                reference = client._verified_session
                if reference is not None and reference[0] == row[2]:
                    result_fingerprint = hashlib.sha256(reference[1]).hexdigest()
            except buyer.BuyerError as exc:
                # connected_client binds this to its last known saved seal, or
                # None if another connection superseded it during the request.
                result_fingerprint = getattr(exc, "session_fingerprint", fingerprint)
                blocked, delay = renewal_failure_policy(exc)
                outcome = "blocked" if blocked else "cooldown"
                if exc.reason in ("renewal_cooldown", "renewal_blocked"):
                    local_gate = True
                    outcome = (
                        "blocked" if exc.reason == "renewal_blocked" else "cooldown"
                    )
                logger.info(
                    "Vinted session maintenance: result=%s stage=%s reason=%s http=%s role=%s retry_seconds=%s",
                    outcome,
                    exc.stage if exc.stage in buyer.AUTH_STAGES else "request",
                    exc.reason if exc.reason in buyer.AUTH_REASONS else "not_confirmed",
                    exc.status if isinstance(exc.status, int) else None,
                    current_role(),
                    None if blocked or local_gate else delay,
                )
                from vinted_connection_notices import record_failure

                if exc.reason not in ("renewal_cooldown", "renewal_blocked"):
                    record_failure(exc, result_fingerprint)
            finally:
                if client:
                    client.session.close()
                with closing(connection()) as conn, conn:
                    current = conn.execute(
                        f"SELECT session,user_id FROM {account_table('vinted_buyer')} WHERE id=1"
                    ).fetchone()
                    if (
                        result_fingerprint is not None
                        and current
                        and current[0]
                        and current[1] == row[2]
                        and hashlib.sha256(current[0]).hexdigest() == result_fingerprint
                    ):
                        if not local_gate:
                            retry_at = time.time() + delay
                            conn.execute(
                                f"UPDATE {account_table('vinted_buyer_maintenance')} SET session_fingerprint=?,retry_at=?,blocked=? WHERE id=1",
                                (result_fingerprint, retry_at, int(blocked)),
                            )
                            if schedule_identity_check:
                                conn.execute(
                                    "INSERT OR REPLACE INTO parameters(key,value) VALUES(?,?)",
                                    (PROBE_RESERVATION_KEY, f"{result_fingerprint}:{float(retry_at)}"),
                                )
                    else:
                        # Keep the original reservation on its original seal.
                        # Its failure must not block a replacement connection.
                        outcome = "session_changed"
                        logger.info(
                            "Vinted session maintenance: result=session_changed; no purchase"
                        )
            if outcome == "verified":
                logger.info("Vinted session maintenance: result=verified; no purchase; role=%s", current_role())
            return outcome
    except buyer.BuyerError as exc:
        if exc.reason == "busy":
            return "busy"
        buyer.record_auth(exc.reason, exc.stage, exc.status)
        logger.info("Vinted session maintenance: result=unavailable")
        return "unavailable"
    except Exception as exc:  # noqa: BLE001 -- never log credentials or stop alerts
        logger.warning("Vinted session maintenance unavailable: %s", type(exc).__name__)
        return "unavailable"


def maintain_seller_connection():
    """Reuse expiry maintenance and account checks in the selling storage scope."""
    if not separated():
        return "shared"
    with account_scope("seller"):
        maintain_connection()
        return check_account_health()
