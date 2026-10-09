"""Expiry-driven maintenance adapted from Blim's MIT-licensed refresh worker.

Source: masolupo/vinted-live-feed at b4ee651da44c7fe5e387f064d46f77a19541d194.
Copyright (c) 2026 masolupo; see third_party/blim/LICENSE.
Uses MSJ's current UK transport, encryption and cross-process buyer lock.
"""

import asyncio
import hashlib
import logging
import time
from contextlib import closing

import vinted_buyer as buyer
from search_settings import connection

logger = logging.getLogger(__name__)
ACCESS_MARGIN = 120
REFRESH_MARGIN = 2 * 86400
RETRY_DELAY = 300
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
    """Scheduling hint only: prefer JWT exp, then valid UK cookie expiry.

    Expired metadata remains a due hint so maintenance can recover after a
    restart. It does not authenticate a cookie or permit any buying action.
    """
    records = _session_cookie_records(saved, name)
    claims = [buyer.token_expiry_timestamp(record["value"]) for record in records]
    known_claims = [expiry for expiry in claims if expiry is not None]
    if known_claims:
        return min(known_claims)
    expiries = [
        record["expires"] for record in records if record.get("expires") is not None
    ]
    return min(expiries) if expiries else None


async def run():
    """Owned by Telegram's lifecycle; no buyer lock in the forking parent."""
    logger.info("Vinted session maintenance: worker started; interval=60s")
    while True:
        await asyncio.sleep(60)
        await asyncio.to_thread(maintain_connection)


def due(saved, now):
    if not _session_cookie_records(saved, "refresh_token_web"):
        return False
    for name, margin in (
        ("access_token_web", ACCESS_MARGIN),
        ("refresh_token_web", REFRESH_MARGIN),
    ):
        expiry = session_cookie_expiry(saved, name)
        if expiry is not None and expiry <= now + margin:
            return True
    return False


def maintain_connection():
    """No purchases: re-read under lock, renew only when due, stop on refusal."""
    try:
        with buyer.exclusive():
            now = time.time()
            with closing(connection()) as conn:
                row = conn.execute(
                    "SELECT session,verified_at FROM vinted_buyer WHERE id=1"
                ).fetchone()
                state = conn.execute(
                    "SELECT * FROM vinted_buyer_maintenance WHERE id=1"
                ).fetchone()
            if not row[0] or not row[1]:
                return "idle"
            fingerprint = hashlib.sha256(row[0]).hexdigest()
            if state["session_fingerprint"] == fingerprint:
                if state["blocked"]:
                    return "blocked"
                if state["retry_at"] > now:
                    return "cooldown"
            if not due(buyer.decrypt(row[0]), now):
                return "fresh"
            # Reserve before contacting Vinted so a restart cannot repeat a
            # network failure immediately. Only a hash of encrypted data is saved.
            with closing(connection()) as conn, conn:
                conn.execute(
                    "UPDATE vinted_buyer_maintenance SET session_fingerprint=?,retry_at=?,blocked=0 WHERE id=1",
                    (fingerprint, now + RETRY_DELAY),
                )
            client = None
            blocked = False
            outcome = "verified"
            delay = RETRY_DELAY
            try:
                client = buyer.connected_client(renew_before=ACCESS_MARGIN)
            except buyer.BuyerError as exc:
                transient = exc.reason in ("network", "rate_limited") or (
                    isinstance(exc.status, int) and exc.status >= 500
                )
                blocked = not transient
                outcome = "blocked" if blocked else "cooldown"
                if exc.reason == "rate_limited":
                    delay = 900
                logger.info(
                    "Vinted session maintenance: result=%s stage=%s reason=%s http=%s",
                    outcome,
                    exc.stage if exc.stage in buyer.AUTH_STAGES else "request",
                    exc.reason if exc.reason in buyer.AUTH_REASONS else "not_confirmed",
                    exc.status if isinstance(exc.status, int) else None,
                )
            finally:
                if client:
                    client.session.close()
                with closing(connection()) as conn, conn:
                    current = conn.execute(
                        "SELECT session FROM vinted_buyer WHERE id=1"
                    ).fetchone()[0]
                    conn.execute(
                        "UPDATE vinted_buyer_maintenance SET session_fingerprint=?,retry_at=?,blocked=? WHERE id=1",
                        (
                            hashlib.sha256(current).hexdigest() if current else None,
                            now + delay,
                            int(blocked),
                        ),
                    )
            if outcome == "verified":
                logger.info("Vinted session maintenance: result=verified; no purchase")
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
