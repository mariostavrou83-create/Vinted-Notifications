"""Private encrypted buyer sessions with optional bounded security checks."""

import base64
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import time
from contextlib import closing, contextmanager
from copy import copy
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from cryptography.fernet import Fernet, InvalidToken

import db
from search_settings import connection
from vinted_http import API_HEADERS, NAVIGATION_HEADERS, BrowserSession

BASE = "https://www.vinted.co.uk"
PICKUP_BASE = "https://api.vinted.co.uk"
# Requests preserves Domain=www.vinted.co.uk as .www.vinted.co.uk. Both
# representations are scoped to the canonical buyer host, not another site.
COOKIE_DOMAINS = frozenset(
    {"www.vinted.co.uk", ".www.vinted.co.uk", "vinted.co.uk", ".vinted.co.uk"}
)
logger = logging.getLogger(__name__)
AUTH_STAGES = {
    "saved_session": "Saved buyer session",
    "homepage": "Vinted homepage",
    "sign_in": "Vinted sign-in endpoint",
    "renewal": "Vinted session renewal",
    "identity": "Vinted account verification",
    "request": "Vinted request",
    "pickup_points": "Vinted pickup points",
}
AUTH_REASONS = {
    "saved_session": "The bot could not restore the saved Vinted session. The buyer connection needs attention.",
    "security_challenge": "Vinted requires a security check for this connection. Check your proxy and CapSolver settings, then check the connection again.",
    "csrf": "Vinted rejected the sign-in security token. Your password has not been confirmed.",
    "credentials": "Vinted did not accept the sign-in credentials or the session has expired.",
    "account_restricted": "Vinted reports an account restriction. Check the account on Vinted before trying to connect it.",
    "forbidden": "Vinted refused the request (HTTP 403), without a recognised reason. This does not establish whether your password is correct.",
    "rate_limited": "Vinted requested a cooldown. Wait before trying again.",
    "unreadable": "Vinted returned a response the bot could not read.",
    "session_refresh": "Vinted requested renewal of the saved buyer session.",
    "renewal_failed": "Vinted could not renew the saved buyer session. Its renewal request or response needs checking. No purchase or payment was started.",
    "refresh_rejected": "Vinted rejected the saved refresh credential. Reconnect your Vinted buyer in Connections. No purchase or payment was started.",
    "account_changed": "Vinted verified a different buyer account. Reconnect the account already linked here. No purchase or payment was started.",
    "signin_redirect": "Vinted redirected this session to sign-in. Reconnect your buyer account.",
    "redirect": "Vinted redirected this account request instead of confirming it. The bot stopped without following the redirect.",
    "home_redirect": "Vinted redirected this request to its homepage instead of confirming the account.",
    "network": "Vinted did not confirm this request. Check your account before retrying.",
    "http_error": "Vinted did not accept this request.",
    "not_confirmed": "Vinted has not confirmed the buyer connection.",
    "endpoint_reached": "The sign-in endpoint accepted the connection and rejected the empty diagnostic request. Your account and password have not been tested.",
    "connected": "Vinted verified the buyer account.",
    "renewed": "Vinted accepted session renewal. Account verification is reported separately.",
    "verification_code": "Vinted sent a sign-in code. Enter it in the verification box below.",
}


def csrf_from_html(html):
    # Next.js serializes the bootstrap object inside a quoted script string.
    # Normalize its quote escaping without evaluating any page JavaScript.
    for _ in range(3):
        html = html.replace('\\"', '"')
    for pattern in (
        r'"(?:CSRF_TOKEN|csrf_token|csrfToken)"\s*:\s*"([A-Za-z0-9._~+/=-]{16,1024})"',
        r'<meta\s+name="csrf-token"\s+content="([A-Za-z0-9._~+/=-]{16,1024})"',
        r'<meta\s+content="([A-Za-z0-9._~+/=-]{16,1024})"\s+name="csrf-token"',
    ):
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return match[1]
    return None


class BuyerError(ValueError):
    def __init__(
        self, message, status=None, *, reason="not_confirmed", stage="request"
    ):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.stage = stage


def token_expiry_timestamp(value):
    """Unverified claim for scheduling only; never proof of authentication."""
    if not isinstance(value, str) or len(value) > 8192:
        return None
    parts = value.split(".")
    if len(parts) != 3 or len(parts[1]) > 4096:
        return None
    try:
        claims = json.loads(
            base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        )
        expiry = claims.get("exp") if isinstance(claims, dict) else None
        if type(expiry) not in (int, float) or not 0 <= expiry <= 4102444800:
            return None
        return expiry
    except (ValueError, TypeError, UnicodeError):
        return None


def token_expiry_hint(value):
    """Diagnostic only: an unverified expiry claim never confirms authentication."""
    expiry = token_expiry_timestamp(value)
    if expiry is None:
        return "unknown"
    return "expired" if expiry <= time.time() else "not_expired"


def security_challenge(response, data=None):
    """Classify explicit challenge signals without storing challenge URLs or cookies."""
    if isinstance(data, dict):
        for key in ("url", "captcha_url", "challenge_url"):
            value = data.get(key)
            if not isinstance(value, str):
                continue
            try:
                host = urlsplit(value).hostname or ""
            except ValueError:
                continue
            if host == "captcha-delivery.com" or host.endswith(".captcha-delivery.com"):
                return True
        if data.get("error") in ("captcha_required", "verification_required"):
            return True
    text = getattr(response, "text", "")
    if not isinstance(text, str):
        return False
    # Only inspect HTML for these phrases; never interpret credential values in JSON.
    text = text[:65536].lower()
    return ("<html" in text or "<script" in text) and any(
        term in text
        for term in (
            "verify you are human",
            "captcha-delivery.com",
            "checking your browser",
        )
    )


def homepage_data(response):
    """Decode only a bounded JSON denial; successful HTML is not JSON data."""
    text = getattr(response, "text", "")
    if response.status_code == 403 and isinstance(text, str) and len(text) <= 65536:
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else None
        except (ValueError, RecursionError):
            pass
    return None


def response_error(response, data, stage):
    """Map a Vinted failure to fixed, non-secret diagnostics. Never echo its body."""
    status = response.status_code
    values, messages, fields, shape = [], [], set(), set()
    pending, inspected = [(data, 0)], 0
    # Gateways and newer web endpoints can wrap errors in an object. Inspect
    # only fixed error/container keys, with a bound; never log arbitrary keys,
    # values, token fields or the response text.
    while pending and inspected < 40:
        inspected += 1
        value, depth = pending.pop()
        if depth > 4:
            continue
        if isinstance(value, list):
            pending.extend((entry, depth + 1) for entry in value[:20])
        elif isinstance(value, dict):
            for key in (
                "code",
                "error",
                "error_code",
                "errorCode",
                "message_code",
                "messageCode",
            ):
                if key in value:
                    shape.add(key)
                    values.append(value[key])
            for key in (
                "message",
                "message_code",
                "messageCode",
                "error",
                "error_description",
                "errorDescription",
                "detail",
                "title",
                "reason",
                "value",
            ):
                if key in value:
                    shape.add(key)
                    messages.append(value[key])
            if isinstance(value.get("field"), str):
                fields.add(value["field"])
            for key in ("error", "errors", "data", "payload", "response", "details"):
                child = value.get(key)
                if isinstance(child, (list, dict)):
                    shape.add(key)
                    pending.append((child, depth + 1))
            errors = value.get("errors")
            if isinstance(errors, dict):
                fields.update(key for key in errors if isinstance(key, str))
                messages.extend(v for v in errors.values() if isinstance(v, str))
    codes = {value.lower() for value in values if isinstance(value, str)}
    numbers = [
        (
            int(value)
            if isinstance(value, str) and re.fullmatch(r"[0-9]{1,4}", value)
            else value
        )
        for value in values
    ]
    numeric_code = next(
        (value for value in numbers if type(value) is int and 0 <= value <= 1000), None
    )
    fields.intersection_update(
        {
            "refresh_token",
            "access_token",
            "client_id",
            "scope",
            "grant_type",
            "csrf_token",
            "password",
            "username",
            "email",
            "device_id",
        }
    )
    hints = " ".join(
        value[:1024].lower() for value in messages if isinstance(value, str)
    )
    known_codes = codes & {
        "invalid_csrf_token",
        "csrf_token_invalid",
        "csrf_error",
        "invalid_token",
        "invalid_grant",
        "invalid_refresh_token",
        "refresh_token_expired",
        "session_expired",
        "authentication_required",
        "unauthorized",
        "unauthenticated",
        "bad_request",
        "forbidden",
        "captcha_required",
        "verification_required",
    }
    redirect = redirect_reason(response)
    logger.info(
        "Vinted response: stage=%s http=%s redirect=%s body=%s "
        "api_code=%s fields=%s csrf_hint=%s refresh_hint=%s required_hint=%s "
        "invalid_hint=%s expired_hint=%s shape=%s code_type=%s known_code=%s auth_hint=%s message_type=%s message_category=%s",
        stage,
        status,
        redirect_target(response),
        "json" if isinstance(data, dict) else "other",
        numeric_code,
        ",".join(sorted(fields)) or "none",
        "csrf" in hints,
        "refresh_token" in fields
        or "refresh_token" in hints
        or "refresh token" in hints,
        any(word in hints for word in ("required", "missing", "blank")),
        "invalid" in hints,
        "expired" in hints,
        ",".join(sorted(shape)) or "other",
        type(data.get("code")).__name__ if isinstance(data, dict) else "none",
        ",".join(sorted(known_codes)) or "other",
        any(
            word in hints
            for word in ("authenticat", "unauthor", "log in", "logged in", "login")
        ),
        type(data.get("message")).__name__ if isinstance(data, dict) else "none",
        (
            {
                "bad request": "bad_request",
                "unauthorized": "unauthorized",
                "forbidden": "forbidden",
            }.get(data["message"].strip().lower(), "other")
            if isinstance(data, dict) and isinstance(data.get("message"), str)
            else "non_text"
        ),
    )
    if status == 429:
        reason = "rate_limited"
    elif (
        security_challenge(response, data)
        or redirect == "security_challenge"
        or codes & {"captcha_required", "verification_required"}
    ):
        reason = "security_challenge"
    elif redirect:
        reason = redirect
    elif codes & {"invalid_csrf_token", "csrf_token_invalid", "csrf_error"}:
        reason = "csrf"
    elif codes & {"user_blocked", "account_blocked", "account_restricted"}:
        reason = "account_restricted"
    elif (
        numeric_code == 100
        or codes
        & {
            "invalid_grant",
            "invalid_credentials",
            "invalid_password",
            "invalid_username",
            "invalid_token",
            "invalid_refresh_token",
            "refresh_token_expired",
            "session_expired",
            "authentication_required",
            "unauthorized",
            "unauthenticated",
        }
        or status == 401
    ):
        reason = "credentials"
    elif status == 403:
        reason = "forbidden"
    elif data is None:
        reason = "unreadable"
    else:
        reason = "http_error"
    return BuyerError(AUTH_REASONS[reason], status, reason=reason, stage=stage)


def redirect_target(response):
    """Fixed labels only: no response paths, queries, userinfo or tokens in logs."""
    reason = redirect_reason(response)
    if reason is None:
        return "none"
    if reason != "redirect":
        return reason
    location = getattr(response, "headers", {}).get("Location", "")
    if not isinstance(location, str) or not location or len(location) > 4096:
        return "missing_or_invalid"
    if any(ord(char) <= 32 for char in location):
        return "missing_or_invalid"
    try:
        target = urlsplit(urljoin(BASE, location))
        if target.username or target.password:
            return "userinfo"
        if target.scheme != "https":
            return "non_https"
        if target.netloc == "www.vinted.co.uk":
            if target.path.rstrip("/") == "/catalog":
                return "uk_catalogue"
            return "same_origin_other_path"
        if target.netloc == "vinted.co.uk":
            return "uk_apex"
        return "other_origin"
    except ValueError:
        return "missing_or_invalid"


def redirect_reason(response):
    """Classify the official redirect without exposing its URL or following it."""
    if response.status_code not in (301, 302, 303, 307, 308):
        return None
    headers = getattr(response, "headers", {})
    location = headers.get("Location", "")
    if (
        not isinstance(location, str)
        or not location
        or len(location) > 4096
        or any(ord(char) <= 32 for char in location)
    ):
        return "redirect"
    try:
        target = urlsplit(urljoin(BASE, location))
        host = target.hostname or ""
        if host == "captcha-delivery.com" or host.endswith(".captcha-delivery.com"):
            return "security_challenge"
        if (
            target.scheme != "https"
            or target.netloc != "www.vinted.co.uk"
            or target.username
            or target.password
        ):
            return "redirect"
        if target.path.rstrip("/") == "/web/api/auth/refresh":
            return "session_refresh"
        if target.path == "/" or not target.path:
            return "home_redirect"
        if target.path.rstrip("/") in (
            "/member/login",
            "/login",
            "/web/api/auth/login",
        ):
            return "signin_redirect"
    except ValueError:
        pass
    return "redirect"


def record_auth(reason, stage="sign_in", status=None):
    # Only fixed labels and numeric HTTP status are persisted or logged.
    reason = (
        reason
        if isinstance(reason, str) and reason in AUTH_REASONS
        else "not_confirmed"
    )
    stage = stage if isinstance(stage, str) and stage in AUTH_STAGES else "request"
    status = status if type(status) is int and 100 <= status <= 599 else None
    checked = time.time()
    with closing(connection()) as conn, conn:
        conn.execute(
            "UPDATE vinted_buyer_access SET checked=?,reason=?,stage=?,http_status=? WHERE id=1",
            (checked, reason, stage, status),
        )
        if stage == "renewal":
            _write_auth_event(
                conn,
                {
                    "kind": "renewal",
                    "reason": reason,
                    "stage": stage,
                    "http_status": status,
                    "checked": checked,
                },
            )
    logger.info(
        "Vinted buyer access: stage=%s reason=%s http=%s", stage, reason, status
    )


AUTH_RESULT_KINDS = {
    "buyer_login": "Password sign-in",
    "buyer_session": "Existing session link",
    "buyer_verify": "Sign-in code",
    "renewal": "Saved session renewal",
}
AUTH_RESULT_FIELDS = frozenset({"kind", "reason", "stage", "http_status", "checked"})


def _auth_event(value):
    """Accept only non-secret, bounded metadata; never pass provider text through."""
    if not isinstance(value, dict) or set(value) != AUTH_RESULT_FIELDS:
        return None
    kind, reason, stage, checked, status = (
        value.get("kind"),
        value.get("reason"),
        value.get("stage"),
        value.get("checked"),
        value.get("http_status"),
    )
    if (
        not isinstance(kind, str)
        or kind not in AUTH_RESULT_KINDS
        or not isinstance(reason, str)
        or reason not in AUTH_REASONS
        or not isinstance(stage, str)
        or stage not in AUTH_STAGES
        or type(checked) not in (int, float)
        or not 0 <= checked <= 4102444800
        or not math.isfinite(checked)
        or (
            status is not None and (type(status) is not int or not 100 <= status <= 599)
        )
        or (kind == "renewal" and stage != "renewal")
        or (reason == "renewed" and kind != "renewal")
    ):
        return None
    return {
        "kind": kind,
        "reason": reason,
        "stage": stage,
        "http_status": status,
        "checked": checked,
    }


def _stored_auth_event(conn, key):
    row = conn.execute("SELECT value FROM parameters WHERE key=?", (key,)).fetchone()
    try:
        return _auth_event(json.loads(row[0])) if row else None
    except (ValueError, TypeError, RecursionError):
        return None


def _write_auth_event(conn, event):
    """A delayed old result cannot overwrite a newer result of the same kind."""
    event = _auth_event(event)
    if event is None:
        return False
    key = "buyer_auth_result_" + event["kind"]
    old = _stored_auth_event(conn, key)
    if old and old["checked"] >= event["checked"]:
        return False
    conn.execute(
        "INSERT OR REPLACE INTO parameters(key,value) VALUES (?,?)",
        (key, json.dumps(event, sort_keys=True, allow_nan=False)),
    )
    return True


def _public_auth_event(event):
    event = _auth_event(event)
    if event is None:
        return None
    try:
        display_zone = ZoneInfo("Europe/London")
    except ZoneInfoNotFoundError:
        display_zone = timezone.utc
    message = AUTH_REASONS[event["reason"]]
    if event["kind"] == "buyer_login" and event["reason"] == "security_challenge":
        message = (
            "Vinted blocked this password sign-in with a security check. "
            "A CapSolver solution does not establish that Vinted accepted login. "
            "This does not establish that your password is wrong."
        )
    return {
        "kind": AUTH_RESULT_KINDS[event["kind"]],
        "kind_code": event["kind"],
        "reason": event["reason"],
        "message": message,
        "stage": AUTH_STAGES[event["stage"]],
        "stage_code": event["stage"],
        "http_status": event["http_status"],
        "checked_at": event["checked"],
        "checked": datetime.fromtimestamp(event["checked"], display_zone).strftime(
            "%d %b %Y, %H:%M:%S %Z"
        ),
    }


def record_connection_result(kind, reason, stage, status):
    """Keep each owner connection method separate from saved-session renewal."""
    if kind not in ("buyer_login", "buyer_session", "buyer_verify"):
        return
    result = {
        "kind": kind,
        "reason": (
            reason
            if isinstance(reason, str) and reason in AUTH_REASONS
            else "not_confirmed"
        ),
        "stage": (
            stage if isinstance(stage, str) and stage in AUTH_STAGES else "request"
        ),
        "http_status": status if type(status) is int and 100 <= status <= 599 else None,
        "checked": time.time(),
    }
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _write_auth_event(conn, result)
        old = _stored_auth_event(conn, "buyer_connection_result")
        if not old or old["checked"] < result["checked"]:
            conn.execute(
                "INSERT OR REPLACE INTO parameters(key,value) VALUES ('buyer_connection_result',?)",
                (json.dumps(result, allow_nan=False),),
            )


def public_connection_result():
    with closing(connection()) as conn:
        event = _stored_auth_event(conn, "buyer_connection_result")
    return _public_auth_event(event) if event and event["kind"] != "renewal" else None


def public_auth_results():
    """Render distinct evidence, including legacy records, without credential values."""
    events = {}
    with closing(connection()) as conn:
        for kind in AUTH_RESULT_KINDS:
            event = _stored_auth_event(conn, "buyer_auth_result_" + kind)
            if event and event["kind"] == kind:
                events[kind] = event
        legacy = _stored_auth_event(conn, "buyer_connection_result")
        if legacy and legacy["kind"] not in events:
            events[legacy["kind"]] = legacy
        row = conn.execute("SELECT * FROM vinted_buyer_access WHERE id=1").fetchone()
    results = {kind: _public_auth_event(event) for kind, event in events.items()}
    if row:
        # This shared row is never evidence of a password sign-in succeeding.
        access = _auth_event(
            {
                "kind": "renewal",
                "reason": row["reason"],
                "stage": "renewal",
                "http_status": row["http_status"],
                "checked": row["checked"],
            }
        )
        if access and row["stage"] == "renewal" and "renewal" not in results:
            results["renewal"] = _public_auth_event(access)
        elif access and row["stage"] in ("saved_session", "identity"):
            access["kind"], access["stage"] = "buyer_session", row["stage"]
            results["saved_check"] = _public_auth_event(access)
            results["saved_check"]["kind"] = "Saved account check"
    return results


def import_auth_results_once():
    """Import observed request metadata once; no login, solver or payment requests."""
    payload = os.getenv("MSJ_BUYER_AUTH_RESULTS_IMPORT_ON_START", "")
    if not isinstance(payload, str) or not payload or len(payload) > 16384:
        return False
    try:
        values = json.loads(payload)
    except (ValueError, TypeError, RecursionError):
        return False
    if not isinstance(values, list) or not 1 <= len(values) <= 16:
        return False
    events = [event for value in values if (event := _auth_event(value))]
    if not events:
        return False
    canonical = json.dumps(
        events, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    marker = (
        "buyer_auth_results_import_" + hashlib.sha256(canonical.encode()).hexdigest()
    )
    # Consume before writing metadata, so a restart cannot replay this import.
    with closing(connection()) as conn, conn:
        if (
            conn.execute(
                "INSERT OR IGNORE INTO parameters(key,value) VALUES (?, '1')", (marker,)
            ).rowcount
            != 1
        ):
            return False
    with closing(connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        for event in sorted(events, key=lambda value: value["checked"]):
            _write_auth_event(conn, event)
    return True


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_buyer (
        id INTEGER PRIMARY KEY CHECK(id=1), session BLOB, pending BLOB,
        login_attempt REAL NOT NULL DEFAULT 0, verified_at REAL,
        user_id TEXT, username TEXT, enabled INTEGER NOT NULL DEFAULT 0,
        max_total INTEGER NOT NULL DEFAULT 0, max_extra INTEGER NOT NULL DEFAULT 0,
        browser_info TEXT NOT NULL DEFAULT '{}')""")
    conn.execute("INSERT OR IGNORE INTO vinted_buyer(id) VALUES (1)")
    buyer_columns = {row[1] for row in conn.execute("PRAGMA table_info(vinted_buyer)")}
    for column, definition in (
        ("network", "BLOB"),
        ("pickup_mode", "TEXT NOT NULL DEFAULT 'saved'"),
        ("preferred_card_last4", "TEXT NOT NULL DEFAULT ''"),
    ):
        if column not in buyer_columns:
            conn.execute(f"ALTER TABLE vinted_buyer ADD COLUMN {column} {definition}")
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_buy_attempts (
        item_id TEXT PRIMARY KEY, state TEXT NOT NULL, checkout_id TEXT,
        total INTEGER, message TEXT NOT NULL, updated REAL NOT NULL)""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(vinted_buy_attempts)")}
    for column in ("action_url", "buyer_id", "transaction_id"):
        if column not in columns:
            conn.execute(f"ALTER TABLE vinted_buy_attempts ADD COLUMN {column} TEXT")
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_buyer_access (
        id INTEGER PRIMARY KEY CHECK(id=1), last_probe REAL NOT NULL DEFAULT 0,
        checked REAL, reason TEXT, stage TEXT, http_status INTEGER)""")
    conn.execute("INSERT OR IGNORE INTO vinted_buyer_access(id) VALUES (1)")
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_buyer_maintenance (
        id INTEGER PRIMARY KEY CHECK(id=1), session_fingerprint TEXT,
        retry_at REAL NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0)""")
    conn.execute("INSERT OR IGNORE INTO vinted_buyer_maintenance(id) VALUES (1)")


@contextmanager
def exclusive(*, wait_seconds=0):
    path = Path(db.DB_PATH).resolve().parent / "vinted-buyer.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BuyerError(
                        "A buyer request is already running. Please wait for it to finish.",
                        reason="busy",
                    ) from None
                time.sleep(min(0.05, remaining))
        yield
    finally:
        os.close(fd)


def cipher():
    path = Path(db.DB_PATH).resolve().parent / "vinted-buyer.key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(Fernet.generate_key())
    return Fernet(path.read_bytes())


def encrypt(data):
    return cipher().encrypt(json.dumps(data).encode())


def decrypt(data):
    try:
        return json.loads(cipher().decrypt(data)) if data else None
    except (InvalidToken, ValueError, TypeError):
        logger.warning("Vinted saved session unavailable: operation=decrypt")
        raise BuyerError(
            AUTH_REASONS["saved_session"], reason="saved_session", stage="saved_session"
        ) from None


def settings():
    with closing(connection()) as conn:
        row = dict(conn.execute("SELECT * FROM vinted_buyer WHERE id=1").fetchone())
        access = dict(
            conn.execute("SELECT * FROM vinted_buyer_access WHERE id=1").fetchone()
        )
    # No credential value is returned to a template or API caller.
    row["connected"] = bool(row.pop("session") and row["verified_at"])
    pending = decrypt(row.pop("pending"))
    row["pending_code"] = bool(pending and pending.get("expires", 0) > time.time())
    row.pop("browser_info")
    row.pop("network")
    config = network_configuration(validate=False)
    row["network"] = {
        "browser": True,
        "proxy": bool(config["proxy"]),
        "capsolver": bool(config["api_key"]),
        "enabled": config["enabled"],
    }
    row["access"] = {
        "reason": (
            access["reason"] if access["reason"] in AUTH_REASONS else "not_confirmed"
        ),
        "stage_code": access["stage"] if access["stage"] in AUTH_STAGES else "request",
        "message": AUTH_REASONS.get(access["reason"], ""),
        "stage": AUTH_STAGES.get(access["stage"], ""),
        "http_status": access["http_status"],
        "checked": access["checked"],
    }
    return row


def proxy_url(value):
    """Validate an owner's explicit fixed proxy, without returning its credentials."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise BuyerError("Enter a complete fixed proxy URL, including its port.")
    if any(ord(char) <= 32 or ord(char) == 127 or char in "<>" for char in value):
        raise BuyerError(
            "The proxy URL contains invalid characters. Remove angle brackets, or paste your actual IPRoyal Copy list row."
        )
    if "://" not in value and len(value.split(":")) == 4:
        from vinted_captcha import normalize_proxy

        normalized = normalize_proxy(value)
        if normalized:
            value = normalized
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https", "socks5")
            or not parsed.hostname
            or not parsed.port
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or (bool(parsed.username) != bool(parsed.password))
        ):
            raise ValueError
    except ValueError:
        raise BuyerError(
            "Paste your actual IPRoyal Copy list row (host:port:username:password), or a complete dedicated proxy URL. Replace example words with your own details."
        ) from None
    return value.rstrip("/")


def network_configuration(*, validate=True):
    with closing(connection()) as conn:
        saved = conn.execute("SELECT network FROM vinted_buyer WHERE id=1").fetchone()
    values = decrypt(saved[0]) if saved and saved[0] else {}
    configured_proxy = values.get("proxy") or os.environ.get(
        "VINTED_BUYER_PROXY_URL", ""
    )
    key = values.get("api_key") or os.environ.get("CAPSOLVER_API_KEY", "")
    return {
        "proxy": (
            proxy_url(configured_proxy)
            if configured_proxy and validate
            else configured_proxy
        ),
        "api_key": key,
        "enabled": values.get(
            "enabled",
            os.environ.get("VINTED_CAPSOLVER_ENABLED", "").lower() in ("1", "true"),
        ),
    }


def save_network(form):
    """Store private credentials encrypted; blank fields retain current settings."""
    with exclusive():
        current = network_configuration(validate=False)
        value = form.get("buyer_proxy_url", "").strip()
        key = form.get("buyer_capsolver_key", "").strip()
        if key and not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", key):
            raise BuyerError("The CapSolver API key contains invalid characters.")
        updated = {
            "proxy": (
                proxy_url(value or current["proxy"])
                if value or current["proxy"]
                else ""
            ),
            "api_key": key or current["api_key"],
            "enabled": form.get("buyer_capsolver_enabled") == "yes",
        }
        if updated["enabled"] and not (updated["proxy"] and updated["api_key"]):
            raise BuyerError(
                "Add your dedicated proxy and CapSolver API key before enabling security checks."
            )
        with closing(connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET network=?,pending=NULL,enabled=0 WHERE id=1",
                (encrypt(updated),),
            )


class Client:
    def __init__(self, saved=None):
        self.network = network_configuration()
        self.session = BrowserSession()
        self.solver_attempted = False
        self.solver_diagnostics = {}
        self.solver_solved = False
        if self.network["proxy"]:
            self.session.proxies.update(
                {"http": self.network["proxy"], "https": self.network["proxy"]}
            )
            self.session.trust_env = False
            self.session.verify = (
                os.environ.get("REQUESTS_CA_BUNDLE")
                or os.environ.get("CURL_CA_BUNDLE")
                or True
            )
        self.session.headers.update(
            {
                **API_HEADERS,
                "Accept": "application/json",
                "Accept-Language": "en-GB",
                "Locale": "en-GB",
                "Origin": BASE,
                "Referer": BASE + "/",
            }
        )
        self.csrf = ""
        self._verified_session = None
        if saved:
            try:
                self.csrf = saved.get("csrf", "")
                self.restore_cookies(saved)
                self.headers()
            except (AttributeError, TypeError, ValueError) as exc:
                self.session.close()
                logger.warning(
                    "Vinted saved session unavailable: operation=restore error_type=%s",
                    type(exc).__name__,
                )
                raise BuyerError(
                    AUTH_REASONS["saved_session"],
                    reason="saved_session",
                    stage="saved_session",
                ) from None

    def restore_cookies(self, saved):
        records = saved.get("cookie_records")
        if records is None:
            records = [
                {
                    "name": name,
                    "value": value,
                    "domain": "www.vinted.co.uk",
                    "secure": True,
                }
                for name, value in saved.get("cookies", {}).items()
            ]
        if not isinstance(records, list) or len(records) > 200:
            raise ValueError("Invalid cookie records")
        for record in records:
            if not isinstance(record, dict):
                raise TypeError("Invalid cookie record")
            name, value = record.get("name"), record.get("value")
            domain, path = record.get("domain"), record.get("path", "/")
            secure, expires = record.get("secure", True), record.get("expires")
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}", name)
                or not isinstance(value, str)
                or len(value) > 16384
                or any(ord(char) < 32 or ord(char) > 126 for char in value)
                or domain not in COOKIE_DOMAINS
                or not isinstance(path, str)
                or not path.startswith("/")
                or len(path) > 2048
                or any(ord(char) <= 32 or ord(char) > 126 for char in path)
                or type(secure) is not bool
                or (
                    expires is not None
                    and (type(expires) is not int or not 0 <= expires <= 253402300799)
                )
            ):
                raise ValueError("Invalid cookie metadata")
            cookie = requests.cookies.create_cookie(
                name, value, domain=domain, path=path, secure=secure, expires=expires
            )
            for flag in (
                "domain_specified",
                "domain_initial_dot",
                "path_specified",
                "discard",
            ):
                if flag in record:
                    if type(record[flag]) is not bool:
                        raise ValueError("Invalid cookie flag")
                    setattr(cookie, flag, record[flag])
            self.session.cookies.set_cookie(cookie)

    def headers(self):
        # The current Vinted web client authenticates with its session cookies.
        # A web cookie is not an independently interchangeable bearer credential.
        self.session.headers.pop("Authorization", None)
        if self.csrf:
            self.session.headers["X-CSRF-Token"] = self.csrf
        else:
            self.session.headers.pop("X-CSRF-Token", None)
        anonymous_id = self.session.cookies.get_dict().get("anon_id")
        if isinstance(anonymous_id, str) and re.fullmatch(
            r"[A-Za-z0-9._~-]{1,256}", anonymous_id
        ):
            self.session.headers["X-Anon-Id"] = anonymous_id
        else:
            self.session.headers.pop("X-Anon-Id", None)

    def solve_challenge(self, response, data=None):
        """One supported task on this client's fixed account proxy and browser."""
        if self.solver_attempted or not self.network["enabled"]:
            return False
        from vinted_captcha import extract_challenge, solve_datadome
        from vinted_http import BROWSER_USER_AGENT

        challenge = extract_challenge(response, data)
        if not challenge:
            return False
        self.solver_attempted = True
        result = solve_datadome(
            challenge,
            proxy=self.network["proxy"],
            api_key=self.network["api_key"],
            user_agent=BROWSER_USER_AGENT,
            enabled=True,
        )
        public_result = getattr(result, "public", None)
        diagnostics = public_result() if callable(public_result) else None
        self.solver_diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        logger.info(
            "Vinted security check: state=%s phase=%s code=%s category=%s provider=%s http=%s polls=%s",
            result.state,
            getattr(result, "stage", "") or "none",
            getattr(result, "code", "") or "none",
            getattr(result, "category", "") or "none",
            getattr(result, "provider_error", "") or "none",
            getattr(result, "http_status", None),
            getattr(result, "polls", 0),
        )
        if result.state != "solved":
            return False
        for cookie in list(self.session.cookies):
            if cookie.name == "datadome":
                self.session.cookies.clear(cookie.domain, cookie.path, cookie.name)
        self.session.cookies.set(
            "datadome", result.cookie, domain="www.vinted.co.uk", secure=True
        )
        # Only a previously verified same-account binding authorizes writing.
        # Export full records so all cookie scopes and expiries survive restart;
        # persist_session's CAS rejects a concurrently replaced connection.
        self.persist_session()
        self.solver_solved = True
        return True

    def retry_security_check(self, response, data, error, *, payment=False):
        """Run after rejected-response cookies have been rolled back."""
        if (
            payment
            or response is None
            or error.reason != "security_challenge"
            or response.status_code not in (200, 401, 403, 301, 302, 303, 307, 308)
            or self.solver_attempted
        ):
            return False
        if not self.solve_challenge(response, data):
            return False
        response.close()
        return True

    def exported(self):
        return {
            "csrf": self.csrf,
            # Retain the legacy view for old callers; restoration uses the full
            # records so domain/path/expiry survive encryption and restarts.
            "cookies": self.session.cookies.get_dict(),
            "cookie_records": [
                {
                    key: getattr(cookie, key)
                    for key in (
                        "name",
                        "value",
                        "domain",
                        "path",
                        "secure",
                        "expires",
                        "domain_specified",
                        "domain_initial_dot",
                        "path_specified",
                        "discard",
                    )
                }
                for cookie in self.session.cookies
            ],
        }

    def log_cookie_evidence(self):
        """Only counts and fixed expiry labels, never cookies, claims or identifiers."""
        prepared = self.session.prepare_request(
            requests.Request("POST", BASE + "/web/api/auth/refresh")
        )
        names = [
            part.split("=", 1)[0].strip()
            for part in prepared.headers.get("Cookie", "").split(";")
        ]
        refresh = [
            cookie
            for cookie in self.session.cookies
            if cookie.name == "refresh_token_web"
        ]
        values = self.session.cookies.get_dict()
        logger.info(
            "Vinted auth cookie evidence: access_sent=%s refresh_sent=%s refresh_stored=%s refresh_expired=%s access_expiry=%s refresh_expiry=%s",
            names.count("access_token_web"),
            names.count("refresh_token_web"),
            len(refresh),
            sum(cookie.is_expired() for cookie in refresh),
            token_expiry_hint(values.get("access_token_web")),
            token_expiry_hint(values.get("refresh_token_web")),
        )

    def bind_verified_session(self, user_id, sealed, saved):
        """Only a same-account identity check may enable session persistence."""
        self._verified_session = (user_id, sealed, saved)

    def persist_session(self):
        """Keep accepted response rotations without overwriting a newer login."""
        if self._verified_session is None:
            return
        user_id, previous_sealed, previous = self._verified_session
        current = self.exported()
        if current == previous:
            return
        sealed = encrypt(current)
        with closing(connection()) as conn, conn:
            updated = conn.execute(
                "UPDATE vinted_buyer SET session=? WHERE id=1 AND user_id=? AND session=?",
                (sealed, user_id, previous_sealed),
            ).rowcount
        if updated != 1:
            raise BuyerError(
                "The saved Vinted connection changed. Recheck it before buying.",
                reason="saved_session",
                stage="saved_session",
            )
        self._verified_session = (user_id, sealed, current)
        logger.info("Vinted verified session rotation: persisted=True")

    @contextmanager
    def accepted_response_cookies(self, stage):
        """Undo requests' automatic cookie merge when a response is rejected.

        An expired identity check may be followed by one normal renewal using
        this same client. Error-response cookies must not replace or delete the
        owner's saved credentials before that renewal. Accepted responses,
        including an explicit two-step login challenge, retain their cookies.
        """
        previous = self.session.cookies.copy()
        try:
            yield
        except Exception:

            def records(cookies, name):
                return {
                    (cookie.domain, cookie.path): (
                        cookie.value,
                        cookie.secure,
                        cookie.expires,
                    )
                    for cookie in cookies
                    if cookie.name == name
                }

            access_changed = records(previous, "access_token_web") != records(
                self.session.cookies, "access_token_web"
            )
            refresh_changed = records(previous, "refresh_token_web") != records(
                self.session.cookies, "refresh_token_web"
            )
            self.session.cookies = previous
            self.headers()
            logger.info(
                "Vinted rejected response cookies: stage=%s access_changed=%s refresh_changed=%s restored=True",
                stage,
                access_changed,
                refresh_changed,
            )
            raise

    def update_tokens(self, response, data):
        """Keep one current token per name after a normal refresh-cookie rotation."""
        returned = getattr(response, "cookies", None)
        returned = (
            list(returned)
            if isinstance(returned, requests.cookies.RequestsCookieJar)
            else []
        )
        access_updated = False
        for field, cookie in (
            ("access_token", "access_token_web"),
            ("refresh_token", "refresh_token_web"),
        ):
            candidates = [entry for entry in returned if entry.name == cookie]
            # For cookie authentication Set-Cookie is authoritative. An OAuth
            # body can contain a different credential; never overwrite the web
            # cookie with that body value or discard its received scope.
            if len(candidates) > 1:
                raise BuyerError(AUTH_REASONS["unreadable"], reason="unreadable")
            received = candidates[0] if candidates else None
            value = received.value if received is not None else data.get(field)
            if not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9._~+/=-]{16,8192}", value
            ):
                continue
            if received is not None and (
                (received.domain and received.domain not in COOKIE_DOMAINS)
                or (received.expires is not None and received.expires <= time.time())
            ):
                continue
            # requests can retain both the imported host cookie and a new
            # domain cookie. Sending both lets the old token shadow the new one.
            for saved in list(self.session.cookies):
                if saved.name == cookie:
                    self.session.cookies.clear(saved.domain, saved.path, saved.name)
            if received is not None and received.domain:
                self.session.cookies.set_cookie(copy(received))
            else:
                self.session.cookies.set(
                    cookie, value, domain="www.vinted.co.uk", secure=True
                )
            if cookie == "access_token_web":
                access_updated = True
        self.headers()
        return access_updated

    def refresh_security_token(self):
        """Read the current public bootstrap before renewing an expired session.

        The frontend security token changes with web releases. Expired account
        cookies can redirect the homepage into authentication, so read its public
        bootstrap in a separate ordinary session and keep our buyer cookies.
        """
        public = Client()
        # Renewal and its public bootstrap share one paid challenge allowance.
        public.solver_attempted = self.solver_attempted
        try:
            public.homepage()
            previous = self.csrf
            self.csrf = public.csrf
            self.headers()
            logger.info(
                "Vinted renewal bootstrap: csrf_changed=%s", previous != self.csrf
            )
        finally:
            self.solver_attempted = self.solver_attempted or public.solver_attempted
            public.session.close()

    def request(self, method, path, body=None, *, allow_challenge=False, params=None):
        pickup_gateway = bool(
            re.fullmatch(
                r"/shipping-estimation/external/shipping_orders/[0-9]{1,24}/nearby_pickup_points",
                path,
            )
        )
        extra = {}
        if pickup_gateway:
            if (
                method != "GET"
                or body is not None
                or allow_challenge
                or not isinstance(params, dict)
                or set(params) != {"country_code", "latitude", "longitude"}
                or params["country_code"] != "GB"
                or any(
                    type(params[key]) not in (int, float)
                    or not math.isfinite(params[key])
                    or not -bound <= params[key] <= bound
                    for key, bound in (("latitude", 90), ("longitude", 180))
                )
            ):
                raise BuyerError("Vinted pickup-point request could not be verified.")
            extra = {
                "params": params,
                "headers": {
                    "Platform": "web",
                    "X-Next-App": "marketplace-web",
                    "Sec-Fetch-Site": "same-site",
                },
            }
        elif params is not None or not path.startswith(("/api/v2/", "/web/api/auth/")):
            raise BuyerError("Unsupported Vinted request.")
        elif method == "POST" and path == "/web/api/auth/refresh" and body is None:
            # Vinted's browser refreshSessionTokens() posts without data.
            # Axios removes Content-Type for this bodyless renewal. Preserve
            # explicitly supplied JSON bodies for other supported callers.
            extra = {"headers": {"Content-Type": None}}
        renewal = path == "/web/api/auth/refresh" or (
            path == "/web/api/auth/oauth"
            and isinstance(body, dict)
            and body.get("grant_type") == "refresh_token"
        )
        stage = (
            "renewal"
            if renewal
            else (
                "sign_in"
                if path.startswith("/web/api/auth/")
                else (
                    "identity"
                    if path == "/api/v2/users/current"
                    else "pickup_points" if pickup_gateway else "request"
                )
            )
        )
        if path.endswith("/payment"):
            referer = BASE + "/checkout?purchase_id=" + path.split("/")[4]
        elif path.startswith("/api/v2/items/"):
            referer = BASE + "/items/" + path.split("/")[4]
        elif renewal:
            referer = BASE + "/"
        elif path.startswith("/web/api/auth/"):
            referer = BASE + "/member/login"
        else:
            referer = self.session.headers.get("Referer", BASE + "/")
        self.session.headers["Referer"] = referer
        previous_access = self.session.cookies.get_dict().get("access_token_web")
        previous_refresh = self.session.cookies.get_dict().get("refresh_token_web")
        if renewal:
            prepared = self.session.prepare_request(
                requests.Request(
                    method, BASE + path, json=body, headers=extra.get("headers")
                )
            )
            cookie = prepared.headers.get("Cookie", "")
            names = {part.split("=", 1)[0].strip() for part in cookie.split(";")}
            logger.info(
                "Vinted web renewal request: csrf=%s refresh_cookie=%s "
                "access_cookie=%s anon_header=%s bearer=%s json_object=%s bodyless=%s content_type=%s method=%s",
                bool(prepared.headers.get("X-CSRF-Token")),
                "refresh_token_web" in names,
                "access_token_web" in names,
                bool(prepared.headers.get("X-Anon-Id")),
                bool(prepared.headers.get("Authorization")),
                isinstance(body, dict),
                prepared.body is None,
                bool(prepared.headers.get("Content-Type")),
                "oauth" if path == "/web/api/auth/oauth" else "cookie",
            )
        response, data = None, None
        try:
            with self.accepted_response_cookies(stage):
                try:
                    response = self.session.request(
                        method,
                        (PICKUP_BASE if pickup_gateway else BASE) + path,
                        json=body,
                        timeout=(4, 12),
                        allow_redirects=False,
                        **extra,
                    )
                except requests.RequestException:
                    raise BuyerError(
                        AUTH_REASONS["network"], reason="network", stage=stage
                    ) from None
                try:
                    data = response.json()
                except (ValueError, RecursionError):
                    data = None
                if (
                    security_challenge(response, data)
                    or redirect_reason(response) == "security_challenge"
                ):
                    raise response_error(response, data, stage)
                if (
                    allow_challenge
                    and response.status_code == 401
                    and isinstance(data, dict)
                    and isinstance(data.get("payload"), dict)
                    and data["payload"].get("id")
                ):
                    return {"challenge_id": str(data["payload"]["id"])}
                if response.status_code not in (200, 201) or not isinstance(data, dict):
                    raise response_error(response, data, stage)
                if any(
                    data.get(k) for k in ("error", "error_code", "errors")
                ) or data.get("code") not in (
                    None,
                    0,
                ):
                    # Vinted can return a business/authentication error inside HTTP 200.
                    # Never adopt token fields from an explicit error response or let a
                    # nominal status turn that response into an accepted checkout.
                    raise response_error(response, data, stage)
                access_updated = self.update_tokens(response, data)
                if renewal:
                    # HTTP 200 can still contain an OAuth error or no usable token.
                    # Never treat the imported stale cookie as proof of renewal.
                    if (
                        any(data.get(k) for k in ("error", "error_code"))
                        or not access_updated
                    ):
                        raise response_error(response, data, stage)
                    returned = getattr(response, "cookies", None)
                    cookie_access = (
                        returned.get_dict().get("access_token_web")
                        if isinstance(returned, requests.cookies.RequestsCookieJar)
                        else None
                    )
                    body_access = data.get("access_token")
                    body_refresh = data.get("refresh_token")
                    cookie_refresh = (
                        returned.get_dict().get("refresh_token_web")
                        if isinstance(returned, requests.cookies.RequestsCookieJar)
                        else None
                    )
                    scope = data.get("scope")
                    current_access = self.session.cookies.get_dict().get(
                        "access_token_web"
                    )
                    sources_match = (
                        body_access == cookie_access
                        if isinstance(body_access, str)
                        and isinstance(cookie_access, str)
                        else None
                    )
                    logger.info(
                        "Vinted session renewal: usable_access_token=True "
                        "token_changed=%s body_access=%s cookie_access=%s "
                        "sources_match=%s scope_present=%s scope_user=%s refresh_changed=%s body_refresh=%s cookie_refresh=%s refresh_sources_match=%s refresh_expiry=%s",
                        previous_access != current_access,
                        isinstance(body_access, str),
                        isinstance(cookie_access, str),
                        sources_match,
                        isinstance(scope, str),
                        isinstance(scope, str) and "user" in scope.split(),
                        previous_refresh
                        != self.session.cookies.get_dict().get("refresh_token_web"),
                        isinstance(body_refresh, str),
                        isinstance(cookie_refresh, str),
                        (
                            body_refresh == cookie_refresh
                            if isinstance(body_refresh, str)
                            and isinstance(cookie_refresh, str)
                            else None
                        ),
                        token_expiry_hint(
                            self.session.cookies.get_dict().get("refresh_token_web")
                        ),
                    )
        except BuyerError as error:
            # Roll back the entire rejected response before adding a solved
            # security cookie. A payment response never authorizes a replay.
            if self.retry_security_check(
                response, data, error, payment=path.endswith("/payment")
            ):
                return self.request(
                    method, path, body, allow_challenge=allow_challenge, params=params
                )
            raise
        if renewal:
            record_auth("renewed", "renewal", response.status_code)
        self.persist_session()
        return data

    def listing_page(self, url, item_id):
        """Read one canonical listing page with the verified account session."""
        from vinted_gallery import listing_url
        from vinted_page_data import _url_id, parse_purchase_item

        target = listing_url(url)
        if not target or _url_id(target) != str(item_id):
            raise BuyerError(
                "The Vinted listing URL could not be verified.",
                reason="item_unavailable",
            )
        navigation = {
            **NAVIGATION_HEADERS,
            "Content-Type": None,
            "Origin": None,
            "Sec-Fetch-Site": "same-origin",
            "Referer": BASE + "/",
        }
        response, data = None, None
        try:
            with self.accepted_response_cookies("request"):
                try:
                    response = self.session.get(
                        target,
                        headers=navigation,
                        timeout=(4, 12),
                        allow_redirects=False,
                    )
                    data = homepage_data(response)
                    if not security_challenge(
                        response, data
                    ) and response.status_code in (
                        301,
                        302,
                        307,
                        308,
                    ):
                        location = response.headers.get("Location", "")
                        redirected = urljoin(target, location)
                        canonical = listing_url(redirected)
                        # A single same-origin, same-item slug redirect is normal page
                        # navigation. Never follow sign-in, challenge or foreign URLs.
                        if (
                            canonical
                            and urlsplit(redirected).netloc == "www.vinted.co.uk"
                            and _url_id(canonical) == str(item_id)
                        ):
                            response = self.session.get(
                                canonical,
                                headers=navigation,
                                timeout=(4, 12),
                                allow_redirects=False,
                            )
                except requests.RequestException:
                    raise BuyerError(
                        AUTH_REASONS["network"], reason="network"
                    ) from None
                data = homepage_data(response)
                if security_challenge(response, data) or response.status_code != 200:
                    raise response_error(response, data, "request")
                # A successful same-origin response can rotate session cookies even
                # when its item payload is incomplete. Preserve those credentials before
                # parsing; incomplete item metadata still cannot authorize a purchase.
                self.update_tokens(response, {})
        except BuyerError as error:
            if self.retry_security_check(response, data, error):
                return self.listing_page(url, item_id)
            raise
        token = csrf_from_html(response.text)
        if token:
            self.csrf = token
            self.headers()
        self.persist_session()
        item = parse_purchase_item(response.text, item_id)
        if item is None:
            raise BuyerError(
                "Vinted's current page did not confirm this item's price, seller and availability. No payment was sent.",
                reason="item_unavailable",
            )
        logger.info("Vinted listing page: result=verified http=200")
        return {"item": item}

    def homepage(self):
        navigation = {**NAVIGATION_HEADERS, "Content-Type": None, "Origin": None}
        response, data = None, None
        try:
            with self.accepted_response_cookies("homepage"):
                try:
                    response = self.session.get(
                        BASE + "/",
                        headers=navigation,
                        timeout=(4, 12),
                        allow_redirects=False,
                    )
                    data = homepage_data(response)
                    if (
                        not security_challenge(response, data)
                        and redirect_reason(response) == "home_redirect"
                    ):
                        # Follow a single canonical homepage redirect on the same HTTPS
                        # origin. Never follow authentication, challenge or other hosts.
                        response = self.session.get(
                            urljoin(BASE, response.headers["Location"]),
                            headers=navigation,
                            timeout=(4, 12),
                            allow_redirects=False,
                        )
                except requests.RequestException:
                    raise BuyerError(
                        AUTH_REASONS["network"], reason="network", stage="homepage"
                    ) from None
                data = homepage_data(response)
                if security_challenge(response, data) or response.status_code != 200:
                    raise response_error(response, data, "homepage")
                self.update_tokens(response, {})
        except BuyerError as error:
            if self.retry_security_check(response, data, error):
                return self.homepage()
            raise
        token = csrf_from_html(response.text)
        if token:
            self.csrf = token
            self.headers()
            self.persist_session()
            return
        self.persist_session()
        raise BuyerError(
            "Vinted did not supply the security token. The buyer connection has not been verified.",
            reason="csrf",
            stage="homepage",
        )

    def identity(self):
        data = self.request("GET", "/api/v2/users/current")
        user = data.get("user") or {}
        if not isinstance(user, dict) or not str(user.get("id", "")).isdigit():
            raise BuyerError(
                "Vinted did not verify the buying account.", stage="identity"
            )
        return (
            str(user["id"]),
            str(user.get("login") or user.get("username") or user["id"])[:100],
        )


def save_connected(client):
    user_id, username = client.identity()
    with closing(connection()) as conn, conn:
        previous = conn.execute(
            "SELECT user_id FROM vinted_buyer WHERE id=1"
        ).fetchone()[0]
        if previous and user_id != previous:
            raise BuyerError(
                AUTH_REASONS["account_changed"],
                reason="account_changed",
                stage="identity",
            )
        conn.execute(
            "UPDATE vinted_buyer SET session=?,pending=NULL,verified_at=?,user_id=?,username=?,enabled=0 WHERE id=1",
            (encrypt(client.exported()), time.time(), user_id, username),
        )
    record_auth("connected", "identity", 200)


def reserve_connection_attempt():
    """Shared cooldown for owner-initiated password and existing-session linking."""
    with closing(connection()) as conn, conn:
        last = conn.execute(
            "SELECT login_attempt FROM vinted_buyer WHERE id=1"
        ).fetchone()[0]
        if time.time() - last < 30:
            raise BuyerError("Wait 30 seconds between connection attempts.")
        conn.execute(
            "UPDATE vinted_buyer SET login_attempt=?,pending=NULL,enabled=0 WHERE id=1",
            (time.time(),),
        )


def link_session(access_token, refresh_token):
    """Verify an owner's existing credentials; never import anti-bot cookies.

    Connection makes only the normal homepage and account-verification requests.
    No password login, credential renewal or purchase is attempted. A supported
    security check can use one explicitly enabled solver task and one retry.
    Tokens are not persisted unless Vinted confirms the authenticated identity.
    """
    tokens = (access_token.strip(), refresh_token.strip())
    if any(not re.fullmatch(r"[A-Za-z0-9._~+/=-]{16,8192}", v) for v in tokens):
        raise BuyerError(
            "Enter the two session token values from your own UK Vinted account, without cookie names or other text."
        )
    with exclusive():
        reserve_connection_attempt()
        client = Client(
            {"cookies": dict(zip(("access_token_web", "refresh_token_web"), tokens))}
        )
        try:
            client.homepage()
            save_connected(client)
            import vinted_session_recovery

            recovered = vinted_session_recovery.recover_linked_client(client)
            if recovered is not None:
                return vinted_session_recovery.summary(recovered)
            return "Vinted verified your existing session. Autobuy is off; review the account and set spending limits before enabling it."
        except BuyerError as exc:
            record_auth(exc.reason, exc.stage, exc.status)
            raise
        finally:
            client.session.close()


def start_login(email, password):
    if not email or not password or len(email) > 254 or len(password) > 1024:
        raise BuyerError(
            "Enter your Vinted username or email and password in this form."
        )
    with exclusive():
        reserve_connection_attempt()
        client = Client()
        try:
            client.homepage()
            result = client.request(
                "POST",
                "/web/api/auth/oauth",
                {
                    "client_id": "web",
                    "scope": "user",
                    "grant_type": "password",
                    "username": email,
                    "password": password,
                },
                allow_challenge=True,
            )
            if result.get("challenge_id"):
                pending = dict(
                    client.exported(),
                    challenge_id=result["challenge_id"],
                    expires=time.time() + 600,
                )
                with closing(connection()) as conn, conn:
                    conn.execute(
                        "UPDATE vinted_buyer SET pending=? WHERE id=1",
                        (encrypt(pending),),
                    )
                record_auth("verification_code", "sign_in", 401)
                return "Vinted sent a sign-in code. Enter it in the verification box below."
            client.homepage()
            save_connected(client)
            return "Vinted buyer connected. Choose your spending limits before enabling Autobuy."
        except BuyerError as exc:
            record_auth(exc.reason, exc.stage, exc.status)
            raise
        finally:
            client.session.close()


def check_signin():
    with exclusive():
        with closing(connection()) as conn, conn:
            last = conn.execute(
                "SELECT last_probe FROM vinted_buyer_access WHERE id=1"
            ).fetchone()[0]
            if time.time() - last < 300:
                raise BuyerError(
                    "The last connection diagnosis is shown below. Wait five minutes before running it again."
                )
            conn.execute(
                "UPDATE vinted_buyer_access SET last_probe=? WHERE id=1", (time.time(),)
            )
        client = Client()
        try:
            client.homepage()
            # A homepage GET cannot establish whether the authentication endpoint
            # accepts this server. Submit one empty request: no account identifier,
            # password, saved buyer session, payment, or alternate network route.
            try:
                client.request(
                    "POST",
                    "/web/api/auth/oauth",
                    {
                        "client_id": "web",
                        "scope": "user",
                        "grant_type": "password",
                        "username": "",
                        "password": "",
                    },
                )
            except BuyerError as exc:
                if exc.status in (400, 401, 422) and exc.reason in (
                    "credentials",
                    "http_error",
                ):
                    record_auth("endpoint_reached", "sign_in", exc.status)
                    return AUTH_REASONS["endpoint_reached"]
                raise
            record_auth("not_confirmed")
            raise BuyerError(
                "The diagnostic returned an unexpected response. No buyer account was connected."
            )
        except BuyerError as exc:
            record_auth(exc.reason, exc.stage, exc.status)
            raise
        finally:
            client.session.close()


def verify_code(code):
    if not re.fullmatch(r"[0-9A-Za-z-]{4,12}", code):
        raise BuyerError("Enter the sign-in code from Vinted.")
    with exclusive():
        with closing(connection()) as conn:
            pending = decrypt(
                conn.execute("SELECT pending FROM vinted_buyer WHERE id=1").fetchone()[
                    0
                ]
            )
        if not pending or pending["expires"] < time.time():
            raise BuyerError("The sign-in attempt expired. Start again.")
        client = Client(pending)
        try:
            client.request(
                "POST",
                "/web/api/auth/oauth",
                {
                    "client_id": "web",
                    "scope": "user",
                    "grant_type": "password",
                    "password_type": "two_factor_challenge_code",
                    "control_code": pending["challenge_id"],
                    "verification_code": code,
                    "is_trusted_device": False,
                },
            )
            client.homepage()
            save_connected(client)
            return "Vinted buyer connected. Choose your spending limits before enabling Autobuy."
        except BuyerError as exc:
            record_auth(exc.reason, exc.stage, exc.status)
            raise
        finally:
            client.session.close()


def renew_saved_client(client):
    """One selected UK renewal; keep accepted rotations before verifying identity.

    The OAuth refresh grant is adapted from Blim's MIT-licensed
    feed/vinted_refresh.py at b4ee651da44c7fe5e387f064d46f77a19541d194.
    Keep MSJ's TLS verification, browser identity, proxy and cross-process lock.
    Never fall through to a second renewal format after an uncertain response.
    """
    method = os.environ.get("VINTED_BUYER_REFRESH_METHOD", "cookie")
    if method not in ("cookie", "oauth"):
        raise BuyerError(
            AUTH_REASONS["renewal_failed"], reason="renewal_failed", stage="renewal"
        )
    path, body = "/web/api/auth/refresh", None
    if method == "oauth":
        refresh = client.session.cookies.get_dict().get("refresh_token_web")
        if not isinstance(refresh, str) or not re.fullmatch(
            r"[A-Za-z0-9._~+/=-]{16,8192}", refresh
        ):
            raise BuyerError(
                AUTH_REASONS["renewal_failed"], reason="renewal_failed", stage="renewal"
            )
        path, body = "/web/api/auth/oauth", {
            "client_id": "web",
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        }
    client.refresh_security_token()
    try:
        if body is None:
            client.request("POST", path)
        else:
            client.request("POST", path, body)
    except BuyerError as renewal:
        if renewal.status in (400, 401, 422) and renewal.reason == "credentials":
            raise BuyerError(
                AUTH_REASONS["refresh_rejected"],
                renewal.status,
                reason="refresh_rejected",
                stage="renewal",
            ) from None
        if renewal.status in (400, 401, 422) and renewal.reason == "http_error":
            raise BuyerError(
                AUTH_REASONS["renewal_failed"],
                renewal.status,
                reason="renewal_failed",
                stage="renewal",
            ) from None
        raise
    with closing(connection()) as conn, conn:
        # Every production caller holds exclusive(). Preserve a rotated token
        # even if identity later fails, without changing buying permissions.
        conn.execute(
            "UPDATE vinted_buyer SET session=? WHERE id=1",
            (encrypt(client.exported()),),
        )
    logger.info("Vinted renewed session: csrf_present=%s", bool(client.csrf))
    if not client.csrf:
        client.homepage()


def connected_client(*, renew_before=0, solve_challenges=True, allow_refresh=True):
    with closing(connection()) as conn:
        row = conn.execute(
            "SELECT session,user_id FROM vinted_buyer WHERE id=1"
        ).fetchone()
    client = None
    try:
        saved = decrypt(row[0])
        if not saved:
            raise BuyerError("Connect your Vinted buyer account in Connections first.")
        client = Client(saved)
        if not solve_challenges:
            # Status reconciliation must not create a paid solver task or replay
            # a challenged request. The saved network configuration is unchanged.
            client.solver_attempted = True
        client.log_cookie_evidence()
        try:
            user_id, _ = client.identity()
        except BuyerError as exc:
            refresh = saved.get("cookies", {}).get("refresh_token_web")
            expired = exc.status == 401 and exc.reason == "credentials"
            if (
                not allow_refresh
                or not (expired or exc.reason == "session_refresh")
                or not refresh
            ):
                raise
            renew_saved_client(client)
            user_id, _ = client.identity()
        else:
            # A successful identity read may already have rotated the cookies.
            # Claims only schedule renewal; Vinted must verify the same account
            # both before and after any proactive renewal.
            from vinted_session_worker import session_cookie_expiry

            current_session = client.exported()
            current = current_session.get("cookies", {})
            expiry = session_cookie_expiry(current_session, "access_token_web")
            refresh_expiry = session_cookie_expiry(current_session, "refresh_token_web")
            if (
                allow_refresh
                and renew_before
                and user_id == row[1]
                and current.get("refresh_token_web")
                and (
                    (expiry is not None and expiry <= time.time() + renew_before)
                    or (
                        refresh_expiry is not None
                        and refresh_expiry <= time.time() + 2 * 86400
                    )
                )
            ):
                renew_saved_client(client)
                user_id, _ = client.identity()
        if user_id != row[1]:
            raise BuyerError(
                "The connected Vinted account changed. Reconnect it before buying."
            )
        saved = client.exported()
        sealed = encrypt(saved)
        with closing(connection()) as conn, conn:
            conn.execute(
                "UPDATE vinted_buyer SET session=?,verified_at=? WHERE id=1",
                (sealed, time.time()),
            )
        client.bind_verified_session(user_id, sealed, saved)
        record_auth("connected", "identity", 200)
        return client
    except BuyerError as exc:
        record_auth(exc.reason, exc.stage, exc.status)
        if client:
            client.session.close()
        raise


def check_saved_connection():
    """Owner-requested identity check; never creates a checkout or payment."""
    with exclusive():
        with closing(connection()) as conn, conn:
            last = conn.execute(
                "SELECT last_probe FROM vinted_buyer_access WHERE id=1"
            ).fetchone()[0]
            if time.time() - last < 30:
                raise BuyerError(
                    "Wait 30 seconds before checking this connection again."
                )
            conn.execute(
                "UPDATE vinted_buyer_access SET last_probe=? WHERE id=1", (time.time(),)
            )
        client = connected_client()
        client.session.close()
    return (
        "Vinted verified your saved buyer session. No checkout or payment was created."
    )


def save_limits(form):
    if "buyer_max_total" in form or "buyer_max_extra" in form:
        raise BuyerError(
            "Budgets are now set on each search. Reload Connections before enabling Autobuy."
        )
    try:
        info = json.loads(form.get("buyer_browser_info", "{}"))
        if not isinstance(info, dict):
            raise TypeError
        browser_info = {
            key: info[key]
            for key in (
                "color_depth",
                "java_enabled",
                "language",
                "screen_height",
                "screen_width",
                "timezone_offset",
            )
        }
        if not all(
            isinstance(browser_info[k], int) and 0 < browser_info[k] <= 20000
            for k in ("screen_height", "screen_width", "color_depth")
        ):
            raise ValueError
        if (
            not isinstance(browser_info["java_enabled"], bool)
            or not isinstance(browser_info["language"], str)
            or len(browser_info["language"]) > 35
            or not isinstance(browser_info["timezone_offset"], int)
            or abs(browser_info["timezone_offset"]) > 900
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise BuyerError(
            "Reload this page to capture your device details, then save the buyer settings again."
        ) from None
    current = settings()
    pickup_mode = form.get("buyer_pickup_mode", current["pickup_mode"])
    preferred_card = form.get(
        "buyer_card_last4", current["preferred_card_last4"]
    ).strip()
    if pickup_mode not in ("saved", "nearest") or (
        preferred_card and not re.fullmatch(r"[0-9]{4}", preferred_card)
    ):
        raise BuyerError(
            "Choose a pickup preference and enter only the four ending digits of your saved card."
        )
    enabled = form.get("buyer_enabled") == "yes"
    if enabled and not current["connected"]:
        raise BuyerError("Connect your Vinted buyer account first.")
    with exclusive(), closing(connection()) as conn, conn:
        conn.execute(
            "UPDATE vinted_buyer SET browser_info=?,enabled=?,pickup_mode=?,preferred_card_last4=? WHERE id=1",
            (json.dumps(browser_info), enabled, pickup_mode, preferred_card),
        )


def disconnect():
    with exclusive(), closing(connection()) as conn, conn:
        conn.execute(
            "UPDATE vinted_buyer SET session=NULL,pending=NULL,verified_at=NULL,user_id=NULL,username=NULL,enabled=0 WHERE id=1"
        )
